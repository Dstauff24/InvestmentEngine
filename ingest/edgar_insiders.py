"""Form 4 insider transactions from SEC EDGAR (D39 / Pillar C).

POINT-IN-TIME HONESTY IS THE POINT OF THIS MODULE. A Form 4 becomes knowledge
when the SEC ACCEPTS it, which can be two business days after the trade. Both
timestamps are stored and every read keys on `filed_at`:

    filed_at          SEC acceptance. The moment the market could know.
    transaction_date  when the insider traded. DESCRIPTIVE ONLY.

Keying a backtest on `transaction_date` would let it see a purchase up to two
days before anyone could act on it. That is the lookahead that makes a strategy
look profitable in testing and lose money live, and it is invisible in the
results — which is why the distinction is enforced in the schema (`filed_at` is
NOT NULL) rather than left to whoever writes the next query.

NON-CRITICAL job (D7 contrast): EDGAR filings are permanent public records and
re-fetchable at any time, so a failure here costs FRESHNESS, not history. That
is the opposite of the chain and estimate snapshots, which cannot be bought
back and are therefore critical.
"""

from __future__ import annotations

import argparse
import logging
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import clock, config, db
from core.budget import BudgetExhausted
from core.jobs import job_run
from ingest.edgar_client import (
    EdgarBlockedError,
    EdgarClient,
    EdgarError,
    raw_xml_document,
)
from ingest.runner import for_each_symbol

log = logging.getLogger(__name__)

JOB_NAME = "edgar_insiders"
FORM_TYPES = ("4", "4/A")


# ── parsing (pure) ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Filing:
    """One Form 4 filing's identity and, crucially, its acceptance time."""

    accession: str
    form: str
    filed_at: datetime
    document: str


def parse_submissions(payload: dict, *, since: date, forms=FORM_TYPES) -> list[Filing]:
    """Form 4 filings from a submissions document, newest first.

    `acceptanceDateTime` is used for `filed_at`, NOT `filingDate`. The
    acceptance timestamp is when the document became public; `filingDate` is a
    date and would collapse a 16:05 acceptance and an 09:00 one onto the same
    instant, which matters because a filing accepted after the close is not
    tradeable until the next session.
    """
    recent = (payload.get("filings") or {}).get("recent") or {}
    accessions = recent.get("accessionNumber") or []
    form_types = recent.get("form") or []
    accepted = recent.get("acceptanceDateTime") or []
    documents = recent.get("primaryDocument") or []
    filing_dates = recent.get("filingDate") or []

    out: list[Filing] = []
    for i, accession in enumerate(accessions):
        form = _at(form_types, i)
        if form not in forms:
            continue
        stamp = _parse_accepted(_at(accepted, i), _at(filing_dates, i))
        if stamp is None or stamp.date() < since:
            continue
        out.append(
            Filing(
                accession=str(accession),
                form=str(form),
                filed_at=stamp,
                document=str(_at(documents, i) or ""),
            )
        )
    return out


