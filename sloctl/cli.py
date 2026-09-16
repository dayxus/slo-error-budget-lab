"""Command line interface: validate, budget, burn, rules generate, report.

Exit codes are part of the contract:

* ``0`` -- everything the command was asked to do succeeded (and, with
  ``--strict``, nothing is over budget).
* ``1`` -- a configuration, sample or engine error, or a violation detected in
  ``--strict`` mode. The message is a single line on stderr.
* ``2`` -- argparse usage error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import __version__, engine
from . import config as config_module
from . import report as report_module
from . import rules as rules_module
from . import samples as samples_module
from .errors import SLOError
from .models import SLO, Evaluation
from .samples import SampleSeries

EXIT_OK = 0
EXIT_ERROR = 1

DEFAULT_SAMPLES_DIR = "examples/samples"
DEFAULT_RULES_DIR = "build/rules"
DEFAULT_REPORT_DIR = "reports"


def render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Minimal fixed-width table: no dependency, stable output, easy to diff."""
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = ["  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))).rstrip())
    for row in rows:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


def _load_slos(path: str) -> List[SLO]:
    return config_module.load_configs(path)


def _load_series(path: str) -> Dict[str, SampleSeries]:
    return samples_module.load_sample_dir(path)


def _evaluations(config_path: str, samples_path: str) -> Sequence[Evaluation]:
    slos = _load_slos(config_path)
    series = _load_series(samples_path)
    return engine.evaluate_all(slos, series)


def _checkout(evaluations: Sequence[Evaluation]) -> Sequence[Evaluation]:
    return sorted(evaluations, key=lambda item: (item.status != "violated", item.slo.service))


def cmd_validate(args: argparse.Namespace) -> int:
    slos = _load_slos(args.config)
    print("OK: %d SLO definition(s) validated" % len(slos))
    for slo in slos:
        print(
            "  %-10s %s over %s, SLI %s (%s)"
            % (
                slo.service,
                report_module.format_objective(slo.objective),
                slo.window,
                slo.sli.name,
                slo.sli.unit,
            )
        )
    return EXIT_OK


def cmd_budget(args: argparse.Namespace) -> int:
    evaluations = _evaluations(args.config, args.samples)
    rows = []
    for evaluation in _checkout(evaluations):
        budget = evaluation.budget
        rows.append(
            [
                evaluation.slo.service,
                evaluation.slo.sli.name,
                report_module.format_objective(evaluation.slo.objective),
                evaluation.slo.window,
                engine.format_duration(budget.total_seconds),
                engine.format_duration(budget.consumed_seconds),
                report_module.format_percent(budget.remaining_ratio, 1),
                evaluation.status,
            ]
        )
    print(render_table(["SERVICE", "SLI", "OBJECTIVE", "WINDOW", "BUDGET", "CONSUMED", "REMAINING", "STATUS"], rows))
    print("")

    violated = [evaluation for evaluation in evaluations if evaluation.status == "violated"]
    print(
        "%d service(s) evaluated, %d compliant, %d over budget"
        % (len(evaluations), len(evaluations) - len(violated), len(violated))
    )
    partial = [evaluation for evaluation in evaluations if evaluation.budget.partial_coverage]
    if partial:
        print(
            "note: samples do not cover the full window for %s; consumption is understated"
            % ", ".join(evaluation.slo.service for evaluation in partial)
        )

    if args.strict and violated:
        print(
            "error: over budget: %s" % ", ".join(evaluation.slo.service for evaluation in violated),
            file=sys.stderr,
        )
        return EXIT_ERROR
    return EXIT_OK


def cmd_burn(args: argparse.Namespace) -> int:
    evaluations = _evaluations(args.config, args.samples)
    rows = []
    for evaluation in _checkout(evaluations):
        for measurement in evaluation.burns:
            rows.append(
                [
                    evaluation.slo.service,
                    measurement.window.label,
                    "%s / %s"
                    % (report_module.format_count(measurement.good), report_module.format_count(measurement.total)),
                    report_module.format_percent(measurement.error_ratio, 3),
                    report_module.format_burn(measurement.burn_rate),
                    measurement.verdict,
                    report_module.format_eta(measurement),
                ]
            )
    print(render_table(["SERVICE", "WINDOW", "GOOD / TOTAL", "ERROR RATIO", "BURN RATE", "VERDICT", "ETA"], rows))
    print("")

    fast = [burn for evaluation in evaluations for burn in evaluation.alerting_windows]
    print(
        "%d window(s) measured across %d service(s); %d at or above the page threshold"
        % (len(rows), len(evaluations), len(fast))
    )
    if fast:
        print(
            "alerting now: %s"
            % ", ".join(
                sorted(
                    "%s@%s (%s)" % (evaluation.slo.service, burn.window.label, burn.verdict)
                    for evaluation in evaluations
                    for burn in evaluation.alerting_windows
                )
            )
        )

    if args.strict and fast:
        print(
            "error: burn rate above the page threshold on %s"
            % ", ".join(sorted({e.slo.service for e in evaluations if e.alerting_windows})),
            file=sys.stderr,
        )
        return EXIT_ERROR
    return EXIT_OK


def cmd_rules_generate(args: argparse.Namespace) -> int:
    slos = _load_slos(args.config)
    written = rules_module.write_rules(slos, Path(args.out))
    for path in written:
        print("wrote %s" % path)
    print("%d rule file(s) generated for %d service(s) in %s" % (len(written), len(slos), args.out))
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    evaluations = _evaluations(args.config, args.samples)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "slo-report.md"
    text = report_module.render_report(evaluations)
    path.write_text(text, encoding="utf-8")
    violated = [evaluation for evaluation in evaluations if evaluation.status == "violated"]
    print(
        "wrote %s (%d line(s), %d service(s), %d over budget)"
        % (path, len(text.splitlines()), len(evaluations), len(violated))
    )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sloctl",
        description="Validate SLI/SLO definitions, compute error budgets and burn rates offline, and generate Prometheus rules.",
    )
    parser.add_argument("--version", action="version", version="sloctl %s" % __version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate every SLO YAML in a directory")
    validate.add_argument("--config", required=True, help="directory (or file) with SLO definitions")
    validate.set_defaults(func=cmd_validate)

    budget = subparsers.add_parser("budget", help="error budget table for every service")
    budget.add_argument("--config", required=True, help="directory (or file) with SLO definitions")
    budget.add_argument("--samples", default=DEFAULT_SAMPLES_DIR, help="directory with sample fixtures")
    budget.add_argument("--strict", action="store_true", help="exit 1 when a service is over budget")
    budget.set_defaults(func=cmd_budget)

    burn = subparsers.add_parser("burn", help="multi-window burn rate table")
    burn.add_argument("--config", required=True, help="directory (or file) with SLO definitions")
    burn.add_argument("--samples", default=DEFAULT_SAMPLES_DIR, help="directory with sample fixtures")
    burn.add_argument("--strict", action="store_true", help="exit 1 when a burn rate is at or above the page threshold")
    burn.set_defaults(func=cmd_burn)

    rules = subparsers.add_parser("rules", help="Prometheus rule generation")
    rules_subparsers = rules.add_subparsers(dest="rules_command", required=True)
    generate = rules_subparsers.add_parser("generate", help="write recording and alerting rule files")
    generate.add_argument("--config", required=True, help="directory (or file) with SLO definitions")
    generate.add_argument("--out", default=DEFAULT_RULES_DIR, help="output directory for the rule files")
    generate.set_defaults(func=cmd_rules_generate)

    report = subparsers.add_parser("report", help="Markdown report of budgets and burn rates")
    report.add_argument("--config", required=True, help="directory (or file) with SLO definitions")
    report.add_argument("--samples", default=DEFAULT_SAMPLES_DIR, help="directory with sample fixtures")
    report.add_argument("--out", default=DEFAULT_REPORT_DIR, help="output directory for slo-report.md")
    report.set_defaults(func=cmd_report)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SLOError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_ERROR
    except FileNotFoundError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
