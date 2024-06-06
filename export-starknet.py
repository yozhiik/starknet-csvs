# import starknet transactions from Voyager
import sys, argparse, requests, json, csv, configparser, re
from datetime import datetime,timezone #imported timezone in order to apply rules needed after python version 3.16

def get_stark_domain(domain):
    try:
        json_page = requests.get("https://api.starknet.id/domain_to_addr?domain=" + domain).json()
        if "addr" in json_page:
            return json_page["addr"]
    except:
        raise Exception("no address found for "+domain+" using starknet api")

def check_api_valid(page_data):
    if "message" in page_data:
        raise Exception('Voyager API not valid, error returned "'+page_data['message']+'".\nRequest a free API key from the team here: https://forms.gle/34RE6d4aiiv16HoW6\nDocs here: https://docs.voyager.online/#overview')

def convert_wei_to_eth(wei_val):
    return float(int(wei_val)*0.000000000000000001)

def get_transactions_data():
    transactions_url = base_url + "/txns"
    payload = {'ps': per_page, 'p': 1}
    payload['to'] = wallet_address 
    json_page = requests.get(transactions_url, headers=headers, params=payload).json()
    check_api_valid(json_page) ## check API key is working
    num_pages = json_page["lastPage"]
    page_data = json_page["items"]
    # random strings passed as the address lead to the entire blockchain's index being passed (currently 900,000 ish) so don't save it all
    if num_pages == 0 or num_pages > max_pages:
        raise Exception("address not found, double check on voyager")
    
    ## loop through and add the rest of the items from the other pages
    for i in range(2, num_pages + 1):
        payload['p'] = i # update page number
        json_page = requests.get(transactions_url, headers=headers, params=payload).json()
        # append this page data to the first page's
        page_data = page_data + json_page["items"]
    return page_data

def get_transfers_data(type):
    transfers_url = base_url + "/token-transfers"
    payload = {'page_size': per_page, 'page': 1}
    payload[type] = wallet_address
    json_page = requests.get(transfers_url, headers=headers, params=payload).json()
    check_api_valid(json_page) ## check API key is working, die if not
    num_pages = json_page["lastPage"]
    page_data = json_page["items"]
    # random strings passed as the address may lead to the entire blockchain's index being passed (currently 900,000 ish) so don't save it all
    if num_pages == 0 or num_pages > max_pages:
        return # just leave blank
    # loop through and add the rest of the items from the other pages
    for i in range(2, num_pages + 1):
        payload['page'] = i
        json_page = requests.get(transfers_url, headers=headers, params=payload).json()
        # append this page data to the first page's
        page_data = page_data + json_page["items"]
    return page_data

def process_fields(page_data, fields):
    # sanitize / tidy up - set blank entries if missing values, replace new lines
    for row in page_data:
        for field in fields:
            if field == "utcTime":
                row[field] = datetime.utcfromtimestamp(row["timestamp"])
            if field == "in_or_out":
                if row["from_address"] == wallet_address:
                    row[field] = "OUT"
                elif row["to_address"] == wallet_address:
                    row[field] = "IN"
            if convert_wei and field == "actualFee":
                row[field] = convert_wei_to_eth(row[field])
            if field == "transfer_amounts":
                if len(row[field]) > 1:
                    raise Exception("transfer_amounts has more than one value")
                row[field] = (row[field][0])
                if convert_wei:
                    if row["token_address"] == eth_contract: # only convert for the eth contract
                        row[field] = convert_wei_to_eth(row[field])
            if not field in row:
                row[field] = ""
            if isinstance(row[field], str):
                row[field] = row[field].replace("\n", "\\n")
            

# init, globals
page = 1
per_page = 100
max_pages = 1000
base_url = "https://api.voyager.online/beta"
convert_wei = True
eth_contract = "0x049d36570d4e46f48e99674bd3fcc84644ddd6b96f7c741b1562b82f9e004dc7"

#cli args input
parser=argparse.ArgumentParser(description="add numbers")
parser.add_argument("wallet_address", type=str)
parser.add_argument("download_type", type=str)
parser.add_argument("api_key", type=str)
args=parser.parse_args()
fname_address = args.wallet_address
wallet_address = args.wallet_address
if re.search("stark", wallet_address):
    wallet_address = get_stark_domain(wallet_address)
download_type=args.download_type
#process to grab date and avoid windows issues when creating files by replacing special characters included on the date
file_created_time = str(datetime.now(timezone.utc)) 
time_for_file_name = re.sub(r'[:+,.]',".",file_created_time) 
f_name = download_type + "_" + fname_address + "_" + time_for_file_name + ".csv"
api_key = args.api_key
headers = {
    'Accept': 'application/json',
    'X-Api-Key': api_key
}

if download_type != "transfers" and download_type != "transactions":
    raise Exception("download type must be 'transactions' or 'transfers'")

if download_type == 'transactions':
    page_data = get_transactions_data()
    fields = ["utcTime","status","type","blockNumber","hash","index","l1VerificationHash","classHash","contractAddress","timestamp","actualFee","actions","contractAlias","classAlias"]
else:
    # this is transfers
    to_page_data = get_transfers_data("to_address")
    from_page_data = get_transfers_data("from_address")
    page_data = to_page_data + from_page_data
    # sort by timestamp
    page_data.sort(key = lambda x: x['timestamp'])
    fields = ["utcTime", "block_number", "token_address", "transaction_hash", "event_selector", "event_index", "timestamp", "invocation_type", "token_type", "from_address", "to_address", "data_len", "transfer_amounts", "in_or_out"]

process_fields(page_data, fields)
with open(f_name, 'w') as csv_file:
    writer = csv.writer(csv_file)
    writer.writerow(fields) # header
    # Write each data row to the CSV file    
    for row in page_data:
        writer.writerow([row[field] for field in fields])