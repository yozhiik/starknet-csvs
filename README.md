# Description
This is a very basic python tool to download transaction and transfer history from Voyager, written in python 3 by me. I am new to python so be nice. 
Voyager updated the API since I wrote this earlier this year, so functionality may be limited. They also released a CSV download for ERC20s ('standard' cryptocurrencies) but that doesn't cover ERC721s (NFTs), so the latest update adds NFT transaction download support
To use it, you'll just need the (free!) API key. Please fill out the form here requesting one and someone from Voyager will get back to you sharpish: https://forms.gle/34RE6d4aiiv16HoW6  
Voyager docs, for reference, are here: https://docs.voyager.online/#overview
Outputs to an output folder

## Features: 
- outputs the timestamp in UTC
- converts eth transactions / fees to eth (from wei). 
- allows a .stark domain as input
- automatically formats output csv filenames using the wallet name you provide, with the current date and time, without needing this as a param any more
- shows, for transfers, if they're going into or coming out of your account
- works with the official API
- allows a Koinly version of the export (currently still very experimental, check it carefully)

## Caveats:
- transactions / transfers before around Nov 2022 may not be fully recorded anywhere that I can find, since emissions for events weren't working at that point (I think)
- As fees end up paid with non-eth currencies like $strk when they start being used. The explorers' APIs will likely change, so need to keep an eye on this!

# Running
- parameters:
- - `--wallet` can be either a full starknet address or a .stark domain
- - `--type` should be one of `ERC20`,`ERC721`,`ERC1155`,`transactions`. ERC721 is NFTs and not available elsewhere! transactions
- - `--api_key` needed, see link above for how to get one 
- - `--format` will default to `verbose`, but can be `standard` (a useful subset of info) or `koinly`, used to import stuff into Koinly
Run using, e.g.:
`python3 export-starknet.py --api_key=xyx0123fdsjfldskjxyx0123fdsjfldskjsfffsd --wallet=test.stark --type=ERC721 --format=koinly`
will generate  
`ERC721_test.stark_2024-11-26 11:03:18.832393-koinly.csv`