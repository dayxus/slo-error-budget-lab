#!/usr/bin/env python3
"""Sync docs/versions.md with the newest Prometheus and Alertmanager releases.

The maintenance workflow runs this weekly, but it is a normal script so it can
be reproduced by hand:

    python3 scripts/sync_upstream_versions.py            # update docs/versions.md
    python3 scripts/sync_upstream_versions.py --dry-run  # print what would change

Only the block between the ``<!-- versions:start -->`` and ``<!-- versions:end -->``
markers is rewritten; every check is recorded with the date it was performed, so
the file doubles as an audit trail instead of a stale list.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

VERSIONS_FILE = Path("docs/versions.md")
START_MARKER = "<!-- versions:start -->"
END_MARKER = "<!-- versions:end -->"
TRACKED = (
    ("Prometheus", "prometheus/prometheus"),
    ("Alertmanager", "prometheus/alertmanager"),
)


def api_get(url: str, timeout: int = 30) -> Dict[str, object]:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        request.add_header("Authorization", "Bearer %s" % token)
    request.add_header("User-Agent", "sloctl-version-sync")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.load(response)


def latest_release(repository: str) -> Dict[str, str]:
    """Newest non-prerelease release of a repository, as plain strings."""
    payload = api_get("https://api.github.com/repos/%s/releases/latest" % repository)
    tag = str(payload.get("tag_name", "")).strip()
    published = str(payload.get("published_at", "")).strip()
    if not tag:
        raise RuntimeError("no tag_name in the latest release of %s" % repository)
    return {"version": tag, "published_at": published[:10], "url": str(payload.get("html_url", ""))}


def render_block(versions: Dict[str, Dict[str, str]], checked_at: str) -> str:
    lines = [
        START_MARKER,
        "",
        "| Component | Latest release | Released | Last checked | Release notes |",
        "| --- | --- | --- | --- | --- |",
    ]
    for label, repository in TRACKED:
        release = versions[repository]
        lines.append(
            "| %s | `%s` | %s | %s | [%s](%s) |"
            % (label, release["version"], release["published_at"], checked_at, repository, release["url"])
        )
    lines += ["", END_MARKER]
    return "\n".join(lines)


def replace_block(text: str, block: str) -> str:
    start = text.find(START_MARKER)
    end = text.find(END_MARKER)
    if start == -1 or end == -1:
        raise RuntimeError("%s is missing the %s / %s markers" % (VERSIONS_FILE, START_MARKER, END_MARKER))
    return text[:start] + block + text[end + len(END_MARKER) :]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Sync docs/versions.md with upstream Prometheus releases.")
    parser.add_argument("--file", default=str(VERSIONS_FILE), help="file to update")
    parser.add_argument("--dry-run", action="store_true", help="print the new block without writing")
    args = parser.parse_args(argv)

    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    versions: Dict[str, Dict[str, str]] = {}
    for label, repository in TRACKED:
        try:
            versions[repository] = latest_release(repository)
        except (urllib.error.URLError, RuntimeError) as exc:
            print("error: could not resolve %s: %s" % (label, exc), file=sys.stderr)
            return 1
        print("%s: %s (released %s)" % (label, versions[repository]["version"], versions[repository]["published_at"]))

    block = render_block(versions, checked_at)
    if args.dry_run:
        print(block)
        return 0

    path = Path(args.file)
    if not path.exists():
        print("error: %s does not exist" % path, file=sys.stderr)
        return 1
    updated = replace_block(path.read_text(encoding="utf-8"), block)
    if updated == path.read_text(encoding="utf-8"):
        print("%s already up to date" % path)
        return 0
    path.write_text(updated, encoding="utf-8")
    print("updated %s" % path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
