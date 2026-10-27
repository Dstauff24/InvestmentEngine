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
from lab.run import render

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
    assert "survives both bars" in verdicts["h0"].verdict
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


def test_excess_versus_SPY_is_the_same_test_as_raw_for_a_SPREAD():
    """Found by the first real run, which reported the two identical on all 12
    pairs. SPY's return is one number for the whole cross-section, so
    subtracting it shifts every symbol equally — and a top-minus-bottom spread
    is invariant to a common shift. Counting it as a separate test inflates the
    multiplicity by a third for no information."""
    rng = random.Random(11)
    days = {}
    for i in range(30):
        day = START + timedelta(days=i)
        market = rng.gauss(0, 1.5)
        days[day] = [
            Observation(
                on=day,
                symbol=f"S{j}",
                value=float(j),
                forward={HORIZON: float(j) / 10 + market},
                benchmark={HORIZON: market},
            )
            for j in range(MIN_SYMBOLS_PER_DAY)
        ]

    raw = measure(days, measurable="x", horizon=HORIZON, basis="raw")
    spy = measure(days, measurable="x", horizon=HORIZON, basis="spy")
    assert raw.mean_spread == pytest.approx(spy.mean_spread)
    assert raw.mean_rank_ic == pytest.approx(spy.mean_rank_ic)
    # The BUCKET MEANS do shift, which is why the basis is kept for those.
    assert raw.quintile_means[0] != pytest.approx(spy.quintile_means[0])


def test_the_primary_bases_EXCLUDE_spy_but_INCLUDE_beta():
    """`spy` is out because it cannot change a spread. `beta` is in for the
    opposite reason: it varies by symbol, so it reorders the cross-section and
    separates a factor from market exposure."""
    from lab.run import BASES

    assert BASES == ("raw", "sector", "beta")
    assert "spy" not in BASES


def test_the_runner_asks_for_EVERY_registered_horizon():
    """The first baseline run reported all six 60- and 120-session cells
    UNMEASURED with 0 days, because the runner took the provider's default
    `(5, 10, 20)` while two hypotheses are registered at `(20, 60, 120)`. The
    label was honest and a conclusion drawn from it would not have been:
    "momentum shows nothing at 120 days" and "nobody computed a 120-day
    return" look identical in the output and are not the same statement."""
    import inspect

    from lab import run as runner

    source = inspect.getsource(runner.run)
    assert "for hypothesis in providable for h in hypothesis.horizons" in source
    assert "horizons=horizons" in source


def test_the_BETA_basis_changes_the_ORDERING_where_the_spy_shift_cannot():
    """THE LOW-VOLATILITY TEST CASE, in miniature. Two groups: high-beta names
    that returned more only because the market rose, and low-beta names that
    returned less for the same reason. RAW, the high-beta group wins and the
    spread is positive. BETA-ADJUSTED, both groups return what their exposure
    predicts and the spread collapses — which is the difference between a
    factor and market exposure wearing a factor's name."""
    days = {}
    for i in range(40):
        day = START + timedelta(days=i)
        market = 1.0  # a steadily rising market
        rows = []
        for j in range(MIN_SYMBOLS_PER_DAY):
            beta = 0.5 + j / (MIN_SYMBOLS_PER_DAY - 1) * 1.5  # 0.5 -> 2.0
            rows.append(
                Observation(
                    on=day,
                    symbol=f"S{j}",
                    value=beta,  # the "factor" IS beta here, by construction
                    forward={HORIZON: beta * market},
                    benchmark={HORIZON: market},
                    beta=beta,
                )
            )
        days[day] = rows

    raw = measure(days, measurable="beta_dressed_as_a_factor", horizon=HORIZON, basis="raw")
    spy = measure(days, measurable="beta_dressed_as_a_factor", horizon=HORIZON, basis="spy")
    adjusted = measure(days, measurable="beta_dressed_as_a_factor", horizon=HORIZON, basis="beta")

    assert raw.mean_spread > 1.0, "raw, the high-beta quintile wins"
    assert spy.mean_spread == pytest.approx(raw.mean_spread), (
        "the flat SPY shift cannot change a spread — that is why it is not a test"
    )
    assert adjusted.mean_spread == pytest.approx(0.0, abs=1e-9), (
        "beta-adjusted, there is nothing left: it was exposure, not a factor"
    )


def test_an_observation_with_NO_beta_drops_out_of_the_beta_basis():
    """The first 252 sessions of any symbol have no point-in-time beta, and a
    missing one must remove the observation rather than default to 1.0."""
    day = START
    rows = [
        Observation(
            on=day,
            symbol=f"S{j}",
            value=float(j),
            forward={HORIZON: float(j)},
            benchmark={HORIZON: 1.0},
            beta=None if j < 10 else 1.0,
        )
        for j in range(MIN_SYMBOLS_PER_DAY)
    ]
    assert rows[0].excess(HORIZON, "beta") is None
    assert rows[-1].excess(HORIZON, "beta") == pytest.approx(float(MIN_SYMBOLS_PER_DAY - 1) - 1.0)


