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

def get_transfers_data(type, token_type):
    transfers_url = base_url + "/event-activity"
    payload = {'page_size': per_page, 'page': page}
    payload[type] = wallet_address
    payload["sort"] = "asc" # ascending sort by default, used for ordering next page too
    payload["token_type"] = token_type
    payload["from_timestamp"] = from_beginning_timestamp
    json_page = requests.get(transfers_url, headers=headers, params=payload).json()
    check_api_valid(json_page) ## check API key is working, die if not
    has_more = json_page["hasMore"]
    page_data = json_page["items"]
    # random strings passed as the address may lead to the entire blockchain's index being passed (currently 900,000 ish) so don't save it all

    # loop through and add the rest of the items from the other pages
    while has_more:
        payload['last_id'] = page_data[-1]["id"]
        json_page = requests.get(transfers_url, headers=headers, params=payload).json()
        # append this page data to the first page's
        page_data = page_data + json_page["items"]
        has_more = json_page["hasMore"]
    return page_data

def process_fields(page_data, fields):
    # sanitize / tidy up - set blank entries if missing values, replace new lines
    for row in page_data:
        for field in fields:
            if field == "utcTime":
                row[field] = datetime.utcfromtimestamp(row["timestamp"]) ## set utc readable date
            if field == "in_or_out": # explicitly point out if it's incoming / outgoing txn from wallet
                if row["transferFrom"] == wallet_address:
                    row[field] = "OUT"
                elif row["transferTo"] == wallet_address:
                    row[field] = "IN"
            if convert_wei and field == "actualFee":
                row[field] = convert_wei_to_eth(row[field])
            if field == "transferValues": #transferValues[0] is the quantity of thing
                if len(row[field]) > 1:
                    raise Exception("transfer_amounts has more than one value")
                row[field] = (row[field][0])
                if convert_wei:
                    if row["tokenAddress"] == eth_contract: # only convert for the eth contract
                        row[field] = convert_wei_to_eth(row[field])
            if not field in row:
                row[field] = ""
            if isinstance(row[field], str): # sanitise line breaks in some inputs
                row[field] = row[field].replace("\n", "\\n")
            
def koinly_format(page_data, koinly_fields):
    koinly_array = []
    for row in page_data:
        koinly_datarow = {}
        koinly_datarow["Date"] = datetime.utcfromtimestamp(row["timestamp"]).strftime("%Y-%m-%d %H:%M UTC") # needs to be this format: 2018-01-03 14:25 UTC
        # if download_type == "ERC721": row["tokenSymbol"] = 'NFT' + row['transferIds'][0] + " " + row["tokenSymbol"] # experimenting with this for Koinly integration, but it doesn't like it
        if row["in_or_out"] == "OUT":
            koinly_datarow["Sent Amount"] = row["transferValues"]
            koinly_datarow["Sent Currency"] = row["tokenSymbol"]
            koinly_datarow["Received Amount"] = 0
            koinly_datarow["Received Currency"] = 0
        elif row["in_or_out"] == "IN":
            koinly_datarow["Received Amount"] = row["transferValues"]
            koinly_datarow["Received Currency"] = row["tokenSymbol"]
            koinly_datarow["Sent Amount"] = 0
            koinly_datarow["Sent Currency"] = 0
        fee = 0
        if "actualFee" in row: fee = row["actualFee"]
        koinly_datarow["Fee Amount"] = fee
        koinly_datarow["Fee Currency"] = "eth"
        koinly_datarow["Net Worth Amount"] = ""
        koinly_datarow["Net Worth Currency"] = ""
        koinly_datarow["Label"] = ""
        tokeninfo = row["tokenAddress"] + "(starknet nft)"
        if "tokenName" in row:
            tokeninfo = row["tokenName"] or "" + " - " + tokeninfo
        if "fromAlias" in row and "toAlias" in row:
            tokeninfo = (row["fromAlias"] or "") + (row["toAlias"] or "") + " - " + tokeninfo
        koinly_datarow["Description"] = row["callName"] + " " + tokeninfo + "(starknet nft)"
        koinly_datarow["TxHash"] = row["txHash"]
        koinly_array.append(koinly_datarow)
    return koinly_array

