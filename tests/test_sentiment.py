"""Pillar D measurements (D32 weight 10). Fixtures only — no DB, no network.

The framing is what these pin: sentiment measures CHANGE, the scorer is
swappable and its identity travels with every reading, and a thin window
abstains instead of reporting a confident delta from nothing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from core import config
from features import sentiment as sent
from features.sentiment import LexiconScorer, ToneScorer, scorer_id

AS_OF = date(2026, 10, 21)


def article(day: date, headline: str, *, article_id: str | None = None, summary: str = "") -> dict:
    return {
        "article_id": article_id or f"{day}-{headline[:12]}",
        "source_provider": "finnhub",
        "symbol": "TEST",
        "published_at": datetime(day.year, day.month, day.day, 14, tzinfo=UTC),
        "headline": headline,
        "summary": summary,
    }


def scope(**overrides) -> config.SentimentScope:
    base = {"trailing_days": 7, "baseline_days": 30, "min_articles": 5, "scorer": "lexicon"}
    base.update(overrides)
    return config.SentimentScope(**base)


def spread(headlines: list[str], *, start_offset: int, days: int) -> list[dict]:
    """Place headlines across a window ending `start_offset` days before AS_OF."""
    out = []
    for i, headline in enumerate(headlines):
        day = AS_OF - timedelta(days=start_offset + (i % days))
        out.append(article(day, headline, article_id=f"{start_offset}-{i}"))
    return out


POS = [
    "Beats estimates",
    "Raises guidance",
    "Upgraded to outperform",
    "Record profit",
    "Strong growth",
]
NEG = [
    "Misses estimates",
    "Downgraded by analysts",
    "Weak demand warning",
    "Profit decline",
    "Lawsuit filed",
]


# ══════════════════════════════════════════════════════════════════════════
# The scorer interface
# ══════════════════════════════════════════════════════════════════════════


def test_the_lexicon_satisfies_the_scorer_protocol():
    """Phase 2's Claude scorer has to satisfy exactly this and nothing more."""
    assert isinstance(LexiconScorer(), ToneScorer)


def test_the_scorer_id_is_name_and_version():
    """Versioned because a REWORDED lexicon is a different instrument under the
    same name, and D39 calibration on a mixed series measures neither."""
    assert scorer_id(LexiconScorer()) == "lexicon/1"


def test_every_reading_carries_the_scorer_that_produced_it():
    features = sent.compute("TEST", spread(POS, start_offset=1, days=5), as_of=AS_OF, cfg=scope())
    assert features.scorer_id == "lexicon/1"


def test_an_unknown_scorer_raises_rather_than_defaulting():
    """Defaulting would score a whole run with the wrong instrument AND stamp
    it with the wrong id — the one failure this design exists to prevent."""
    with pytest.raises(config.ConfigError, match="unknown sentiment scorer"):
        sent.get_scorer("claude-opus-5")


def test_a_substitute_scorer_changes_the_recorded_id():
    """The swap Phase 2 performs, in miniature."""

    class Stub:
        name = "stub"
        version = "7"

        def score(self, text):
            return 0.5

    features = sent.compute(
        "TEST", spread(POS, start_offset=1, days=5), as_of=AS_OF, scorer=Stub(), cfg=scope()
    )
    assert features.scorer_id == "stub/7"


# ── the lexicon itself ──────────────────────────────────────────────────────


def test_positive_and_negative_headlines_score_in_the_right_direction():
    s = LexiconScorer()
    assert s.score("Apple beats earnings, raises guidance") > 0
    assert s.score("Apple misses estimates, downgraded by analysts") < 0


def test_negation_flips_the_polarity():
    """Without this, "did not beat expectations" scores POSITIVE, which is
    worse than crude."""
    s = LexiconScorer()
    assert s.score("did not beat expectations") < 0
    assert s.score("beat expectations") > 0


def test_unscoreable_text_is_none_not_zero():
    """ "No opinion" and "neutral" are different. A pile of zeros from
    unscoreable text would drag every delta toward nothing."""
    s = LexiconScorer()
    assert s.score("Apple announces new store in Ohio") is None
    assert s.score("") is None
    assert s.score("   ") is None


