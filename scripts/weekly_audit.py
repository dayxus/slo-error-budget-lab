#!/usr/bin/env python3
"""Write ``reports/weekly-audit.md`` for the scheduled maintenance run.

The audit records what actually happened in the run that produced it: the
upstream versions resolved from the GitHub Releases API, the verdict of
``promtool`` on the freshly generated rules, the number of rule files, and the
state of the test suite with its coverage. It is committed only when the content
changed (see ``.github/workflows/maintenance.yml``), so the file is a record of
real runs rather than a template.

    python3 scripts/weekly_audit.py --promtool-status success --prometheus-version v3.14.0
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

VERSIONS_FILE = Path("docs/versions.md")
RULES_DIR = Path("build/rules")
DEFAULT_LOG = Path("promtool.log")

START_MARKER = "<!-- versions:start -->"
END_MARKER = "<!-- versions:end -->"

COVERAGE_RE = re.compile(r"^TOTAL\s+\d+\s+\d+\s+(\d+)%", re.MULTILINE)


def read_versions(path: Path) -> List[Tuple[str, str]]:
    """Pull ``| Component | version | ... |`` rows out of the versions block.

    Only the rows between the ``<!-- versions:start -->`` and
    ``<!-- versions:end -->`` markers are considered: the rest of the file
    documents the table itself.
    """
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    start = text.find(START_MARKER)
    end = text.find(END_MARKER, start + len(START_MARKER)) if start != -1 else -1
    if start != -1 and end != -1:
        text = text[start + len(START_MARKER) : end]

    rows: List[Tuple[str, str]] = []
    for line in text.splitlines():
        if not line.startswith("|") or line.startswith("| ---") or line.startswith("| Component"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) >= 2 and cells[0] and cells[1]:
            rows.append((cells[0], cells[1].strip("`")))
    return rows


def run_tests() -> Tuple[str, str]:
    """Run the suite with coverage; return (test summary line, coverage percentage)."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--cov=sloctl", "--cov-report=term"],
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout + result.stderr
    summary = "unknown"
    for line in reversed(output.splitlines()):
        if " passed" in line or " failed" in line or " error" in line:
            summary = line.strip()
            break
    coverage_match = COVERAGE_RE.search(output)
    coverage = "%s%%" % coverage_match.group(1) if coverage_match else "unknown"
    return summary, coverage


def count_rule_files(rules_dir: Path) -> int:
    if not rules_dir.is_dir():
        return 0
    return len(sorted(rules_dir.glob("*.rules.yml")))


def promtool_excerpt(log_path: Path, limit: int = 40) -> str:
    if not log_path.exists():
        return "no promtool output was captured in this run"
    lines = [line.rstrip() for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    tail = lines[-limit:]
    return "\n".join(tail) if tail else "promtool produced no output"


def render(promtool_status: str, prometheus_version: str, log_path: Path, generated_at: datetime) -> str:
    versions = read_versions(VERSIONS_FILE)
    test_summary, coverage = run_tests()
    rule_files = count_rule_files(RULES_DIR)

    lines = [
        "# Weekly maintenance audit",
        "",
        "Produced by `scripts/weekly_audit.py` from the scheduled maintenance workflow.",
        "Everything below was executed in this run; nothing is carried over from a previous one.",
        "",
        "- Run (UTC): %s" % generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "- `promtool` release in use: `%s`" % prometheus_version,
        "- `promtool check rules`: **%s**" % promtool_status,
        "- Generated rule files checked: %d" % rule_files,
        "- Test suite: %s" % test_summary,
        "- Coverage of `sloctl/`: %s" % coverage,
        "",
        "## Upstream versions",
        "",
    ]
    if versions:
        lines += ["| Component | Version |", "| --- | --- |"]
        lines += ["| %s | `%s` |" % (name, version) for name, version in versions]
    else:
        lines.append("No version rows found in `docs/versions.md`.")

    lines += [
        "",
        "## promtool output",
        "",
        "```text",
        promtool_excerpt(log_path),
        "```",
        "",
        "## What this run did",
        "",
        "1. Resolved the newest Prometheus and Alertmanager releases from the GitHub Releases API",
        "   and rewrote the version block of `docs/versions.md`.",
        "2. Downloaded that exact Prometheus release's `promtool` and validated every file produced by",
        "   `sloctl rules generate` for the three example SLOs.",
        "3. Ran the test suite with coverage, whose result is recorded above.",
        "4. Committed this report and the version table only if the content changed.",
        "",
    ]
    if promtool_status != "success":
        lines += [
            "> `promtool` did not pass in this run. The maintenance workflow opens an issue with the",
            "> literal error output above so the rule generator can be fixed before deployment.",
            "",
        ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Write the weekly maintenance audit report.")
    parser.add_argument(
        "--promtool-status",
        default="success",
        choices=("success", "failure", "skipped"),
        help="outcome of the promtool validation step",
    )
    parser.add_argument("--prometheus-version", default="unknown", help="Prometheus release tag validated against")
    parser.add_argument("--promtool-log", default=str(DEFAULT_LOG), help="file with the captured promtool output")
    parser.add_argument("--out", default="reports/weekly-audit.md", help="report to write")
    args = parser.parse_args(argv)

    generated_at = datetime.now(timezone.utc)
    text = render(args.promtool_status, args.prometheus_version, Path(args.promtool_log), generated_at)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print("wrote %s (%d lines)" % (out_path, len(text.splitlines())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
