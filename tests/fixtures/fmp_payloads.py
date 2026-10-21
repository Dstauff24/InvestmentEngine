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
        "symbol": "BRK-B",
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
        "symbol": "BRK-B",
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
    "BRK-B": 3_900_000,  # in — the share class, in FMP's real DASH spelling
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


# ══════════════════════════════════════════════════════════════════════════
# Ingestion payloads (step 5)
# ══════════════════════════════════════════════════════════════════════════

# GET /stable/analyst-estimates?symbol=AAPL&period=quarter
ANALYST_ESTIMATES_QUARTER = [
    {  # already reported — must be dropped; diffing it would fake revision signal
        "date": "2026-06-30",
        "symbol": "AAPL",
        "estimatedRevenueAvg": 90_000_000_000,
        "estimatedRevenueLow": 88e9,
        "estimatedRevenueHigh": 92e9,
        "estimatedEpsAvg": 1.40,
        "estimatedEpsLow": 1.35,
        "estimatedEpsHigh": 1.45,
        "estimatedEbitdaAvg": 30e9,
        "estimatedNetIncomeAvg": 21e9,
        "numberAnalystEstimatedRevenue": 28,
        "numberAnalystsEstimatedEps": 30,
    },
    {
        "date": "2026-12-31",
        "symbol": "AAPL",
        "estimatedRevenueAvg": 128_000_000_000,
        "estimatedRevenueLow": 124e9,
        "estimatedRevenueHigh": 131e9,
        "estimatedEpsAvg": 2.35,
        "estimatedEpsLow": 2.25,
        "estimatedEpsHigh": 2.45,
        "estimatedEbitdaAvg": 45e9,
        "estimatedNetIncomeAvg": 36e9,
        "numberAnalystEstimatedRevenue": 29,
        "numberAnalystsEstimatedEps": 31,
    },
    {
        "date": "2027-03-31",
        "symbol": "AAPL",
        "estimatedRevenueAvg": 99_000_000_000,
        "estimatedRevenueLow": None,
        "estimatedRevenueHigh": None,
        "estimatedEpsAvg": 1.70,
        "estimatedEpsLow": None,
        "estimatedEpsHigh": None,
        "estimatedEbitdaAvg": None,
        "estimatedNetIncomeAvg": None,
        "numberAnalystEstimatedRevenue": 12,
        "numberAnalystsEstimatedEps": 14,
    },
    {
        "date": "2027-06-30",
        "symbol": "AAPL",
        "estimatedRevenueAvg": 95_000_000_000,
        "estimatedEpsAvg": 1.60,
        "numberAnalystEstimatedRevenue": 10,
        "numberAnalystsEstimatedEps": 11,
    },
]

# GET /stable/income-statement — field names from the live Sep 3 capture.
# Note `fiscalYear`, which /stable/ renamed from /api/v3/'s `calendarYear`.
INCOME = [
    {
        "date": "2026-06-27",
        "symbol": "AAPL",
        "fiscalYear": "2026",
        "period": "Q3",
        "reportedCurrency": "USD",
        "revenue": 109_417_000_000,
        "grossProfit": 54_760_000_000,
        "operatingIncome": 32_000_000_000,
        "netIncome": 29_789_000_000,
        "ebitda": 36_000_000_000,
        "epsDiluted": 2.03,
        "weightedAverageShsOutDil": 14_750_302_000,
        "filingDate": "2026-07-31",
        "acceptedDate": "2026-07-31 06:01:02",
    },
    {
        "date": "2026-03-28",
        "symbol": "AAPL",
        "fiscalYear": "2026",
        "period": "Q2",
        "reportedCurrency": "USD",
        "revenue": 95_000_000_000,
        "grossProfit": 45_600_000_000,
        "operatingIncome": 28_000_000_000,
        "netIncome": 24_000_000_000,
        "ebitda": 31_000_000_000,
        "epsDiluted": 1.60,
        "weightedAverageShsOutDil": 14_900_000_000,
        "filingDate": "2026-05-01",
        "acceptedDate": "2026-05-01 06:01:02",
    },
]

# The /api/v3/ spellings, to prove both are accepted.
INCOME_LEGACY = [
    {
        "date": "2026-06-27",
        "symbol": "AAPL",
        "calendarYear": "2026",
        "period": "Q3",
        "revenue": 109_417_000_000,
        "grossProfit": 54_760_000_000,
        "epsdiluted": 2.03,
        "netIncome": 29_789_000_000,
        "fillingDate": "2026-07-31",
    },
]

BALANCE = [
    {
        "date": "2026-06-27",
        "totalAssets": 383_266_000_000,
        "totalStockholdersEquity": 107_520_000_000,
        "totalDebt": 84_307_000_000,
        "cashAndCashEquivalents": 39_544_000_000,
    },
    # 2026-03-28 deliberately absent: a gap must leave columns NULL, not drop
    # the period.
]

