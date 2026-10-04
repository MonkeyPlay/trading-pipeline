# Label disagreement: v5 against NQ-v2

Generated 2026-10-03 20:57 UTC by `scripts/label_disagreement_report.py` (guideline stage 1E). Every row is in the CSV beside this file.

- **Old:** `nq_labels_v5_candidate` on `nq_features_v3` snapshots, restored from the pre-0008 backup (374 sessions).
- **New:** `nq_prompt_v2_1_impl2` outcomes recorded on `nq_evidence_v1_r0929` snapshots; the IB direction (no recorded counterpart in nq_prompt_v2_1_impl2) from `nq_prompt_v2_1_impl3`, recomputed.
- **Compared:** the 269 sessions both have, 2025-09-02 to 2026-09-25.
- **Checks:** recomputing NQ-v2 reproduces every recorded label: yes. v5 replayed on today's bars with its own code (commit f00a0c1) reproduces the stored v5 label on 1614 of 1614 session-targets. The frozen daily ATR A agrees within 0.000001 on 250 of 250 sessions (largest difference 0.00 points).

## Summary

| v5 -> NQ-v2 | agree | disagree | only NQ-v2 labelled | only v5 labelled | both unavailable | unexplained |
|---|---|---|---|---|---|---|
| `first_move_5m` -> `first_move_5m` | 90 | 159 | 9 | 0 | 11 | 0 |
| `direction_15m` -> `direction_15m` | 182 | 68 | 19 | 0 | 0 | 0 |
| `opening_type_15m` -> `opening_type_15m` | 61 | 189 | 19 | 0 | 0 | 0 |
| `direction_1h` -> `ib_direction` | 200 | 50 | 19 | 0 | 0 | 0 |
| `direction_rth` -> `close_direction_rth` | 195 | 53 | 0 | 0 | 21 | 0 |
| `session_type_rth` -> `session_type_rth` | 102 | 146 | 0 | 0 | 21 | 0 |

Labels compare through the canonical mapping (v5 up / down / flat = bullish / bearish / neutral_band; two_sided = two_sided_whipsaw; the session-type names). An NQ-v2 `ambiguous_intrabar` or `uncovered` is counted as a disagreement, since it is a definition outcome, not missing data.

Not compared: v5 (no NQ-v2 counterpart): first_break_1h, range_15m_regime, range_1h_regime, range_rth_regime; NQ-v2 opening bias, first level, LO-v1 and the descriptors have no v5 counterpart.

## `first_move_5m` -> `first_move_5m`

Rows v5, columns NQ-v2:

| v5 \ NQ-v2 | (ambiguous_intrabar) | down_first | up_first |
|---|---|---|---|
| (ambiguous_intrabar) | 1 |  |  |
| (invalid_reference) | 10 | 4 | 5 |
| down_first | 60 | 49 | 1 |
| neither | 27 | 5 | 10 |
| up_first | 54 | 2 | 41 |

Why they differ:

| cause | sessions | examples |
|---|---|---|
| threshold: T vs 0.10 A | 159 | 2025-09-30: v5 down_first, NQ-v2 (ambiguous_intrabar) - T = 4 pts, v5 barrier 0.10A = 24.69 pts; with v5's barrier NQ-v2 gives v5's label<br>2025-10-01: v5 up_first, NQ-v2 (ambiguous_intrabar) - T = 5 pts, v5 barrier 0.10A = 24.28 pts; with v5's barrier NQ-v2 gives v5's label |
| v5 unavailable: invalid_reference | 9 | 2025-09-02: v5 (invalid_reference), NQ-v2 down_first - <br>2025-09-08: v5 (invalid_reference), NQ-v2 up_first -  |

## `direction_15m` -> `direction_15m`

Rows v5, columns NQ-v2:

| v5 \ NQ-v2 | bearish | bullish | neutral_band |
|---|---|---|---|
| (invalid_reference) | 10 | 7 | 2 |
| down | 72 |  |  |
| flat | 33 | 35 | 30 |
| up |  | 80 |  |

