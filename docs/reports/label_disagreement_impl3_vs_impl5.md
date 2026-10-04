# Label disagreement: impl3 against impl5

Generated 2026-10-04 14:06 UTC by `scripts/label_revision_report.py`, reading only; nothing was recorded. Every row is in the CSV beside this file.

- **Stored:** `nq_prompt_v2_1_impl3` outcomes on `nq_evidence_v3_r0929` snapshots (274 sessions); structure annotations `nq_structure_rules_v2` (v3 gives the same labels).
- **New:** `nq_prompt_v2_1_impl5` (FL-v3, OS-v2, LO-v2) on `nq_evidence_v5_r0929` snapshots (nq_conv_v5, strict completeness) built in memory; structure `nq_structure_rules_v4`.
- **Steps:** stored -> impl3 replayed from git (91ea3a5) on today's bars -> impl3 on the new snapshot -> impl5 on the new snapshot. The step where a label moves names the cause.
- **Checks:** the replay reproduces 6850 of 6850 stored session-targets.

## Summary

| target | same | changed | label -> other label | label -> unavailable | unavailable -> label | unavailable, other reason | unexplained |
|---|---|---|---|---|---|---|---|
| `first_move_5m` | 274 | 0 | 0 | 0 | 0 | 0 | 0 |
| `direction_15m` | 274 | 0 | 0 | 0 | 0 | 0 | 0 |
| `opening_type_15m` | 273 | 1 | 0 | 1 | 0 | 0 | 0 |
| `opening_bias_30m` | 274 | 0 | 0 | 0 | 0 | 0 | 0 |
| `close_direction_rth` | 274 | 0 | 0 | 0 | 0 | 0 | 0 |
| `session_type_rth` | 274 | 0 | 0 | 0 | 0 | 0 | 0 |
| `first_level_tested` | 123 | 151 | 87 | 42 | 22 | 0 | 0 |
| `first_level_outcome` | 155 | 119 | 55 | 42 | 22 | 0 | 0 |
| `ib_direction` | 274 | 0 | 0 | 0 | 0 | 0 | 0 |
| `opening_drive_strength` | 273 | 1 | 0 | 0 | 0 | 1 | 0 |
| `opening_range_extension` | 274 | 0 | 0 | 0 | 0 | 0 | 0 |
| `ib_extension` | 274 | 0 | 0 | 0 | 0 | 0 | 0 |
| `gap_outcome` | 274 | 0 | 0 | 0 | 0 | 0 | 0 |
| `session_high_timing` | 274 | 0 | 0 | 0 | 0 | 0 | 0 |
| `session_low_timing` | 274 | 0 | 0 | 0 | 0 | 0 | 0 |
| `morning_pullback` | 274 | 0 | 0 | 0 | 0 | 0 | 0 |
| `afternoon_continuation` | 274 | 0 | 0 | 0 | 0 | 0 | 0 |
| `opening_direction_matched` | 274 | 0 | 0 | 0 | 0 | 0 | 0 |
| `direction_15m_matched` | 274 | 0 | 0 | 0 | 0 | 0 | 0 |
| `first_15m_pattern` | 273 | 1 | 0 | 0 | 0 | 1 | 0 |
| `trend_persistence` | 274 | 0 | 0 | 0 | 0 | 0 | 0 |
| `on_high_outcome` | 264 | 10 | 8 | 2 | 0 | 0 | 0 |
| `on_low_outcome` | 264 | 10 | 8 | 2 | 0 | 0 | 0 |
| `prev_rth_high_outcome` | 265 | 9 | 9 | 0 | 0 | 0 | 0 |
| `prev_rth_low_outcome` | 269 | 5 | 5 | 0 | 0 | 0 | 0 |

Unavailable values show as `(reason)`. A session counts as "same" when the label or the reason is unchanged.

### Causes, all targets

| cause | session-targets |
|---|---|
| FL candidates: a new candidate reached first | 109 |
| follows the first level tested | 104 |
| LO-v2: a wick beyond the level is a breach | 45 |
| FL candidates: a new candidate on the other side in the same bar - ambiguous | 42 |
| snapshot: on_high, on_low changed | 7 |

## Label changes by target

### `opening_type_15m`

| stored | new | sessions |
|---|---|---|
| two_sided_whipsaw | (missing_reference) | 1 |

Why:

| cause | sessions | examples |
|---|---|---|
| snapshot: on_high, on_low changed | 1 | 2026-09-10: two_sided_whipsaw -> (missing_reference) - snapshot: on_high 29790.75 -> incomplete; on_low 29333.0 -> incomplete (rule sweep_low_rebound undecidable: on_high unavailable; on_low unavailable) |

### `first_level_tested`

