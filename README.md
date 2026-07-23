# starkscan-export.py

The previous Voyager-based version lives at tag [`voyager-final`](https://github.com/yozhiik/starknet-csvs/releases/tag/voyager-final); Voyager retired its free API in 2026.

Exports a Starknet wallet's history from the [Starkscan](https://starkscan.co) API to CSV, including a [Koinly](https://koinly.io)-ready format for tax reporting. This replaces `export-starknet.py` after Voyager retired its free API (old Voyager keys are explicitly denied on their new gateway, and their old transfer endpoint now returns an empty index).

## Setup

1. Get a free Starkscan API key via GitHub sign-in at https://starkscan.co/api-key
2. Create a `.env` file next to the script:
```
STARKSCAN_KEY=your_api_key
STARKSCAN_RPC_URL=your_starkscan_rpc_url
```
The RPC url is used only to sanity-check the wallet address on-chain before fetching, so typos fail fast. The script fails with a clear message if either variable is missing or rejected.

## Usage

```
python3 starkscan-export.py --wallet=<0x address or name.stark> [options]
```

| option | default | meaning |
|---|---|---|
| `-w` `--wallet` | | full 0x address or a `.stark` domain (resolved via starknet.id) |
| `-l` `--wallets-file` | | path to a file listing one wallet per line (0x or .stark mixed freely, `#` comments and blank lines ignored); each wallet is exported to its own CSV. Pass exactly one of `--wallet` / `--wallets-file` |
| `-t` `--type` | `all` | `all` (every token transfer), `ERC20`, `ERC721`, `ERC1155`, or `transactions` |
| `-f` `--format` | `verbose` | `verbose` (every API field), `standard` (useful subset), `koinly` |
| `--from-date` | start of chain | inclusive start date `YYYY-MM-DD`, interpreted as 00:00:00 UTC |
| `--to-date` | now | inclusive end date `YYYY-MM-DD`, interpreted as 23:59:59 UTC |
| `-c` `--chain` | `SN_MAIN` | Starkscan chain id |

Examples:
```
python3 starkscan-export.py --wallet=me.stark --format=koinly
python3 starkscan-export.py --wallet=0xabc... --type=all --format=koinly --from-date=2025-04-06 --to-date=2026-04-05
python3 starkscan-export.py --wallet=0xabc... --type=transactions --format=standard
python3 starkscan-export.py --wallets-file=wallets.txt --format=koinly
```
A `wallets.txt` template is included and gitignored (it's your personal address list - don't commit it). In batch mode a failing wallet is reported and skipped so it doesn't kill the rest; the script lists any failures at the end. Placeholder assignments are shared across all wallets in the batch, which is what you want if they all feed the same Koinly account.

Output goes to `output/` next to the script; the filename records the wallet, type, format, date range and export time. Date filtering is done server-side by block range where the API supports it (transfers) and always re-trimmed client-side to exact UTC day boundaries.

## The Koinly format

Follows Koinly's current universal template (checked against their help center, July 2026):
- headers `Date, Sent Amount, Sent Currency, Received Amount, Received Currency, Fee Amount, Fee Currency, Net Worth Amount, Net Worth Currency, Label, Description, TxHash`
- dates as `YYYY-MM-DD HH:mm:ss UTC`, unused cells empty, dot decimal separators

### Network fees
Starknet fee payments appear on-chain as token sends to the sequencer address. The script recognises all three historical sequencer addresses and folds each transaction's fee into the `Fee Amount`/`Fee Currency` columns of another row from the same transaction (exactly once per transaction, so nothing is double-counted). If a transaction contains only the fee payment, it's exported as a withdrawal with the `cost` label instead. Fees paid in STRK are handled.

Sanity check: `sum(Received) - sum(Sent) - sum(Fees)` for ETH from the exported CSV reconciled **to the exact wei** against the live on-chain balance for both test wallets.

### NFTs and LP positions
Koinly custom files still cannot import NFTs directly - their only supported route is placeholder currencies, so the script does that automatically:
- every unique NFT (keyed on contract **and** token id) gets its own `NFTx` placeholder; Koinly documents a hard limit of 5000 and each placeholder must only ever track a single NFT
- LP position NFTs (detected via Starkscan's action classification plus a symbol list: Ekubo, JediSwap v2) get `LPx` placeholders instead, so liquidity in/out legs match up. Koinly doesn't document an LP placeholder limit; the script warns past 1000
- assignments persist in `log/koinly_placeholder_map.json` so re-runs and future exports always give the same NFT the same placeholder. **Keep this file.** On first run it seeds its counters from the old script's `last_used_*.txt` files (checking the repo root and `output/`, taking the highest), so numbering continues after placeholders you've already imported into Koinly rather than reusing them
- the NFT's real name, contract, and token id are recorded in the Description column

### Run log and undo
Every export appends an entry to `log/koinly_run_log.json` (the `log/` folder is gitignored - it contains your wallet addresses): human-readable run date, wallet, type/format, date range, rows written, output filename, and - for koinly runs - the placeholder counters before/after plus exactly which new placeholders were assigned to which NFT. It doubles as a simple database of what you've imported and when.

Ran a test you want to take back? `python3 starkscan-export.py --undo-last` reverts the most recent run's placeholder assignments and counters (repeat to step further back). It reminds you which CSV to delete; it obviously can't un-import anything already uploaded to Koinly.

The two files in `log/` are the ones worth backing up privately - they have no git safety net, and losing the placeholder map after real imports would scramble NFT numbering.

### Unknown tokens
Tokens Starkscan has no metadata for (usually scam airdrops) are exported as `UNKNOWN:<contract address>` so each stays a distinct currency instead of merging into one; Koinly may ask you to map these on import, or you can delete those rows if they're junk. If token decimals are unknown the raw integer amount is exported and flagged in the Description.

### Importing tips
- import with `--type=all`: filtering to `ERC721`/`ERC1155` drops the ERC20 legs of NFT trades and all fees (the script warns about this)
- a partial `--from-date` range means Koinly won't see earlier history, so balances won't build from zero (the script notes this too)
- self-transfers are skipped (Koinly's double-ledger model doesn't want them)

## Caveats
- the Starkscan transfers/transactions routes are marked "beta" certification in their OpenAPI spec, so field names could change
- free tier is rate limited (~1-2 req/s); the script throttles itself and honours 429 backoff, so large wallets just take a minute or two
- `transactions` exports have no fee column (the list endpoint doesn't include fees; Koinly imports use transfers anyway)
- `.stark` resolution depends on the starknet.id API, which has had outages; pass the 0x address directly if it's down
