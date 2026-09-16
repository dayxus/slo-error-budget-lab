"""Load and validate the declarative SLO definitions.

One YAML file per service::

    service: checkout
    description: Checkout API availability
    sli:
      name: availability
      unit: requests
      good_query: sum(rate(http_requests_total{job="checkout",status!~"5.."}[5m]))
      total_query: sum(rate(http_requests_total{job="checkout"}[5m]))
    objective: 0.999          # or "99.9%"
    window: 30d
    labels:
      team: platform

Validation is deliberately strict and every message names the file, the field
and what was expected: a silent default in an SLO file is an incident waiting
to happen. Unknown keys are rejected instead of ignored.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Union

import yaml

from . import promql
from .engine import parse_duration
from .errors import ConfigError, EngineError
from .models import LABEL_NAME_RE, SLO, SLISpec

REQUIRED_KEYS = ("service", "sli", "objective", "window")
OPTIONAL_KEYS = ("description", "labels")
ALLOWED_KEYS = REQUIRED_KEYS + OPTIONAL_KEYS
SLI_REQUIRED_KEYS = ("name", "good_query", "total_query")
SLI_OPTIONAL_KEYS = ("unit",)
SLI_ALLOWED_KEYS = SLI_REQUIRED_KEYS + SLI_OPTIONAL_KEYS

CONFIG_SUFFIXES = (".yaml", ".yml")


def _fail(source: str, message: str) -> ConfigError:
    return ConfigError("%s: %s" % (source, message))


def parse_objective(value: object, source: str, field: str = "objective") -> Decimal:
    """Accept ``0.999``, ``"99.9%"`` and ``"0.999"``; reject anything else."""
    if isinstance(value, bool) or value is None:
        raise _fail(source, "%s must be a number or a percentage string, got %r" % (field, value))
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("%"):
            try:
                objective = Decimal(text[:-1]) / Decimal(100)
            except InvalidOperation as exc:
                raise _fail(source, "%s %r is not a valid percentage" % (field, value)) from exc
            if objective <= 0 or objective >= 1:
                raise _fail(
                    source,
                    "%s %r must be greater than 0%% and lower than 100%% (a 100%% objective leaves no error budget)"
                    % (field, value),
                )
            return objective
        try:
            objective = Decimal(text)
        except InvalidOperation as exc:
            raise _fail(source, "%s %r is not a number" % (field, value)) from exc
    elif isinstance(value, (int, float)):
        objective = Decimal(str(value))
    elif isinstance(value, Decimal):
        objective = value
    else:
        raise _fail(source, "%s must be a number or a percentage string, got %r" % (field, value))

    if objective <= 0 or objective >= 1:
        raise _fail(
            source,
            "%s must be greater than 0 and lower than 1 (a 100%% objective leaves no error budget); got %s"
            % (field, objective),
        )
    return objective


def parse_window(value: object, source: str) -> str:
    if not isinstance(value, str):
        raise _fail(source, "window must be a duration string such as 30d, got %r" % (value,))
    try:
        parse_duration(value)
    except EngineError:
        raise _fail(
            source,
            "window %r is not a valid duration: use a positive integer plus one of s, m, h, d, w "
            "(for example 5m, 1h, 30d)" % (value,),
        ) from None
    return value


def parse_sli(raw: object, source: str) -> SLISpec:
    if not isinstance(raw, Mapping):
        raise _fail(source, "sli must be a mapping with name, good_query and total_query")
    missing = [key for key in SLI_REQUIRED_KEYS if key not in raw]
    if missing:
        raise _fail(source, "sli is missing required key(s): %s" % ", ".join(missing))
    unknown = sorted(set(raw) - set(SLI_ALLOWED_KEYS))
    if unknown:
        raise _fail(
            source,
            "sli has unknown key(s) %s (allowed: %s)" % (", ".join(unknown), ", ".join(SLI_ALLOWED_KEYS)),
        )
    name = raw["name"]
    if not isinstance(name, str):
        raise _fail(source, "sli.name must be a string")
    good_query, total_query = raw["good_query"], raw["total_query"]
    if not isinstance(good_query, str) or not isinstance(total_query, str):
        raise _fail(source, "sli.good_query and sli.total_query must be strings")
    try:
        promql.validate_sli_queries(name, good_query, total_query)
    except ConfigError as exc:
        raise _fail(source, str(exc)) from exc
    unit = raw.get("unit", "requests")
    if not isinstance(unit, str):
        raise _fail(source, "sli.unit must be a string")
    try:
        return SLISpec(name=name, good_query=good_query, total_query=total_query, unit=unit)
    except ConfigError as exc:
        raise _fail(source, str(exc)) from exc


def parse_labels(raw: object, source: str) -> Dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise _fail(source, "labels must be a mapping of label name to string value")
    labels: Dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not LABEL_NAME_RE.match(key):
            raise _fail(source, "label name %r is not a valid Prometheus label name" % (key,))
        if not isinstance(value, str):
            raise _fail(source, "label %r must be a string, got %r" % (key, value))
        labels[key] = value
    return labels


def parse_slo(document: object, source: str) -> SLO:
    """Validate one parsed YAML document into an :class:`SLO`."""
    if not isinstance(document, Mapping):
        raise _fail(source, "top level must be a mapping of SLO fields")
    missing = [key for key in REQUIRED_KEYS if key not in document]
    if missing:
        raise _fail(source, "missing required key(s): %s" % ", ".join(missing))
    unknown = sorted(set(document) - set(ALLOWED_KEYS))
    if unknown:
        raise _fail(
            source,
            "unknown key(s) %s (allowed: %s)" % (", ".join(unknown), ", ".join(ALLOWED_KEYS)),
        )

    service = document["service"]
    if not isinstance(service, str):
        raise _fail(source, "service must be a string")

    sli = parse_sli(document["sli"], source)
    objective = parse_objective(document["objective"], source)
    window = parse_window(document["window"], source)

    description = document.get("description", "")
    if not isinstance(description, str):
        raise _fail(source, "description must be a string")
    labels = parse_labels(document.get("labels"), source)

    try:
        return SLO(
            service=service,
            objective=objective,
            window=window,
            window_seconds=parse_duration(window),
            sli=sli,
            description=description,
            labels=labels,
            source=source,
        )
    except ConfigError as exc:
        raise _fail(source, str(exc)) from exc


def load_config_file(path: Union[str, Path]) -> SLO:
    """Load and validate a single SLO file."""
    path = Path(path)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError("configuration file %s does not exist" % path) from None
    try:
        document = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError("%s: invalid YAML (%s)" % (path, exc)) from exc
    if document is None:
        raise ConfigError("%s: file is empty; expected one SLO definition" % path)
    return parse_slo(document, str(path))


def _config_files(directory: Path) -> List[Path]:
    files: List[Path] = []
    for suffix in CONFIG_SUFFIXES:
        files.extend(sorted(directory.glob("*%s" % suffix)))
    return sorted(set(files))


def load_config_dir(path: Union[str, Path]) -> List[SLO]:
    """Load every SLO file in a directory, sorted by service name."""
    directory = Path(path)
    if not directory.exists():
        raise ConfigError("configuration directory %s does not exist" % directory)
    if not directory.is_dir():
        raise ConfigError("configuration path %s is not a directory" % directory)
    files = _config_files(directory)
    if not files:
        raise ConfigError("configuration directory %s contains no *.yaml files" % directory)

    slos: List[SLO] = []
    seen: Dict[str, str] = {}
    for file_path in files:
        slo = load_config_file(file_path)
        if slo.service in seen:
            raise ConfigError(
                "service %r is defined by both %s and %s; one SLO per service"
                % (slo.service, seen[slo.service], slo.source)
            )
        seen[slo.service] = slo.source
        slos.append(slo)
    return sorted(slos, key=lambda slo: slo.service)


def load_configs(path: Union[str, Path]) -> List[SLO]:
    """Load SLOs from either a directory or a single file."""
    candidate = Path(path)
    if candidate.is_dir():
        return load_config_dir(candidate)
    return [load_config_file(candidate)]


def describe(slos: Sequence[SLO]) -> str:
    """One-line summary used by ``sloctl validate``."""
    return ", ".join("%s (%s over %s)" % (slo.service, _percent(slo.objective), slo.window) for slo in slos)


def _percent(objective: Decimal) -> str:
    text = format((objective * 100).normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "%s%%" % text


def to_document(slo: SLO) -> Dict[str, Any]:
    """Round-trip helper: render an SLO back to its YAML shape (used in docs/tests)."""
    return {
        "service": slo.service,
        "description": slo.description,
        "sli": {
            "name": slo.sli.name,
            "unit": slo.sli.unit,
            "good_query": slo.sli.good_query,
            "total_query": slo.sli.total_query,
        },
        "objective": str(slo.objective),
        "window": slo.window,
        "labels": dict(slo.labels),
    }
