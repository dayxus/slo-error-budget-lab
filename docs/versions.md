# Versions of the upstream tooling this project is checked against

`sloctl` generates Prometheus rule files, so the only dependency with a real
compatibility surface is the Prometheus rule format itself. This table is kept
current by `.github/workflows/maintenance.yml` (weekly), which queries the
GitHub Releases API and rewrites the block below. The exact rule set generated
in each run is validated with that release's own `promtool`, so a PromQL syntax
change upstream surfaces as a failed job plus an issue, not as a surprise in
production.

<!-- versions:start -->

| Component | Latest release | Released | Last checked | Release notes |
| --- | --- | --- | --- | --- |
| Prometheus | `v3.14.0` | 2026-08-18 | 2026-09-21 | [prometheus/prometheus](https://github.com/prometheus/prometheus/releases/tag/v3.14.0) |
| Alertmanager | `v0.34.1` | 2026-09-17 | 2026-09-21 | [prometheus/alertmanager](https://github.com/prometheus/alertmanager/releases/tag/v0.34.1) |

<!-- versions:end -->

## What each entry means

| Column | Meaning |
| --- | --- |
| Component | Upstream project the project depends on at runtime |
| Latest release | Tag of the newest non-prerelease release on GitHub |
| Released | Publication date of that release |
| Last checked | Date the maintenance workflow last confirmed the entry |
| Release notes | Link to the release on GitHub |

## Compatibility policy

- The generated rules only use stable PromQL functions (`rate`, `sum`, arithmetic
  operators, `and`) and the stable rule-file schema, so any Prometheus `>= 2.40`
  is expected to accept them.
- The maintenance job always validates against the *newest* release. If a future
  release drops syntax the generator relies on, the job fails and opens an issue;
  no pin is added until a human decides what to do about it.
- `promtool` is downloaded from the official release asset for the runner
  architecture (linux-amd64); no third-party mirror is used.