def parse_form4(xml_text: str, *, accession: str, filed_at: datetime) -> list[dict]:
    """Rows for `insider_transactions` from one Form 4 XML document.

    Returns BOTH non-derivative and derivative transactions, and every
    transaction code — unfiltered on purpose. The cluster rule lives in config
    (D25), so narrowing here would mean a re-fetch every time the rule changed,
    and EDGAR is a public service we have asked for restraint around.
    """
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError as exc:
        raise EdgarError(f"{accession}: Form 4 XML did not parse: {exc}") from exc

    issuer_cik = _text(root, "./issuer/issuerCik")
    symbol = (_text(root, "./issuer/issuerTradingSymbol") or "").strip().upper()

    # `or` is WRONG on an Element: one with no children is falsy, so
    # `find(...) or root` silently falls back to the document root whenever
    # reportingOwner exists but is childless — and then every identity field
    # reads as None on a filing that had them.
    owner = root.find("./reportingOwner")
    if owner is None:
        owner = root
    insider_cik = _text(owner, "./reportingOwnerId/rptOwnerCik")
    insider_name = _text(owner, "./reportingOwnerId/rptOwnerName")
    rel = owner.find("./reportingOwnerRelationship")
    is_director = _flag(rel, "isDirector")
    is_officer = _flag(rel, "isOfficer")
    is_ten_pct = _flag(rel, "isTenPercentOwner")
    title = _text(rel, "./officerTitle") if rel is not None else None

    footnotes = xml_text.lower()
    # The 10b5-1 checkbox is a recent addition and older filings note the plan
    # only in free-text footnotes, so both are checked. A false negative here
    # weakens a discount on SALES and never fabricates a purchase.
    is_10b5_1 = (
        _flag(root, "aff10b5One") is True or "10b5-1" in footnotes or "rule 10b5-1" in footnotes
    )

    rows: list[dict] = []
    index = 0
    for derivative, path in (
        (False, "./nonDerivativeTable/nonDerivativeTransaction"),
        (True, "./derivativeTable/derivativeTransaction"),
    ):
        for txn in root.findall(path):
            shares = _num(txn, "./transactionAmounts/transactionShares/value")
            price = _num(txn, "./transactionAmounts/transactionPricePerShare/value")
            rows.append(
                {
                    "accession_number": accession,
                    "txn_index": index,
                    "symbol": symbol,
                    "issuer_cik": issuer_cik,
                    "filed_at": filed_at,
                    # The ET trading day of ACCEPTANCE, for joining to daily
                    # series. Derived from filed_at, never from the transaction.
                    "filed_session": clock.session_date(filed_at),
                    "transaction_date": _date(txn, "./transactionDate/value"),
                    "insider_cik": insider_cik,
                    "insider_name": insider_name,
                    "insider_title": title,
                    "is_director": is_director,
                    "is_officer": is_officer,
                    "is_ten_percent_owner": is_ten_pct,
                    "transaction_code": _text(txn, "./transactionCoding/transactionCode"),
                    "acquired_disposed": _text(
                        txn,
                        "./transactionAmounts/transactionAcquiredDisposedCode/value",
                    ),
                    "shares": shares,
                    "price_per_share": price,
                    "value_usd": (
                        None if shares is None or price is None else round(shares * price, 2)
                    ),
                    "shares_owned_after": _num(
                        txn, "./postTransactionAmounts/sharesOwnedFollowingTransaction/value"
                    ),
                    "is_10b5_1": is_10b5_1,
                    "security_title": _text(txn, "./securityTitle/value"),
                    "is_derivative": derivative,
                }
            )
            index += 1
    return rows


# ── writing ─────────────────────────────────────────────────────────────────


UPSERT = """
    INSERT INTO insider_transactions (
        accession_number, txn_index, symbol, issuer_cik, filed_at, filed_session,
        transaction_date, insider_cik, insider_name, insider_title, is_director,
        is_officer, is_ten_percent_owner, transaction_code, acquired_disposed,
        shares, price_per_share, value_usd, shares_owned_after, is_10b5_1,
        security_title, is_derivative, source
    ) VALUES (
        %(accession_number)s, %(txn_index)s, %(symbol)s, %(issuer_cik)s, %(filed_at)s,
        %(filed_session)s, %(transaction_date)s, %(insider_cik)s, %(insider_name)s,
        %(insider_title)s, %(is_director)s, %(is_officer)s, %(is_ten_percent_owner)s,
        %(transaction_code)s, %(acquired_disposed)s, %(shares)s, %(price_per_share)s,
        %(value_usd)s, %(shares_owned_after)s, %(is_10b5_1)s, %(security_title)s,
        %(is_derivative)s, 'sec_edgar'
    )
    -- Re-fetch idempotence only. An AMENDED Form 4 arrives under a new
    -- accession number, so this never rewrites a filing's history.
    ON CONFLICT (accession_number, txn_index) DO UPDATE SET
        shares             = EXCLUDED.shares,
        price_per_share    = EXCLUDED.price_per_share,
        value_usd          = EXCLUDED.value_usd,
        shares_owned_after = EXCLUDED.shares_owned_after,
        is_10b5_1          = EXCLUDED.is_10b5_1
"""


def known_accessions(symbol: str) -> set[str]:
    """Accession numbers already stored, so a re-run fetches no documents.

    This is the whole reason a daily run is cheap: the submissions call is one
    request per symbol, and only genuinely new filings cost a second.
    """
    rows = db.fetch_all(
        "SELECT DISTINCT accession_number FROM insider_transactions WHERE symbol = %s",
        (symbol,),
    )
    return {r["accession_number"] for r in rows}


