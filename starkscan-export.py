# export starknet transactions / token transfers from Starkscan (https://starkscan.co)
# replaces export-starknet.py which relied on the retired Voyager free API
#
# credentials come from .env in this folder:
#   STARKSCAN_KEY=...       (create one via github sign-in at https://starkscan.co/api-key)
#   STARKSCAN_RPC_URL=...   (starkscan RPC url, used to sanity-check the wallet address on-chain)
import os, sys, argparse, requests, json, csv, re, time
from datetime import datetime, timezone
from decimal import Decimal, getcontext

getcontext().prec = 80  # uint256 amounts have up to 78 digits, default prec 28 silently rounds

API_BASE = "https://api.starkscan.co"
PAGE_LIMIT = 100
REQUEST_PAUSE = 0.6  # free tier is 1-2 req/s, stay under it
MAX_RETRIES = 5
KOINLY_MAX_NFT_PLACEHOLDERS = 5000  # koinly supports NFT1..NFT5000, one per unique NFT
KOINLY_MAX_LP_PLACEHOLDERS = 1000   # LPx cap isn't documented by koinly; warn conservatively
# mainnet sequencer / fee collectors over the chain's history - fee payments show up in the
# transfer feed as plain sends to these (actionContext is null on this endpoint, so the
# address list is the working detector)
SEQUENCER_ADDRESSES = {
    0x1176a1bd84444c89232ec27754698e5d2e7e1a7f1539f12027f28b23ec9f3d8,
    0x5dcd266a80b8a5f29f04d779c6b166b80150c24f2180a75e82427242dab20a9,
    0x46a89ae102987331d369645031b49c27738ed096f2789c24449966da4c6de6b,
}
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# state and output paths are resolved relative to this script (not the cwd) so placeholder
# numbering stays consistent no matter where the script is run from
PLACEHOLDER_MAP_FNAME = os.path.join(SCRIPT_DIR, "state", "koinly_placeholder_map.json")
# human-readable per-run audit log: what was exported when, for which wallet, and which
# placeholder numbers each run consumed - lets --undo-last revert a test run cleanly
RUN_LOG_FNAME = os.path.join(SCRIPT_DIR, "state", "koinly_run_log.json")
# legacy counter files from export-starknet.py, used to seed counters so numbering continues
# past placeholders already imported into koinly. copies exist both in the repo root and in
# output/ - trust whichever is highest, reusing an old placeholder corrupts koinly cost basis
LEGACY_LP_COUNTER_FNAMES = [os.path.join(SCRIPT_DIR, "last_used_lp_id.txt"),
                            os.path.join(SCRIPT_DIR, "output", "last_used_lp_id.txt")]
LEGACY_NFT_COUNTER_FNAMES = [os.path.join(SCRIPT_DIR, "last_used_nft_id.txt"),
                             os.path.join(SCRIPT_DIR, "output", "last_used_nft_id.txt")]
# symbols of LP position NFTs, kept from the old script as a fallback when
# starkscan's actionContext doesn't identify an lp_add/lp_remove
LP_SYMBOLS = {"EkuPo", "JEDI-V2-POS"}

# starkscan's transfer table misclassifies some NFT collections as erc20 with the token id
# in the amount field (single-felt Transfer events), and has no metadata for others.
# contracts listed here get reclassified/named; kind "lp" routes to LP placeholders.
# the script prints a note when it meets an unknown no-metadata contract - add it here if
# it's an NFT/LP collection.
KNOWN_NFT_CONTRACTS = {  # address: (name, "nft"|"lp") - widely-used position NFTs only;
    # add personal collections to nft_contracts_local.txt (gitignored) instead
    "0x7b696af58c967c1b14c9dde0ace001720635a660a8e90c565ea459345318b30": ("Ekubo Position", "lp"),
    "0x469b656239972a2501f2f1cd71bf4e844d64b7cae6773aa84c702327c476e5b": ("JediSwap V2 Positions NFT", "lp"),
}
LOCAL_NFT_CONTRACTS_FNAME = os.path.join(SCRIPT_DIR, "nft_contracts_local.txt")
# on-chain lookups for contracts starkscan has no metadata for, cached across runs.
# selectors are starknet_keccak of the entrypoint names (verified against pycryptodome)
NAME_CACHE_FNAME = os.path.join(SCRIPT_DIR, "state", "contract_names_cache.json")
SELECTOR_NAME = "0x361458367e696363fbcc70777d07ebbd2394e89fd0adcaf147faccd1d294d60"
SELECTOR_SYMBOL = "0x216b05c387bab9ac31918a3e61672f4618601f3c598a2f3f2710f37053e1ea4"
SELECTOR_DECIMALS = "0x4c4fb1ab068f6039d5780c68dd0fa2f8742cceb3426d19667778ca7f3518a9"
SELECTOR_OWNER_OF = "0x3552df12bdc6089cf963c40c4cf56fbfd4bd14680c244d1c5494c2790f1ea5c"
SELECTOR_OWNEROF = "0x2962ba17806af798afa6eaf4aa8c93a9fb60a3e305045b6eea33435086cae9"


