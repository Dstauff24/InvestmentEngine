"""Pillar D inputs: news tone CHANGE and volume spike (D32 weight 10).

Nothing here scores anything. Two readings, and the framing is the point:

**SENTIMENT MEASURES CHANGE, NOT LEVEL.** A stock whose coverage is
permanently gloomy is not a sell signal every day — it is a stock with gloomy
coverage. A stock whose coverage just turned is information. So the readings
are a tone DELTA (trailing window against a longer baseline) and a news-VOLUME
spike measured against the symbol's own normal flow. Absolute tone feeds both;
it is not a signal by itself, and D32 demoted this pillar to weight 10 partly
because free-tier news invites exactly that mistake.

Per-symbol normalisation matters as much as the delta. A biotech with two
articles a week and a mega-cap with forty have different "normal", so a raw
article count would make the mega-cap look permanently newsworthy and the
biotech never.

**THE SCORER IS SWAPPABLE AND ITS IDENTITY IS RECORDED.** A lexicon ships in
Phase 1 and a Claude API scorer arrives in Phase 2. They will disagree —
differently calibrated, differently wrong — so every reading carries
`scorer_id` ("name/version"), and `news_articles.sentiment_scorer` persists it.
Averaging readings from two instruments measures neither, and D39's
calibration curve would be computed on a series that silently changed
definition halfway through. Versioned because a reworded lexicon is a different
instrument under the same name.

STANDING CONVENTION: insufficient data returns `None`. A symbol with three
articles in the window abstains rather than reporting a confident delta from
nothing — three articles can disagree wildly and average to anything.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Protocol, runtime_checkable

from core import clock, config, db
from features.evidence import EMPTY, EvidenceDepth, depth_from_dates

log = logging.getLogger(__name__)

__all__ = [
    "LexiconScorer",
    "SentimentFeatures",
    "ToneScorer",
    "ToneWindow",
    "compute",
    "dedupe_by_article",
    "get_scorer",
    "load_articles",
    "news_coverage_start",
    "tone_delta",
    "volume_spike",
]


# ── the scorer interface ────────────────────────────────────────────────────


@runtime_checkable
class ToneScorer(Protocol):
    """One text in, one tone out. The whole surface Phase 2 has to satisfy.

    `score` returns -1.0 (maximally negative) to +1.0, or None when the text
    carries no usable signal — an empty headline, or one with no scoreable
    content. None is NOT zero: "no opinion" and "neutral" are different, and
    averaging a pile of zeros from unscoreable text would drag every delta
    toward nothing.
    """

    name: str
    version: str

    def score(self, text: str) -> float | None: ...


def scorer_id(scorer: ToneScorer) -> str:
    """ "name/version" — what gets persisted beside every reading."""
    return f"{scorer.name}/{scorer.version}"


# ── the Phase 1 lexicon ─────────────────────────────────────────────────────

# Deliberately small and finance-specific. A general-purpose sentiment lexicon
# scores "volatile" and "aggressive" as negative, which in market copy they are
# not, and it misses the words that matter ("guidance", "downgrade", "beat").
#
# Weights are coarse on purpose: a lexicon cannot distinguish "raised guidance"
# from "raised concerns about guidance", and pretending to three decimal places
# of precision would invite trusting it more than it deserves. Phase 2's job.
POSITIVE = {
    "beat": 1.0,
    "beats": 1.0,
    "exceeded": 1.0,
    "surpassed": 1.0,
    "upgrade": 1.0,
    "upgraded": 1.0,
    "outperform": 0.8,
    "raised": 0.8,
    "raises": 0.8,
    "boost": 0.6,
    "boosted": 0.6,
    "strong": 0.6,
    "stronger": 0.6,
    "growth": 0.5,
    "profit": 0.5,
    "record": 0.6,
    "surge": 0.8,
    "surged": 0.8,
    "rally": 0.6,
    "rallied": 0.6,
    "approval": 0.7,
    "approved": 0.7,
    "wins": 0.6,
    "won": 0.5,
    "expansion": 0.5,
    "buyback": 0.6,
    "dividend": 0.3,
    "beating": 0.8,
    "optimistic": 0.6,
    "bullish": 0.8,
    "momentum": 0.4,
    "accelerating": 0.6,
}

NEGATIVE = {
    "miss": 1.0,
    "missed": 1.0,
    "misses": 1.0,
    "downgrade": 1.0,
    "downgraded": 1.0,
    "underperform": 0.8,
    "cut": 0.8,
    "cuts": 0.8,
    "slashed": 1.0,
    "weak": 0.6,
    "weaker": 0.6,
    "decline": 0.6,
    "declined": 0.6,
    "loss": 0.7,
    "losses": 0.7,
    "plunge": 0.9,
    "plunged": 0.9,
    "slump": 0.8,
    "tumble": 0.8,
    "tumbled": 0.8,
    "lawsuit": 0.7,
    "probe": 0.7,
    "investigation": 0.7,
    "recall": 0.8,
    "warning": 0.7,
    "warns": 0.7,
    "layoffs": 0.7,
    "bankruptcy": 1.0,
    "delay": 0.5,
    "delayed": 0.5,
    "halted": 0.8,
    "bearish": 0.8,
    "concerns": 0.5,
    "disappointing": 0.8,
    "shortfall": 0.8,
    "probes": 0.7,
}

# A negation flips the polarity of the next few words. Crude, and the reason
# the lexicon is a Phase 1 placeholder rather than a destination — but without
# it "did not beat expectations" scores positive, which is worse than crude.
NEGATORS = {
    "not",
    "no",
    "never",
    "without",
    "fails",
    "failed",
    "fail",
    "cannot",
    "isn't",
    "doesn't",
    "didn't",
    "won't",
}
NEGATION_SCOPE = 3

_WORD = re.compile(r"[a-z']+")


class LexiconScorer:
    """Phase 1 tone scorer: a finance word list with negation handling.

    Honest about what it is. It cannot read "raised concerns about guidance" as
    anything but positive-then-negative, it has no idea which company a clause
    refers to in an article tagged to four symbols, and it scores a headline
    about a competitor's trouble as that competitor's news. Phase 2 replaces it
    with the Claude API, and `scorer_id` is what keeps the two series apart.
    """

    name = "lexicon"
    version = "1"

    def score(self, text: str) -> float | None:
        if not text:
            return None
        words = _WORD.findall(text.lower())
        if not words:
            return None

        total = 0.0
        hits = 0
        for i, word in enumerate(words):
            weight = POSITIVE.get(word, 0.0) - NEGATIVE.get(word, 0.0)
            if weight == 0.0:
                continue
            window = words[max(0, i - NEGATION_SCOPE) : i]
            if any(w in NEGATORS for w in window):
                weight = -weight
            total += weight
            hits += 1

        if hits == 0:
            # No scoreable words. NOT zero — "no opinion" and "neutral" are
            # different, and a pile of zeros from unscoreable text would drag
            # every delta toward nothing.
            return None
        # Mean rather than sum, so a long article is not automatically louder
        # than a headline, then clamped to the stated range.
        return max(-1.0, min(1.0, total / hits))


_SCORERS: dict[str, ToneScorer] = {"lexicon": LexiconScorer()}


def get_scorer(name: str | None = None) -> ToneScorer:
    """The configured scorer. Raises on an unknown name rather than defaulting.

    Defaulting would silently score a whole run with the wrong instrument and
    stamp it with the wrong `scorer_id`, which is the one failure this design
    exists to prevent.
    """
    key = name or config.scoring().sentiment.scorer
    try:
        return _SCORERS[key]
    except KeyError:
        raise config.ConfigError(
            f"unknown sentiment scorer {key!r}; known: {sorted(_SCORERS)}. The "
            f"Claude API scorer arrives in Phase 2 (D39)."
        ) from None


# ── dedupe ──────────────────────────────────────────────────────────────────


def dedupe_by_article(articles: list[dict]) -> list[dict]:
    """One row per article id, newest kept.

    WITHIN a symbol. An article tagged to four symbols legitimately counts once
    for each of them — it really is news about each — and `news_articles` is
    keyed `(source_provider, article_id, symbol)` so the storage already says
    so. What this removes is the same article id appearing twice for the SAME
    symbol, which a re-ingest or a provider resend can produce and which would
    otherwise let one story vote twice in a 5-article window.
    """
    seen: dict[str, dict] = {}
    for article in sorted(articles, key=lambda a: _as_dt(a["published_at"])):
        seen[str(article.get("article_id"))] = article
    return sorted(seen.values(), key=lambda a: _as_dt(a["published_at"]))


# ── tone change ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ToneWindow:
    """Mean tone over one window, and how much it rested on."""

    mean: float | None
    articles: int
    scored: int
    """Articles the scorer could actually read — `None` results are excluded."""


@dataclass(frozen=True)
class SentimentFeatures:
    """Everything Pillar D reads for one symbol."""

    symbol: str
    as_of: date
    scorer_id: str
    """"name/version". NEVER average readings across two of these."""

    tone_delta: float | None
    """Trailing mean minus baseline mean. THE tone reading (D32: change)."""
    trailing: ToneWindow
    baseline: ToneWindow

    volume_spike: float | None
    """Trailing articles/day over baseline articles/day. 1.0 is normal flow."""
    trailing_per_day: float | None
    baseline_per_day: float | None

    depth: EvidenceDepth
    """Distinct publication days in the baseline window — our own series."""

    def as_dict(self) -> dict:
        out = asdict(self)
        out["trailing"] = asdict(self.trailing)
        out["baseline"] = asdict(self.baseline)
        out["depth"] = asdict(self.depth)
        return out


def _window_tone(articles: list[dict], scorer: ToneScorer) -> ToneWindow:
    scores = []
    for article in articles:
        text = " ".join(part for part in (article.get("headline"), article.get("summary")) if part)
        value = scorer.score(text)
        if value is not None:
            scores.append(value)
    return ToneWindow(
        mean=(sum(scores) / len(scores)) if scores else None,
        articles=len(articles),
        scored=len(scores),
    )


def tone_delta(
    articles: list[dict],
    *,
    as_of: date,
    scorer: ToneScorer,
    trailing_days: int,
    baseline_days: int,
    min_articles: int,
) -> tuple[float | None, ToneWindow, ToneWindow]:
    """Trailing mean tone minus baseline mean tone.

    The baseline window EXCLUDES the trailing window. Overlapping them would
    dilute the change being measured — the same reason relative volume excludes
    today from its own baseline — and with a 7-day trailing window inside a
    30-day baseline the dilution is about a quarter of the signal.

    Returns (delta, trailing, baseline). The delta is None unless BOTH windows
    cleared `min_articles`: a confident-looking number computed against three
    baseline articles is worse than an abstention, because nothing downstream
    can tell it was thin.
    """
    trailing_start = as_of - timedelta(days=trailing_days)
    baseline_start = as_of - timedelta(days=baseline_days)

    trailing_rows = [a for a in articles if trailing_start < _day(a) <= as_of]
    baseline_rows = [a for a in articles if baseline_start < _day(a) <= trailing_start]

    trailing = _window_tone(trailing_rows, scorer)
    baseline = _window_tone(baseline_rows, scorer)

    if (
        trailing.scored < min_articles
        or baseline.scored < min_articles
        or trailing.mean is None
        or baseline.mean is None
    ):
        return None, trailing, baseline
    return trailing.mean - baseline.mean, trailing, baseline


# ── volume spike ────────────────────────────────────────────────────────────


def volume_spike(
    articles: list[dict],
    *,
    as_of: date,
    trailing_days: int,
    baseline_days: int,
    min_articles: int,
    coverage_start: date | None = None,
) -> tuple[float | None, float | None, float | None]:
    """Trailing articles/day over baseline articles/day. 1.0 is normal flow.

    PER DAY on both sides, because the windows are different lengths —
    comparing raw counts would report a spike of 7/30 on a normal symbol.

    Normalised against the SYMBOL'S OWN flow, not the market's: a mega-cap with
    forty articles a week and a biotech with two have different normal, so an
    absolute threshold would fire constantly on one and never on the other.

    `coverage_start` IS NOT OPTIONAL IN SPIRIT. The baseline rate divides by
    the days in the baseline window, and a day with no article means one of two
    very different things: there was no news, or THE ENGINE WAS NOT COLLECTING
    YET. Dividing by nominal days treats the second as the first, which
    deflates the denominator and inflates the ratio.

    Found on real data 2026-10-21, eight days into the news series: JNJ
    reported a 7.4x "spike" purely because its 23-day baseline window divided
    30 articles by 23 days when the series only reached back two of them. A
    7x reading that means "we started collecting last week" is worse than no
    reading, because nothing downstream can tell the difference.

    So the baseline is measured over the window ACTUALLY COVERED, and when that
    coverage is shorter than the trailing window the function abstains — a
    7-day rate cannot be judged against a 2-day notion of normal.

    Returns (spike, trailing_per_day, baseline_per_day).
    """
    trailing_start = as_of - timedelta(days=trailing_days)
    baseline_start = as_of - timedelta(days=baseline_days)
    # Never earlier than the day the engine began collecting.
    effective_start = max(baseline_start, coverage_start) if coverage_start else baseline_start

    trailing_n = sum(1 for a in articles if trailing_start < _day(a) <= as_of)
    baseline_n = sum(1 for a in articles if effective_start < _day(a) <= trailing_start)
    covered_days = (trailing_start - effective_start).days

    trailing_rate = trailing_n / max(1, trailing_days)
    if covered_days <= 0:
        # The baseline window lies entirely before our coverage: there is no
        # "normal" yet, only a first impression.
        return None, trailing_rate, None

    baseline_rate = baseline_n / covered_days

    if covered_days < trailing_days:
        # Some coverage, but not enough to be a baseline. Reported as a rate so
        # the caller can see what little we have, without a ratio built on it.
        return None, trailing_rate, baseline_rate
    if baseline_n < min_articles or baseline_rate == 0:
        return None, trailing_rate, baseline_rate if baseline_n else None
    return trailing_rate / baseline_rate, trailing_rate, baseline_rate


# ── the bundle ──────────────────────────────────────────────────────────────


def compute(
    symbol: str,
    articles: list[dict],
    *,
    as_of: date,
    scorer: ToneScorer | None = None,
    cfg: config.SentimentScope | None = None,
    coverage_start: date | None = None,
) -> SentimentFeatures:
    """Measure one symbol. Pure: no DB, no network.

    `coverage_start` is the day the engine began collecting news. Pass it (see
    `news_coverage_start`) or the volume spike will divide by days nobody was
    watching and report a burst that is really a short series.
    """
    scope = cfg or config.scoring().sentiment
    engine = scorer or get_scorer(scope.scorer)
    rows = dedupe_by_article(articles)

    delta, trailing, baseline = tone_delta(
        rows,
        as_of=as_of,
        scorer=engine,
        trailing_days=scope.trailing_days,
        baseline_days=scope.baseline_days,
        min_articles=scope.min_articles,
    )
    spike, trailing_rate, baseline_rate = volume_spike(
        rows,
        as_of=as_of,
        trailing_days=scope.trailing_days,
        baseline_days=scope.baseline_days,
        min_articles=scope.min_articles,
        coverage_start=coverage_start,
    )

    window_start = as_of - timedelta(days=scope.baseline_days)
    return SentimentFeatures(
        symbol=symbol,
        as_of=as_of,
        scorer_id=scorer_id(engine),
        tone_delta=delta,
        trailing=trailing,
        baseline=baseline,
        volume_spike=spike,
        trailing_per_day=trailing_rate,
        baseline_per_day=baseline_rate,
        depth=depth_from_dates([_day(a) for a in rows if window_start < _day(a) <= as_of])
        if rows
        else EMPTY,
    )


# ── loading (the only part that touches the database) ───────────────────────


ARTICLES_SQL = """
    SELECT article_id, source_provider, symbol, published_at, headline, summary,
           publisher, sentiment_score, sentiment_scorer
    FROM   news_articles
    WHERE  symbol = %s AND published_at > %s AND published_at <= %s
    ORDER  BY published_at