def test_a_long_article_is_not_automatically_louder_than_a_headline():
    """Mean, not sum — otherwise article length becomes a tone signal."""
    s = LexiconScorer()
    short = s.score("beat")
    padded = s.score("beat " + "the company said " * 20)
    assert short == pytest.approx(padded)


def test_scores_stay_inside_the_stated_range():
    s = LexiconScorer()
    assert -1.0 <= s.score("beat beat beat upgrade surge record") <= 1.0
    assert -1.0 <= s.score("miss miss downgrade bankruptcy plunge") <= 1.0


# ══════════════════════════════════════════════════════════════════════════
# CHANGE, not level
# ══════════════════════════════════════════════════════════════════════════


def test_tone_improving_gives_a_positive_delta():
    articles = spread(NEG, start_offset=10, days=20) + spread(POS, start_offset=1, days=5)
    delta, trailing, baseline = sent.tone_delta(
        articles,
        as_of=AS_OF,
        scorer=LexiconScorer(),
        trailing_days=7,
        baseline_days=30,
        min_articles=5,
    )
    assert delta is not None and delta > 0
    assert trailing.mean > baseline.mean


def test_tone_deteriorating_gives_a_negative_delta():
    articles = spread(POS, start_offset=10, days=20) + spread(NEG, start_offset=1, days=5)
    delta, _, _ = sent.tone_delta(
        articles,
        as_of=AS_OF,
        scorer=LexiconScorer(),
        trailing_days=7,
        baseline_days=30,
        min_articles=5,
    )
    assert delta is not None and delta < 0


def test_PERSISTENTLY_negative_coverage_is_not_a_signal():
    """THE point of measuring change. A stock whose coverage is permanently
    gloomy is not a sell signal every day — it is a stock with gloomy
    coverage."""
    articles = spread(NEG, start_offset=10, days=20) + spread(NEG, start_offset=1, days=5)
    delta, trailing, baseline = sent.tone_delta(
        articles,
        as_of=AS_OF,
        scorer=LexiconScorer(),
        trailing_days=7,
        baseline_days=30,
        min_articles=5,
    )
    assert delta == pytest.approx(0.0, abs=0.05), (
        "unchanging tone must read as no change, however negative the level"
    )
    assert trailing.mean < 0, "...while the LEVEL is still reported as an input"
    assert baseline.mean < 0


def test_the_baseline_window_excludes_the_trailing_window():
    """Overlapping them dilutes the change being measured — about a quarter of
    the signal with 7 days inside 30."""
    trailing_only = spread(POS, start_offset=1, days=5)
    baseline_only = spread(NEG, start_offset=10, days=15)
    _, trailing, baseline = sent.tone_delta(
        trailing_only + baseline_only,
        as_of=AS_OF,
        scorer=LexiconScorer(),
        trailing_days=7,
        baseline_days=30,
        min_articles=5,
    )
    assert trailing.articles == len(trailing_only)
    assert baseline.articles == len(baseline_only)
    assert trailing.mean > 0 and baseline.mean < 0


# ── abstention ──────────────────────────────────────────────────────────────


def test_three_articles_abstain_rather_than_reporting_a_confident_delta():
    """THE floor. Three articles can disagree wildly and average to anything."""
    articles = spread(NEG, start_offset=10, days=20) + spread(POS[:3], start_offset=1, days=3)
    delta, trailing, _ = sent.tone_delta(
        articles,
        as_of=AS_OF,
        scorer=LexiconScorer(),
        trailing_days=7,
        baseline_days=30,
        min_articles=5,
    )
    assert delta is None
    assert trailing.scored == 3, "the count is still reported so the gap is visible"


def test_a_thin_BASELINE_also_abstains():
    """Both sides must clear the floor: a delta against three baseline articles
    is thin in a way nothing downstream could detect."""
    articles = spread(NEG[:3], start_offset=10, days=3) + spread(POS, start_offset=1, days=5)
    delta, _, baseline = sent.tone_delta(
        articles,
        as_of=AS_OF,
        scorer=LexiconScorer(),
        trailing_days=7,
        baseline_days=30,
        min_articles=5,
    )
    assert delta is None
    assert baseline.scored == 3


