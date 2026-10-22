"""Feature -> pillar-score mapping (D2/D32/D41). Fixtures only, no DB.

D41 is the load-bearing decision here: an abstaining pillar can neither agree
nor conflict, and `None` never becomes 50.
"""

from __future__ import annotations

from datetime import date

import pytest

from core import config
from features.catalyst import CatalystFeatures, EarningsSurprise, InsiderCluster
from features.evidence import EMPTY, EvidenceDepth
from features.fundamental import FundamentalFeatures, RevisionMomentum
from features.sentiment import SentimentFeatures, ToneWindow
from features.technical import MAStack, PriceStructure, TechnicalFeatures, WeekStructure
from scoring import pillars as pl

AS_OF = date(2026, 10, 21)
MAP = config.scoring().mapping


# ── builders ────────────────────────────────────────────────────────────────


def tech(**kw) -> TechnicalFeatures:
    ma = MAStack(
        **{
            "fast": 100.0,
            "mid": 95.0,
            "slow": 90.0,
            "stacked": True,
            "fast_rising": True,
            "mid_rising": True,
            "price_above_fast": True,
            "price_above_slow": True,
            "pct_from_fast": 2.0,
            **kw.pop("ma", {}),
        }
    )
    week = WeekStructure(
        **{
            "high": 120.0,
            "low": 80.0,
            "pct_from_high": -5.0,
            "pct_above_low": 25.0,
            "range_position": 0.8,
            "is_new_high": False,
            "bars_used": 252,
            **kw.pop("week52", {}),
        }
    )
    base = {
        "symbol": "TEST",
        "session_date": AS_OF,
        "close": 100.0,
        "ma": ma,
        "rsi": 58.0,
        "atr": 2.0,
        "atr_pct": 2.0,
        "relative_volume": 1.1,
        "week52": week,
        "rs_vs_sector": 7.5,
        # Pillar A does not read price structure — that is the recipes' input —
        # so an empty one is honest here rather than a fabricated base.
        "structure": PriceStructure(),
        "bars_available": 300,
    }
    base.update(kw)
    return TechnicalFeatures(**base)


def fund(**kw) -> FundamentalFeatures:
    revision = kw.pop(
        "revision",
        RevisionMomentum(
            fiscal_period_end=date(2027, 3, 31),
            eps_pct=5.0,
            revenue_pct=3.0,
            eps_first=2.0,
            eps_last=2.1,
            analyst_count_eps=12,
            depth=EvidenceDepth(observations=10, span_days=40),
        ),
    )
    base = {
        "symbol": "TEST",
        "as_of": AS_OF,
        "fiscal_date_ending": date(2026, 6, 30),
        "revenue_growth_yoy": 20.0,
        "eps_growth_yoy": 25.0,
        "revenue_acceleration": 5.0,
        "eps_acceleration": 5.0,
        "gross_margin": 40.0,
        "operating_margin": 20.0,
        "net_margin": 15.0,
        "gross_margin_trend": 1.0,
        "operating_margin_trend": 1.0,
        "net_margin_trend": 1.0,
        "roe": 1.2,
        "roic": 0.3,
        "debt_to_equity": 0.5,
        "revision": revision,
        "quarters_available": 12,
    }
    base.update(kw)
    return FundamentalFeatures(**base)


def cata(**kw) -> CatalystFeatures:
    insiders = kw.pop(
        "insiders",
        InsiderCluster(
            is_cluster=False,
            distinct_insiders=1,
            purchases=1,
            aggregate_usd=50_000.0,
            largest_usd=50_000.0,
            window_days=90,
            depth=EvidenceDepth(observations=30, span_days=90),
        ),
    )
    base = {
        "symbol": "TEST",
        "as_of": AS_OF,
        "days_to_earnings": 8,
        "days_since_earnings": 80,
        "days_to_ex_dividend": 40,
        "next_dividend_amount": 0.25,
        "insiders": insiders,
        # O12: the last reported quarter\'s beat, as a fact. The default is
        # the honest one — unverified, which the recipe refuses rather than
        # reads as a miss.
        "surprise": EarningsSurprise(None, None, None, None, None),
    }
    base.update(kw)
    return CatalystFeatures(**base)