def load_known_nft_contracts():
    # merge the built-in list with the user's local one (lines of: address,kind,name)
    by_int = {int(a, 16): v for a, v in KNOWN_NFT_CONTRACTS.items()}
    if os.path.exists(LOCAL_NFT_CONTRACTS_FNAME):
        with open(LOCAL_NFT_CONTRACTS_FNAME) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                addr, kind, name = line.split(",", 2)
                if kind.strip() not in ("nft", "lp"):
                    raise Exception("bad kind in " + LOCAL_NFT_CONTRACTS_FNAME + " line: " + line + " (must be nft or lp)")
                by_int[int(addr.strip(), 16)] = (name.strip(), kind.strip())
    return by_int


KNOWN_NFT_BY_INT = load_known_nft_contracts()


def load_env():
    env_path = os.path.join(SCRIPT_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    missing = [k for k in ("STARKSCAN_KEY", "STARKSCAN_RPC_URL") if not os.environ.get(k)]
    if missing:
        raise Exception("missing " + " and ".join(missing) + " - add them to .env (get a key at https://starkscan.co/api-key)")
    return os.environ["STARKSCAN_KEY"], os.environ["STARKSCAN_RPC_URL"]


def get_stark_domain(domain):
    try:
        json_page = requests.get("https://api.starknet.id/domain_to_addr", params={"domain": domain}, timeout=30).json()
    except Exception as e:
        raise Exception("could not reach the starknet.id api to resolve " + domain + " (" + str(e.__class__.__name__) + ") - try again later or pass the 0x address directly")
    if "addr" not in json_page:
        raise Exception("no address found for " + domain + " using starknet.id api: " + json.dumps(json_page))
    return json_page["addr"]


def check_address_exists(rpc_url, api_key, address):
    # starknet_getClassHashAt fails with CONTRACT_NOT_FOUND for undeployed/typo'd addresses.
    # only a warning: undeployed wallets can still have received tokens, and the export
    # dies later with "no results" anyway if the address is really a typo
    body = {"jsonrpc": "2.0", "id": 1, "method": "starknet_getClassHashAt",
            "params": {"block_id": "latest", "contract_address": address}}
    resp = requests.post(rpc_url, json=body, headers={"X-Starkscan-Api-Key": api_key}, timeout=30)
    if resp.status_code != 200:
        raise Exception("RPC check failed (HTTP " + str(resp.status_code) + ") - check STARKSCAN_RPC_URL in .env: " + resp.text[:200])
    error = resp.json().get("error")
    if error:
        if "Contract not found" in json.dumps(error) or error.get("code") == 20:
            print("WARNING: no contract deployed at this address - double check it, or it may just be an undeployed wallet")
        else:
            raise Exception("RPC error while checking the address (not an address problem, likely infrastructure): " + json.dumps(error))


def api_get(api_key, path, params):
    url = API_BASE + path
    headers = {"Accept": "application/json", "X-Starkscan-Api-Key": api_key}
    for attempt in range(MAX_RETRIES):
        resp = requests.get(url, headers=headers, params=params, timeout=60)
        if resp.status_code == 429:
            try:
                wait = int(resp.headers.get("Retry-After", "2"))
            except ValueError:  # Retry-After may legally be an http date
                wait = 5
            print("rate limited, waiting " + str(wait) + "s...")
            time.sleep(wait)
            continue
        if resp.status_code in (401, 403):
            raise Exception("Starkscan rejected the API key (HTTP " + str(resp.status_code) + ") - check STARKSCAN_KEY in .env: " + resp.text[:200])
        if resp.status_code >= 500:
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code != 200:
            raise Exception("Starkscan API error HTTP " + str(resp.status_code) + " for " + path + ": " + resp.text[:300])
        return resp.json()
    raise Exception("giving up on " + path + " after " + str(MAX_RETRIES) + " attempts")


def parse_date_arg(value, end_of_day):
    try:
        d = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise Exception("dates must be YYYY-MM-DD, got: " + value)
    if end_of_day:
        d = d.replace(hour=23, minute=59, second=59)
    return d.replace(tzinfo=timezone.utc)


def block_at_date(api_key, chain, dt, closest):
    # resolve a date to a block number so the api can filter server-side
    try:
        page = api_get(api_key, "/v1/" + chain + "/block-at-timestamp",
                       {"timestamp": dt.strftime("%Y-%m-%dT%H:%M:%SZ"), "closest": closest})
    except Exception as e:
        if "No indexed block found" in str(e):
            raise Exception("no starknet blocks exist " + ("at or after " if closest == "after" else "at or before ") + dt.strftime("%Y-%m-%d %H:%M:%S") + " UTC - check your date range")
        raise
    return page["block"]["blockNumber"]


def fetch_paginated(api_key, path, extra_params=None):
    items = []
    cursor = None
    while True:
        params = {"limit": PAGE_LIMIT}
        params.update(extra_params or {})
        if cursor:
            params["cursor"] = cursor
        page = api_get(api_key, path, params)
        items.extend(page["items"])
        cursor = page.get("nextCursor")
        print("fetched " + str(len(items)) + " rows...", end="\r")
        if not cursor:
            break
        time.sleep(REQUEST_PAUSE)
    print()
    return items


def norm_addr(addr):
    return int(addr, 16) if addr else None


def decode_felt_string(felts):
    # decodes a cairo short string, or a cairo1 ByteArray [n_words, words..., pending, pending_len]
    def felt_ascii(h):
        v = int(h, 16)
        return bytes.fromhex(hex(v)[2:].rjust((len(hex(v)) - 2 + 1) // 2 * 2, "0")).decode("utf-8", errors="replace") if v else ""
    if len(felts) == 1:
        return felt_ascii(felts[0])
    try:
        n = int(felts[0], 16)
        return "".join(felt_ascii(w) for w in felts[1:1 + n] + [felts[1 + n]])
    except Exception:
        return ""


def rpc_call(contract, selector, calldata):
    try:
        resp = requests.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": "starknet_call",
                                            "params": {"request": {"contract_address": contract, "entry_point_selector": selector, "calldata": calldata},
                                                       "block_id": "latest"}},
                             headers={"X-Starkscan-Api-Key": api_key}, timeout=30).json()
        return resp.get("result")
    except Exception:
        return None


def resolve_contract_info(samples):
    # ask the chain directly about contracts starkscan has no metadata for:
    #  - name()/symbol() for display
    #  - decimals() answers  -> behaves like an ERC20 (and we learn the decimals)
    #  - owner_of(sample id) answers -> provably an ERC721, the "amount" was a token id
    # samples: {address: [candidate token ids seen in this wallet's transfers, as hex]}
    # results (including failures, as nulls) are cached in log/ so each contract is queried once
    if not samples:
        return {}
    cache = {}
    if os.path.exists(NAME_CACHE_FNAME):
        with open(NAME_CACHE_FNAME) as f:
            cache = json.load(f)
    changed = False
    for addr, sample_ids in samples.items():
        if addr in cache and "kind" in cache[addr]:
            continue
        entry = {"name": None, "symbol": None, "kind": None, "decimals": None}
        for label, selector in (("name", SELECTOR_NAME), ("symbol", SELECTOR_SYMBOL)):
            result = rpc_call(addr, selector, [])
            if result:
                entry[label] = decode_felt_string(result).strip() or None
        decimals_result = rpc_call(addr, SELECTOR_DECIMALS, [])
        if decimals_result:
            entry["kind"] = "erc20"
            entry["decimals"] = int(decimals_result[0], 16)
        else:
            for tid in sample_ids[:3]:  # burned ids fail owner_of, so try a few
                if rpc_call(addr, SELECTOR_OWNER_OF, [tid, "0x0"]) or rpc_call(addr, SELECTOR_OWNEROF, [tid, "0x0"]):
                    entry["kind"] = "erc721"
                    break
        cache[addr] = entry
        changed = True
    if changed:
        os.makedirs(os.path.dirname(NAME_CACHE_FNAME), exist_ok=True)
        tmp = NAME_CACHE_FNAME + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f, indent=1)
        os.replace(tmp, NAME_CACHE_FNAME)
    return {a: cache[a] for a in samples}