def test_unscoreable_articles_do_not_count_toward_the_floor():
    """Five headlines the scorer cannot read are not five articles of evidence."""
    bland = [
        article(AS_OF - timedelta(days=i), "Company opens office in Ohio", article_id=f"b{i}")
        for i in range(1, 7)
    ]
    delta, trailing, _ = sent.tone_delta(
        spread(NEG, start_offset=10, days=20) + bland,
        as_of=AS_OF,
        scorer=LexiconScorer(),
        trailing_days=7,
        baseline_days=30,
        min_articles=5,
    )
    assert trailing.articles == 6
    assert trailing.scored == 0
    assert delta is None


def test_no_articles_at_all_abstains():
    delta, trailing, baseline = sent.tone_delta(
        [],
        as_of=AS_OF,
        scorer=LexiconScorer(),
        trailing_days=7,
        baseline_days=30,
        min_articles=5,
    )
    assert delta is None
    assert trailing.mean is None and baseline.mean is None


# ══════════════════════════════════════════════════════════════════════════
# Volume spike — against the symbol's OWN normal
# ══════════════════════════════════════════════════════════════════════════


def test_a_steady_news_flow_reads_as_about_one():
    articles = [
        article(AS_OF - timedelta(days=i), "Beats estimates", article_id=f"s{i}")
        for i in range(1, 30)
    ]
    spike, trailing_rate, baseline_rate = sent.volume_spike(
        articles, as_of=AS_OF, trailing_days=7, baseline_days=30, min_articles=5
    )
    assert spike == pytest.approx(1.0, abs=0.15)
    assert trailing_rate == pytest.approx(1.0, abs=0.15)
    assert baseline_rate == pytest.approx(1.0, abs=0.15)


def test_a_burst_of_coverage_reads_above_one():
    quiet = [
        article(AS_OF - timedelta(days=8 + i), "Beats estimates", article_id=f"q{i}")
        for i in range(0, 22, 2)
    ]
    burst = [
        article(AS_OF - timedelta(days=1), "Beats estimates", article_id=f"burst{i}")
        for i in range(20)
    ]
    spike, _, _ = sent.volume_spike(
        quiet + burst, as_of=AS_OF, trailing_days=7, baseline_days=30, min_articles=5
    )
    assert spike > 3.0


def test_the_rate_is_per_day_because_the_windows_differ_in_length():
    """Comparing raw counts would report a spike of 7/30 on a normal symbol."""
    articles = [
        article(AS_OF - timedelta(days=i), "Beats estimates", article_id=f"r{i}")
        for i in range(1, 30)
    ]
    spike, trailing_rate, _ = sent.volume_spike(
        articles, as_of=AS_OF, trailing_days=7, baseline_days=30, min_articles=5
    )
    assert 0.8 < spike < 1.2, "one article a day is normal flow, not a 0.23x drought"
    assert trailing_rate == pytest.approx(7 / 7, abs=0.2)


def test_normalisation_is_per_symbol_so_a_megacap_is_not_permanently_newsworthy():
    """Forty articles a week is normal for one name and a spike for another."""
    loud = [
        article(AS_OF - timedelta(days=i % 30), "Beats estimates", article_id=f"l{i}")
        for i in range(150)
    ]
    quiet = [
        article(AS_OF - timedelta(days=i * 3), "Beats estimates", article_id=f"qq{i}")
        for i in range(1, 10)
    ]
    loud_spike, _, _ = sent.volume_spike(
        loud, as_of=AS_OF, trailing_days=7, baseline_days=30, min_articles=5
    )
    quiet_spike, _, _ = sent.volume_spike(
        quiet, as_of=AS_OF, trailing_days=7, baseline_days=30, min_articles=5
    )
    assert loud_spike == pytest.approx(1.0, abs=0.3), "its own normal, not the market's"
    assert quiet_spike == pytest.approx(1.0, abs=0.6)


