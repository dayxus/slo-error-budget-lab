# Weekly maintenance audit

Produced by `scripts/weekly_audit.py` from the scheduled maintenance workflow.
Everything below was executed in this run; nothing is carried over from a previous one.

- Run (UTC): 2026-10-05T14:49:18Z
- `promtool` release in use: `v3.15.0`
- `promtool check rules`: **success**
- Generated rule files checked: 6
- Test suite: 148 passed in 4.17s
- Coverage of `sloctl/`: 89%

## Upstream versions

| Component | Version |
| --- | --- |
| Prometheus | `v3.15.0` |
| Alertmanager | `v0.34.1` |

## promtool output

```text
Checking build/rules/checkout_alerts.rules.yml
  SUCCESS: 3 rules found
Checking build/rules/checkout_recording.rules.yml
  SUCCESS: 16 rules found
Checking build/rules/payments_alerts.rules.yml
  SUCCESS: 3 rules found
Checking build/rules/payments_recording.rules.yml
  SUCCESS: 16 rules found
Checking build/rules/search_alerts.rules.yml
  SUCCESS: 3 rules found
Checking build/rules/search_recording.rules.yml
  SUCCESS: 16 rules found
```

## What this run did

1. Resolved the newest Prometheus and Alertmanager releases from the GitHub Releases API
   and rewrote the version block of `docs/versions.md`.
2. Downloaded that exact Prometheus release's `promtool` and validated every file produced by
   `sloctl rules generate` for the three example SLOs.
3. Ran the test suite with coverage, whose result is recorded above.
4. Committed this report and the version table only if the content changed.
