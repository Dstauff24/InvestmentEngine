"""O18 stage 1: the cross-section harness and its significance bar.

Fixtures only. The point of these tests is that the harness can tell a signal
from noise and says so honestly in both directions — a lab that cannot produce
"not significant" on random data is not a lab.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from lab import multiple_testing as mt
from lab.cross_section import (
    MIN_SYMBOLS_PER_DAY,
    Observation,
    measure,
    quintile_buckets,
    spearman,
)

START = date(2026, 1, 5)
HORIZON = 5


def observations(day: date, pairs, *, benchmark=None, sector=None) -> list[Observation]:
    return [
        Observation(
            on=day,
            symbol=symbol,
            value=value,
            forward={HORIZON: fwd},
            benchmark={HORIZON: benchmark} if benchmark is not None else {},
            sector={HORIZON: sector} if sector is not None else {},
        )
        for symbol, value, fwd in pairs
    ]


def window(builder, days: int = 40) -> dict[date, list[Observation]]:
    return {START + timedelta(days=i): builder(START + timedelta(days=i), i) for i in range(days)}


# ── the mechanics ───────────────────────────────────────────────────────────


def test_quintiles_cut_by_POSITION_on_a_measurable_with_spread():
    values = [float(i) for i in range(50)]
    buckets = quintile_buckets(values)
    assert sorted(set(buckets)) == [0, 1, 2, 3, 4]
    assert all(buckets.count(b) == 10 for b in range(5))


def test_a_HEAVILY_TIED_day_is_uncuttable_and_is_SKIPPED():
    """Inside a block of identical values there is no ordering. Breaking the
    tie by position would put identical symbols in different quintiles and call
    the difference a signal — so ties share a bucket, the day has no bottom
    quintile, and it drops out of the window entirely."""
    values = [0.0] * 40 + [0.9] * 10
    assert len(set(quintile_buckets(values))) == 2, "ties collapse; they do not spread out"

    rows = observations(START, [(f"S{i}", 0.0 if i < 40 else 0.9, float(i)) for i in range(50)])
    assert measure({START: rows}, measurable="lumpy", horizon=HORIZON).days == 0


def test_spearman_refuses_a_series_with_no_spread():
    """A measurable that is the same for every symbol has no ordering, and a
    correlation of 0.0 would read as "measured, found nothing"."""
    assert spearman([1.0, 1.0, 1.0, 1.0], [1.0, 2.0, 3.0, 4.0]) is None
    assert spearman([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0]) == pytest.approx(1.0)
    assert spearman([1.0, 2.0, 3.0, 4.0], [4.0, 3.0, 2.0, 1.0]) == pytest.approx(-1.0)


def test_a_day_too_thin_to_cut_is_SKIPPED_not_measured():
    """Ten symbols per bucket is the floor: below it one symbol moves a
    bucket's mean by a tenth of its own return."""
    thin = {START: observations(START, [(f"S{i}", float(i), float(i)) for i in range(20)])}
    result = measure(thin, measurable="thin", horizon=HORIZON)
    assert result.days == 0
    assert result.mean_spread is None, "no spread, not a spread of zero"


def test_a_missing_forward_return_is_dropped_rather_than_zeroed():
    """A symbol delisted inside the window has no 20-session return, and
    inventing one builds survivorship in a second time."""
    rows = observations(START, [(f"S{i}", float(i), float(i)) for i in range(MIN_SYMBOLS_PER_DAY)])
    rows.append(Observation(on=START, symbol="GONE", value=99.0, forward={}))
    result = measure({START: rows}, measurable="x", horizon=HORIZON)
    assert result.symbol_days == MIN_SYMBOLS_PER_DAY


# ── can it find a signal, and can it refuse one? ────────────────────────────


def test_a_REAL_signal_is_found_with_a_positive_spread_and_IC():
    """Forward return proportional to the measurable: the top quintile must
    beat the bottom, the rank IC must be near 1, and the ladder monotone."""

    rng = random.Random(7)

    def day(d, _i):
        # Signal PLUS NOISE, because a perfectly constant spread has zero
        # variance and therefore no t-stat at all — real days never do.
        return observations(
            d,
            [
                (f"S{j}", float(j), float(j) / 10 + rng.gauss(0, 1))
                for j in range(MIN_SYMBOLS_PER_DAY)
            ],
        )

    result = measure(window(day), measurable="strong", horizon=HORIZON)
    assert result.days == 40
    assert result.mean_spread > 0
    assert result.mean_rank_ic > 0.7
    assert result.monotone
    assert result.t_stat > 3
    assert mt.two_sided_p(result.t_stat) < 0.001


def test_PURE_NOISE_is_not_significant():
    """The test that makes the rest trustworthy. Random values against random
    returns must come back with a t-stat that does not clear any bar."""
    rng = random.Random(20261026)

    def day(d, _i):
        return observations(
            d,
            [(f"S{j}", rng.gauss(0, 1), rng.gauss(0, 2)) for j in range(MIN_SYMBOLS_PER_DAY)],
        )

    result = measure(window(day, days=60), measurable="noise", horizon=HORIZON)
    assert abs(result.t_stat) < 2.0
    assert mt.two_sided_p(result.t_stat) > 0.05


def test_the_NEWEY_WEST_t_stat_is_the_smaller_one_on_a_persistent_series():
    """Overlapping windows share sessions, so the naive standard error is too
    small. On a positively autocorrelated spread series the corrected t-stat
    must be the more conservative of the two — if it were not, the correction
    would be decoration."""
    import statistics

    from lab.cross_section import newey_west_t

    series = [1.0 + 0.01 * i for i in range(60)]  # trending, highly persistent
    mean = statistics.fmean(series)
    naive = mean / (statistics.stdev(series) / len(series) ** 0.5)
    corrected = newey_west_t(series, lag=20)

    assert corrected is not None
    assert abs(corrected) < abs(naive), (
        "a correction that did not shrink a persistent series' t-stat would be decoration"
    )


