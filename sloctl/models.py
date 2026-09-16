"""Immutable models shared by the engine, the rule generator and the CLI.

``sloctl.config`` builds :class:`SLO` from YAML, ``sloctl.samples`` aggregates
the fixtures, and every derived number is computed by ``sloctl.engine``, so
there is exactly one place to audit when a number looks wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Optional, Tuple

from .errors import ConfigError

SERVICE_NAME_PATTERN = r"^[a-z][a-z0-9_-]{1,40}$"
SLI_NAME_PATTERN = r"^[a-z][a-z0-9_]{1,30}$"
LABEL_NAME_PATTERN = r"^[a-zA-Z_][a-zA-Z0-9_]*$"

SERVICE_NAME_RE = re.compile(SERVICE_NAME_PATTERN)
SLI_NAME_RE = re.compile(SLI_NAME_PATTERN)
LABEL_NAME_RE = re.compile(LABEL_NAME_PATTERN)

#: Recording-rule windows kept per service. They cover the short window of every
#: generated multi-burn-rate alert plus the windows printed by ``sloctl burn``.
RATIO_WINDOWS: Tuple[str, ...] = ("5m", "30m", "1h", "6h", "1d", "3d")


@dataclass(frozen=True)
class SLISpec:
    """Declarative SLI: two PromQL expressions whose ratio *is* the SLI value.

    Both expressions must contain a range selector (``[5m]``). The generator
    rewrites that selector to build the recording rule of every window, which is
    why the fixtures and the rules share a single definition.
    """

    name: str
    good_query: str
    total_query: str
    unit: str = "requests"

    def __post_init__(self) -> None:
        if not SLI_NAME_RE.match(self.name):
            raise ConfigError("sli.name %r is invalid: it must match %s" % (self.name, SLI_NAME_PATTERN))
        for attribute in ("good_query", "total_query"):
            value = getattr(self, attribute)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError("sli.%s must be a non-empty PromQL expression" % attribute)
        if not isinstance(self.unit, str) or not self.unit.strip():
            raise ConfigError("sli.unit must be a non-empty string")


@dataclass(frozen=True)
class SLO:
    """One service-level objective exactly as declared in ``examples/slo/*.yaml``."""

    service: str
    objective: Decimal
    window: str
    window_seconds: int
    sli: SLISpec
    description: str = ""
    labels: Dict[str, str] = field(default_factory=dict)
    source: str = ""

    def __post_init__(self) -> None:
        if not SERVICE_NAME_RE.match(self.service):
            raise ConfigError("service %r is invalid: it must match %s" % (self.service, SERVICE_NAME_PATTERN))
        if not isinstance(self.objective, Decimal):
            raise ConfigError("objective of %s must be a decimal.Decimal" % self.service)
        if self.objective <= 0 or self.objective >= 1:
            raise ConfigError(
                "objective of %s must be greater than 0 and lower than 1 "
                "(100%% leaves no error budget); got %s" % (self.service, self.objective)
            )
        if self.window_seconds <= 0:
            raise ConfigError("window of %s must be a positive duration" % self.service)
        for key, value in self.labels.items():
            if key == "service":
                raise ConfigError("labels.service is reserved; the service label is added automatically")
            if not LABEL_NAME_RE.match(key):
                raise ConfigError("label %r of %s is not a valid Prometheus label name" % (key, self.service))
            if not isinstance(value, str):
                raise ConfigError("label %r of %s must be a string" % (key, self.service))

    @property
    def error_budget_ratio(self) -> Decimal:
        """Fraction of the window that may burn before the objective is missed."""
        return Decimal(1) - self.objective

    @property
    def budget_seconds(self) -> Decimal:
        """Error budget of the full window: 99.9% over 30d is 2592s (43m 12s)."""
        return Decimal(self.window_seconds) * self.error_budget_ratio

    @property
    def description_or_default(self) -> str:
        return self.description or "%s %s objective" % (self.service, self.sli.name)


@dataclass(frozen=True)
class Budget:
    """Error budget of one SLO plus what the samples actually consumed."""

    window_seconds: int
    coverage_seconds: int
    total_seconds: Decimal
    consumed_seconds: Decimal

    @property
    def remaining_seconds(self) -> Decimal:
        return self.total_seconds - self.consumed_seconds

    @property
    def consumed_ratio(self) -> Decimal:
        if self.total_seconds == 0:
            return Decimal(0) if self.consumed_seconds == 0 else Decimal("Infinity")
        return self.consumed_seconds / self.total_seconds

    @property
    def remaining_ratio(self) -> Decimal:
        return Decimal(1) - self.consumed_ratio

    @property
    def status(self) -> str:
        return "compliant" if self.consumed_ratio <= 1 else "violated"

    @property
    def partial_coverage(self) -> bool:
        return self.coverage_seconds < self.window_seconds


@dataclass(frozen=True)
class BurnWindow:
    """A lookback window plus the thresholds that turn a burn rate into a verdict.

    ``short_label``/``short_threshold`` describe the companion short window used
    by the generated alerts (long + short pair, SRE workbook style). They stay
    empty for windows that only feed the CLI table.
    """

    label: str
    seconds: int
    fast_threshold: Decimal
    severity: str = "ticket"
    short_label: str = ""
    short_seconds: int = 0
    short_threshold: Decimal = Decimal(0)
    short_severity: str = ""

    @property
    def has_alert_pair(self) -> bool:
        return bool(self.short_label) and self.short_seconds > 0


#: Windows printed by ``sloctl burn``. Thresholds are the workbook values for the
#: windows the workbook defines; 1d uses 3x, a deliberately conservative middle
#: ground documented in docs/slo-theory.md.
EVAL_BURN_WINDOWS: Tuple[BurnWindow, ...] = (
    BurnWindow(label="1h", seconds=3600, fast_threshold=Decimal("14.4"), severity="page"),
    BurnWindow(label="6h", seconds=21600, fast_threshold=Decimal("6"), severity="ticket"),
    BurnWindow(label="1d", seconds=86400, fast_threshold=Decimal("3"), severity="ticket"),
    BurnWindow(label="3d", seconds=259200, fast_threshold=Decimal("1"), severity="ticket"),
)

#: Windows emitted as alerting rules: multi-window multi-burn-rate, SRE workbook.
ALERT_BURN_WINDOWS: Tuple[BurnWindow, ...] = (
    BurnWindow(
        label="1h",
        seconds=3600,
        fast_threshold=Decimal("14.4"),
        severity="page",
        short_label="5m",
        short_seconds=300,
        short_threshold=Decimal("14.4"),
        short_severity="page",
    ),
    BurnWindow(
        label="6h",
        seconds=21600,
        fast_threshold=Decimal("6"),
        severity="ticket",
        short_label="30m",
        short_seconds=1800,
        short_threshold=Decimal("6"),
        short_severity="ticket",
    ),
    BurnWindow(
        label="3d",
        seconds=259200,
        fast_threshold=Decimal("1"),
        severity="ticket",
        short_label="6h",
        short_seconds=21600,
        short_threshold=Decimal("1"),
        short_severity="ticket",
    ),
)

#: How long an alert must stay firing before it is delivered.
ALERT_FOR: Dict[str, str] = {"1h": "2m", "6h": "15m", "3d": "1h"}


@dataclass(frozen=True)
class BurnMeasurement:
    """Burn rate of one lookback window over the sample fixtures."""

    window: BurnWindow
    good: Decimal
    total: Decimal
    burn_rate: Optional[Decimal]
    eta_seconds: Optional[Decimal]

    @property
    def error_ratio(self) -> Optional[Decimal]:
        if self.total == 0:
            return None
        return Decimal(1) - (self.good / self.total)

    @property
    def verdict(self) -> str:
        if self.burn_rate is None:
            return "no data"
        if self.burn_rate >= self.window.fast_threshold:
            return "fast burn"
        if self.burn_rate >= 1:
            return "slow burn"
        return "ok"

    @property
    def is_alerting(self) -> bool:
        return self.verdict == "fast burn"


@dataclass(frozen=True)
class Evaluation:
    """Everything the report needs about one service: budget plus burn rates."""

    slo: SLO
    good: Decimal
    total: Decimal
    budget: Budget
    burns: Tuple[BurnMeasurement, ...]

    @property
    def status(self) -> str:
        return self.budget.status

    @property
    def worst_burn(self) -> Optional[BurnMeasurement]:
        measured = [burn for burn in self.burns if burn.burn_rate is not None]
        if not measured:
            return None
        return max(measured, key=lambda burn: burn.burn_rate)

    @property
    def alerting_windows(self) -> Tuple[BurnMeasurement, ...]:
        return tuple(burn for burn in self.burns if burn.is_alerting)