def sent(**kw) -> SentimentFeatures:
    base = {
        "symbol": "TEST",
        "as_of": AS_OF,
        "scorer_id": "lexicon/1",
        "tone_delta": 0.1,
        "trailing": ToneWindow(mean=0.3, articles=20, scored=12),
        "baseline": ToneWindow(mean=0.2, articles=60, scored=30),
        "volume_spike": 1.5,
        "trailing_per_day": 3.0,
        "baseline_per_day": 2.0,
        "depth": EvidenceDepth(observations=30, span_days=30),
    }
    base.update(kw)
    return SentimentFeatures(**base)


# ══════════════════════════════════════════════════════════════════════════
# The mapping primitives
# ══════════════════════════════════════════════════════════════════════════


def test_a_signed_signal_is_neutral_at_zero():
    assert pl.signed_score(0.0, 10.0) == pytest.approx(50.0)


def test_full_credit_earns_100_and_its_negative_earns_0():
    assert pl.signed_score(10.0, 10.0) == pytest.approx(100.0)
    assert pl.signed_score(-10.0, 10.0) == pytest.approx(0.0)


def test_a_signed_signal_saturates_rather_than_growing_without_bound():
    """A company growing 400% is not sixteen times better than one at 25%, and
    an unbounded map would let one wild sub-signal dominate its pillar."""
    assert pl.signed_score(400.0, 25.0) == pytest.approx(100.0)
    assert pl.signed_score(-400.0, 25.0) == pytest.approx(0.0)


def test_half_credit_is_halfway():
    assert pl.signed_score(5.0, 10.0) == pytest.approx(75.0)


def test_a_signed_signal_abstains_on_none():
    assert pl.signed_score(None, 10.0) is None


def test_a_band_scores_100_inside_and_falls_off_outside():
    """RSI of 95 is not better than 60 — it is a different and worse thing, so
    a monotonic map would score it highest."""
    assert pl.band_score(58.0, 45.0, 70.0) == pytest.approx(100.0)
    assert pl.band_score(95.0, 45.0, 70.0) < 50.0
    assert pl.band_score(20.0, 45.0, 70.0) < 50.0
    assert pl.band_score(100.0, 45.0, 70.0) == pytest.approx(0.0)
    assert pl.band_score(0.0, 45.0, 70.0) == pytest.approx(0.0)


def test_proximity_abstains_beyond_the_horizon_rather_than_scoring_zero():
    """A catalyst 200 days out is NO news, not BAD news. Scoring it 0 would
    drag Pillar C down for every stock with nothing scheduled."""
    assert pl.proximity_score(5, 10, 45) == pytest.approx(100.0)
    assert pl.proximity_score(45, 10, 45) == pytest.approx(50.0)
    assert pl.proximity_score(200, 10, 45) is None
    assert pl.proximity_score(None, 10, 45) is None


# ══════════════════════════════════════════════════════════════════════════
# D41: abstention
# ══════════════════════════════════════════════════════════════════════════


def test_a_pillar_with_no_data_abstains_rather_than_scoring_fifty():
    """50 is a claim that the evidence was NEUTRAL, and we have none."""
    empty = sent(tone_delta=None, volume_spike=None)
    score = pl.sentiment_score(empty, MAP)
    assert score.score is None
    assert score.scored is False


def test_an_abstaining_pillar_neither_agrees_nor_conflicts():
    """THE D41 rule. Silence is not agreement and it is not an objection."""
    score = pl.sentiment_score(sent(tone_delta=None, volume_spike=None), MAP)
    assert score.agrees is False
    assert score.conflicts is False


def test_a_pillar_abstains_only_when_EVERY_sub_signal_is_missing():
    """Renormalization one level down: what IS knowable is still used."""
    partial = sent(tone_delta=0.25, volume_spike=None)
    score = pl.sentiment_score(partial, MAP)
    assert score.score is not None
    assert "tone_delta" in score.inputs
    assert "volume_spike" in score.missing


