"""Build the PromQL expressions that back the generated rules.

The SLI is declared once, as two queries that carry a placeholder range
selector; every window (recording rules, burn-rate alerts) is produced by
rewriting that selector. Keeping the queries in one place is what lets the
alerting rules and the fixture-based evaluation agree on what "availability"
means.
"""

from __future__ import annotations

import re

from .errors import ConfigError
from .models import SLI_NAME_RE, SLO

RANGE_SELECTOR_RE = re.compile(r"\[[^\[\]]+\]")
IDENTIFIER_SANITIZER_RE = re.compile(r"[^a-zA-Z0-9_]")


def has_range_selector(query: str) -> bool:
    return bool(RANGE_SELECTOR_RE.search(query))


def with_window(query: str, window: str) -> str:
    """``sum(rate(x[5m]))`` + ``1h`` -> ``sum(rate(x[1h]))``."""
    rewritten, replacements = RANGE_SELECTOR_RE.subn("[%s]" % window, query)
    if replacements == 0:
        raise ConfigError("query %r has no range selector such as [5m] to rewrite for window %s" % (query, window))
    return rewritten


def sanitize_identifier(value: str) -> str:
    """Make a string safe to use inside a Prometheus metric name."""
    cleaned = IDENTIFIER_SANITIZER_RE.sub("_", value)
    if not cleaned or cleaned[0].isdigit():
        cleaned = "_" + cleaned
    return cleaned


def camel_case(value: str) -> str:
    """``payment-api`` -> ``PaymentApi``, used for alert names."""
    parts = re.split(r"[^a-zA-Z0-9]+", value)
    return "".join(part[:1].upper() + part[1:] for part in parts if part) or "Service"


def ratio_name(service: str, sli_name: str, window: str) -> str:
    return "%s:sli_%s:ratio_rate%s" % (sanitize_identifier(service), sanitize_identifier(sli_name), window)


def burn_rate_name(service: str, window: str) -> str:
    return "%s:slo:burn_rate%s" % (sanitize_identifier(service), window)


def budget_remaining_name(service: str) -> str:
    return "%s:slo:error_budget_remaining" % sanitize_identifier(service)


def budget_consumed_name(service: str) -> str:
    return "%s:slo:error_budget_consumed" % sanitize_identifier(service)


def _ratio_block(slo: SLO, window: str) -> str:
    return "(\n  %s\n)\n/\n(\n  %s\n)" % (
        with_window(slo.sli.good_query, window),
        with_window(slo.sli.total_query, window),
    )


def ratio_expr(slo: SLO, window: str) -> str:
    """Good-over-total ratio of the SLI over ``window``."""
    return _ratio_block(slo, window)


def burn_rate_expr(slo: SLO, window: str) -> str:
    """Error ratio over ``window`` divided by the budget the objective allows.

    A value of 1 means "at this rate the whole budget is gone in one window".
    """
    return "(\n  1\n  -\n  %s\n)\n/\n(1 - %s)" % (_ratio_block(slo, window), slo.error_budget_ratio)


def error_budget_remaining_expr(slo: SLO) -> str:
    """Fraction of the error budget still available (negative means overspent)."""
    return "1\n-\n(\n  (1 - %s)\n  /\n  (1 - %s)\n)" % (
        ratio_name(slo.service, slo.sli.name, slo.window),
        slo.error_budget_ratio,
    )


def error_budget_consumed_expr(slo: SLO) -> str:
    """Fraction of the error budget already spent, derived from ``remaining``."""
    return "1 - %s" % budget_remaining_name(slo.service)


def validate_sli_queries(sli_name: str, good_query: str, total_query: str) -> None:
    """Reject queries the rule generator could not rewrite."""
    for attribute, query in (("good_query", good_query), ("total_query", total_query)):
        if not has_range_selector(query):
            raise ConfigError(
                "sli.%s of %r has no range selector such as [5m], so rules for other windows "
                "cannot be generated" % (attribute, sli_name)
            )


def sli_label_names(sli_name: str) -> bool:
    return bool(SLI_NAME_RE.match(sli_name))