Why they differ:

| cause | sessions | examples |
|---|---|---|
| threshold: T vs 1/10 A | 68 | 2025-09-30: v5 flat, NQ-v2 bearish - close - O = -22.25 pts; NQ-v2 band T = 4 pts, v5 band 1/10A = 24.69 pts<br>2025-10-07: v5 flat, NQ-v2 bullish - close - O = 8.75 pts; NQ-v2 band T = 5 pts, v5 band 1/10A = 24.34 pts |
| v5 unavailable: invalid_reference | 19 | 2025-09-02: v5 (invalid_reference), NQ-v2 bullish - <br>2025-09-03: v5 (invalid_reference), NQ-v2 bearish -  |

## `opening_type_15m` -> `opening_type_15m`

Rows v5, columns NQ-v2:

| v5 \ NQ-v2 | opening_drive_down | opening_drive_up | range | sweep_high_reverse | sweep_low_rebound | two_sided_whipsaw |
|---|---|---|---|---|---|---|
| (invalid_reference) | 2 | 1 |  | 1 | 1 | 14 |
| drive_down | 11 |  |  | 1 |  | 14 |
| drive_up |  | 10 |  |  |  | 13 |
| mixed | 9 | 2 | 8 | 2 | 6 | 69 |
| range |  |  | 5 | 2 | 2 | 43 |
| sweep_high_reverse |  |  |  | 3 |  | 3 |
| sweep_low_rebound |  |  |  |  | 3 |  |
| two_sided |  |  |  | 5 | 10 | 29 |

Why they differ:

| cause | sessions | examples |
|---|---|---|
| v5 'mixed' has no NQ-v2 class | 96 | 2025-09-30: v5 mixed, NQ-v2 two_sided_whipsaw - NQ-v2: two_sided_whipsaw<br>2025-10-01: v5 mixed, NQ-v2 two_sided_whipsaw - NQ-v2: two_sided_whipsaw |
| threshold: both O + T and O - T reached (v5 two-sided needs 0.12 A each way) | 43 | 2025-10-03: v5 range, NQ-v2 two_sided_whipsaw - H15 - O = 32.0, O - L15 = 20.0, T = 4<br>2025-10-07: v5 range, NQ-v2 two_sided_whipsaw - H15 - O = 26.25, O - L15 = 20.0, T = 5 |
| v5 unavailable: invalid_reference | 19 | 2025-09-02: v5 (invalid_reference), NQ-v2 sweep_low_rebound - <br>2025-09-03: v5 (invalid_reference), NQ-v2 two_sided_whipsaw -  |
| opening_drive_down fails under NQ-v2 | 15 | 2025-10-06: v5 drive_down, NQ-v2 two_sided_whipsaw - counter-excursion H15 - O = 7.50 > T; T = 5<br>2025-10-09: v5 drive_down, NQ-v2 two_sided_whipsaw - counter-excursion H15 - O = 5.75 > T; T = 4 |
| NQ-v2 rule order | 15 | 2025-11-14: v5 two_sided, NQ-v2 sweep_low_rebound - two_sided_whipsaw also holds under NQ-v2, which checks sweep_low_rebound first<br>2025-11-18: v5 two_sided, NQ-v2 sweep_high_reverse - two_sided_whipsaw also holds under NQ-v2, which checks sweep_high_reverse first |
| opening_drive_up fails under NQ-v2 | 13 | 2025-10-08: v5 drive_up, NQ-v2 two_sided_whipsaw - counter-excursion O - L15 = 5.75 > T; T = 5<br>2025-10-23: v5 drive_up, NQ-v2 two_sided_whipsaw - counter-excursion O - L15 = 17.25 > T; T = 7 |
| sweep definition: NQ-v2 adds the previous-RTH levels, breach >= T | 4 | 2025-11-05: v5 range, NQ-v2 sweep_high_reverse - v5 range; T = 8; v5 sweeps used the ON level only, breach 0.02 A<br>2026-04-09: v5 range, NQ-v2 sweep_low_rebound - v5 range; T = 7; v5 sweeps used the ON level only, breach 0.02 A |
| sweep_high_reverse fails under NQ-v2 | 3 | 2026-01-05: v5 sweep_high_reverse, NQ-v2 two_sided_whipsaw - no frozen reference (ON and previous-RTH high / low) breached by >= T, reclaimed and not gap-crossed; v5 used the ON level only, with a 0.02 A breach<br>2026-07-21: v5 sweep_high_reverse, NQ-v2 two_sided_whipsaw - no frozen reference (ON and previous-RTH high / low) breached by >= T, reclaimed and not gap-crossed; v5 used the ON level only, with a 0.02 A breach |

