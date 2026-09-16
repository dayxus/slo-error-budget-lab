# SLO and error budget theory, as implemented by sloctl

This document is the specification of the math in `sloctl/engine.py`. If the code
and this document disagree, one of them is a bug: every number below is checked
by `tests/test_engine.py` with a hand-computed answer.

## 1. Definitions

| Term | Meaning | Where it lives |
| --- | --- | --- |
| SLI | A ratio of good events to total events, measured from a metric | `sli.good_query` / `sli.total_query` in the SLO YAML |
| SLO | An objective for that ratio over a rolling window | `objective` and `window` |
| Error budget | The amount of badness the objective allows inside the window | `SLO.budget_seconds` |
| Error ratio | Observed fraction of bad events | `engine.bad_ratio` |
| Budget consumed | Observed bad time, in seconds | `Budget.consumed_seconds` |
| Burn rate | How many times faster than allowed the budget is being spent | `engine.burn_rate` |
| Time to exhaustion | How long the remaining budget lasts at the current burn rate | `engine.eta_seconds` |

The engine never needs a Prometheus: it applies exactly these definitions to a
declarative SLO plus a series of `(timestamp, good, total)` samples. The same
definitions are compiled into PromQL by `sloctl/promql.py`, which is why the
offline numbers and the alerting rules cannot drift apart.

## 2. The formulas

### 2.1 Error budget

```
budget_seconds = window_seconds * (1 - objective)
```

A 99.9% objective over 30 days allows `2592000 * 0.001 = 2592` seconds of badness,
which is **43m 12s**. That is the number a "three nines" promise actually sells.

| Objective | Window | Budget |
| --- | --- | --- |
| 99.9% | 30d | 2592s = 43m 12s |
| 99.95% | 30d | 1296s = 21m 36s |
| 99.5% | 30d | 12960s = 3h 36m |
| 99.9% | 7d | 604.8s = 10m 5s |

### 2.2 Consumption

```
bad_ratio          = 1 - good / total
consumed_seconds   = bad_ratio * coverage_seconds
```

`coverage_seconds` is how much wall-clock time the sample series accounts for
(one `step_seconds` per sample). Reporting it is deliberate: a budget compared
against a series that only covers a week of a 30 day window is not the same
claim as a full window, and the CLI prints a `PARTIAL coverage` note instead of
hiding it.

```
consumed_ratio = consumed_seconds / budget_seconds
               = bad_ratio / (1 - objective)
status         = "compliant" if consumed_ratio <= 1 else "violated"
```

An SLO is missed exactly when the consumed time exceeds the budget, so an
evaluation that lands *on* the limit is still compliant. The boundary is tested
both ways in `tests/test_engine.py`.

### 2.3 Burn rate

```
burn_rate = bad_ratio(window) / (1 - objective)
```

Burn rate is dimensionless and window-relative: **1x means "at this rate the
entire budget is gone in exactly one window"**. 14.4x means it is gone in
`window / 14.4`. Because it is relative, the same expression works for a 5 minute
lookback and for a 3 day lookback, which is what makes multi-window alerting
possible.

Verdicts used by `sloctl burn`:

| Verdict | Condition |
| --- | --- |
| `ok` | burn rate < 1 |
| `slow burn` | 1 <= burn rate < the window's alert threshold |
| `fast burn` | burn rate >= the window's alert threshold (the alert would fire) |

`fast burn` therefore means "this window is at or above its own alerting
threshold", not "the biggest threshold in the table". For the 3d window the
threshold *is* 1, so that window can only be `ok` or `fast burn`.

### 2.4 Time to exhaustion

At burn rate `b`, badness accrues at `b * (1 - objective)` seconds per second, so
the remaining budget lasts:

```
eta_seconds = remaining_fraction * window_seconds / burn_rate
```

Two sanity checks, both in the test suite:

- `b = 1` with an untouched budget gives `window_seconds`: one full window of
  budget, exactly as the definition of burn rate promises.
- doubling `b` halves the ETA.

`burn_rate = 0` (nothing is failing) returns `stable` rather than an infinite
number, and a budget that is already overspent returns `exhausted` rather than a
negative estimate.

## 3. Multi-window multi-burn-rate alerting

A single fast-burn alert either pages for a blip or misses a slow bleed. The SRE
Workbook's answer, implemented in `sloctl/rules.py`, is to require two windows:
a long one that proves the budget really is being burned, and a short one that
proves it is still happening now.

