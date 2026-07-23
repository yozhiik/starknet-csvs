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
PLACEHOLDER_MAP_FNAME = os.path.join(SCRIPT_DIR, "log", "koinly_placeholder_map.json")
# human-readable per-run audit log: what was exported when, for which wallet, and which
# placeholder numbers each run consumed - lets --undo-last revert a test run cleanly
RUN_LOG_FNAME = os.path.join(SCRIPT_DIR, "log", "koinly_run_log.json")
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


def iso_to_dt(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")) if iso else None


def process_transfers(items, wallet_address):
    wallet = norm_addr(wallet_address)
    for row in items:
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
    description = " ".join(x for x in (row["actionLabel"] or row["actionKind"], row["protocolName"]) if x)
    if not row["tokenDecimals"] and not is_nft:
        description += " (token decimals unknown, amount is raw)"
    tokeninfo = (row["tokenName"] + " - " if row["tokenName"] else "") + row["tokenAddress"]
    if is_nft:
        is_lp = row["actionKind"] in ("lp_add", "lp_remove") or symbol in LP_SYMBOLS
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
parser.add_argument("-f", "--format", type=str, default="verbose", choices=["verbose", "standard", "koinly"])
parser.add_argument("-c", "--chain", type=str, default="SN_MAIN")
parser.add_argument("--from-date", dest="from_date", type=str, default=None,
                    help="inclusive start date YYYY-MM-DD in UTC (default: start of chain)")
parser.add_argument("--to-date", dest="to_date", type=str, default=None,
                    help="inclusive end date YYYY-MM-DD in UTC (default: now)")
parser.add_argument("--undo-last", dest="undo_last", action="store_true",
                    help="revert the placeholder assignments/counters of the most recent run (see koinly_run_log.json), then exit")
args = parser.parse_args()

if args.undo_last:
    undo_last_run()
    sys.exit(0)
if bool(args.wallet) == bool(args.wallets_file):
    raise Exception("pass exactly one of --wallet or --wallets-file")
if args.format == "koinly" and args.type == "transactions":
    raise Exception("koinly needs token transfers, not transactions - use --type all (or ERC20/ERC721)")
if args.format == "koinly" and args.type in ("ERC721", "ERC1155"):
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


def export_wallet(wallet_input):
    wallet_address = wallet_input
    if re.search(r"\.stark$", wallet_address):
        wallet_address = get_stark_domain(wallet_address)
        print(wallet_input + " resolved to " + wallet_address)
    check_address_exists(rpc_url, api_key, wallet_address)

    time_for_file_name = re.sub(r"[:+,.]", ".", datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds"))
    f_name = os.path.join(SCRIPT_DIR, "output", args.type + "_" + wallet_input + range_tag + "_" + args.format + "_" + time_for_file_name + ".csv")

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
        "wallet": wallet_input,
        "wallet_address": wallet_address,
        "type": args.type, "format": args.format,
        "from_date": args.from_date, "to_date": args.to_date,
    }
    if args.format == "koinly":
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
    elif args.format == "standard":
        rows_out, out_fields = page_data, fields
    else:  # verbose: every field starkscan returned
        rows_out, out_fields = page_data, list(page_data[0].keys())
    write_csv(f_name, out_fields, rows_out)
    log_entry["rows_written"] = len(rows_out)
    log_entry["output_file"] = os.path.basename(f_name)
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
