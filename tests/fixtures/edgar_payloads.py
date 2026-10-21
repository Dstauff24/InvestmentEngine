"""SEC Form 4 and submissions payloads, shaped from the real documents.

Form 4 XML is verbose and the fields that matter are easy to confuse:
`transactionCode` distinguishes a conviction purchase from a payroll event, and
`acceptanceDateTime` is the only timestamp that says when the market could
know. Both are represented exactly as EDGAR emits them.
"""

from __future__ import annotations

FORM4_PURCHASE = """<?xml version="1.0"?>
<ownershipDocument>
  <schemaVersion>X0508</schemaVersion>
  <documentType>4</documentType>
  <periodOfReport>2026-10-19</periodOfReport>
  <issuer>
    <issuerCik>0000320193</issuerCik>
    <issuerName>Apple Inc.</issuerName>
    <issuerTradingSymbol>AAPL</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0001214128</rptOwnerCik>
      <rptOwnerName>COOK TIMOTHY D</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector>
      <isOfficer>1</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
      <officerTitle>Chief Executive Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-10-19</value></transactionDate>
      <transactionCoding>
        <transactionFormType>4</transactionFormType>
        <transactionCode>P</transactionCode>
        <equitySwapInvolved>0</equitySwapInvolved>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>5000</value></transactionShares>
        <transactionPricePerShare><value>230.50</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>3280000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

# An AWARD, not a purchase. Counting this as conviction buying is the classic
# way to build an insider signal that measures a compensation schedule.
FORM4_AWARD = FORM4_PURCHASE.replace(
    "<transactionCode>P</transactionCode>", "<transactionCode>A</transactionCode>"
).replace(
    "<transactionPricePerShare><value>230.50</value></transactionPricePerShare>",
    "<transactionPricePerShare><value>0</value></transactionPricePerShare>",
)

# An option EXERCISE, likewise not an open-market purchase.
FORM4_EXERCISE = FORM4_PURCHASE.replace(
    "<transactionCode>P</transactionCode>", "<transactionCode>M</transactionCode>"
)

# A 10b5-1 SALE: scheduled months earlier, so it carries no opinion about today.
FORM4_PLANNED_SALE = (
    FORM4_PURCHASE.replace(
        "<transactionCode>P</transactionCode>", "<transactionCode>S</transactionCode>"
    )
    .replace(
        "<transactionAcquiredDisposedCode><value>A</value>",
        "<transactionAcquiredDisposedCode><value>D</value>",
    )
    .replace(
        "<documentType>4</documentType>",
        "<documentType>4</documentType>\n  <aff10b5One>1</aff10b5One>",
    )
)

# A derivative grant — excluded from clusters by config, which the loader
# refuses to let anyone turn off.
FORM4_DERIVATIVE = """<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <issuer>
    <issuerCik>0000320193</issuerCik>
    <issuerTradingSymbol>AAPL</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0001214128</rptOwnerCik>
      <rptOwnerName>COOK TIMOTHY D</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship><isOfficer>1</isOfficer></reportingOwnerRelationship>
  </reportingOwner>
  <derivativeTable>
    <derivativeTransaction>
      <securityTitle><value>Stock Option</value></securityTitle>
      <transactionDate><value>2026-10-19</value></transactionDate>
      <transactionCoding><transactionCode>A</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>100000</value></transactionShares>
        <transactionPricePerShare><value>0</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </derivativeTransaction>
  </derivativeTable>
</ownershipDocument>
"""

FORM4_TWO_TRANSACTIONS = FORM4_PURCHASE.replace(
    "</nonDerivativeTable>",
    """  <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-10-20</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>231.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
</nonDerivativeTable>""",
)

MALFORMED = "<ownershipDocument><unclosed>"


def submissions(entries: list[dict]) -> dict:
    """A submissions document in EDGAR's column-oriented `recent` shape.

    EDGAR returns PARALLEL ARRAYS rather than a list of objects, which is the
    kind of shape where an off-by-one quietly attaches the wrong timestamp to
    the wrong filing — so the fixture keeps that shape rather than tidying it.
    """
    return {
        "cik": "320193",
        "filings": {
            "recent": {
                "accessionNumber": [e["accession"] for e in entries],
                "form": [e.get("form", "4") for e in entries],
                "filingDate": [e.get("filing_date", "2026-10-21") for e in entries],
                "acceptanceDateTime": [e.get("accepted") for e in entries],
                "primaryDocument": [e.get("document", "doc.xml") for e in entries],
            }
        },
    }


COMPANY_TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
    "2": {"cik_str": 1067983, "ticker": "BRK-B", "title": "Berkshire Hathaway"},
}
