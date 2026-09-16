"""Unit tests for the Markdown report and its number formatting.

The CLI tests exercise the report end to end through a subprocess; these tests
pin the exact strings a reader will see in `reports/slo-report.md` and the
formatting rules that keep the tables comparable between windows.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from sloctl.config import load_config_dir
from sloctl.engine import evaluate_all
from sloctl.models import BurnMeasurement, BurnWindow
from sloctl.report import (
    budget_table,
    burn_table,
    format_burn,
    format_count,
    format_eta,
    format_objective,
    format_percent,
    render_report,
)
from sloctl.samples import load_sample_dir

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "examples" / "slo"
SAMPLES_DIR = REPO_ROOT / "examples" / "samples"


def evaluations():
    return evaluate_all(load_config_dir(CONFIG_DIR), load_sample_dir(SAMPLES_DIR))


def test_percent_formatting_drops_noise_but_keeps_precision():
    assert format_percent(Decimal("0.999"), 1) == "99.9%"
    assert format_percent(Decimal("0.5"), 1) == "50%"
    assert format_percent(Decimal("0.5487"), 2) == "54.87%"
    assert format_percent(Decimal("-0.4"), 1) == "-40%"
    assert format_percent(Decimal("Infinity"), 1) == ">100%"
    assert format_percent(None) == "n/a"


def test_objective_formatting_reads_like_an_slo():
    assert format_objective(Decimal("0.999")) == "99.9%"
    assert format_objective(Decimal("0.9995")) == "99.95%"
    assert format_objective(Decimal("0.995")) == "99.5%"


def test_burn_rate_formatting_keeps_small_values_readable():
    assert format_burn(Decimal("0.05")) == "0.05x"
    assert format_burn(Decimal("14.4")) == "14.40x"
    assert format_burn(Decimal("402.5")) == "402x"
    assert format_burn(Decimal("1234")) == "1,234x"
    assert format_burn(None) == "n/a"


def test_counts_are_grouped_for_reading():
    assert format_count(Decimal(8640000)) == "8,640,000"
    assert format_count(Decimal("119159.5")) == "119,159.50"


def test_eta_column_distinguishes_stable_exhausted_and_unknown():
    window = BurnWindow(label="1h", seconds=3600, fast_threshold=Decimal("14.4"))
    assert format_eta(BurnMeasurement(window, Decimal(10), Decimal(10), Decimal(0), None)) == "stable"
    assert format_eta(BurnMeasurement(window, Decimal(10), Decimal(10), Decimal(2), Decimal(0))) == "exhausted"
    assert format_eta(BurnMeasurement(window, Decimal(0), Decimal(0), None, None)) == "no data"
    assert format_eta(BurnMeasurement(window, Decimal(10), Decimal(10), Decimal(2), Decimal(2592))) == "43m 12s"


def test_budget_table_ranks_violations_first():
    table = budget_table(evaluations())
    lines = table.splitlines()
    assert lines[0].startswith("| Service | SLI | Objective | Window | Budget | Consumed | Remaining | Status |")
    assert lines[2].startswith("| search |")
    assert "| checkout |" in table
    assert "43m 12s" in table and "21m 36s" in table and "3h 36m" in table


def test_burn_table_lists_every_window_of_every_service():
    table = burn_table(evaluations())
    assert table.count("\n") == 1 + 3 * 4  # header separator + 3 services x 4 windows
    assert "fast burn" in table and "slow burn" in table and "| ok |" in table


def test_report_has_every_documented_section():
    text = render_report(evaluations())
    for heading in (
        "# SLO and error budget report",
        "## Summary",
        "## Error budgets",
        "## Burn rates",
        "## Definitions applied",
        "## Coverage notes",
        "## How to reproduce",
    ):
        assert heading in text
    assert "3 service(s) evaluated: checkout, payments, search" in text
    assert "1 service(s) over budget: search" in text
    assert "docs/slo-theory.md" in text


def test_report_states_the_full_coverage_of_the_fixtures():
    text = render_report(evaluations())
    assert "**checkout**: samples cover 30d of the 30d window (full window coverage)." in text
    assert "PARTIAL coverage" not in text


def test_report_timestamp_is_utc_and_explicit():
    from datetime import datetime, timezone

    stamped = datetime(2026, 9, 15, 12, 34, 56, tzinfo=timezone.utc)
    assert "Generated at: 2026-09-15T12:34:56Z" in render_report(evaluations(), generated_at=stamped)
