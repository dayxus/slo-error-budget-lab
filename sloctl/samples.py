"""Load the synthetic sample fixtures used to evaluate budgets offline.

A fixture is a JSON document describing one service over one SLO window::

    {
      "service": "checkout",
      "sli": "availability",
      "step_seconds": 1800,
      "note": "synthetic series",
      "samples": [
        {"timestamp": "2026-08-16T00:00:00Z", "good": 59995, "total": 60000}
      ]
    }

Each sample represents one ``step_seconds`` interval that ends at ``timestamp``,
which is what makes ``coverage_seconds`` a real quantity instead of a guess.
Everything is validated on load so the engine never divides by a broken series.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from .errors import SamplesError
from .models import SERVICE_NAME_RE, SLI_NAME_RE

TOP_LEVEL_KEYS = ("service", "sli", "step_seconds", "samples", "note")
SAMPLE_KEYS = ("timestamp", "good", "total")


def parse_timestamp(value: str, source: str) -> datetime:
    """Parse an ISO-8601 UTC timestamp.

    ``datetime.fromisoformat`` only learned to accept the ``Z`` suffix in 3.11,
    and this project supports 3.9, so the suffix is normalised by hand.
    """
    if not isinstance(value, str):
        raise SamplesError("%s: timestamp must be an ISO-8601 string, got %r" % (source, value))
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SamplesError("%s: timestamp %r is not ISO-8601 (%s)" % (source, value, exc)) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_decimal(value: object, field: str, source: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SamplesError("%s: sample field %r must be a number, got %r" % (source, field, value))
    return Decimal(str(value))


@dataclass(frozen=True)
class Sample:
    """One interval of good/total events."""

    timestamp: datetime
    good: Decimal
    total: Decimal

    @property
    def bad(self) -> Decimal:
        return self.total - self.good


@dataclass(frozen=True)
class SampleSeries:
    """A validated series for one service."""

    service: str
    sli: str
    step_seconds: int
    samples: Tuple[Sample, ...]
    source: str
    note: str = ""

    @property
    def start(self) -> datetime:
        return self.samples[0].timestamp

    @property
    def end(self) -> datetime:
        return self.samples[-1].timestamp

    @property
    def coverage_seconds(self) -> int:
        """Wall-clock time the series accounts for: one step per sample."""
        return len(self.samples) * self.step_seconds

    def totals(self) -> Tuple[Decimal, Decimal]:
        """Good and total events across the whole series."""
        good = sum((sample.good for sample in self.samples), Decimal(0))
        total = sum((sample.total for sample in self.samples), Decimal(0))
        return good, total

    def window_totals(self, seconds: int) -> Tuple[Decimal, Decimal]:
        """Good and total events in the last ``seconds`` before the last sample."""
        if seconds <= 0:
            raise SamplesError("lookback window must be positive, got %s" % seconds)
        cutoff = self.end - timedelta(seconds=seconds)
        selected = [sample for sample in self.samples if sample.timestamp > cutoff]
        good = sum((sample.good for sample in selected), Decimal(0))
        total = sum((sample.total for sample in selected), Decimal(0))
        return good, total

    def describe(self) -> str:
        return "%s %s..%s (%d samples x %ds)" % (
            self.service,
            self.start.isoformat().replace("+00:00", "Z"),
            self.end.isoformat().replace("+00:00", "Z"),
            len(self.samples),
            self.step_seconds,
        )


def _parse_sample(raw: object, source: str, index: int) -> Sample:
    if not isinstance(raw, dict):
        raise SamplesError("%s: samples[%d] must be an object" % (source, index))
    missing = [key for key in SAMPLE_KEYS if key not in raw]
    if missing:
        raise SamplesError("%s: samples[%d] is missing %s" % (source, index, ", ".join(missing)))
    unknown = sorted(set(raw) - set(SAMPLE_KEYS))
    if unknown:
        raise SamplesError(
            "%s: samples[%d] has unknown key(s) %s (allowed: %s)"
            % (source, index, ", ".join(unknown), ", ".join(SAMPLE_KEYS))
        )
    total = _as_decimal(raw["total"], "total", source)
    good = _as_decimal(raw["good"], "good", source)
    if total < 0 or good < 0:
        raise SamplesError("%s: samples[%d] has negative counts (good=%s total=%s)" % (source, index, good, total))
    if good > total:
        raise SamplesError(
            "%s: samples[%d] has good=%s greater than total=%s; "
            "the SLI ratio would be negative" % (source, index, good, total)
        )
    return Sample(timestamp=parse_timestamp(raw["timestamp"], source), good=good, total=total)


def load_sample_file(path: Path) -> SampleSeries:
    """Load and validate one fixture file."""
    path = Path(path)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SamplesError("sample file %s does not exist" % path) from None
    try:
        document = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SamplesError("%s: invalid JSON (%s)" % (path, exc)) from exc
    if not isinstance(document, dict):
        raise SamplesError("%s: top level must be an object" % path)

    source = str(path)
    missing = [key for key in TOP_LEVEL_KEYS if key not in document]
    if missing:
        raise SamplesError("%s: missing required key(s): %s" % (path, ", ".join(missing)))
    unknown = sorted(set(document) - set(TOP_LEVEL_KEYS))
    if unknown:
        raise SamplesError(
            "%s: unknown key(s) %s (allowed: %s)" % (path, ", ".join(unknown), ", ".join(TOP_LEVEL_KEYS))
        )

    service = document["service"]
    if not isinstance(service, str) or not SERVICE_NAME_RE.match(service):
        raise SamplesError("%s: service %r is invalid (must match a Prometheus-safe name)" % (path, service))
    sli = document["sli"]
    if not isinstance(sli, str) or not SLI_NAME_RE.match(sli):
        raise SamplesError("%s: sli %r is invalid (must match a Prometheus-safe name)" % (path, sli))

    step = document["step_seconds"]
    if isinstance(step, bool) or not isinstance(step, int) or step <= 0:
        raise SamplesError("%s: step_seconds must be a positive integer, got %r" % (path, step))

    raw_samples = document["samples"]
    if not isinstance(raw_samples, list):
        raise SamplesError("%s: samples must be a list" % path)
    if not raw_samples:
        raise SamplesError("%s: samples is empty; there is nothing to evaluate" % path)

    samples = [_parse_sample(raw, source, index) for index, raw in enumerate(raw_samples)]
    for index in range(1, len(samples)):
        delta = int((samples[index].timestamp - samples[index - 1].timestamp).total_seconds())
        if delta <= 0:
            raise SamplesError("%s: samples[%d] is not after samples[%d]" % (path, index, index - 1))
        if delta != step:
            raise SamplesError(
                "%s: samples[%d] is %ds after the previous sample but step_seconds is %ds" % (path, index, delta, step)
            )

    note = document["note"]
    if not isinstance(note, str):
        raise SamplesError("%s: note must be a string" % path)

    return SampleSeries(
        service=service,
        sli=sli,
        step_seconds=step,
        samples=tuple(samples),
        source=source,
        note=note,
    )


def load_sample_dir(path: Path) -> Dict[str, SampleSeries]:
    """Load every ``*.json`` fixture in a directory, keyed by service name."""
    path = Path(path)
    if not path.exists():
        raise SamplesError("sample directory %s does not exist" % path)
    if not path.is_dir():
        raise SamplesError("sample path %s is not a directory" % path)
    files: Sequence[Path] = sorted(path.glob("*.json"))
    if not files:
        raise SamplesError("sample directory %s contains no *.json fixtures" % path)

    series: Dict[str, SampleSeries] = {}
    for file_path in files:
        loaded = load_sample_file(file_path)
        if loaded.service in series:
            raise SamplesError(
                "service %r is defined by both %s and %s"
                % (loaded.service, series[loaded.service].source, loaded.source)
            )
        series[loaded.service] = loaded
    return series


def load_many(paths: List[Path]) -> Dict[str, SampleSeries]:
    """Load an explicit list of fixture files, keyed by service name."""
    series: Dict[str, SampleSeries] = {}
    for path in paths:
        loaded = load_sample_file(Path(path))
        if loaded.service in series:
            raise SamplesError(
                "service %r appears twice: %s and %s" % (loaded.service, series[loaded.service].source, loaded.source)
            )
        series[loaded.service] = loaded
    return series