# ── survivorship: the caveat the report must carry (O22) ────────────────────


def test_every_result_carries_HOW_MANY_SYMBOLS_stood_behind_it():
    """The universe is TODAY's constituent list, so the further back a date
    sits the more of the market that existed then is missing — and what is
    missing is not random, it is the firms that failed. The symbol count is the
    only thing on the report that says by how much."""
    days = {}
    for i in range(10):
        day = START + timedelta(days=i)
        # Coverage GROWS over the window, as it does in reality.
        n = MIN_SYMBOLS_PER_DAY + i * 5
        days[day] = observations(day, [(f"S{j}", float(j), float(j) / 10) for j in range(n)])

    result = measure(days, measurable="growing", horizon=HORIZON)
    assert result.symbols_min == MIN_SYMBOLS_PER_DAY
    assert result.symbols_max == MIN_SYMBOLS_PER_DAY + 45
    assert result.symbols_min < result.symbols_median < result.symbols_max
    assert result.first_day == START
    assert result.last_day == START + timedelta(days=9)


def test_a_BEAR_regime_cell_is_labelled_a_survivor_biased_upper_bound():
    """Survivorship is not uniform: it is worst exactly in the bear markets,
    because the 2008-09 losers are absent from today's list. So a bear cell is
    a CEILING and the report says so on the line itself — a reader should not
    have to remember which regimes were bull."""
    from lab.run import BEAR_REGIMES, render

    one = measure(
        {START: observations(START, [(f"S{j}", float(j), float(j) / 10) for j in range(60)])},
        measurable="x",
        horizon=HORIZON,
    )
    body = render([one], {}, regime_results={"bear_volatile": [one], "bull_calm": [one]})

    assert "bear_volatile   << SURVIVOR-BIASED UPPER BOUND" in body
    assert "bull_calm\n" in body, "a bull cell carries no such flag"
    assert "A LABELLED CELL IS A CEILING, NOT AN ESTIMATE" in body
    assert set(BEAR_REGIMES) == {"bear_calm", "bear_normal", "bear_volatile"}


def test_the_report_states_that_survivorship_GROWS_with_distance():
    """Not a generic disclaimer: the direction is the point. A number from 2009
    is more biased than one from 2025, and the coverage line is what lets a
    reader see it."""
    one = measure(
        {START: observations(START, [(f"S{j}", float(j), float(j) / 10) for j in range(60)])},
        measurable="x",
        horizon=HORIZON,
    )
    body = render([one], {})
    assert "SURVIVORSHIP GROWS WITH DISTANCE FROM TODAY" in body
    assert "symbols/day min" in body
    assert "O22" in body


# ── the registered direction reaches the verdict column ─────────────────────
#
# It was validated at registration and then ignored, so a cell pointing the
# OPPOSITE way to its hypothesis printed "survives" — which is the discovery
# pre-registration exists to prevent, handed back by the report.


def test_the_expected_spread_sign_is_inverted_for_a_lower_is_better_hypothesis():
    """The spread is top-OF-THE-MEASURABLE minus bottom, so `low_volatility`
    (measuring volatility) is CONFIRMED by a negative spread. Easy to get
    backwards, which is why it is pinned."""
    assert mt.expected_spread_sign("higher_is_better") == 1
    assert mt.expected_spread_sign("lower_is_better") == -1
    assert mt.expected_spread_sign("unsigned") == 0


def test_a_significant_result_against_its_direction_says_CONTRADICTS():
    """low_volatility's real shape: +12.52% at 120 sessions on a
    `lower_is_better` hypothesis is HIGH volatility outearning low."""
    verdicts = {
        v.name: v
        for v in mt.adjust(
            {f"h{i}": p for i, p in enumerate([0.001] + [0.5] * 9)},
            expected={"h0": -1},
            observed={"h0": +1},
        )
    }
    assert verdicts["h0"].contradicts
    assert "CONTRADICTS its registered direction" in verdicts["h0"].verdict
    assert "survives" not in verdicts["h0"].verdict


def test_a_significant_result_WITH_its_direction_still_says_survives():
    verdicts = {
        v.name: v
        for v in mt.adjust(
            {f"h{i}": p for i, p in enumerate([0.001] + [0.5] * 9)},
            expected={"h0": -1},
            observed={"h0": -1},
        )
    }
    assert not verdicts["h0"].contradicts
    assert verdicts["h0"].verdict == "survives both bars"


