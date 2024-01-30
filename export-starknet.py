# import starknet transactions from Voyager
import sys, argparse, requests, json, csv 
from datetime import datetime

import argparse
parser=argparse.ArgumentParser(description="add numbers")
parser.add_argument("wallet_address", type=str)
parser.add_argument("download_type", type=str)
parser.add_argument("filename", type=str)
args=parser.parse_args()
wallet_address=args.wallet_address
download_type=args.download_type
f_name=args.filename
per_page = 100
max_pages = 100000

# init 
page = 1

if download_type != "transfers" and download_type != "transactions":
    print("download type must be 'transactions' or 'transfers'")
    sys.exit()

if download_type == 'transactions':
    base_url = "https://voyager.online/api/txns"
    payload = {'to': wallet_address, 'ps': per_page, 'p': page, 'type': "null"}

    json_page = requests.get(base_url, params=payload).json()
    num_pages = json_page["lastPage"]
    page_data = json_page["items"]

    # random strings passed as the address lead to the entire blockchain's index being passed (currently 900,000 ish) so don't save it all
    if num_pages == 0 or num_pages > max_pages:
        print("address not found, double check on voyager")
        sys.exit()

    ## loop through and add the rest of the items from the other pages
    for i in range(2, num_pages + 1):
        payload = {'to': wallet_address, 'ps': per_page, 'p': i, 'type': "null"}
        json_page = requests.get(base_url, params=payload).json()
        # append this page data to the first page's
        page_data = page_data + json_page["items"]

    fields = ["utcTime", "blockId", "blockNumber", "hash", "index", "l1VerificationHash", "type", "class_hash", "sender_address", "contract_address", "timestamp", "actual_fee", "execution_status", "revert_error", "domain", "status", "finality_status", "operations", "classAlias", "contractAlias", "senderAlias"]
else:
    # this is transfers
    base_url = "https://voyager.online/api/contract/" + wallet_address + "/transfers"
    payload = {'ps': per_page, 'p': page}

    json_page = requests.get(base_url, params=payload).json()
    num_pages = json_page["lastPage"]
    page_data = json_page["items"]

    # random strings passed as the address lead to the entire blockchain's index being passed (currently 900,000 ish) so don't save it all
    if num_pages == 0 or num_pages > max_pages:
        print("address not found, double check on voyager")
        sys.exit()

    ## loop through and add the rest of the items from the other pages
    for i in range(2, num_pages + 1):
        payload = {'ps': per_page, 'p': i}
        json_page = requests.get(base_url, params=payload).json()
        # append this page data to the first page's
        page_data = page_data + json_page["items"]

    fields = ["utcTime","blockNumber", "tokenAddress", "timestamp", "transferFrom", "transferTo", "transferValue", "txHash", "callName", "tokenName", "tokenSymbol", "tokenDecimals", "blockHash", "fromAlias", "toAlias"]

# sanitise / tidy up - set blank entries if missing values, replace new lines
for row in page_data:
    for field in fields:
        if field == "utcTime":
            row[field] = datetime.utcfromtimestamp(row["timestamp"])
        if not field in row:
            row[field] = ""
        if isinstance(row[field], str):
            row[field] = row[field].replace("\n", "\\n")
        

with open(f_name, 'w') as csv_file:
    writer = csv.writer(csv_file)
    writer.writerow(fields) # header
    # Write each data row to the CSV file    
    for row in page_data:
        writer.writerow([row[field] for field in fields])