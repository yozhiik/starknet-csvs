## Description
This is a very basic python tool to download transaction and transfer history from Voyager, written in python 3 by me. This is literally my first day writing a python script so be nice. It outputs the timestamp in UTC, but I haven't messed with values in WEI for e.g. fees, since fees may later be paid in $STRK. You can use Excel or whatever to massage it into the format you like for Koinly / whatever tax software you use.

Caveat: transactions / transfers before around Nov 2022 may not be fully recorded anywhere that I can find!

### Setup
just run:
`python export-starknet.py [wallet address] [transactions / transfers] output.csv`
