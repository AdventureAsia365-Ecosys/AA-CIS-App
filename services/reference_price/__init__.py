"""services.reference_price — AA-1091 item 5: the structured reference price of the AA tours (CIS 402).

parse.py     free text (raw_tours.price_raw) -> amount, currency, basis + review reasons (pure)
store.py     scope read, status rule, staff writes (save, accept, clear, parse, import plan / apply)
workbook.py  Excel export and import reader (openpyxl, in memory)
"""