def ingest_symbol(client: EdgarClient, symbol: str, cik: str, *, since: date) -> int:
    """Fetch and store this symbol's new Form 4 filings. Returns rows written."""
    filings = parse_submissions(client.submissions(cik), since=since)
    if not filings:
        return 0

    seen = known_accessions(symbol)
    rows: list[dict] = []
    for filing in filings:
        if filing.accession in seen or not filing.document:
            continue
        try:
            # The submissions feed points at the RENDERED view; we need the data.
            document = raw_xml_document(filing.document)
            xml_text = client.filing_document(cik, filing.accession, document)
            parsed = parse_form4(xml_text, accession=filing.accession, filed_at=filing.filed_at)
        except EdgarError as exc:
            # One unparseable filing must not cost the other nineteen. Logged
            # rather than raised, and the accession stays unstored so a later
            # run retries it.
            log.warning("%s: skipping %s: %s", symbol, filing.accession, exc)
            continue
        # The issuer symbol in the XML is authoritative, but a filing fetched
        # for one ticker must not be written under another's name.
        rows.extend(r | {"symbol": symbol} for r in parsed if r["shares"] is not None)

    return db.executemany(UPSERT, rows) if rows else 0


def run(client: EdgarClient | None = None, *, symbols: list[str] | None = None) -> int:
    client = client or EdgarClient()
    cfg = config.universe().edgar
    day = clock.session_date()
    since = day - timedelta(days=cfg.lookback_days)
    universe = symbols if symbols is not None else db.active_symbols()

    if not universe:
        raise RuntimeError("no active tickers; run `python -m ingest.universe` first")

    # One request for the whole market rather than one per symbol.
    try:
        cik_by_ticker = client.company_tickers()
    except EdgarBlockedError:
        raise
    except BudgetExhausted:
        raise

    missing = [s for s in universe if s not in cik_by_ticker]
    if missing:
        log.info(
            "no EDGAR CIK for %d of %d symbols (ETFs and foreign issuers have none): %s",
            len(missing),
            len(universe),
            ", ".join(missing[:10]),
        )
    covered = [s for s in universe if s in cik_by_ticker]

    with job_run(
        JOB_NAME,
        session_date=day,
        is_critical=False,
        triggered_by="manual",
        clean_stop=BudgetExhausted,
    ) as job:
        outcome = for_each_symbol(
            covered,
            lambda s: ingest_symbol(client, s, cik_by_ticker[s], since=since),
            run=job,
            label="insiders",
            stop_on=BudgetExhausted,
        )
        job.note(
            failed_symbols=sorted(outcome.failed)[:50],
            symbols_without_cik=len(missing),
            since=str(since),
        )
    return outcome.rows


# ── internals ───────────────────────────────────────────────────────────────


def _at(values, index):
    return values[index] if index < len(values) else None


def _parse_accepted(accepted, filing_date) -> datetime | None:
    """Acceptance timestamp, falling back to the CLOSE of the filing date.

    The fallback direction matters. Assuming 00:00 would make the filing look
    knowable at the open of its filing date — manufacturing up to a full day of
    lookahead on exactly the rows where we are least sure. The close is the
    latest plausible moment inside the filing date, so a downstream rule that
    waits for the next session cannot act on information nobody had.

    Only reached when EDGAR omits `acceptanceDateTime`, which is rare and
    historical. `filed_at` is NOT NULL, so the alternative is discarding the
    row entirely.
    """
    if accepted:
        try:
            return clock.to_utc(datetime.fromisoformat(str(accepted)))
        except ValueError:
            log.warning("unparseable acceptanceDateTime %r", accepted)
    if filing_date:
        try:
            return clock.session_close_utc(date.fromisoformat(str(filing_date)))
        except ValueError:
            return None
    return None


def _text(node, path: str) -> str | None:
    if node is None:
        return None
    found = node.find(path)
    if found is None:
        found = node.find(f"{path}/value") if not path.endswith("/value") else None
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def _flag(node, name: str) -> bool | None:
    if node is None:
        return None
    found = node.find(f"./{name}")
    if found is None:
        found = node.find(f".//{name}/value")
    if found is None or found.text is None:
        return None
    return found.text.strip() in {"1", "true", "True"}


def _num(node, path: str) -> float | None:
    raw = _text(node, path)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _date(node, path: str) -> date | None:
    raw = _text(node, path)
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--symbols", nargs="*", help="override the universe (debugging)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    rows = run(symbols=args.symbols)
    print(f"insider transactions: {rows} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