"""


def load_articles(symbol: str, *, as_of: date, days: int | None = None) -> list[dict]:
    """Articles published in the baseline window, oldest first.

    Bounded on `published_at` rather than `ingested_at`. An article is news
    when it is published; the ingest timestamp says when the engine noticed,
    which is an artefact of cron timing rather than of the market. (Unlike a
    Form 4, where ACCEPTANCE is the knowledge moment — a filing is not public
    until the SEC accepts it, while a news article is public the moment it is
    published.)
    """
    window = days or config.scoring().sentiment.baseline_days
    return db.fetch_all(
        ARTICLES_SQL,
        (
            symbol,
            clock.session_close_utc(as_of - timedelta(days=window)),
            clock.session_close_utc(as_of),
        ),
    )


COVERAGE_SQL = "SELECT min(published_at) AS started FROM news_articles"


def news_coverage_start() -> date | None:
    """The day the engine's news series begins. Global, not per symbol.

    Deliberately NOT per symbol: a symbol's own earliest article is when it
    first made news, which is a fact about the symbol. When the ENGINE started
    watching is a fact about us, and it is the one that tells a gap in the data
    apart from a quiet week.
    """
    row = db.fetch_one(COVERAGE_SQL)
    started = row and row["started"]
    return clock.session_date(clock.to_utc(started)) if started else None


# ── internals ───────────────────────────────────────────────────────────────


def _as_dt(value: object) -> datetime:
    if isinstance(value, datetime):
        return clock.to_utc(value)
    return clock.to_utc(datetime.fromisoformat(str(value)))


def _day(article: dict) -> date:
    """The ET session date an article belongs to."""
    return clock.session_date(_as_dt(article["published_at"]))