| Burn rate | Long window | Short window | Severity | Budget consumed if sustained | `window / burn_rate` |
| --- | --- | --- | --- | --- | --- |
| 14.4x | 1h | 5m | page | 2% of a 30d budget | 2d 2h |
| 6x | 6h | 30m | ticket | 5% of a 30d budget | 5d |
| 1x | 3d | 6h | ticket | 10% of a 30d budget | 30d |

The "budget consumed" column is derived, not memorised: `burn_rate * window /
slo_window` gives `14.4 * 1h / 30d = 2%`, `6 * 6h / 30d = 5%` and
`1 * 3d / 30d = 10%`. The last column is `2.4` applied to a 30d window.

`sloctl burn` completes the picture with a 1d window at a 3x threshold
(`3 * 1d / 30d = 10%` per day, budget gone in 10 days) so that a burn which is
visible in a day but not in six hours still shows up in a report.

Generated alerting rule, for the payments service (`payments_alerts.rules.yml`):

```yaml
- alert: PaymentsSLOErrorBudgetBurn1h
  expr: |-
    payments:slo:burn_rate1h > 14.4
    and
    payments:slo:burn_rate5m > 14.4
  for: 2m
  labels:
    severity: page
    service: payments
    window: 1h
    short_window: 5m
    slo_target: '0.9995'
  annotations:
    runbook_url: https://github.com/dayxus/sre-runbooks-postmortem
```

## 4. Worked example: the three fixtures

`examples/samples/` holds a synthetic 30 day series per service (1440 samples of
30 minutes). The numbers below are the literal output of
`python3 -m sloctl budget` and `python3 -m sloctl burn` on this repository.

### checkout — healthy

99.9% over 30d, budget 43m 12s, consumed 2m 9s (5%), remaining 95%. Burn rate is
0.04x on every window, ETA measured in years: the objective is not at risk, and
the correct action is to spend the budget on risky change.

### payments — burning, not yet violated

99.95% over 30d, budget 21m 36s, consumed 9m 50s (45.5%), remaining 54.5%.

| Window | Burn rate | Verdict | ETA |
| --- | --- | --- | --- |
| 1h | 40.00x | fast burn (page) | 9h 48m |
| 6h | 6.99x | fast burn (ticket) | 2d 8h |
| 1d | 2.05x | slow burn | 7d 23h |
| 3d | 0.95x | ok | 17d 3h |

The baseline error ratio (0.02%) burns budget 0.4x — well inside the objective — and
a one hour incident at the end of the series pushes the short windows over their
thresholds. This is the case the alerting rules exist for: the monthly budget is
still half full, so a naive "remaining budget < 25%" alert would stay silent,
while the 1h window says the budget is gone in under ten hours.

### search — already violated

99.5% over 30d, budget 3h 36m, consumed 5h 2m, remaining -40%. A sustained 0.7%
error ratio against a 0.5% allowance burns 1.4x on every window, so
`consumed_ratio = 1.4 > 1` and the SLO is missed until the bad samples roll out of
the window. `eta` is `exhausted` on every row: there is nothing left to estimate.

## 5. Edge cases the engine treats as first class

| Case | Behaviour | Rationale |
| --- | --- | --- |
| Consumption exactly equal to the budget | `compliant`, remaining 0% | `<=` is the contract; the boundary is tested |
| Objective of 100% | Rejected by the config loader | A budget of zero cannot be measured, and it is not a promise anybody can keep |
| Zero traffic in a window | Error ratio 0, burn rate 0 | No requests means no failed requests; the alternative is a division by zero reported as an outage |
| Empty sample series | `EngineError: coverage must be positive` | Better a loud failure than a table full of zeroes |
| `good > total` | Rejected at load and in the engine, with both counts in the message | The SLI query is wrong, and the message says which fixture to look at |
| Samples shorter than the window | Numbers computed, `PARTIAL coverage` printed | The consumption is understated; saying so is cheaper than being wrong |
| Burn rate 0 | ETA `stable` | Not "infinite days", which reads like a bug |

## 6. What this model does not cover

- **Request-based SLIs only.** Window-based SLIs (e.g. "95% of requests faster
  than 300ms") need histogram queries; the ratio form here does not express them.
- **No time-series storage.** The evaluation is a batch job over samples you
  provide; the rolling window is whatever the file contains, not a live query.
- **No alert routing.** The generated rules carry `severity` and `team` labels and
  a `runbook_url`; wiring them to Alertmanager receivers is out of scope.
- **No burn-rate forecasting.** ETA is the current rate extended linearly, which
  is exactly what a burn rate means and exactly as naive as it sounds.
