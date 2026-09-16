"""End-to-end tests of the CLI contract: exit codes, output paths, output content.

The CLI is exercised through a subprocess because that is how it is used: a
shell, a Makefile and a CI job. Importing ``main()`` directly would not catch an
entry point that only works inside the test process.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from sloctl.cli import main, render_table

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "examples" / "slo"
SAMPLES_DIR = REPO_ROOT / "examples" / "samples"


def run_cli(*args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    environment = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    return subprocess.run(
        [sys.executable, "-m", "sloctl", *args],
        cwd=str(cwd),
        env=environment,
        capture_output=True,
        text=True,
    )


def test_version_flag():
    result = run_cli("--version")
    assert result.returncode == 0
    assert result.stdout.startswith("sloctl ")


def test_validate_exits_zero_on_the_examples():
    result = run_cli("validate", "--config", str(CONFIG_DIR))
    assert result.returncode == 0
    assert "OK: 3 SLO definition(s) validated" in result.stdout


def test_validate_lists_every_service_with_its_objective():
    output = run_cli("validate", "--config", str(CONFIG_DIR)).stdout
    for service, objective in (("checkout", "99.9%"), ("payments", "99.95%"), ("search", "99.5%")):
        assert service in output
        assert objective in output


def test_validate_exits_one_on_a_broken_config(tmp_path):
    (tmp_path / "broken.yaml").write_text(
        "service: checkout\n"
        "sli:\n"
        "  name: availability\n"
        '  good_query: sum(rate(http_requests_total{status!~"5.."}[5m]))\n'
        "  total_query: sum(rate(http_requests_total[5m]))\n"
        "objective: 1.5\n"
        "window: 30d\n",
        encoding="utf-8",
    )
    result = run_cli("validate", "--config", str(tmp_path))
    assert result.returncode == 1
    assert result.stderr.startswith("error: ")
    assert "objective" in result.stderr


def test_validate_exits_one_when_the_directory_is_missing(tmp_path):
    result = run_cli("validate", "--config", str(tmp_path / "nope"))
    assert result.returncode == 1
    assert "does not exist" in result.stderr


def test_budget_prints_the_documented_budgets():
    result = run_cli("budget", "--config", str(CONFIG_DIR), "--samples", str(SAMPLES_DIR))
    assert result.returncode == 0
    assert "43m 12s" in result.stdout  # 99.9% over 30d
    assert "21m 36s" in result.stdout  # 99.95% over 30d
    assert "3h 36m" in result.stdout  # 99.5% over 30d
    assert "violated" in result.stdout and "compliant" in result.stdout
    assert "3 service(s) evaluated, 2 compliant, 1 over budget" in result.stdout


def test_budget_reports_the_full_window_as_covered():
    output = run_cli("budget", "--config", str(CONFIG_DIR), "--samples", str(SAMPLES_DIR)).stdout
    assert "note: samples do not cover the full window" not in output


def test_budget_without_strict_exits_zero_even_with_a_violation():
    result = run_cli("budget", "--config", str(CONFIG_DIR), "--samples", str(SAMPLES_DIR))
    assert result.returncode == 0
    assert "1 over budget" in result.stdout


def test_budget_strict_gate_exits_one_on_a_violation():
    result = run_cli("budget", "--config", str(CONFIG_DIR), "--samples", str(SAMPLES_DIR), "--strict")
    assert result.returncode == 1
    assert "over budget: search" in result.stderr


def test_burn_table_shows_the_four_windows_per_service():
    result = run_cli("burn", "--config", str(CONFIG_DIR), "--samples", str(SAMPLES_DIR))
    assert result.returncode == 0
    for window in ("1h", "6h", "1d", "3d"):
        assert window in result.stdout
    assert "12 window(s) measured across 3 service(s)" in result.stdout


def test_burn_flags_the_payments_incident_as_fast_burn():
    output = run_cli("burn", "--config", str(CONFIG_DIR), "--samples", str(SAMPLES_DIR)).stdout
    assert "fast burn" in output
    assert "slow burn" in output
    assert "payments@1h (fast burn)" in output


def test_burn_strict_gate_exits_one():
    result = run_cli("burn", "--config", str(CONFIG_DIR), "--samples", str(SAMPLES_DIR), "--strict")
    assert result.returncode == 1
    assert "burn rate above the page threshold" in result.stderr


def test_burn_reports_exhausted_budgets(tmp_path):
    output = run_cli("burn", "--config", str(CONFIG_DIR), "--samples", str(SAMPLES_DIR)).stdout
    assert "exhausted" in output


def test_rules_generate_writes_two_files_per_service(tmp_path):
    out_dir = tmp_path / "rules"
    result = run_cli("rules", "generate", "--config", str(CONFIG_DIR), "--out", str(out_dir))
    assert result.returncode == 0
    written = sorted(path.name for path in out_dir.glob("*.rules.yml"))
    assert written == [
        "checkout_alerts.rules.yml",
        "checkout_recording.rules.yml",
        "payments_alerts.rules.yml",
        "payments_recording.rules.yml",
        "search_alerts.rules.yml",
        "search_recording.rules.yml",
    ]
    assert "6 rule file(s) generated for 3 service(s)" in result.stdout


def test_report_writes_the_markdown_report(tmp_path):
    out_dir = tmp_path / "reports"
    result = run_cli("report", "--config", str(CONFIG_DIR), "--samples", str(SAMPLES_DIR), "--out", str(out_dir))
    assert result.returncode == 0
    report = out_dir / "slo-report.md"
    assert report.exists()
    text = report.read_text(encoding="utf-8")
    assert "# SLO and error budget report" in text
    for service in ("checkout", "payments", "search"):
        assert "| %s |" % service in text
    assert "43m 12s" in text
    assert "full window coverage" in text


def test_report_creates_the_output_directory(tmp_path):
    out_dir = tmp_path / "nested" / "reports"
    assert (
        run_cli("report", "--config", str(CONFIG_DIR), "--samples", str(SAMPLES_DIR), "--out", str(out_dir)).returncode
        == 0
    )
    assert (out_dir / "slo-report.md").exists()


def test_missing_sample_directory_fails_with_a_clear_message(tmp_path):
    result = run_cli("budget", "--config", str(CONFIG_DIR), "--samples", str(tmp_path / "nope"))
    assert result.returncode == 1
    assert "sample directory" in result.stderr


def test_sample_fixture_without_an_slo_fails(tmp_path):
    orphan = tmp_path / "samples"
    orphan.mkdir()
    for fixture in sorted(SAMPLES_DIR.glob("*.json")):
        (orphan / fixture.name).write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    (orphan / "legacy-checkout.json").write_text(
        (SAMPLES_DIR / "checkout-healthy.json").read_text(encoding="utf-8").replace('"checkout"', '"legacy-checkout"'),
        encoding="utf-8",
    )
    result = run_cli("budget", "--config", str(CONFIG_DIR), "--samples", str(orphan))
    assert result.returncode == 1
    assert "without a matching SLO" in result.stderr


def test_usage_error_exits_two():
    result = run_cli()
    assert result.returncode == 2
    assert "usage" in result.stderr.lower()


def test_unknown_subcommand_exits_two():
    assert run_cli("budgett").returncode == 2


def test_cli_works_from_another_directory(tmp_path):
    result = run_cli(
        "budget",
        "--config",
        str(CONFIG_DIR),
        "--samples",
        str(SAMPLES_DIR),
        cwd=tmp_path,
    )
    assert result.returncode == 0
    assert "checkout" in result.stdout


def test_help_lists_every_documented_subcommand():
    result = run_cli("--help")
    assert result.returncode == 0
    for subcommand in ("validate", "budget", "burn", "rules", "report"):
        assert subcommand in result.stdout


# The tests below call the entry point in-process. They cover the parts that a
# subprocess cannot: the exit-code mapping, the error formatting and the table
# renderer.


def test_main_returns_zero_for_a_valid_configuration(capsys):
    assert main(["validate", "--config", str(CONFIG_DIR)]) == 0
    assert "OK: 3 SLO definition(s) validated" in capsys.readouterr().out


def test_main_maps_errors_to_one_line_and_exit_one(tmp_path, capsys):
    assert main(["validate", "--config", str(tmp_path)]) == 1
    stderr = capsys.readouterr().err
    assert stderr.startswith("error: ")
    assert stderr.count("\n") == 1


def test_main_strict_budget_reports_the_offending_services(capsys):
    exit_code = main(["budget", "--config", str(CONFIG_DIR), "--samples", str(SAMPLES_DIR), "--strict"])
    assert exit_code == 1
    assert "over budget: search" in capsys.readouterr().err


def test_main_strict_burn_reports_the_offending_services(capsys):
    exit_code = main(["burn", "--config", str(CONFIG_DIR), "--samples", str(SAMPLES_DIR), "--strict"])
    assert exit_code == 1
    assert "payments" in capsys.readouterr().err


def test_main_writes_rules_and_reports_in_process(tmp_path, capsys):
    assert main(["rules", "generate", "--config", str(CONFIG_DIR), "--out", str(tmp_path / "rules")]) == 0
    assert main(["report", "--config", str(CONFIG_DIR), "--samples", str(SAMPLES_DIR), "--out", str(tmp_path)]) == 0
    captured = capsys.readouterr().out
    assert "6 rule file(s) generated" in captured
    assert "slo-report.md" in captured
    assert (tmp_path / "slo-report.md").exists()


def test_render_table_pads_every_column():
    table = render_table(["A", "LONG"], [["1", "2"], ["333", "4"]])
    assert table.splitlines()[0].startswith("A    LONG")
    assert table.splitlines()[1].startswith("---  ----")
    assert table.splitlines()[2].endswith("2")


def test_parser_requires_a_subcommand():
    with pytest.raises(SystemExit) as error:
        main([])
    assert error.value.code == 2
