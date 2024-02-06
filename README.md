# Description
This is a very basic python tool to download transaction and transfer history from Voyager, written in python 3 by me. I am new to python so be nice.
To use it, you'll just need the (free!) API key. Please fill out the form here requesting one and someone from Voyager will get back to you sharpish: https://forms.gle/34RE6d4aiiv16HoW6
Voyager docs, for reference, are here: https://docs.voyager.online/#overview

## Features: 
- outputs the timestamp in UTC
- converts eth transactions / fees to eth (from wei). 
- allows a .stark domain as input
- automatically formats output csv filenames using the wallet name you provide, with the current date and time, without needing this as a param any more
- shows, for transfers, if they're going into or coming out of your account
- works with the official API!

## Caveats:
- transactions / transfers before around Nov 2022 may not be fully recorded anywhere that I can find, since emissions for events weren't working at that point (I think)
- As fees end up paid with non-eth currencies like $strk when they start being used. The explorers' APIs will likely change, so need to keep an eye on this!

# Setup
just run:
`python export-starknet.py [wallet address] [transactions / transfers] api_key`
e.g. 
`python export-starknet.py test.stark transfers xyx0123fdsjfldskj`
will generate 
transfers_paper.stark_2024-02-06 21:26:00.552429.csv