## `direction_1h` -> `ib_direction`

Rows v5, columns NQ-v2:

| v5 \ NQ-v2 | bearish | bullish | neutral_band |
|---|---|---|---|
| (invalid_reference) | 9 | 9 | 1 |
| down | 91 |  |  |
| flat | 18 | 32 | 14 |
| up |  | 95 |  |

Why they differ:

| cause | sessions | examples |
|---|---|---|
| threshold: T vs 1/10 A | 50 | 2025-10-07: v5 flat, NQ-v2 bearish - close - O = -23.75 pts; NQ-v2 band T = 5 pts, v5 band 1/10A = 24.34 pts<br>2025-10-10: v5 flat, NQ-v2 bullish - close - O = 21.75 pts; NQ-v2 band T = 4 pts, v5 band 1/10A = 24.43 pts |
| v5 unavailable: invalid_reference | 19 | 2025-09-02: v5 (invalid_reference), NQ-v2 bullish - <br>2025-09-03: v5 (invalid_reference), NQ-v2 bullish -  |

## `direction_rth` -> `close_direction_rth`

Rows v5, columns NQ-v2:

| v5 \ NQ-v2 | (missing_threshold) | (shortened_session) | bearish | bullish | neutral_band |
|---|---|---|---|---|---|
| (invalid_reference) | 19 |  |  |  |  |
| (shortened_session) |  | 2 |  |  |  |
| down |  |  | 85 |  |  |
| flat |  |  | 30 | 23 | 13 |
| up |  |  |  | 97 |  |

Why they differ:

| cause | sessions | examples |
|---|---|---|
| threshold: B vs 1/5 A | 53 | 2025-09-29: v5 flat, NQ-v2 bearish - close - O = -13.50 pts; NQ-v2 band B = 13 pts, v5 band 1/5A = 49.32 pts<br>2025-10-06: v5 flat, NQ-v2 bearish - close - O = -30.00 pts; NQ-v2 band B = 13 pts, v5 band 1/5A = 48.48 pts |

## `session_type_rth` -> `session_type_rth`

Rows v5, columns NQ-v2:

| v5 \ NQ-v2 | (missing_threshold) | (shortened_session) | (uncovered) | bear_trend_day | bull_trend_day | range_day | reversal_day | two_sided_volatile_day |
|---|---|---|---|---|---|---|---|---|
| (invalid_reference) | 19 |  |  |  |  |  |  |  |
| (shortened_session) |  | 2 |  |  |  |  |  |  |
| bear_trend |  |  | 2 | 19 |  |  | 4 |  |
| bull_trend |  |  | 6 |  | 32 |  | 5 |  |
| mixed |  |  | 59 | 4 | 5 | 4 | 6 | 5 |
| range |  |  | 15 | 1 |  | 37 | 14 |  |
| reversal |  |  | 1 |  |  |  | 12 |  |
| two_sided_volatile |  |  | 1 |  |  | 2 | 12 | 2 |

Why they differ:

| cause | sessions | examples |
|---|---|---|
| v5 'mixed' has no NQ-v2 class | 83 | 2025-09-30: v5 mixed, NQ-v2 reversal_day - NQ-v2: reversal_day; close bullish, E = 0.404700, CL = 0.917755, R/A = 0.78<br>2025-10-03: v5 mixed, NQ-v2 (uncovered) - NQ-v2: uncovered; close bearish, E = 0.559755, CL = 0.299285, R/A = 1.01 |
| NQ-v2 rule order | 22 | 2025-09-29: v5 range, NQ-v2 reversal_day - range_day also holds under P2, which checks reversal_day first; close bearish, E = 0.069231, CL = 0.291026, R/A = 0.79<br>2025-10-10: v5 bear_trend, NQ-v2 reversal_day - bear_trend_day also holds under P2, which checks reversal_day first; close bearish, E = 0.907098, CL = 0.018431, R/A = 4.11 |
| no P2 rule fits (NQ-v2 uncovered; v5 range) | 15 | 2025-12-02: v5 range, NQ-v2 (uncovered) - close bullish, E = 0.421722, CL = 0.727984, R/A = 0.55<br>2025-12-04: v5 range, NQ-v2 (uncovered) - close bearish, E = 0.375697, CL = 0.619844, R/A = 0.51 |
| two_sided_volatile_day fails under P2 | 8 | 2025-11-25: v5 two_sided_volatile, NQ-v2 reversal_day - E = 0.42 >= 0.35; NQ-v2 gives reversal_day<br>2026-01-30: v5 two_sided_volatile, NQ-v2 reversal_day - E = 0.43 >= 0.35; NQ-v2 gives reversal_day |
| no P2 rule fits (NQ-v2 uncovered; v5 bull_trend) | 6 | 2025-10-20: v5 bull_trend, NQ-v2 (uncovered) - close bullish, E = 0.758081, CL = 0.773723, R/A = 0.67<br>2025-11-10: v5 bull_trend, NQ-v2 (uncovered) - close bullish, E = 0.564830, CL = 0.846029, R/A = 0.80 |
| bull_trend_day fails under P2 | 4 | 2026-01-09: v5 bull_trend, NQ-v2 reversal_day - E = 0.59 < 0.60; NQ-v2 gives reversal_day<br>2026-02-24: v5 bull_trend, NQ-v2 reversal_day - E = 0.59 < 0.60; NQ-v2 gives reversal_day |
| bear_trend_day fails under P2 | 3 | 2026-01-02: v5 bear_trend, NQ-v2 reversal_day - E = 0.56 < 0.60, CL = 0.22 > 0.20; NQ-v2 gives reversal_day<br>2026-03-13: v5 bear_trend, NQ-v2 reversal_day - E = 0.54 < 0.60; NQ-v2 gives reversal_day |
| no P2 rule fits (NQ-v2 uncovered; v5 bear_trend) | 2 | 2025-10-07: v5 bear_trend, NQ-v2 (uncovered) - close bearish, E = 0.684821, CL = 0.216071, R/A = 1.15<br>2026-03-30: v5 bear_trend, NQ-v2 (uncovered) - close bearish, E = 0.726034, CL = 0.216545, R/A = 1.18 |
| no P2 rule fits (NQ-v2 uncovered; v5 two_sided_volatile) | 1 | 2025-10-16: v5 two_sided_volatile, NQ-v2 (uncovered) - close bearish, E = 0.400653, CL = 0.348881, R/A = 1.57 |
| no P2 rule fits (NQ-v2 uncovered; v5 reversal) | 1 | 2026-04-09: v5 reversal, NQ-v2 (uncovered) - close bullish, E = 0.542441, CL = 0.932094, R/A = 0.63 |
| range_day fails under P2 | 1 | 2026-08-12: v5 range, NQ-v2 bear_trend_day - E = 0.76 >= 0.35; NQ-v2 gives bear_trend_day |