| stored | new | sessions |
|---|---|---|
| vwap | (ambiguous_intrabar) | 21 |
| vwap | long_ma | 18 |
| (none_tested) | long_ma | 13 |
| on_low | premarket_low | 9 |
| on_high | long_ma | 8 |
| prev_rth_high | (ambiguous_intrabar) | 7 |
| vwap | premarket_low | 6 |
| vwap | premarket_high | 6 |
| prev_rth_high | long_ma | 5 |
| on_high | premarket_high | 5 |
| overnight_open | (ambiguous_intrabar) | 5 |
| on_low | long_ma | 4 |
| overnight_open | premarket_low | 4 |
| prev_rth_low | (ambiguous_intrabar) | 4 |
| prev_rth_close | premarket_low | 4 |
| (none_tested) | premarket_high | 4 |
| (none_tested) | premarket_low | 4 |
| prev_rth_close | long_ma | 3 |
| prev_rth_low | premarket_high | 3 |
| on_high | premarket_low | 2 |
| prev_rth_high | premarket_low | 2 |
| on_high | (ambiguous_intrabar) | 2 |
| prev_rth_high | premarket_high | 2 |
| prev_rth_close | (ambiguous_intrabar) | 2 |
| prev_rth_low | long_ma | 2 |
| overnight_open | long_ma | 2 |
| overnight_open | premarket_high | 1 |
| prev_rth_close | premarket_high | 1 |
| on_low | (ambiguous_intrabar) | 1 |
| (coincident_levels) | premarket_low | 1 |

Why:

| cause | sessions | examples |
|---|---|---|
| FL candidates: a new candidate reached first | 109 | 2025-09-03: vwap -> long_ma - FL candidates: Long MA (not an impl3 candidate) reached first<br>2025-09-04: vwap -> premarket_low - FL candidates: Premarket Low (not an impl3 candidate) reached first |
| FL candidates: a new candidate on the other side in the same bar - ambiguous | 42 | 2025-09-09: vwap -> (ambiguous_intrabar) - FL candidates: long_ma, premarket_high on the other side of the open in the same bar - ambiguous (estimate vwap at 23834.318315; levels on both sides of the 09:30 bar's open reached: long_ma, premarket_high, vwap)<br>2025-09-16: vwap -> (ambiguous_intrabar) - FL candidates: premarket_low on the other side of the open in the same bar - ambiguous (estimate premarket_low at 24584.5; levels on both sides of the 09:30 bar's open reached: premarket_low, vwap) |

### `first_level_outcome`

| stored | new | sessions |
|---|---|---|
| test_rejection | break_reclaim_acceptance | 23 |
| break_acceptance | (upstream_unavailable) | 15 |
| test_rejection | (upstream_unavailable) | 12 |
| (upstream_unavailable) | break_reclaim_acceptance | 11 |
| (upstream_unavailable) | break_acceptance | 10 |
| break_without_acceptance | (upstream_unavailable) | 9 |
| break_acceptance | break_reclaim_acceptance | 8 |
| break_reclaim_acceptance | break_without_acceptance | 7 |
| break_reclaim_acceptance | break_acceptance | 6 |
| break_reclaim_acceptance | (upstream_unavailable) | 6 |
| break_without_acceptance | break_acceptance | 5 |
| break_without_acceptance | break_reclaim_acceptance | 3 |
| test_rejection | break_acceptance | 2 |
| (upstream_unavailable) | break_without_acceptance | 1 |
| test_rejection | break_without_acceptance | 1 |

Why:

| cause | sessions | examples |
|---|---|---|
| follows the first level tested | 104 | 2025-09-03: break_without_acceptance -> break_reclaim_acceptance - follows the first level tested<br>2025-09-09: break_without_acceptance -> (upstream_unavailable) - follows the first level tested (first level tested unavailable (ambiguous_intrabar)) |
| LO-v2: a wick beyond the level is a breach | 15 | 2025-09-05: test_rejection -> break_reclaim_acceptance - LO-v2: a wick beyond the level is a breach<br>2025-09-12: test_rejection -> break_reclaim_acceptance - LO-v2: a wick beyond the level is a breach |

### `opening_drive_strength`

| stored | new | sessions |
|---|---|---|
| (not_applicable) | (upstream_unavailable) | 1 |

Why:

| cause | sessions | examples |
|---|---|---|
| snapshot: on_high, on_low changed | 1 | 2026-09-10: (not_applicable) -> (upstream_unavailable) - snapshot: on_high 29790.75 -> incomplete; on_low 29333.0 -> incomplete (opening type unavailable (missing_reference)) |

### `first_15m_pattern`

| stored | new | sessions |
|---|---|---|
| (uncovered) | (upstream_unavailable) | 1 |

Why:

| cause | sessions | examples |
|---|---|---|
| snapshot: on_high, on_low changed | 1 | 2026-09-10: (uncovered) -> (upstream_unavailable) - snapshot: on_high 29790.75 -> incomplete; on_low 29333.0 -> incomplete (opening type unavailable (missing_reference): drive or sweep unknown) |

### `on_high_outcome`

| stored | new | sessions |
|---|---|---|
| test_rejection | break_reclaim_acceptance | 8 |
| not_tested | (missing_reference) | 2 |

Why:

| cause | sessions | examples |
|---|---|---|
| LO-v2: a wick beyond the level is a breach | 8 | 2025-09-05: test_rejection -> break_reclaim_acceptance - LO-v2: a wick beyond the level is a breach<br>2025-11-03: test_rejection -> break_reclaim_acceptance - LO-v2: a wick beyond the level is a breach |
| snapshot: on_high, on_low changed | 2 | 2025-12-11: not_tested -> (missing_reference) - snapshot: on_high 26047.5 -> incomplete; on_low 25644.5 -> incomplete (on_high unavailable)<br>2026-09-10: not_tested -> (missing_reference) - snapshot: on_high 29790.75 -> incomplete; on_low 29333.0 -> incomplete (on_high unavailable) |

### `on_low_outcome`

| stored | new | sessions |
|---|---|---|
| test_rejection | break_reclaim_acceptance | 8 |
| not_tested | (missing_reference) | 2 |

Why:

| cause | sessions | examples |
|---|---|---|
| LO-v2: a wick beyond the level is a breach | 8 | 2025-12-22: test_rejection -> break_reclaim_acceptance - LO-v2: a wick beyond the level is a breach<br>2026-01-09: test_rejection -> break_reclaim_acceptance - LO-v2: a wick beyond the level is a breach |
| snapshot: on_high, on_low changed | 2 | 2025-12-11: not_tested -> (missing_reference) - snapshot: on_high 26047.5 -> incomplete; on_low 25644.5 -> incomplete (on_low unavailable)<br>2026-09-10: not_tested -> (missing_reference) - snapshot: on_high 29790.75 -> incomplete; on_low 29333.0 -> incomplete (on_low unavailable) |

### `prev_rth_high_outcome`

| stored | new | sessions |
|---|---|---|
| test_rejection | break_reclaim_acceptance | 9 |

Why:

| cause | sessions | examples |
|---|---|---|
| LO-v2: a wick beyond the level is a breach | 9 | 2025-09-09: test_rejection -> break_reclaim_acceptance - LO-v2: a wick beyond the level is a breach<br>2025-11-26: test_rejection -> break_reclaim_acceptance - LO-v2: a wick beyond the level is a breach |

### `prev_rth_low_outcome`

| stored | new | sessions |
|---|---|---|
| test_rejection | break_reclaim_acceptance | 5 |

Why:

| cause | sessions | examples |
|---|---|---|
| LO-v2: a wick beyond the level is a breach | 5 | 2025-11-13: test_rejection -> break_reclaim_acceptance - LO-v2: a wick beyond the level is a breach<br>2025-12-09: test_rejection -> break_reclaim_acceptance - LO-v2: a wick beyond the level is a breach |


## Structure annotation: nq_structure_rules_v2 against nq_structure_rules_v4

Values compared (3288 session-fields); a basis-only change is not a difference. The rule step is nq_structure_rules_v4 on the stored snapshot, the snapshot step the same rules on the new one.

| field | changed |
|---|---|
| Overnight Structure | 2 |
| Price vs Long MA | 2 |
| Long MA Slope | 2 |
| Fast MA Alignment | 2 |
| Chop Score | 2 |

## Structure changes by field

### Overnight Structure

| stored | new | sessions |
|---|---|---|
| V-reversal | (the overnight window is incomplete: 928 of 929 minutes (ratio 0.9989)) | 1 |
| Downtrend | (the overnight window is incomplete: 928 of 929 minutes (ratio 0.9989)) | 1 |

Why:

| cause | sessions | examples |
|---|---|---|
| rules v4: the window is incomplete | 2 | 2025-12-11: V-reversal -> (the overnight window is incomplete: 928 of 929 minutes (ratio 0.9989)) - rules v4: the window is incomplete<br>2026-09-10: Downtrend -> (the overnight window is incomplete: 928 of 929 minutes (ratio 0.9989)) - rules v4: the window is incomplete |

### Price vs Long MA