CASHFLOW = [
    {
        "date": "2026-06-27",
        "operatingCashFlow": 34_369_000_000,
        "capitalExpenditure": -2_455_000_000,
        "freeCashFlow": 31_914_000_000,
    },
    {
        "date": "2026-03-28",
        "operatingCashFlow": 30_000_000_000,
        "capitalExpenditure": -2_400_000_000,
        "freeCashFlow": 27_600_000_000,
    },
]

RATIOS = [
    {
        "date": "2026-06-27",
        "returnOnEquity": 1.42,
        "currentRatio": 0.94,
        "debtEquityRatio": 0.78,
        "priceEarningsRatio": 33.2,
        "priceToSalesRatio": 8.7,
        "priceToBookRatio": 47.1,
        "priceEarningsToGrowthRatio": 2.4,
        "payoutRatio": 0.16,
    },
]

KEY_METRICS = [
    {
        "date": "2026-06-27",
        "roic": 0.55,
        "enterpriseValueOverEBITDA": 24.8,
        "dividendPerShare": 1.00,
    },
]

# GET /stable/earnings-calendar
EARNINGS_CALENDAR = [
    {
        "date": "2026-10-15",
        "symbol": "AAPL",
        "eps": 2.03,
        "epsEstimated": 1.98,
        "time": "amc",
        "revenue": 109_417_000_000,
        "revenueEstimated": 108_000_000_000,
    },
    {
        "date": "2026-10-29",
        "symbol": "AAPL",
        "eps": None,
        "epsEstimated": 2.35,
        "time": "amc",
        "revenue": None,
        "revenueEstimated": 128_000_000_000,
    },
    {
        "date": "2026-10-22",
        "symbol": "MSFT",
        "eps": None,
        "epsEstimated": 3.35,
        "time": "amc",
        "revenue": None,
        "revenueEstimated": 70_000_000_000,
    },
    {
        "date": "2026-10-30",
        "symbol": "NOTINUNIVERSE",
        "eps": None,
        "epsEstimated": 0.5,
        "time": "bmo",
        "revenue": None,
        "revenueEstimated": 1e9,
    },
    {
        "date": "2026-11-05",
        "symbol": "AAPL",
        "eps": None,
        "epsEstimated": None,
        "time": "--",
        "revenue": None,
        "revenueEstimated": None,
    },
]

# GET /stable/dividends-calendar
DIVIDEND_CALENDAR = [
    {
        "date": "2026-11-06",
        "symbol": "AAPL",
        "dividend": 0.26,
        "adjDividend": 0.26,
        "declarationDate": "2026-10-15",
        "recordDate": "2026-11-09",
        "paymentDate": "2026-11-13",
    },
    {
        "date": "2026-11-12",
        "symbol": "MSFT",
        "dividend": 0.83,
        "adjDividend": 0.83,
        "declarationDate": "2026-09-10",
        "recordDate": "2026-11-13",
        "paymentDate": "2026-12-11",
    },
    {"date": "2026-11-15", "symbol": "NOTINUNIVERSE", "dividend": 1.00, "adjDividend": 1.00},
    {"date": "2026-11-20", "symbol": "AAPL", "dividend": None, "adjDividend": None},
]

# GET /stable/splits-calendar
SPLIT_CALENDAR = [
    {"date": "2026-11-18", "symbol": "AAPL", "numerator": 4, "denominator": 1},
    {"date": "2026-12-02", "symbol": "MSFT", "numerator": 1, "denominator": 10},
    {"date": "2026-12-09", "symbol": "AAPL", "numerator": 0, "denominator": 1},
]

# Historical dividends for one symbol, showing a CUT (D19(5)).
HISTORICAL_DIVIDENDS = [
    {"symbol": "AAPL", "date": "2026-02-06", "dividend": 0.24},
    {"symbol": "AAPL", "date": "2026-05-07", "dividend": 0.26},
    {"symbol": "AAPL", "date": "2026-08-06", "dividend": 0.26},
    {"symbol": "AAPL", "date": "2026-11-06", "dividend": 0.10},  # the cut
]

# GET /company-news?symbol=AAPL  (Finnhub)
FINNHUB_NEWS = [
    {
        "category": "company",
        "datetime": 1_792_000_000,
        "headline": "Apple beats on iPhone demand",
        "id": 7_411_001,
        "image": "https://img.example/1.png",
        "related": "AAPL",
        "source": "Reuters",
        "summary": "Revenue topped estimates.",
        "url": "https://ex.com/1",
    },
    {
        "category": "company",
        "datetime": 1_792_086_400,
        "headline": "Analysts raise Apple targets",
        "id": 7_411_002,
        "image": "",
        "related": "AAPL",
        "source": "Bloomberg",
        "summary": "",
        "url": "https://ex.com/2",
    },
    # duplicate id inside one window — Finnhub does this
    {
        "category": "company",
        "datetime": 1_792_086_400,
        "headline": "Analysts raise Apple targets",
        "id": 7_411_002,
        "related": "AAPL",
        "source": "Bloomberg",
        "url": "https://ex.com/2",
    },
    {"category": "company", "datetime": None, "headline": "No timestamp", "id": 7_411_003},
    {"category": "company", "datetime": 1_792_100_000, "headline": "No id"},
]