def iso_to_dt(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")) if iso else None


def process_transfers(items, wallet_address):
    wallet = norm_addr(wallet_address)
    unknown_samples = {}
    for row in items:
        if (row["tokenSymbol"] is None and row["tokenDecimals"] is None and row["tokenId"] is None
                and norm_addr(row["tokenAddress"]) not in KNOWN_NFT_BY_INT):
            ids = unknown_samples.setdefault(row["tokenAddress"], [])
            if row["rawValue"] is not None and len(ids) < 3:
                ids.append(hex(int(row["rawValue"])))
    resolved_names = resolve_contract_info(unknown_samples)
    for row in items:
        # fix up collections starkscan mislabels or has no metadata for
        known = KNOWN_NFT_BY_INT.get(norm_addr(row["tokenAddress"]))
        if known:
            row["tokenName"] = row["tokenName"] or known[0]
            row["tokenSymbol"] = row["tokenSymbol"] or known[0]
            row["nftKind"] = known[1]
            if row["tokenId"] is None and row["rawValue"] is not None:
                # misclassified as erc20: the "amount" is actually the token id
                row["tokenId"] = row["rawValue"]
                row["standard"] = "erc721"
                row["amount"] = row["rawValue"] = None
        else:
            row["nftKind"] = ""
            res = resolved_names.get(row["tokenAddress"])
            if res:
                if row["tokenSymbol"] is None:
                    # names read straight from the contract; flagged so koinly output keeps
                    # these address-qualified (a scam token can claim any symbol it likes)
                    row["tokenSymbol"] = res["symbol"] or res["name"]
                    row["tokenName"] = row["tokenName"] or res["name"]
                    row["symbolFromChain"] = bool(res["symbol"] or res["name"])
                if res.get("kind") == "erc721" and row["tokenId"] is None and row["rawValue"] is not None:
                    # proven NFT (owner_of answered): the "amount" is really the token id
                    row["tokenId"] = row["rawValue"]
                    row["standard"] = "erc721"
                    row["amount"] = row["rawValue"] = None
                    row["nftKind"] = "lp" if re.search(r"position", res.get("name") or "", re.I) else "nft"
                elif res.get("kind") == "erc20" and row["tokenDecimals"] is None and res.get("decimals"):
                    row["tokenDecimals"] = res["decimals"]  # so amounts scale properly
        dt = iso_to_dt(row["timestampIso"])
        row["utcTime"] = dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""
        sender, receiver = norm_addr(row["fromAddress"]), norm_addr(row["toAddress"])
        if sender == wallet and receiver == wallet:
            row["in_or_out"] = "SELF"
        elif sender == wallet:
            row["in_or_out"] = "OUT"
        else:
            row["in_or_out"] = "IN"  # includes mints, where fromAddress is null
        ctx = row.get("actionContext") or {}
        row["actionKind"] = ctx.get("actionKind") or ""
        row["actionLabel"] = ctx.get("actionLabel") or ""
        row["protocolName"] = ctx.get("protocolName") or ""
        # human amount: starkscan's "amount" is NOT decimals-adjusted (it matches rawValue), so adjust here
        base_amount = row["rawValue"] if row["rawValue"] is not None else row["amount"]
        if base_amount is not None and row["tokenDecimals"]:
            row["displayAmount"] = format(Decimal(base_amount) / (Decimal(10) ** row["tokenDecimals"]), "f")
        elif base_amount is not None:
            row["displayAmount"] = base_amount
        else:
            row["displayAmount"] = "1"  # ERC721 transfer of a single NFT
        for field in list(row):
            if row[field] is None:
                row[field] = ""
            elif isinstance(row[field], str):
                row[field] = row[field].replace("\n", "\\n")
            elif isinstance(row[field], (dict, list)):
                row[field] = json.dumps(row[field])
    if unknown_samples:
        print("NOTE: " + str(len(unknown_samples)) + " token contract(s) have no starkscan metadata; classified via on-chain calls:")
        for a in sorted(unknown_samples):
            res = resolved_names.get(a) or {}
            label = (res.get("name") or "?") + " / " + (res.get("symbol") or "?")
            kind = res.get("kind")
            verdict = {"erc20": "token (decimals answered)", "erc721": "NFT (owner_of answered) - auto-handled"}.get(kind,
                      "UNDETERMINED - if it's an NFT/LP, add to nft_contracts_local.txt (address,kind,name)")
            print("        " + a + "  ->  " + label + "  ->  " + verdict)


def process_transactions(items):
    for row in items:
        dt = iso_to_dt(row["timestampIso"])
        row["utcTime"] = dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""
        for field in list(row):
            if row[field] is None:
                row[field] = ""
            elif isinstance(row[field], (dict, list)):
                row[field] = json.dumps(row[field])


def load_placeholder_map():
    if os.path.exists(PLACEHOLDER_MAP_FNAME):
        with open(PLACEHOLDER_MAP_FNAME) as f:
            return json.load(f)
    # first run: seed counters from the old voyager script's tracker files if present
    state = {"nft": {}, "lp": {}, "next_nft": 1, "next_lp": 1}
    for fnames, counter in ((LEGACY_NFT_COUNTER_FNAMES, "next_nft"), (LEGACY_LP_COUNTER_FNAMES, "next_lp")):
        for fname in fnames:
            if os.path.exists(fname):
                with open(fname) as f:
                    state[counter] = max(state[counter], int(f.read()))
    if state["next_nft"] > 1 or state["next_lp"] > 1:
        print("seeded placeholder counters from legacy files: next NFT" + str(state["next_nft"]) + ", next LP" + str(state["next_lp"]))
    return state


def save_placeholder_map(state):
    # write via temp file + rename so a crash can't corrupt the accumulated placeholder history
    os.makedirs(os.path.dirname(PLACEHOLDER_MAP_FNAME), exist_ok=True)
    tmp = PLACEHOLDER_MAP_FNAME + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, PLACEHOLDER_MAP_FNAME)