| stored | new | sessions |
|---|---|---|
| Above/crossing | (the moving averages' 2m history is incomplete: incomplete 2m bucket(s) 23:00Z) | 1 |
| Below | (the moving averages' 2m history is incomplete: incomplete 2m bucket(s) 22:00Z) | 1 |

Why:

| cause | sessions | examples |
|---|---|---|
| rules v4: the window is incomplete | 2 | 2025-12-11: Above/crossing -> (the moving averages' 2m history is incomplete: incomplete 2m bucket(s) 23:00Z) - rules v4: the window is incomplete<br>2026-09-10: Below -> (the moving averages' 2m history is incomplete: incomplete 2m bucket(s) 22:00Z) - rules v4: the window is incomplete |

### Long MA Slope

| stored | new | sessions |
|---|---|---|
| Rising | (the moving averages' 2m history is incomplete: incomplete 2m bucket(s) 23:00Z) | 1 |
| Falling | (the moving averages' 2m history is incomplete: incomplete 2m bucket(s) 22:00Z) | 1 |

Why:

| cause | sessions | examples |
|---|---|---|
| rules v4: the window is incomplete | 2 | 2025-12-11: Rising -> (the moving averages' 2m history is incomplete: incomplete 2m bucket(s) 23:00Z) - rules v4: the window is incomplete<br>2026-09-10: Falling -> (the moving averages' 2m history is incomplete: incomplete 2m bucket(s) 22:00Z) - rules v4: the window is incomplete |

### Fast MA Alignment

| stored | new | sessions |
|---|---|---|
| Bullish | (the moving averages' 2m history is incomplete: incomplete 2m bucket(s) 23:00Z) | 1 |
| Bearish | (the moving averages' 2m history is incomplete: incomplete 2m bucket(s) 22:00Z) | 1 |

Why:

| cause | sessions | examples |
|---|---|---|
| rules v4: the window is incomplete | 2 | 2025-12-11: Bullish -> (the moving averages' 2m history is incomplete: incomplete 2m bucket(s) 23:00Z) - rules v4: the window is incomplete<br>2026-09-10: Bearish -> (the moving averages' 2m history is incomplete: incomplete 2m bucket(s) 22:00Z) - rules v4: the window is incomplete |

### Chop Score

| stored | new | sessions |
|---|---|---|
| 1 | (the moving averages' 2m history is incomplete: incomplete 2m bucket(s) 23:00Z) | 1 |
| 0 | (the moving averages' 2m history is incomplete: incomplete 2m bucket(s) 22:00Z) | 1 |

Why:

| cause | sessions | examples |
|---|---|---|
| rules v4: the window is incomplete | 2 | 2025-12-11: 1 -> (the moving averages' 2m history is incomplete: incomplete 2m bucket(s) 23:00Z) - rules v4: the window is incomplete<br>2026-09-10: 0 -> (the moving averages' 2m history is incomplete: incomplete 2m bucket(s) 22:00Z) - rules v4: the window is incomplete |


## New snapshots with something unavailable

| session | stored (nq_evidence_v3_r0929) | new (nq_evidence_v5_r0929) |
|---|---|---|
| 2025-09-02 | daily ATR insufficient_history | daily ATR incomplete_history |
| 2025-09-03 | daily ATR insufficient_history | daily ATR incomplete_history |
| 2025-09-04 | daily ATR insufficient_history | daily ATR incomplete_history |
| 2025-09-05 | daily ATR insufficient_history | daily ATR incomplete_history |
| 2025-09-08 | daily ATR insufficient_history | daily ATR incomplete_history |
| 2025-09-09 | daily ATR insufficient_history | daily ATR incomplete_history |
| 2025-09-10 | daily ATR insufficient_history | daily ATR incomplete_history |
| 2025-09-11 | daily ATR insufficient_history | daily ATR incomplete_history |
| 2025-09-12 | daily ATR insufficient_history | daily ATR incomplete_history |
| 2025-09-15 | daily ATR insufficient_history | daily ATR incomplete_history |
| 2025-09-16 | daily ATR insufficient_history | daily ATR incomplete_history |
| 2025-09-17 | daily ATR insufficient_history | daily ATR incomplete_history |
| 2025-09-18 | daily ATR insufficient_history | daily ATR incomplete_history |
| 2025-09-19 | daily ATR insufficient_history | daily ATR incomplete_history |
| 2025-09-22 | daily ATR insufficient_history | daily ATR incomplete_history |
| 2025-09-23 | daily ATR insufficient_history | daily ATR incomplete_history |
| 2025-09-24 | daily ATR insufficient_history | daily ATR incomplete_history |
| 2025-09-25 | daily ATR insufficient_history | daily ATR incomplete_history |
| 2025-09-26 | daily ATR insufficient_history | daily ATR incomplete_history |
| 2025-12-11 | overnight_open missing; overnight 928 of 929 minutes | on_high incomplete; on_low incomplete; overnight_open missing; vwap incomplete; overnight 928 of 929 minutes |
| 2026-09-10 | overnight_open missing; overnight 928 of 929 minutes | on_high incomplete; on_low incomplete; overnight_open missing; vwap incomplete; overnight 928 of 929 minutes |
