"""Known-answer tests for the error budget math.

Every expected value here is hand-computed in docs/slo-theory.md; if one of
these fails, either the engine or the document is wrong, and both are wrong in
the same way as far as an operator is concerned.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from sloctl.config import load_config_dir
from sloctl.engine import (
    bad_ratio,
    burn_rate,
    compute_budget,
    eta_seconds,
    evaluate,
    evaluate_all,
    format_duration,
    parse_duration,
)
from sloctl.errors import EngineError, SamplesError
from sloctl.models import SLO, SLISpec
from sloctl.samples import Sample, SampleSeries, load_sample_dir

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES_DIR = REPO_ROOT / "examples" / "samples"
CONFIG_DIR = REPO_ROOT / "examples" / "slo"

DAY = 86400
THIRTY_DAYS = 30 * DAY


def make_slo(objective: str = "0.999", window: str = "30d", service: str = "checkout") -> SLO:
    return SLO(
        service=service,
        objective=Decimal(objective),
        window=window,
        window_seconds=parse_duration(window),
        sli=SLISpec(
            name="availability",
            good_query="sum(rate(http_requests_total[5m]))",
            total_query="sum(rate(http_requests_total[5m]))",
        ),
    )


def make_series(
    bad: Decimal,
    total: Decimal = Decimal(100000),
    steps: int = 48,
    step_seconds: int = 1800,
    service: str = "checkout",
) -> SampleSeries:
    end = datetime(2026, 9, 15, tzinfo=timezone.utc)
    first = end - timedelta(seconds=(steps - 1) * step_seconds)
    samples = tuple(
        Sample(
            timestamp=first + timedelta(seconds=index * step_seconds),
            good=total - bad,
            total=total,
        )
        for index in range(steps)
    )
    return SampleSeries(
        service=service,
        sli="availability",
        step_seconds=step_seconds,
        samples=samples,
        source="<in-memory>",
    )


def example_evaluations():
    slos = load_config_dir(CONFIG_DIR)
    series = load_sample_dir(SAMPLES_DIR)
    return {evaluation.slo.service: evaluation for evaluation in evaluate_all(slos, series)}


@pytest.mark.parametrize(
    "objective, window_seconds, expected_seconds, expected_text",
    [
        ("0.999", THIRTY_DAYS, Decimal(2592), "43m 12s"),
        ("0.9995", THIRTY_DAYS, Decimal(1296), "21m 36s"),
        ("0.995", THIRTY_DAYS, Decimal(12960), "3h 36m"),
    ],
)
def test_total_budget_for_known_objectives(objective, window_seconds, expected_seconds, expected_text):
    budget = compute_budget(Decimal(objective), window_seconds, window_seconds, Decimal(1000), Decimal(1000))
    assert budget.total_seconds == expected_seconds
    assert format_duration(budget.total_seconds) == expected_text
    assert budget.consumed_seconds == 0
    assert budget.remaining_ratio == 1
    assert budget.status == "compliant"


def test_total_budget_scales_with_the_window():
    budget = compute_budget(Decimal("0.999"), 7 * DAY, 7 * DAY, Decimal(1000), Decimal(1000))
    assert budget.total_seconds == Decimal("604.8")
    assert format_duration(budget.total_seconds) == "10m 5s"


def test_consumed_seconds_is_the_observed_bad_time():
    # 0.05% of the requests failed, which is half of the 0.1% the objective allows.
    budget = compute_budget(Decimal("0.999"), THIRTY_DAYS, THIRTY_DAYS, Decimal(99950), Decimal(100000))
    assert budget.consumed_seconds == Decimal("1296")
    assert budget.remaining_seconds == Decimal("1296")
    assert budget.consumed_ratio == Decimal("0.5")
    assert budget.remaining_ratio == Decimal("0.5")
    assert budget.status == "compliant"
    assert not budget.partial_coverage


def test_budget_boundary_exactly_on_the_limit_is_compliant():
    # bad ratio == 1 - objective, so the budget is fully spent but not exceeded.
    budget = compute_budget(Decimal("0.999"), THIRTY_DAYS, THIRTY_DAYS, Decimal(99900), Decimal(100000))
    assert budget.consumed_seconds == budget.total_seconds == Decimal("2592")
    assert budget.remaining_ratio == 0
    assert budget.status == "compliant"


def test_budget_boundary_one_event_over_is_violated():
    budget = compute_budget(Decimal("0.999"), THIRTY_DAYS, THIRTY_DAYS, Decimal(99899), Decimal(100000))
    assert budget.consumed_seconds > budget.total_seconds
    assert budget.remaining_ratio < 0
    assert budget.status == "violated"


def test_zero_budget_is_compliant_only_with_no_bad_events():
    clean = compute_budget(Decimal(1), THIRTY_DAYS, THIRTY_DAYS, Decimal(1000), Decimal(1000))
    assert clean.total_seconds == 0
    assert clean.consumed_ratio == 0
    assert clean.status == "compliant"

    dirty = compute_budget(Decimal(1), THIRTY_DAYS, THIRTY_DAYS, Decimal(999), Decimal(1000))
    assert dirty.total_seconds == 0
    assert dirty.consumed_ratio == Decimal("Infinity")
    assert dirty.status == "violated"


def test_burn_rate_is_undefined_without_a_budget():
    with pytest.raises(EngineError, match="no error budget"):
        burn_rate(Decimal(1), Decimal(999), Decimal(1000))


def test_partial_coverage_is_reported_not_hidden():
    budget = compute_budget(Decimal("0.999"), THIRTY_DAYS, 7 * DAY, Decimal(99900), Decimal(100000))
    assert budget.partial_coverage


def test_negative_or_zero_coverage_fails_loudly():
    with pytest.raises(EngineError, match="coverage must be positive"):
        compute_budget(Decimal("0.999"), THIRTY_DAYS, 0, Decimal(1), Decimal(1))


def test_good_above_total_fails_with_the_counts_in_the_message():
    with pytest.raises(EngineError) as error:
        compute_budget(Decimal("0.999"), THIRTY_DAYS, THIRTY_DAYS, Decimal(1001), Decimal(1000))
    assert "1001" in str(error.value) and "1000" in str(error.value)


def test_empty_sample_series_cannot_be_evaluated():
    empty = SampleSeries(service="checkout", sli="availability", step_seconds=1800, samples=(), source="<in-memory>")
    assert empty.coverage_seconds == 0
    with pytest.raises(EngineError, match="no samples were loaded"):
        evaluate(make_slo(), empty)


def test_zero_traffic_window_has_no_bad_ratio():
    assert bad_ratio(Decimal(0), Decimal(0)) == 0


@pytest.mark.parametrize(
    "good, total, expected",
    [
        (Decimal(10000), Decimal(10000), "0"),
        (Decimal(9856), Decimal(10000), "14.4"),
        (Decimal(9990), Decimal(10000), "1"),
        (Decimal(9900), Decimal(10000), "10"),
        (Decimal(9500), Decimal(10000), "50"),
    ],
)
def test_burn_rate_known_values(good, total, expected):
    assert burn_rate(Decimal("0.999"), good, total) == Decimal(expected)


def test_burn_rate_is_zero_when_nothing_fails():
    assert burn_rate(Decimal("0.999"), Decimal(1000), Decimal(1000)) == 0


def test_eta_of_a_full_budget_at_burn_rate_one_is_one_full_window():
    eta = eta_seconds(Decimal(1), THIRTY_DAYS, Decimal(1))
    assert eta == Decimal(THIRTY_DAYS)
    assert format_duration(eta) == "30d"


def test_eta_shrinks_as_the_burn_rate_grows():
    assert format_duration(eta_seconds(Decimal(1), THIRTY_DAYS, Decimal("14.4"))) == "2d 2h"
    assert format_duration(eta_seconds(Decimal("0.5"), THIRTY_DAYS, Decimal(2))) == "7d 12h"


def test_eta_is_stable_when_nothing_burns():
    assert eta_seconds(Decimal(1), THIRTY_DAYS, Decimal(0)) is None
    assert eta_seconds(Decimal(1), THIRTY_DAYS, None) is None


def test_eta_is_zero_once_the_budget_is_gone():
    assert eta_seconds(Decimal("-0.05"), THIRTY_DAYS, Decimal(2)) == 0


@pytest.mark.parametrize(
    "value, expected",
    [("5m", 300), ("30m", 1800), ("1h", 3600), ("1d", 86400), ("3d", 259200), ("30d", 2592000), ("2w", 1209600)],
)
def test_parse_duration(value, expected):
    assert parse_duration(value) == expected


@pytest.mark.parametrize("value", ["", "30", "d30", "30x", "0d", "-5m", "1.5h", "30 days"])
def test_parse_duration_rejects_garbage(value):
    with pytest.raises(EngineError, match="invalid|greater than zero"):
        parse_duration(value)


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0, "0s"),
        (12, "12s"),
        (90, "1m 30s"),
        (2592, "43m 12s"),
        (1296, "21m 36s"),
        (12960, "3h 36m"),
        (3600, "1h"),
        (86400, "1d"),
        (187200, "2d 4h"),
        (Decimal("1295.87"), "21m 36s"),
    ],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


def test_negative_durations_print_with_a_sign():
    assert format_duration(-90) == "-1m 30s"


def test_evaluate_rejects_a_series_of_another_service():
    with pytest.raises(EngineError, match="payments"):
        evaluate(make_slo(service="payments"), make_series(Decimal(5), service="checkout"))


def test_evaluate_all_requires_samples_for_every_slo():
    series = load_sample_dir(SAMPLES_DIR)
    series.pop("search")
    with pytest.raises(EngineError, match="no samples found for service 'search'"):
        evaluate_all(load_config_dir(CONFIG_DIR), series)


def test_evaluate_all_rejects_fixtures_without_an_slo():
    slos = [slo for slo in load_config_dir(CONFIG_DIR) if slo.service != "search"]
    with pytest.raises(EngineError, match="without a matching SLO"):
        evaluate_all(slos, load_sample_dir(SAMPLES_DIR))


def test_example_fixtures_land_in_the_documented_states():
    results = example_evaluations()

    checkout = results["checkout"]
    assert checkout.status == "compliant"
    assert {burn.verdict for burn in checkout.burns} == {"ok"}
    assert checkout.budget.remaining_ratio > Decimal("0.9")

    payments = results["payments"]
    assert payments.status == "compliant"
    assert payments.budget.total_seconds == Decimal(1296)
    assert payments.budget.remaining_ratio < Decimal("0.9")
    verdicts = {burn.window.label: burn.verdict for burn in payments.burns}
    assert verdicts == {"1h": "fast burn", "6h": "fast burn", "1d": "slow burn", "3d": "ok"}
    fast = payments.alerting_windows[0]
    assert fast.window.severity == "page"
    assert fast.eta_seconds is not None and fast.eta_seconds < THIRTY_DAYS

    search = results["search"]
    assert search.status == "violated"
    assert search.budget.remaining_ratio < 0
    assert all(burn.burn_rate > 1 for burn in search.burns)
    assert all(burn.eta_seconds == 0 for burn in search.burns)


def test_consumed_seconds_equals_an_independent_recomputation():
    for evaluation in example_evaluations().values():
        expected = (Decimal(1) - evaluation.good / evaluation.total) * Decimal(evaluation.budget.coverage_seconds)
        assert evaluation.budget.consumed_seconds == expected


def test_sample_loader_rejects_an_empty_series(tmp_path):
    fixture = tmp_path / "empty.json"
    fixture.write_text(
        '{"service": "checkout", "sli": "availability", "step_seconds": 1800, "note": "", "samples": []}',
        encoding="utf-8",
    )
    with pytest.raises(SamplesError, match="samples is empty"):
        load_sample_dir(tmp_path)


def test_sample_loader_rejects_good_above_total(tmp_path):
    fixture = tmp_path / "broken.json"
    fixture.write_text(
        '{"service": "checkout", "sli": "availability", "step_seconds": 1800, "note": "",'
        ' "samples": [{"timestamp": "2026-09-15T00:00:00Z", "good": 20, "total": 10}]}',
        encoding="utf-8",
    )
    with pytest.raises(SamplesError, match="greater than total"):
        load_sample_dir(tmp_path)