def nft_placeholder(state, kind, token_address, token_id):
    # koinly can only track NFTs via NFT1..NFT5000 / LP placeholder currencies, one per unique NFT.
    # key on contract+id (token ids alone collide across collections) and persist the mapping
    # so the same NFT gets the same placeholder on every run
    key = token_address + ":" + str(token_id)
    if key not in state[kind]:
        state[kind][key] = kind.upper() + str(state["next_" + kind])
        state["next_" + kind] += 1
        if kind == "nft" and state["next_nft"] > KOINLY_MAX_NFT_PLACEHOLDERS:
            print("WARNING: passed koinly's documented limit of " + str(KOINLY_MAX_NFT_PLACEHOLDERS) + " NFT placeholders")
        if kind == "lp" and state["next_lp"] > KOINLY_MAX_LP_PLACEHOLDERS:
            print("WARNING: passed " + str(KOINLY_MAX_LP_PLACEHOLDERS) + " LP placeholders, koinly may not support this many")
    return state[kind][key]


def is_fee_row(row):
    return row["in_or_out"] == "OUT" and (row["actionKind"] == "fee" or norm_addr(row["toAddress"]) in SEQUENCER_ADDRESSES)


def build_koinly_row(row, state):
    is_nft = row["tokenId"] != "" or row["standard"].upper() in ("ERC721", "ERC1155")
    symbol = row["tokenSymbol"]
    if not symbol:
        # koinly's SYMBOL:ADDRESS syntax keeps each unknown token distinct instead of
        # merging every unnamed (usually scam airdrop) token into one currency
        symbol = "UNKNOWN:" + row["tokenAddress"]
    elif row.get("symbolFromChain") and not is_nft:
        # self-reported symbol: keep it address-qualified so it can't impersonate a real token
        symbol = symbol + ":" + row["tokenAddress"]
    description = " ".join(x for x in (row["actionLabel"] or row["actionKind"], row["protocolName"]) if x)
    if not row["tokenDecimals"] and not is_nft:
        description += " (token decimals unknown, amount is raw)"
    tokeninfo = (row["tokenName"] + " - " if row["tokenName"] else "") + row["tokenAddress"]
    if is_nft:
        is_lp = row.get("nftKind") == "lp" or row["actionKind"] in ("lp_add", "lp_remove") or symbol in LP_SYMBOLS
        placeholder = nft_placeholder(state, "lp" if is_lp else "nft", row["tokenAddress"], row["tokenId"])
        description += " " + symbol + " " + tokeninfo + " #" + str(row["tokenId"]) + " (starknet " + ("LP position" if is_lp else "nft") + " as " + placeholder + ")"
        symbol = placeholder
    else:
        description += " " + tokeninfo
    koinly_datarow = {
        "Date": row["utcTime"] + " UTC",
        "Sent Amount": "", "Sent Currency": "",
        "Received Amount": "", "Received Currency": "",
        "Fee Amount": "", "Fee Currency": "",
        "Net Worth Amount": "", "Net Worth Currency": "",
        "Label": "",
        "Description": description.strip(),
        "TxHash": row["txHash"],
    }
    if row["in_or_out"] == "OUT":
        koinly_datarow["Sent Amount"] = row["displayAmount"]
        koinly_datarow["Sent Currency"] = symbol
    else:
        koinly_datarow["Received Amount"] = row["displayAmount"]
        koinly_datarow["Received Currency"] = symbol
    return koinly_datarow