def test_an_unsigned_hypothesis_can_never_contradict():
    """Nothing was pre-committed, so either sign is admissible as a finding —
    `rsi_reset` is registered unsigned and came back negative."""
    verdicts = {
        v.name: v
        for v in mt.adjust(
            {f"h{i}": p for i, p in enumerate([0.001] + [0.5] * 9)},
            expected={"h0": 0},
            observed={"h0": -1},
        )
    }
    assert not verdicts["h0"].contradicts
    assert "unsigned" in verdicts["h0"].verdict


def test_an_insignificant_result_never_contradicts_however_it_points():
    """A contradiction is a DETECTED effect in the wrong direction. A null is
    not evidence against the hypothesis, it is no evidence."""
    verdicts = {
        v.name: v
        for v in mt.adjust({"h0": 0.9}, expected={"h0": +1}, observed={"h0": -1})
    }
    assert not verdicts["h0"].contradicts
    assert verdicts["h0"].verdict == "not significant"


def test_the_uncomputable_regime_cell_carries_the_survivorship_label():
    """`unknown` is the WORST cell in the deep window, not an exempt one: D45
    cannot compute a regime until the benchmark has 200 sessions, which here
    means the 2008-09 crisis at the thinnest symbol coverage in the report."""
    from lab.run import LABELLED_REGIMES, render

    one = measure(
        {START: observations(START, [(f"S{j}", float(j), float(j) / 10) for j in range(60)])},
        measurable="x",
        horizon=HORIZON,
    )
    body = render([one], {}, regime_results={"unknown": [one], "bull_calm": [one]})

    assert "unknown   << SURVIVOR-BIASED UPPER BOUND" in body
    assert "bull_calm\n" in body, "a bull cell still carries no flag"
    assert "unknown` is labelled for the same reason" in body
    assert "unknown" in LABELLED_REGIMES


# ── a hypothesis this window cannot confirm says so on every row ────────────


def test_a_withheld_hypothesis_is_marked_on_every_row_and_footnoted():
    """`short_term_reversal` is externally motivated (so not a sign flip) AND
    registered after a related result was seen (so not independent of it). Both
    are true, and a result printed without the second reads as a clean
    out-of-sample confirmation."""
    from lab.registry import REGISTRY
    from lab.run import render

    results = [
        measure(
            {START: observations(START, [(f"S{j}", float(j), float(j) / 10) for j in range(60)])},
            measurable="short_term_reversal",
            horizon=h,
        )
        for h in (5, 10, 20)
    ]
    body = render(results, {})

    assert body.count("<< CANNOT CONFIRM HERE") == 3, "every row, not just the first"
    assert "admissible as a REFUSAL of the hypothesis, never as" in body
    assert REGISTRY["short_term_reversal"].confirmation_withheld in body
    assert "confirmation from forward daily_candidates only" in body


def test_a_hypothesis_with_nothing_withheld_carries_no_marker():
    """The marker has to mean something, so it cannot appear by default."""
    from lab.run import render

    one = measure(
        {START: observations(START, [(f"S{j}", float(j), float(j) / 10) for j in range(60)])},
        measurable="rsi_reset",
        horizon=HORIZON,
    )
    body = render([one], {})

    assert "CANNOT CONFIRM HERE" not in body
    assert "admissible as a REFUSAL" not in body


# ── the positive control's caveat, pre-registered before it runs ────────────


def test_the_positive_control_carries_a_pre_registered_vendor_estimate_caveat():
    """The control decides how far the lab's nulls can be trusted, so the thing
    that could make it fail for a measurement reason rather than a market one
    is named BEFORE it runs — otherwise the explanation gets invented after a
    result nobody liked."""
    from lab.registry import REGISTRY

    caveat = REGISTRY["earnings_surprise_magnitude"].caveat
    assert caveat, "the control must not run without its caveat"
    assert "VENDOR ESTIMATES" in caveat
    assert "lastUpdated" in caveat, "the diagnostic names the field it keys on"
    # BOTH branches are pre-committed, which is what makes it a rule rather
    # than a line of prose: a diagnostic only written down for the failure
    # case would leave a passing result free of the same limitation.
    assert "ON FAILURE" in caveat
    assert "ON SUCCESS" in caveat


def test_a_caveat_and_a_withheld_confirmation_are_different_claims():
    """One bounds what the number means; the other says this window cannot
    support the hypothesis at all. Collapsing them would let a bounded result
    read as an unusable one, or the reverse."""
    from lab.registry import REGISTRY

    control = REGISTRY["earnings_surprise_magnitude"]
    reversal = REGISTRY["short_term_reversal"]

    assert control.caveat and not control.confirmation_withheld
    assert reversal.confirmation_withheld and not reversal.caveat
