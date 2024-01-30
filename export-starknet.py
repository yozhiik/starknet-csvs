# import starknet transactions from Voyager
import sys, argparse, requests, json, csv 
from datetime import datetime

import argparse
parser=argparse.ArgumentParser(description="add numbers")
parser.add_argument("wallet_address", type=str)
parser.add_argument("filename", type=str)
args=parser.parse_args()
wallet_address=args.wallet_address
f_name=args.filename

base_url = "https://voyager.online/api/txns"
per_page = 100

# pull in data from API
page = 1
payload = {'to': wallet_address, 'ps': per_page, 'p': page, 'type': "null"}
json_page = requests.get(base_url, params=payload).json()
num_pages = json_page["lastPage"]
page_data = json_page["items"]

# random strings passed as the address lead to the entire blockchain's index being passed (currently 900,000 ish) so don't save it all
if num_pages == 0 or num_pages > 100000:
    print("address not found, double check on voyager")
    sys.exit()

## loop through and add the rest of the items from the other pages
for i in range(2, num_pages + 1):
    payload = {'to': wallet_address, 'ps': per_page, 'p': i, 'type': "null"}
    json_page = requests.get(base_url, params=payload).json()
    # append this page data to the first page's
    page_data = page_data + json_page["items"]


fields = ["utcTime", "blockId", "blockNumber", "hash", "index", "l1VerificationHash", "type", "class_hash", "sender_address", "contract_address", "timestamp", "actual_fee", "execution_status", "revert_error", "domain", "status", "finality_status", "operations", "classAlias", "contractAlias", "senderAlias"]

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