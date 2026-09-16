"""The error-budget math.

Every quantity here is defined in docs/slo-theory.md; this module is the single
implementation of those definitions, and the tests in ``tests/test_engine.py``
pin the values down with hand-computed answers.

Vocabulary used below (all of it derived from ``objective`` and ``window``):

* ``budget_seconds``  = window_seconds * (1 - objective)  -- allowed bad time
* ``consumed_seconds``= bad_ratio * coverage_seconds      -- bad time observed
* ``burn_rate``       = bad_ratio / (1 - objective)       -- 1x burns the budget in one window
* ``eta``             = remaining_ratio * window_seconds / burn_rate

Arithmetic uses :class:`decimal.Decimal` throughout so that a budget boundary
comparison is exact instead of a float lottery.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Optional, Sequence, Tuple

from .errors import EngineError
from .models import (
    EVAL_BURN_WINDOWS,
    SLO,
    Budget,
    BurnMeasurement,
    BurnWindow,
    Evaluation,
)
from .samples import SampleSeries

DURATION_RE = re.compile(r"^(\d+)([smhdw])$")
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DURATION_UNITS: Tuple[Tuple[str, int], ...] = (("d", 86400), ("h", 3600), ("m", 60), ("s", 1))


def parse_duration(value: str) -> int:
    """``"30d"`` -> 2592000. Raises :class:`EngineError` on anything else."""
    if not isinstance(value, str):
        raise EngineError("duration must be a string like '30d', got %r" % (value,))
    match = DURATION_RE.match(value.strip())
    if not match:
        raise EngineError(
            "duration %r is invalid: use a positive integer plus one of s, m, h, d, w (for example 5m or 30d)"
            % (value,)
        )
    count, unit = int(match.group(1)), match.group(2)
    if count <= 0:
        raise EngineError("duration %r must be greater than zero" % (value,))
    return count * UNIT_SECONDS[unit]


def format_duration(seconds: object) -> str:
    """Seconds -> compact human string: 2592 -> ``43m 12s``, 12960 -> ``3h 36m``.

    At most two units are printed, largest first, because the audience is a
    human deciding whether to spend the rest of the budget.
    """
    exact = Decimal(seconds).to_integral_value(rounding=ROUND_HALF_UP)
    total = int(exact)
    if total < 0:
        return "-" + format_duration(-total)
    parts = []
    for suffix, size in _DURATION_UNITS:
        if total >= size:
            count, total = divmod(total, size)
            parts.append("%d%s" % (count, suffix))
        if len(parts) == 2:
            break
    if not parts:
        return "0s"
    return " ".join(parts)


def bad_ratio(good: Decimal, total: Decimal) -> Decimal:
    """Fraction of events that were bad. Zero traffic means zero bad events."""
    if total < 0 or good < 0:
        raise EngineError("event counts cannot be negative (good=%s total=%s)" % (good, total))
    if good > total:
        raise EngineError(
            "good events (%s) exceed total events (%s); check the SLI queries or the fixture" % (good, total)
        )
    if total == 0:
        return Decimal(0)
    return Decimal(1) - (Decimal(good) / Decimal(total))


def burn_rate(objective: Decimal, good: Decimal, total: Decimal) -> Decimal:
    """Observed error ratio divided by the error ratio the objective allows."""
    allowed = Decimal(1) - objective
    if allowed <= 0:
        raise EngineError("objective %s leaves no error budget to burn; a burn rate is undefined" % objective)
    return bad_ratio(good, total) / allowed


def compute_budget(
    objective: Decimal,
    window_seconds: int,
    coverage_seconds: int,
    good: Decimal,
    total: Decimal,
) -> Budget:
    """Budget of a window plus the bad time the samples actually consumed."""
    if window_seconds <= 0:
        raise EngineError("window must be positive, got %s seconds" % window_seconds)
    if coverage_seconds <= 0:
        raise EngineError("coverage must be positive, got %s seconds: no samples were loaded" % coverage_seconds)
    if objective <= 0 or objective > 1:
        raise EngineError("objective must be in (0, 1], got %s" % objective)
    total_seconds = Decimal(window_seconds) * (Decimal(1) - objective)
    consumed_seconds = bad_ratio(good, total) * Decimal(coverage_seconds)
    return Budget(
        window_seconds=window_seconds,
        coverage_seconds=coverage_seconds,
        total_seconds=total_seconds,
        consumed_seconds=consumed_seconds,
    )


def eta_seconds(
    remaining_ratio: Decimal,
    window_seconds: int,
    current_burn_rate: Optional[Decimal],
) -> Optional[Decimal]:
    """Seconds until the budget is gone at the current burn rate.

    ``None`` means "not going to happen at this rate" (burn rate zero). A burned
    budget returns 0 rather than a negative estimate.
    """
    if current_burn_rate is None or current_burn_rate <= 0:
        return None
    if remaining_ratio <= 0:
        return Decimal(0)
    return (remaining_ratio * Decimal(window_seconds)) / current_burn_rate


def measure_window(
    objective: Decimal,
    window: BurnWindow,
    good: Decimal,
    total: Decimal,
    remaining_ratio: Decimal,
    slo_window_seconds: int,
) -> BurnMeasurement:
    """Burn rate of one lookback window using the SLO window for the ETA."""
    if total == 0:
        return BurnMeasurement(window=window, good=good, total=total, burn_rate=None, eta_seconds=None)
    rate = burn_rate(objective, good, total)
    return BurnMeasurement(
        window=window,
        good=good,
        total=total,
        burn_rate=rate,
        eta_seconds=eta_seconds(remaining_ratio, slo_window_seconds, rate),
    )


def evaluate(
    slo: SLO,
    series: SampleSeries,
    burn_windows: Sequence[BurnWindow] = EVAL_BURN_WINDOWS,
) -> Evaluation:
    """Evaluate one SLO against one sample series.

    The budget uses every sample in the series (which is why ``coverage_seconds``
    is reported); each burn window uses the tail of the series that covers it.
    """
    if series.service != slo.service:
        raise EngineError("sample series is for service %r but the SLO is for %r" % (series.service, slo.service))
    good, total = series.totals()
    budget = compute_budget(
        objective=slo.objective,
        window_seconds=slo.window_seconds,
        coverage_seconds=series.coverage_seconds,
        good=good,
        total=total,
    )
    measurements = []
    for window in burn_windows:
        window_good, window_total = series.window_totals(window.seconds)
        measurements.append(
            measure_window(
                objective=slo.objective,
                window=window,
                good=window_good,
                total=window_total,
                remaining_ratio=budget.remaining_ratio,
                slo_window_seconds=slo.window_seconds,
            )
        )
    return Evaluation(
        slo=slo,
        good=good,
        total=total,
        budget=budget,
        burns=tuple(measurements),
    )


def evaluate_all(
    slos: Iterable[SLO],
    series_by_service: dict,
    burn_windows: Sequence[BurnWindow] = EVAL_BURN_WINDOWS,
) -> Tuple[Evaluation, ...]:
    """Evaluate a collection of SLOs against a mapping of sample series.

    Every SLO must have samples and every loaded series must belong to an SLO,
    otherwise the caller would silently evaluate a subset of what it asked for.
    """
    evaluations = []
    slo_list = list(slos)
    known = {slo.service for slo in slo_list}
    for slo in slo_list:
        if slo.service not in series_by_service:
            raise EngineError("no samples found for service %r; add a fixture under examples/samples/" % slo.service)
        evaluations.append(evaluate(slo, series_by_service[slo.service], burn_windows=burn_windows))
    orphans = sorted(set(series_by_service) - known)
    if orphans:
        raise EngineError("sample fixtures without a matching SLO definition: %s" % ", ".join(orphans))
    return tuple(evaluations)
