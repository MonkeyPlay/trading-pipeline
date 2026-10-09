# RTH evaluations: forward-record health

Operational checks of the two frozen RTH evaluations (`rth_continuation_v2`, research only;
`rth_operational_v1`) and the live RTH analogue sets they are built from. Each entry is a
read-only check run with the production checkout's code (`rth_eval.status`, `rth_windows`):
counts, missing cases and timing only. **No predictive score is computed for, or read from,
these checks.** Both evaluations are scored once, at their endpoint (60 counted sessions or
2027-06-30).

In `status`, a case "counted" has no exclusion reason; the tool's own word for it is
`scored`, which means eligible for scoring later, not scored.

## 2026-10-09 (first session after registration, 13:31 UTC)

Checked 15:00 UTC, production `d588004`.

**Live RTH sets (`nq_match_rth_v2`):**

- **Windows:** all 60 first-hour windows (1–60 minutes) were issued live by Auto, one set
  each. None is missing, and none was revised.
- **First and last:** minute 1 was stored 13:42:29 UTC; minute 60 at 14:41:29 UTC.
- **Checkpoints:** 15, 30 and 60 are present (13:56:28, 14:11:28 and 14:41:29 UTC).

**`rth_continuation_v2` (research only):**

| Checkpoint | Cutoff (ET) | Stored (UTC) | Delay after cutoff | Window still ahead | Case |
|---|---|---|---|---|---|
| 15 | 09:45 | 13:56:28 | 11.5 min | 3.5 min | counted |
| 30 | 10:00 | 14:11:28 | 11.5 min | 3.5 min | counted |
| 45 | 10:15 | 14:26:30 | 11.5 min | 3.5 min | counted |

**`rth_operational_v1`:**

| Checkpoint | Window starts (ET) | Stored (UTC) | Lead to the window | Information age at the start | Case |
|---|---|---|---|---|---|
| 15 | 09:58 | 13:56:29 | +1.5 min | 13 min | counted |
| 30 | 10:13 | 14:11:29 | +1.5 min | 13 min | counted |
| 45 | 10:28 | 14:26:31 | +1.5 min | 13 min | counted |

**Missing cases:** none. Both evaluations have 3 of 3 opportunities, with no `no_forecast`,
`not_eligible`, `no_window` or other reason.

**Counted sessions toward the endpoint:** 1 of 60 for each.

**Timing:** the feed's delay shows plainly here. Each research forecast was stored with
3.5 of its 15 minutes left, which is why that evaluation is research only. Each operational
forecast was stored 1.5 minutes before its own window began, as defined.