def test_no_established_normal_means_no_spike():
    """A symbol the engine has barely seen cannot have a spike, only a first
    impression."""
    articles = [article(AS_OF - timedelta(days=1), "Beats estimates", article_id="n1")]
    spike, _, _ = sent.volume_spike(
        articles, as_of=AS_OF, trailing_days=7, baseline_days=30, min_articles=5
    )
    assert spike is None


# ══════════════════════════════════════════════════════════════════════════
# Dedupe
# ══════════════════════════════════════════════════════════════════════════


def test_the_same_article_twice_for_one_symbol_votes_once():
    """A re-ingest or a provider resend would otherwise let one story vote
    twice in a 5-article window."""
    dup = article(AS_OF - timedelta(days=1), "Beats estimates", article_id="same")
    assert len(sent.dedupe_by_article([dup, dict(dup), dict(dup)])) == 1


def test_different_articles_are_all_kept():
    rows = [
        article(AS_OF - timedelta(days=i), "Beats estimates", article_id=f"d{i}")
        for i in range(1, 6)
    ]
    assert len(sent.dedupe_by_article(rows)) == 5


def test_dedupe_is_within_a_symbol_not_across_symbols():
    """An article tagged to four symbols IS news about each of them, and
    news_articles is keyed (provider, article_id, symbol) to say so."""
    shared = article(AS_OF - timedelta(days=1), "Beats estimates", article_id="shared")
    for_aapl = shared | {"symbol": "AAPL"}
    for_msft = shared | {"symbol": "MSFT"}
    # Each symbol's own list keeps its single copy.
    assert len(sent.dedupe_by_article([for_aapl])) == 1
    assert len(sent.dedupe_by_article([for_msft])) == 1


def test_dedupe_keeps_the_newest_copy():
    old = article(AS_OF - timedelta(days=5), "Misses estimates", article_id="same")
    new = article(AS_OF - timedelta(days=1), "Beats estimates", article_id="same")
    kept = sent.dedupe_by_article([old, new])
    assert len(kept) == 1
    assert kept[0]["headline"] == "Beats estimates"


def test_a_duplicate_cannot_lift_a_thin_window_over_the_floor():
    """The dedupe has to happen BEFORE the floor is applied, or one story
    repeated five times clears a 5-article bar on its own."""
    dup = article(AS_OF - timedelta(days=1), "Beats estimates", article_id="same")
    features = sent.compute(
        "TEST",
        [dict(dup) for _ in range(6)] + spread(NEG, start_offset=10, days=20),
        as_of=AS_OF,
        cfg=scope(),
    )
    assert features.tone_delta is None
    assert features.trailing.articles == 1


# ══════════════════════════════════════════════════════════════════════════
# The bundle
# ══════════════════════════════════════════════════════════════════════════


def test_compute_reports_depth_of_our_own_series():
    articles = [
        article(AS_OF - timedelta(days=i), "Beats estimates", article_id=f"p{i}")
        for i in range(1, 15)
    ]
    features = sent.compute("TEST", articles, as_of=AS_OF, cfg=scope())
    assert features.depth.observations == 14
    assert features.depth.span_days == 13


def test_compute_on_nothing_abstains_everywhere():
    features = sent.compute("TEST", [], as_of=AS_OF, cfg=scope())
    assert features.tone_delta is None
    assert features.volume_spike is None
    assert features.depth.is_empty is True
    assert features.scorer_id == "lexicon/1", "the scorer is still named"


def test_as_dict_flattens_the_windows_and_depth():
    features = sent.compute("TEST", spread(POS, start_offset=1, days=5), as_of=AS_OF, cfg=scope())
    out = features.as_dict()
    assert isinstance(out["trailing"], dict) and "scored" in out["trailing"]
    assert isinstance(out["depth"], dict)
    assert out["scorer_id"] == "lexicon/1"


def test_a_baseline_no_longer_than_the_trailing_window_is_rejected():
    """Otherwise the delta compares a window against itself and is always 0."""
    with pytest.raises(Exception, match="must exceed"):
        config.SentimentScope(trailing_days=30, baseline_days=30, min_articles=5, scorer="lexicon")