def test_a_low_scoring_pillar_conflicts_but_a_missing_one_does_not():
    low = pl.sentiment_score(sent(tone_delta=-1.0, volume_spike=None), MAP)
    missing = pl.sentiment_score(sent(tone_delta=None, volume_spike=None), MAP)
    assert low.conflicts is True
    assert missing.conflicts is False


def test_revision_momentum_abstains_below_the_depth_floor():
    """ "Correct at 2 snapshots, trustworthy at 60" — this is where the line
    between them is drawn for SCORING purposes."""
    thin = fund(
        revision=RevisionMomentum(
            fiscal_period_end=date(2027, 3, 31),
            eps_pct=19.5,
            revenue_pct=None,
            eps_first=2.0,
            eps_last=2.39,
            analyst_count_eps=12,
            depth=EvidenceDepth(observations=2, span_days=7),
        )
    )
    score = pl.fundamental_score(thin, MAP)
    assert "revision_momentum" in score.missing
    assert score.score is not None, "the other sub-signals still score"


def test_revision_momentum_scores_once_the_series_is_deep_enough():
    deep = fund(
        revision=RevisionMomentum(
            fiscal_period_end=date(2027, 3, 31),
            eps_pct=5.0,
            revenue_pct=None,
            eps_first=2.0,
            eps_last=2.1,
            analyst_count_eps=12,
            depth=EvidenceDepth(observations=10, span_days=40),
        )
    )
    assert "revision_momentum" in pl.fundamental_score(deep, MAP).inputs


def test_an_unknown_insider_verdict_stays_unknown():
    """D41 one level down: "the series is too young to judge" is not "no
    cluster"."""
    unknown = cata(
        insiders=InsiderCluster(
            is_cluster=None,
            distinct_insiders=0,
            purchases=0,
            aggregate_usd=0.0,
            largest_usd=None,
            window_days=90,
            depth=EMPTY,
        )
    )
    assert "insider_cluster" in pl.catalyst_score(unknown, MAP).missing


# ══════════════════════════════════════════════════════════════════════════
# Pillar A
# ══════════════════════════════════════════════════════════════════════════


def test_a_clean_uptrend_scores_pillar_a_highly():
    assert pl.technical_score(tech(), MAP).score > 70


def test_a_downtrend_scores_pillar_a_low():
    weak = tech(
        ma={
            "stacked": False,
            "fast_rising": False,
            "mid_rising": False,
            "price_above_fast": False,
            "price_above_slow": False,
        },
        week52={"range_position": 0.05},
        rsi=25.0,
        rs_vs_sector=-15.0,
    )
    assert pl.technical_score(weak, MAP).score < 25


def test_atr_is_not_scored_because_it_is_a_risk_unit_not_an_opinion():
    """Scoring it would reward or punish volatility itself; D17 sizes against
    it instead."""
    calm = pl.technical_score(tech(atr=1.0, atr_pct=1.0), MAP)
    wild = pl.technical_score(tech(atr=20.0, atr_pct=20.0), MAP)
    assert calm.score == pytest.approx(wild.score)
    assert "atr" not in calm.inputs and "atr_pct" not in calm.inputs


def test_relative_strength_is_a_pillar_a_input_per_d33():
    strong = pl.technical_score(tech(rs_vs_sector=15.0), MAP)
    weak = pl.technical_score(tech(rs_vs_sector=-15.0), MAP)
    assert "relative_strength" in strong.inputs
    assert strong.score > weak.score


def test_a_young_listing_still_scores_what_it_can():
    young = tech(
        ma={"slow": None, "stacked": None, "mid_rising": None},
        week52={"high": None, "low": None, "range_position": None, "bars_used": 60},
        rs_vs_sector=None,
    )
    score = pl.technical_score(young, MAP)
    assert score.score is not None
    assert "rsi_health" in score.inputs
    assert set(score.missing) >= {"ma_structure", "range_position", "relative_strength"}


# ══════════════════════════════════════════════════════════════════════════
# Pillars B, C, D
# ══════════════════════════════════════════════════════════════════════════