# "Date", "Sent Amount", "Sent Currency", "Received Amount", "Received Currency", "Fee Amount", "Fee Currency", "Net Worth Amount", "Net Worth Currency", "Label", "Description", "TxHash"

# init, globals
page = 1
per_page = 100
max_pages = 1000
base_url = "https://api.voyager.online/beta"
from_beginning_timestamp = 1633309200 # Starknet mainnet launch date, 4 Oct 2021
convert_wei = True
eth_contract = "0x049d36570d4e46f48e99674bd3fcc84644ddd6b96f7c741b1562b82f9e004dc7"

# set defaults

#cli args input
parser=argparse.ArgumentParser(description="add numbers")
parser.add_argument('-w', '--wallet', type=str, required=True)
parser.add_argument('-t', '--type', type=str, choices=['ERC20','ERC721','ERC1155','transactions'], required=True)
parser.add_argument('-a', '--api_key', type=str, required=True)
parser.add_argument('-f','--format', type = str, default="verbose", choices=['verbose', 'standard', 'koinly'], required=False)

args=parser.parse_args()
fname_address = args.wallet
wallet_address = args.wallet
if re.search("stark", wallet_address): 
    wallet_address = get_stark_domain(wallet_address)
download_type = args.type
format = args.format
# sanity check:
if format == 'koinly' and download_type == 'transactions': raise Exception("Koinly needs to import ERC20 / ERC721 / ERC1155 transfers instead of transactions")
#process to grab date and avoid windows issues when creating files by replacing special characters included on the date
file_created_time = str(datetime.now(timezone.utc)) 
time_for_file_name = re.sub(r'[:+,.]',".",file_created_time) 
f_name = download_type + "_" + fname_address + "_" + time_for_file_name + ".csv"
api_key = args.api_key
headers = {
    'Accept': 'application/json',
    'X-Api-Key': api_key
}

if download_type == 'transactions':
    page_data = get_transactions_data()
    fields = ["utcTime","status","type","blockNumber","hash","index","l1VerificationHash","classHash","contractAddress","timestamp","actualFee","actions","contractAlias","classAlias"]
else:
    # this is token transfers
    to_page_data = get_transfers_data("to_address", download_type)
    from_page_data = get_transfers_data("from_address", download_type)
    page_data = to_page_data + from_page_data
    # sort by timestamp
    page_data.sort(key = lambda x: x['timestamp'])
    fields = ["utcTime", "blockNumber", "callName", "tokenSymbol", "tokenName", "tokenAddress", "txHash", "timestamp", "invocationType", "fromAlias", "toAlias", "transferFrom", "transferTo", "transferValues", "in_or_out"]
    # available: 'blockHash', 'blockNumber', 'timestamp', 'tokenAddress', 'tokenName', 'tokenSymbol', 'tokenDecimals', 'txHash', 'callName', 'invocationType', 'eventId', 'data', 'keys', 'id', 'transferFrom', 'transferTo', 'transferDataLen', 'transferValues', 'transferIds', 'selector', 'name', 'nestedName', 'nestedEventNames', 'dataDecoded', {...}, {...}], 'keyDecoded', 'fromAlias', 'toAlias', 'abiVerified'

process_fields(page_data, fields)

if len(page_data) == 0: 
    raise Exception("no results returned")
#output 
if args.format == 'koinly':
    koinly_fields = ["Date", "Sent Amount", "Sent Currency", "Received Amount", "Received Currency", "Fee Amount", "Fee Currency", "Net Worth Amount", "Net Worth Currency", "Label", "Description", "TxHash"]
    koinly_dataset = koinly_format(page_data, koinly_fields)
    with open(f_name, 'w') as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(koinly_fields) # header
        # Write each data row to the CSV file    
        for row in koinly_dataset:
            writer.writerow([row[field] for field in koinly_fields])

else:
    with open(f_name, 'w') as csv_file:
        writer = csv.writer(csv_file)
        if format == 'standard':
            writer.writerow(fields) # header
            for row in page_data:
                writer.writerow([row[field] for field in fields])
        else: # write out verbose
            verbose_fields = list(page_data[0].keys()) # redefine to use all fields
            writer.writerow(verbose_fields) # header
            for row in page_data:
                writer.writerow([row[field] for field in verbose_fields])