def koinly_format(items, state):
    # current koinly universal template rules (support.koinly.io, checked jul 2026):
    # date YYYY-MM-DD HH:mm:ss in UTC, unused cells left empty, dot decimal separator.
    # network fees appear in the starkscan feed as sends to the sequencer; fold each tx's
    # fee into the Fee columns of another row from the same tx so balances reconcile
    # without double counting, instead of exporting the fee as a bare withdrawal
    koinly_array = []
    skipped_self = 0
    tx_groups = []  # rows are sorted, so rows of the same tx are contiguous
    for row in items:
        if row["in_or_out"] == "SELF":
            skipped_self += 1
            continue
        if tx_groups and tx_groups[-1][0]["txHash"] == row["txHash"]:
            tx_groups[-1].append(row)
        else:
            tx_groups.append([row])
    for group in tx_groups:
        fee_rows = [r for r in group if is_fee_row(r)]
        normal_rows = [r for r in group if not is_fee_row(r)]
        datarows = [build_koinly_row(r, state) for r in normal_rows]
        if fee_rows:
            # sum per currency (a paymaster tx could emit both an ETH and an STRK fee)
            fees_by_currency = {}
            for r in fee_rows:
                cur = r["tokenSymbol"] or "ETH"
                fees_by_currency.setdefault(cur, [Decimal(0), r])
                fees_by_currency[cur][0] += Decimal(r["displayAmount"])
            target = next((d for d in datarows if d["Sent Amount"]), None) or next(iter(datarows), None)
            for fee_currency, (fee_total, sample_row) in fees_by_currency.items():
                fee_amount = format(fee_total, "f")
                if target is not None and not target["Fee Amount"]:
                    target["Fee Amount"] = fee_amount
                    target["Fee Currency"] = fee_currency
                else:
                    # no sibling row to carry it (or it already carries another currency):
                    # keep the fee as its own cost row
                    fee_datarow = build_koinly_row(sample_row, state)
                    fee_datarow["Sent Amount"] = fee_amount
                    fee_datarow["Label"] = "cost"
                    fee_datarow["Description"] = ("network fee " + fee_datarow["Description"]).strip()
                    datarows.append(fee_datarow)
        koinly_array.extend(datarows)
    if skipped_self:
        print("skipped " + str(skipped_self) + " self-transfer(s), koinly doesn't need them")
    return koinly_array