def test_revenue_and_eps_growth_are_one_sub_signal_not_two():
    """Otherwise a company reporting both gets double the growth influence of
    one reporting only revenue."""
    score = pl.fundamental_score(fund(), MAP)
    assert "growth" in score.inputs
    assert "revenue_growth" not in score.inputs


def test_shrinking_fundamentals_score_low():
    shrinking = fund(
        revenue_growth_yoy=-25.0,
        eps_growth_yoy=-25.0,
        revenue_acceleration=-10.0,
        eps_acceleration=-10.0,
        gross_margin_trend=-2.0,
        operating_margin_trend=-2.0,
        net_margin_trend=-2.0,
        revision=RevisionMomentum(
            fiscal_period_end=date(2027, 3, 31),
            eps_pct=-5.0,
            revenue_pct=None,
            eps_first=2.0,
            eps_last=1.9,
            analyst_count_eps=12,
            depth=EvidenceDepth(observations=10, span_days=40),
        ),
    )
    assert pl.fundamental_score(shrinking, MAP).score < 10


def test_an_imminent_ex_date_is_a_risk_that_subtracts():
    """D19(3) forbids a new swing long within two days, so this is an active
    reason for caution rather than an abstention."""
    risky = pl.catalyst_score(cata(days_to_ex_dividend=1), MAP)
    clear = pl.catalyst_score(cata(days_to_ex_dividend=40), MAP)
    assert risky.inputs["ex_dividend_risk"] == pytest.approx(0.0)
    assert clear.inputs["ex_dividend_risk"] == pytest.approx(50.0)
    assert risky.score < clear.score


def test_an_insider_cluster_lifts_pillar_c():
    clustered = cata(
        insiders=InsiderCluster(
            is_cluster=True,
            distinct_insiders=3,
            purchases=3,
            aggregate_usd=600_000.0,
            largest_usd=300_000.0,
            window_days=90,
            depth=EvidenceDepth(observations=30, span_days=90),
        )
    )
    assert pl.catalyst_score(clustered, MAP).score > pl.catalyst_score(cata(), MAP).score


def test_pillar_d_scores_the_DELTA_not_the_level():
    """D32: a permanently gloomy stock would otherwise be marked down every day
    for news already in the price."""
    gloomy_but_stable = sent(
        tone_delta=0.0,
        trailing=ToneWindow(mean=-0.8, articles=20, scored=12),
        baseline=ToneWindow(mean=-0.8, articles=60, scored=30),
        volume_spike=None,
    )
    assert pl.sentiment_score(gloomy_but_stable, MAP).score == pytest.approx(50.0)


def test_a_quiet_tape_is_not_bearish():
    """The volume spike maps from 1.0 UPWARD only and never below neutral."""
    quiet = pl.sentiment_score(sent(tone_delta=None, volume_spike=0.2), MAP)
    normal = pl.sentiment_score(sent(tone_delta=None, volume_spike=1.0), MAP)
    assert quiet.score == pytest.approx(50.0)
    assert normal.score == pytest.approx(50.0)


def test_a_coverage_burst_lifts_pillar_d():
    burst = pl.sentiment_score(sent(tone_delta=None, volume_spike=3.0), MAP)
    assert burst.score == pytest.approx(100.0)


# ══════════════════════════════════════════════════════════════════════════
# The mask
# ══════════════════════════════════════════════════════════════════════════


def test_the_mask_names_the_pillars_that_scored():
    scores = pl.score_all("TEST", tech(), fund(), cata(), sent(tone_delta=None, volume_spike=None))
    assert scores.mask == "A,B,C"
    assert len(scores.scored) == 3


def test_a_full_house_masks_all_four():
    assert pl.score_all("TEST", tech(), fund(), cata(), sent()).mask == "A,B,C,D"


def test_agreeing_and_conflicting_exclude_abstainers():
    scores = pl.score_all("TEST", tech(), fund(), cata(), sent(tone_delta=None, volume_spike=None))
    assert "D" not in scores.agreeing
    assert "D" not in scores.conflicting