# ── the significance bar ────────────────────────────────────────────────────


def test_the_bar_TIGHTENS_as_more_hypotheses_are_tested():
    """Twenty signals at p<0.05 yields one discovery from noise alone, so a
    lab that reports each test against its own bar finds something every run."""
    one = mt.adjust({"a": 0.04})
    twenty = mt.adjust({f"h{i}": 0.04 for i in range(20)})
    assert one[0].bonferroni_threshold == pytest.approx(mt.ALPHA)
    assert twenty[0].bonferroni_threshold == pytest.approx(mt.ALPHA / 20)
    assert one[0].survives_bonferroni
    assert not twenty[0].survives_bonferroni


def test_BH_uses_the_LARGEST_qualifying_p_as_its_threshold():
    """Taking each p against its own k/m*q would reject a small p whose
    neighbours failed, which is not the procedure."""
    verdicts = {v.name: v for v in mt.adjust({"a": 0.001, "b": 0.02, "c": 0.9}, q=0.10)}
    assert verdicts["a"].survives_fdr
    assert verdicts["b"].survives_fdr, "b qualifies at k=2: 0.02 <= 2/3 * 0.10"
    assert not verdicts["c"].survives_fdr


def test_an_UNMEASURED_hypothesis_does_not_make_the_others_bar_harder():
    """It was never a chance to find something. It is still returned, so the
    registry and the report cannot disagree about what was tried."""
    verdicts = {v.name: v for v in mt.adjust({"a": 0.04, "unmeasured": None})}
    assert verdicts["a"].tests == 1
    assert verdicts["a"].bonferroni_threshold == pytest.approx(mt.ALPHA)
    assert verdicts["unmeasured"].verdict == "UNMEASURED"


def test_a_verdict_SAYS_WHICH_BAR_it_cleared():
    """ "Significant" without the bar is the claim this module exists to stop."""
    verdicts = {
        v.name: v for v in mt.adjust({f"h{i}": p for i, p in enumerate([0.001] + [0.5] * 9)})
    }
    assert verdicts["h0"].verdict == "survives both bars"
    assert verdicts["h9"].verdict == "not significant"


# ── the registry: pre-registration is the part that has to REFUSE ───────────


def test_a_hypothesis_without_an_economic_rationale_is_REFUSED():
    """A signal without a reason is a coincidence with a ticker. The registry
    refuses it at construction, not at review time."""
    from lab.registry import Hypothesis

    with pytest.raises(ValueError, match="no economic rationale"):
        Hypothesis(
            name="mystery",
            measurable="x",
            rationale="it works",
            direction="higher_is_better",
            family="misc",
            registered_on=date(2026, 10, 26),
        )


def test_the_DIRECTION_is_part_of_the_pre_registration():
    """Choosing the sign after seeing the spread is a one-sided test wearing a
    disguise, and it doubles the effective number of hypotheses."""
    from lab.registry import Hypothesis

    with pytest.raises(ValueError, match="direction"):
        Hypothesis(
            name="unsigned_by_accident",
            measurable="x",
            rationale="a long enough rationale to pass the length check easily",
            direction="whichever way the data goes",
            family="misc",
            registered_on=date(2026, 10, 26),
        )


def test_a_duplicate_name_is_refused_because_the_COUNT_is_the_bar():
    from lab.registry import REGISTRY, Hypothesis, register

    existing = next(iter(REGISTRY.values()))
    with pytest.raises(ValueError, match="already registered"):
        register(
            Hypothesis(
                name=existing.name,
                measurable="x",
                rationale="a long enough rationale to pass the length check easily",
                direction="unsigned",
                family="misc",
                registered_on=date(2026, 10, 26),
            )
        )


def test_every_registered_hypothesis_carries_a_rationale_and_a_DATE():
    """A rationale written after the measurement is not a rationale, and a
    dated one can be checked against the run that measured it."""
    from lab.registry import MIN_RATIONALE_CHARS, REGISTRY

    assert REGISTRY, "the registry ships with stage 1's batch, not empty"
    for h in REGISTRY.values():
        assert len(h.rationale) >= MIN_RATIONALE_CHARS
        assert h.registered_on is not None
        assert h.name not in h.rationale, "the rationale says WHY, not what the number is"


def test_the_registry_includes_the_questions_the_ENGINE_raised():
    """D53's composite question in both of its forms, and O20's hold-length
    finding as a question rather than a retrofit."""
    from lab.registry import REGISTRY

    assert "composite_as_ranking" in REGISTRY
    assert "composite_as_filter_on_recipe_signals" in REGISTRY
    assert "short_hold_reclaim" in REGISTRY
    # The one whose evidence was withdrawn must say so where somebody will read it.
    assert "withdrawn" in REGISTRY["short_hold_reclaim"].notes


def test_the_test_COUNT_is_what_sets_the_bar_and_it_comes_from_the_registry():
    """The significance bar is `alpha / m`, so `m` cannot be whatever subset
    somebody chose to print after seeing the answers."""
    from lab.registry import REGISTRY, measured_count

    assert measured_count() == len(REGISTRY)
    bar = mt.adjust(dict.fromkeys(REGISTRY, 0.04))
    assert bar[0].tests == measured_count()
    assert bar[0].bonferroni_threshold == pytest.approx(mt.ALPHA / measured_count())