def load_run_log():
    if os.path.exists(RUN_LOG_FNAME):
        with open(RUN_LOG_FNAME) as f:
            return json.load(f)
    return {"runs": []}


def save_run_log(log):
    os.makedirs(os.path.dirname(RUN_LOG_FNAME), exist_ok=True)
    tmp = RUN_LOG_FNAME + ".tmp"
    with open(tmp, "w") as f:
        json.dump(log, f, indent=1)
    os.replace(tmp, RUN_LOG_FNAME)


def undo_last_run():
    # reverts the placeholder assignments and counters of the most recent logged run
    # (it does NOT un-import anything from koinly - delete the csv / koinly import manually)
    log = load_run_log()
    if not log["runs"]:
        raise Exception("run log is empty, nothing to undo")
    entry = log["runs"].pop()
    state = load_placeholder_map()
    for placeholder, key in entry.get("new_placeholders", {}).items():
        kind = "lp" if placeholder.startswith("LP") else "nft"
        state[kind].pop(key, None)
    if "counters_before" in entry:
        state["next_nft"] = entry["counters_before"]["next_nft"]
        state["next_lp"] = entry["counters_before"]["next_lp"]
    save_placeholder_map(state)
    save_run_log(log)
    print("undid run of " + entry["date_run"] + " for wallet " + entry["wallet"])
    if entry.get("new_placeholders"):
        print("removed placeholders: " + ", ".join(entry["new_placeholders"]))
    print("counters restored to next NFT" + str(state["next_nft"]) + ", next LP" + str(state["next_lp"]))
    print("NOTE: the exported csv still exists (" + entry.get("output_file", "?") + ") - delete it yourself if it was a test")


