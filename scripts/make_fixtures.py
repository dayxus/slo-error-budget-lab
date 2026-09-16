#!/usr/bin/env python3
"""Regenerate the synthetic sample fixtures under ``examples/samples/``.

The fixtures are what make this repository runnable offline: no Prometheus, no
cluster, no production data, yet the same math the engine applies to real time
series applies here. Each file is 30 days of 30-minute intervals of good/total
request counts for one service.

Generation is deterministic (fixed seed, fixed end timestamp, integer counts),
so re-running this script on any machine reproduces byte-identical files:

    python3 scripts/make_fixtures.py
    python3 scripts/make_fixtures.py --check   # fail if the files on disk differ

The three profiles are deliberately different from each other:

* ``checkout`` -- healthy: a slow error ratio, budget almost untouched.
* ``payments`` -- burning: a healthy baseline plus a one hour incident at the
  end of the series, which trips the fast burn alerts without exhausting the
  monthly budget yet.
* ``search`` -- already violated: a sustained error ratio above the objective,
  so the budget is overspent and every window burns slowly.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Tuple

STEP_SECONDS = 1800
DAYS = 30
SAMPLE_COUNT = DAYS * 86400 // STEP_SECONDS
END = datetime(2026, 9, 15, 0, 0, 0, tzinfo=timezone.utc)

Profile = Callable[[int, datetime, random.Random], float]


def checkout_ratio(index: int, timestamp: datetime, rng: random.Random) -> float:
    """Business hours carry a little more traffic and a little more error."""
    baseline = 6e-5 if 8 <= timestamp.hour < 20 else 4e-5
    return baseline * (1.0 + rng.uniform(-0.12, 0.12))


def payments_ratio(index: int, timestamp: datetime, rng: random.Random) -> float:
    """A calm baseline with a one hour incident closing the series."""
    if index >= SAMPLE_COUNT - 2:
        return 2e-2
    return 2e-4 * (1.0 + rng.uniform(-0.12, 0.12))


def search_ratio(index: int, timestamp: datetime, rng: random.Random) -> float:
    """Sustained degradation: above the objective, budget overspent."""
    return 7e-3 * (1.0 + rng.uniform(-0.05, 0.05))


PROFILES: Tuple[Dict[str, object], ...] = (
    {
        "file": "checkout-healthy.json",
        "service": "checkout",
        "sli": "availability",
        "total_per_step": 120000,
        "seed": 20260915,
        "ratio": checkout_ratio,
        "note": "synthetic healthy series: slow error ratio, budget almost untouched",
    },
    {
        "file": "payments-burning.json",
        "service": "payments",
        "sli": "availability",
        "total_per_step": 90000,
        "seed": 20260916,
        "ratio": payments_ratio,
        "note": "synthetic series with a one hour incident in the last window: fast burn, budget not exhausted",
    },
    {
        "file": "search-violated.json",
        "service": "search",
        "sli": "availability",
        "total_per_step": 60000,
        "seed": 20260917,
        "ratio": search_ratio,
        "note": "synthetic degraded series: error ratio above the objective, budget already overspent",
    },
)


def build_samples(profile: Dict[str, object]) -> List[Dict[str, object]]:
    rng = random.Random(int(profile["seed"]))
    ratio_fn: Profile = profile["ratio"]  # type: ignore[assignment]
    total_per_step = int(profile["total_per_step"])
    samples: List[Dict[str, object]] = []
    first = END - timedelta(seconds=(SAMPLE_COUNT - 1) * STEP_SECONDS)
    for index in range(SAMPLE_COUNT):
        timestamp = first + timedelta(seconds=index * STEP_SECONDS)
        ratio = max(0.0, ratio_fn(index, timestamp, rng))
        bad = int(round(ratio * total_per_step))
        bad = max(0, min(bad, total_per_step))
        samples.append(
            {
                "timestamp": timestamp,
                "good": total_per_step - bad,
                "total": total_per_step,
            }
        )
    return samples


def render(profile: Dict[str, object], samples: List[Dict[str, object]]) -> str:
    lines = [
        "{",
        '  "service": %s,' % json.dumps(profile["service"]),
        '  "sli": %s,' % json.dumps(profile["sli"]),
        '  "step_seconds": %d,' % STEP_SECONDS,
        '  "note": %s,' % json.dumps(profile["note"]),
        '  "samples": [',
    ]
    body = []
    for sample in samples:
        timestamp = sample["timestamp"].strftime("%Y-%m-%dT%H:%M:%SZ")  # type: ignore[union-attr]
        body.append('    {"timestamp": "%s", "good": %d, "total": %d}' % (timestamp, sample["good"], sample["total"]))
    lines.append(",\n".join(body))
    lines.append("  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Regenerate the synthetic sample fixtures.")
    parser.add_argument("--out", default="examples/samples", help="output directory")
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if the files on disk differ from a fresh generation"
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stale = []
    for profile in PROFILES:
        text = render(profile, build_samples(profile))
        path = out_dir / str(profile["file"])
        if args.check:
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != text:
                stale.append(str(path))
            continue
        path.write_text(text, encoding="utf-8")
        print("wrote %s (%d samples)" % (path, SAMPLE_COUNT))

    if args.check:
        if stale:
            print("fixtures are stale: %s" % ", ".join(stale), file=sys.stderr)
            return 1
        print("all fixtures match a fresh generation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