# ══════════════════════════════════════════════════════════════════════════
# Coverage bounding — "no news" vs "we weren't looking"
# ══════════════════════════════════════════════════════════════════════════
#
# Found on real data 2026-10-21, eight days into the news series: JNJ reported
# a 7.4x volume "spike" because its 23-day baseline window divided 30 articles
# by 23 days when the series only reached back two of them. A 7x reading that
# actually means "we started collecting last week" is worse than no reading,
# because nothing downstream can tell the difference.


def test_a_short_series_does_not_fake_a_spike():
    """THE bug. Without coverage bounding this reports a large spike."""
    coverage = AS_OF - timedelta(days=8)
    articles = [
        article(AS_OF - timedelta(days=d), "Beats estimates", article_id=f"c{d}-{i}")
        for d in (8, 7)
        for i in range(15)
    ] + [
        article(AS_OF - timedelta(days=1), "Beats estimates", article_id=f"t{i}") for i in range(20)
    ]

    naive, _, _ = sent.volume_spike(
        articles, as_of=AS_OF, trailing_days=7, baseline_days=30, min_articles=5
    )
    bounded, trailing_rate, baseline_rate = sent.volume_spike(
        articles,
        as_of=AS_OF,
        trailing_days=7,
        baseline_days=30,
        min_articles=5,
        coverage_start=coverage,
    )

    assert naive is not None and naive > 2, "the unbounded form inflates"
    assert bounded is None, "one covered baseline day cannot be a notion of normal"
    assert trailing_rate is not None, "what IS known is still reported"
    assert baseline_rate is not None


def test_a_baseline_window_entirely_before_coverage_has_no_normal():
    coverage = AS_OF - timedelta(days=3)
    articles = [
        article(AS_OF - timedelta(days=1), "Beats estimates", article_id=f"x{i}") for i in range(10)
    ]
    spike, trailing_rate, baseline_rate = sent.volume_spike(
        articles,
        as_of=AS_OF,
        trailing_days=7,
        baseline_days=30,
        min_articles=5,
        coverage_start=coverage,
    )
    assert spike is None
    assert baseline_rate is None, "there is no baseline at all, not a rate of zero"
    assert trailing_rate == pytest.approx(10 / 7)


def test_full_coverage_restores_the_normal_comparison():
    """Once the series is long enough, the reading works as designed."""
    coverage = AS_OF - timedelta(days=365)
    articles = [
        article(AS_OF - timedelta(days=i), "Beats estimates", article_id=f"f{i}")
        for i in range(1, 30)
    ]
    spike, _, _ = sent.volume_spike(
        articles,
        as_of=AS_OF,
        trailing_days=7,
        baseline_days=30,
        min_articles=5,
        coverage_start=coverage,
    )
    assert spike == pytest.approx(1.0, abs=0.2)


def test_a_genuine_quiet_baseline_still_reads_as_a_spike():
    """Coverage bounding must not suppress a REAL spike: the difference is
    whether we were watching, not whether it was quiet."""
    coverage = AS_OF - timedelta(days=365)
    quiet = [
        article(AS_OF - timedelta(days=8 + i * 3), "Beats estimates", article_id=f"q{i}")
        for i in range(6)
    ]
    burst = [
        article(AS_OF - timedelta(days=1), "Beats estimates", article_id=f"b{i}") for i in range(25)
    ]
    spike, _, _ = sent.volume_spike(
        quiet + burst,
        as_of=AS_OF,
        trailing_days=7,
        baseline_days=30,
        min_articles=5,
        coverage_start=coverage,
    )
    assert spike is not None and spike > 5


def test_compute_passes_coverage_through():
    coverage = AS_OF - timedelta(days=4)
    articles = [
        article(AS_OF - timedelta(days=1), "Beats estimates", article_id=f"p{i}") for i in range(10)
    ]
    features = sent.compute("TEST", articles, as_of=AS_OF, cfg=scope(), coverage_start=coverage)
    assert features.volume_spike is None
