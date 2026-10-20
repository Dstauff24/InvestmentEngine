"""Trimmed FMP `/stable/` responses. Field names copied from the live payloads.

Small on purpose: enough symbols to exercise index overlap, dotted share
classes, and both sides of the liquidity boundary, without pretending to be the
real 500.
"""

# GET /stable/sp500-constituent
SP500 = [
    {
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "sector": "Technology",
        "subSector": "Consumer Electronics",
    },
    {
        "symbol": "MSFT",
        "name": "Microsoft Corporation",
        "sector": "Technology",
        "subSector": "Software",
    },
    {
        "symbol": "JNJ",
        "name": "Johnson & Johnson",
        "sector": "Healthcare",
        "subSector": "Drug Manufacturers",
    },
    {
        "symbol": "BRK.B",
        "name": "Berkshire Hathaway Inc.",
        "sector": "Financial Services",
        "subSector": "Insurance",
    },
    {
        "symbol": "NWSA",
        "name": "News Corporation",
        "sector": "Communication Services",
        "subSector": "Publishing",
    },
]

# GET /stable/nasdaq-constituent — AAPL and MSFT overlap with the S&P 500.
NASDAQ100 = [
    {
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "sector": "Technology",
        "subSector": "Consumer Electronics",
    },
    {
        "symbol": "MSFT",
        "name": "Microsoft Corporation",
        "sector": "Technology",
        "subSector": "Software",
    },
    {
        "symbol": "ZS",
        "name": "Zscaler, Inc.",
        "sector": "Technology",
        "subSector": "Software - Infrastructure",
    },
]

# GET /stable/quote?symbol=AAPL,MSFT,...
QUOTES = [
    {
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "price": 232.5,
        "exchange": "NASDAQ",
        "marketCap": 3_500_000_000_000,
    },
    {
        "symbol": "MSFT",
        "name": "Microsoft Corporation",
        "price": 415.0,
        "exchange": "NASDAQ",
        "marketCap": 3_100_000_000_000,
    },
    {
        "symbol": "JNJ",
        "name": "Johnson & Johnson",
        "price": 158.2,
        "exchange": "NYSE",
        "marketCap": 380_000_000_000,
    },
    {
        "symbol": "BRK.B",
        "name": "Berkshire Hathaway Inc.",
        "price": 465.0,
        "exchange": "NYSE",
        "marketCap": 1_000_000_000_000,
    },
    {
        "symbol": "NWSA",
        "name": "News Corporation",
        "price": 28.4,
        "exchange": "NASDAQ",
        "marketCap": 16_000_000_000,
    },
    {
        "symbol": "ZS",
        "name": "Zscaler, Inc.",
        "price": 195.0,
        "exchange": "NASDAQ",
        "marketCap": 30_000_000_000,
    },
]


def daily_bars(volume: int, *, days: int = 20) -> list[dict]:
    """A /stable/historical-price-eod/full array, newest first."""
    return [
        {"date": f"2026-10-{day:02d}", "close": 100.0, "volume": volume}
        for day in range(days, 0, -1)
    ]


# Average daily volume by symbol, chosen to sit either side of the 1M D14 gate.
VOLUMES = {
    "AAPL": 52_000_000,  # comfortably in
    "MSFT": 21_000_000,  # in
    "JNJ": 7_400_000,  # in
    "BRK.B": 3_900_000,  # in — also exercises the dotted share class
    "NWSA": 900_000,  # OUT: below 1M
    "ZS": 1_400_000,  # in
}

# A valid constituents_static.txt, matching the shipped file's shape.
STATIC_FILE = """\
# config/constituents_static.txt
# Static constituent fallback (D29).
# AS_OF: 2026-10-20
# FORMAT: SYMBOL,INDEX
#
AAPL,sp500
MSFT,sp500
JNJ,sp500
BRK.B,sp500
NWSA,sp500
AAPL,ndx
MSFT,ndx
ZS,ndx
"""

STATIC_FILE_NO_HEADER = """\
# no AS_OF line at all
AAPL,sp500
ZS,ndx
"""

STATIC_FILE_BAD_HEADER = """\
# AS_OF: sometime last spring
AAPL,sp500
"""

STATIC_FILE_EMPTY = """\
# AS_OF: 2026-10-20
# SYMBOL,INDEX
"""
