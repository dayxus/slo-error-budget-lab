#!/usr/bin/env python3
"""Download the latest ``promtool`` from the official Prometheus release.

Used by the CI job that validates the generated rules, by the weekly
maintenance workflow and by ``make promtool``, so all three paths agree on
which release they trust: the newest non-prerelease on
``github.com/prometheus/prometheus``, fetched from the official release asset
(no third-party mirror, no pinned copy that silently rots).

    python3 scripts/fetch_promtool.py --out build/tools/bin
    python3 scripts/fetch_promtool.py --out build/tools/bin --version v3.14.0
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

RELEASES_API = "https://api.github.com/repos/prometheus/prometheus/releases/latest"
TAG_API = "https://api.github.com/repos/prometheus/prometheus/releases/tags/%s"

MACHINE_ALIASES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
    "armv7l": "armv7",
    "i386": "386",
    "i686": "386",
}


def platform_suffix() -> str:
    """``darwin-arm64`` / ``linux-amd64``: the suffix Prometheus uses in assets."""
    system = platform.system().lower()
    os_name = {"darwin": "darwin", "linux": "linux", "windows": "windows"}.get(system)
    if os_name is None:
        raise RuntimeError("unsupported platform: %s" % system)
    machine = MACHINE_ALIASES.get(platform.machine().lower())
    if machine is None:
        raise RuntimeError("unsupported machine architecture: %s" % platform.machine())
    return "%s-%s" % (os_name, machine)


def api_get(url: str) -> Dict[str, object]:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        request.add_header("Authorization", "Bearer %s" % token)
    request.add_header("User-Agent", "sloctl-promtool-fetch")
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        return json.load(response)


def find_asset(version: Optional[str]) -> Tuple[str, str]:
    """Return ``(tag, download_url)`` of the matching archive."""
    payload = api_get(TAG_API % version if version else RELEASES_API)
    tag = str(payload.get("tag_name", "")).strip()
    suffix = platform_suffix()
    assets = payload.get("assets") or []
    for asset in assets:  # type: ignore[union-attr]
        name = str(asset.get("name", ""))
        if name.endswith("%s.tar.gz" % suffix) or name.endswith("%s.zip" % suffix):
            return tag, str(asset.get("browser_download_url", ""))
    available = ", ".join(sorted(str(asset.get("name", "")) for asset in assets))  # type: ignore[union-attr]
    raise RuntimeError("no asset for %s in release %s (available: %s)" % (suffix, tag, available))


def download(url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=180) as response:  # noqa: S310
        destination.write_bytes(response.read())
    return destination


def extract_member(archive: Path, member_name: str, out_dir: Path) -> Path:
    """Extract the promtool binary from a tar.gz or zip release asset."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / (member_name + (".exe" if member_name == "promtool" and platform.system() == "Windows" else ""))
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            for entry in bundle.namelist():
                if entry.endswith(member_name) or entry.endswith(member_name + ".exe"):
                    with bundle.open(entry) as source, open(target, "wb") as destination:
                        destination.write(source.read())
                    break
            else:
                raise RuntimeError("%s not found inside %s" % (member_name, archive))
    else:
        with tarfile.open(archive, "r:gz") as bundle:
            handled = False
            for entry in bundle.getmembers():
                if entry.isfile() and entry.name.rsplit("/", 1)[-1] == member_name:
                    source = bundle.extractfile(entry)
                    if source is None:
                        continue
                    target.write_bytes(source.read())
                    handled = True
                    break
            if not handled:
                raise RuntimeError("%s not found inside %s" % (member_name, archive))
    target.chmod(0o755)
    return target


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Download promtool from the official Prometheus release.")
    parser.add_argument("--out", default="build/tools/bin", help="directory to place the binary in")
    parser.add_argument("--version", default=None, help="release tag to fetch (default: latest)")
    parser.add_argument("--keep-archive", action="store_true", help="keep the downloaded archive next to the binary")
    args = parser.parse_args(argv)

    try:
        tag, url = find_asset(args.version)
    except (RuntimeError, urllib.error.URLError) as exc:  # type: ignore[attr-defined]
        print("error: could not resolve the Prometheus release: %s" % exc, file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    archive = out_dir.parent / url.rsplit("/", 1)[-1]
    try:
        download(url, archive)
        binary = extract_member(archive, "promtool", out_dir)
    except (RuntimeError, urllib.error.URLError) as exc:  # type: ignore[attr-defined]
        print("error: could not download promtool %s: %s" % (tag, exc), file=sys.stderr)
        return 1
    finally:
        if archive.exists() and not args.keep_archive:
            archive.unlink()

    print("%s -> %s" % (tag, binary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