def write_csv(f_name, fields, rows):
    os.makedirs(os.path.dirname(f_name), exist_ok=True)
    with open(f_name, "w", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(fields)
        for row in rows:
            writer.writerow([row.get(field, "") for field in fields])
    print("wrote " + str(len(rows)) + " rows to " + f_name)


parser = argparse.ArgumentParser(description="export starknet history from starkscan")
parser.add_argument("-w", "--wallet", type=str, help="0x address or .stark domain")
parser.add_argument("-l", "--wallets-file", dest="wallets_file", type=str,
                    help="path to a file with one wallet per line (0x or .stark, # comments allowed) - exports each in turn")
parser.add_argument("-t", "--type", type=str, default="all",
                    choices=["ERC20", "ERC721", "ERC1155", "all", "transactions"],
                    help="token transfers filtered by standard, all transfers, or transactions")
parser.add_argument("-f", "--format", type=str, default="verbose", choices=["verbose", "standard", "koinly", "all"],
                    help="'all' writes two files per wallet: koinly + full-detail verbose")
parser.add_argument("-c", "--chain", type=str, default="SN_MAIN")
parser.add_argument("--from-date", dest="from_date", type=str, default=None,
                    help="inclusive start date YYYY-MM-DD in UTC (default: start of chain)")
parser.add_argument("--to-date", dest="to_date", type=str, default=None,
                    help="inclusive end date YYYY-MM-DD in UTC (default: now)")
parser.add_argument("--undo", dest="undo", action="store_true",
                    help="revert whatever the last invocation did - one wallet or a whole --wallets-file batch - then exit")
parser.add_argument("--undo-last", dest="undo_last", type=int, nargs="?", const=1, default=None, metavar="N",
                    help="surgical form: revert exactly the last run (or last N runs), then exit")
args = parser.parse_args()

if args.undo_last:
    for _ in range(args.undo_last):
        undo_last_run()
    sys.exit(0)
if args.undo:
    runs = load_run_log()["runs"]
    if not runs:
        raise Exception("run log is empty, nothing to undo")
    target = runs[-1].get("batch")
    if not target:
        raise Exception("the last run predates batch ids - use --undo-last N instead (N = wallets in that batch, check state/koinly_run_log.json)")
    count = 0
    while True:
        runs = load_run_log()["runs"]
        if not runs or runs[-1].get("batch") != target:
            break
        undo_last_run()
        count += 1
    print("undid all " + str(count) + " run(s) of batch " + target)
    sys.exit(0)
if bool(args.wallet) == bool(args.wallets_file):
    raise Exception("pass exactly one of --wallet or --wallets-file")
if args.format in ("koinly", "all") and args.type == "transactions":
    raise Exception("koinly needs token transfers, not transactions - use --type all (or ERC20/ERC721)")
if args.format in ("koinly", "all") and args.type in ("ERC721", "ERC1155"):
    print("WARNING: filtering to " + args.type + " drops the ERC20 legs of NFT trades AND all network fees - use --type all for a complete koinly import")

api_key, rpc_url = load_env()

from_dt = parse_date_arg(args.from_date, end_of_day=False) if args.from_date else None
to_dt = parse_date_arg(args.to_date, end_of_day=True) if args.to_date else None
if from_dt and to_dt and from_dt > to_dt:
    raise Exception("--from-date is after --to-date")
# resolve dates to blocks so the transfers endpoint can filter server-side
block_params = {}
if from_dt:
    block_params["from_block"] = block_at_date(api_key, args.chain, from_dt, "after")
if to_dt:
    block_params["to_block"] = block_at_date(api_key, args.chain, to_dt, "before")


def in_date_range(row):
    ts = iso_to_dt(row["timestampIso"])
    if ts is None:
        return True
    return not ((from_dt and ts < from_dt) or (to_dt and ts > to_dt))


range_tag = ("_from" + args.from_date if args.from_date else "") + ("_to" + args.to_date if args.to_date else "")
run_batch_id = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def export_wallet(wallet_input):
    wallet_address = wallet_input
    if re.search(r"\.stark$", wallet_address):
        wallet_address = get_stark_domain(wallet_address)
        print(wallet_input + " resolved to " + wallet_address)
    check_address_exists(rpc_url, api_key, wallet_address)

    time_for_file_name = re.sub(r"[:+,.]", ".", datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds"))

    if args.type == "transactions":
        # the transactions endpoint has no block filters, so the range is applied client-side only
        page_data = fetch_paginated(api_key, "/v1/" + args.chain + "/address/" + wallet_address + "/transactions")
        page_data = [row for row in page_data if in_date_range(row)]
        process_transactions(page_data)
        page_data.sort(key=lambda x: (x["blockNumber"] or 0, x["txIndex"] or 0))
        fields = ["utcTime", "blockNumber", "txHash", "txType", "executionStatus", "finalityStatus",
                  "fromAddress", "toAddress", "primaryMethod", "callCount", "transferCount", "counterparty", "kinds"]
    else:
        page_data = fetch_paginated(api_key, "/v1/" + args.chain + "/address/" + wallet_address + "/transfers?direction=any", block_params)
        page_data = [row for row in page_data if in_date_range(row)]  # block bounds are coarse, trim exactly
        if args.type != "all":
            page_data = [row for row in page_data if (row["standard"] or "").upper() == args.type]
        process_transfers(page_data, wallet_address)
        page_data.sort(key=lambda x: (x["blockNumber"] or 0, x["txIndex"] or 0, x["logIndex"] or 0))
        fields = ["utcTime", "blockNumber", "standard", "tokenSymbol", "tokenName", "tokenAddress", "tokenId",
                  "displayAmount", "in_or_out", "fromAddress", "toAddress", "txHash", "actionKind", "protocolName"]

    if len(page_data) == 0:
        raise Exception("no results returned - the wallet exists but starkscan has no " + args.type + " history for it"
                        + (" in the given date range" if (from_dt or to_dt) else ""))

    log_entry = {
        "date_run": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "batch": run_batch_id,  # same for every wallet of one invocation, so --undo-batch can revert them together
        "wallet": wallet_input,
        "wallet_address": wallet_address,
        "type": args.type, "format": args.format,
        "from_date": args.from_date, "to_date": args.to_date,
    }
    formats = ["koinly", "verbose"] if args.format == "all" else [args.format]
    written_files = []
    rows_written = {}
    for fmt in formats:
        f_name = os.path.join(SCRIPT_DIR, "output", args.type + "_" + wallet_input + range_tag + "_" + fmt + "_" + time_for_file_name + ".csv")
        if fmt == "koinly":
            if from_dt:
                print("NOTE: a partial date range means koinly won't see the wallet's earlier history, so balances won't build up from zero")
            koinly_fields = ["Date", "Sent Amount", "Sent Currency", "Received Amount", "Received Currency",
                             "Fee Amount", "Fee Currency", "Net Worth Amount", "Net Worth Currency",
                             "Label", "Description", "TxHash"]
            state = load_placeholder_map()
            counters_before = {"next_nft": state["next_nft"], "next_lp": state["next_lp"]}
            keys_before = {"nft": set(state["nft"]), "lp": set(state["lp"])}
            rows_out = koinly_format(page_data, state)
            save_placeholder_map(state)
            out_fields = koinly_fields
            log_entry["counters_before"] = counters_before
            log_entry["counters_after"] = {"next_nft": state["next_nft"], "next_lp": state["next_lp"]}
            log_entry["new_placeholders"] = {state[kind][key]: key for kind in ("nft", "lp")
                                             for key in state[kind] if key not in keys_before[kind]}
        elif fmt == "standard":
            rows_out, out_fields = page_data, fields
        else:  # verbose: every field starkscan returned
            rows_out, out_fields = page_data, list(page_data[0].keys())
        write_csv(f_name, out_fields, rows_out)
        written_files.append(os.path.basename(f_name))
        rows_written[fmt] = len(rows_out)
    log_entry["rows_written"] = rows_written if len(formats) > 1 else rows_written[formats[0]]
    log_entry["output_file"] = "; ".join(written_files)
    log = load_run_log()
    log["runs"].append(log_entry)
    save_run_log(log)


if args.wallets_file:
    with open(args.wallets_file) as wf:
        wallets = [line.strip() for line in wf if line.strip() and not line.strip().startswith("#")]
    if not wallets:
        raise Exception("no wallet addresses found in " + args.wallets_file)
else:
    wallets = [args.wallet]

failed = []
for i, wallet in enumerate(wallets):
    if len(wallets) > 1:
        print("--- wallet " + str(i + 1) + "/" + str(len(wallets)) + ": " + wallet + " ---")
    try:
        export_wallet(wallet)
    except Exception as e:
        # keep going so one bad address doesn't kill a batch export
        print("ERROR exporting " + wallet + ": " + str(e))
        failed.append(wallet)
if failed:
    raise Exception(str(len(failed)) + " of " + str(len(wallets)) + " wallet(s) failed: " + ", ".join(failed))
