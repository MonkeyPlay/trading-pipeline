# Forward record: fan_intermarket_v2_lin_pois_ivx_frozen

Code 797d6a83004c; as of 2026-10-06 23:19 UTC. Every issue is classified under the rule version it was made under (its stored parameters). Live horizons are the live evaluation; delayed-origin ones are the delayed-feed evaluation, a different experiment - never evidence about live forecasts; late, expired and issues made under no rules are research.

## Rules `fan_intermarket_v2_lin_pois_ivx_frozen_forward_v1`

Active 2026-10-06 20:05 UTC to 2026-10-06 20:34 UTC. **Operations:** 1 session(s) expected, 1 attempted; 2 marks expected by the calendar, 2 attempted, 1 issued (3 issues in all), 1 stale, 0 failed, **0 missed**; from each session's first attempt: 2 expected, 0 missed.

| Horizon | Class | Issued | Scored | Sessions | v2 CRPS | Model CRPS | Model minus v2 | 95 % interval | 90 % band covers (v2 / model) | Origin bar revised |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 min | expired | 3 | 3 | 1 | 0.5552 | 0.5610 | +1.05 % | too few sessions | 100.0 % / 100.0 % | 33.3 % (largest 0.79 bps) |
| 5 min | expired | 3 | 3 | 1 | 1.3708 | 1.3552 | -1.14 % | too few sessions | 100.0 % / 100.0 % | 33.3 % (largest 0.79 bps) |
| 15 min | v1 delayed | 2 | 2 | 1 | 1.7683 | 1.7718 | +0.20 % | too few sessions | 100.0 % / 100.0 % | 50.0 % (largest 0.79 bps) |
| 15 min | expired | 1 | 1 | 1 | 4.8964 | 4.9252 | +0.59 % | too few sessions | 100.0 % / 100.0 % | 0.0 % (largest 0.00 bps) |
| 20 min | v1 delayed | 2 | 2 | 1 | 2.6644 | 2.7217 | +2.15 % | too few sessions | 100.0 % / 100.0 % | 50.0 % (largest 0.79 bps) |
| 20 min | expired | 1 | 1 | 1 | 3.3797 | 3.2174 | -4.80 % | too few sessions | 100.0 % / 100.0 % | 0.0 % (largest 0.00 bps) |
| 30 min | v1 delayed | 3 | 3 | 1 | 2.2286 | 2.1097 | -5.34 % | too few sessions | 100.0 % / 100.0 % | 33.3 % (largest 0.79 bps) |
| 60 min | v1 'on time' (a delayed-origin class) | 1 | 1 | 1 | 3.8604 | 3.8736 | +0.34 % | too few sessions | 100.0 % / 100.0 % | 0.0 % (largest 0.00 bps) |
| 60 min | v1 delayed | 1 | 1 | 1 | 3.3978 | 3.0726 | -9.57 % | too few sessions | 100.0 % / 100.0 % | 0.0 % (largest 0.00 bps) |

**Timing**, in seconds after the mark (median / 90th percentile / largest); the live deadline is none under these rules. When the origin bar reached the store is the feed's part; the trigger's start, the collection and the computation are the pipeline's.

| Trigger | Marks tried | Issued | Found the origin bar missing | Failed | Within the deadline | Issued after | Trigger started after | Collection | Computation | Origin bar stored after |
|---|---|---|---|---|---|---|---|---|---|---|
| not recorded (runs before triggers were) | 4 | 3 | 2 | 0 | - | 684 / 1180 / 1304 | - | - | - | 667 / 1163 / 1287 |

## Rules `fan_intermarket_v2_lin_pois_ivx_frozen_forward_v2`

Active 2026-10-06 20:34 UTC to 2026-10-06 23:19 UTC. **Operations:** 2 session(s) expected, 2 attempted; 6 marks expected by the calendar, 6 attempted, 3 issued (4 issues in all), 3 stale, 0 failed, **0 missed**; from each session's first attempt: 6 expected, 0 missed.

| Horizon | Class | Issued | Scored | Sessions | v2 CRPS | Model CRPS | Model minus v2 | 95 % interval | 90 % band covers (v2 / model) | Origin bar revised |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 min | expired | 4 | 2 | 1 | 0.5411 | 0.5110 | -5.55 % | too few sessions | 100.0 % / 100.0 % | 0.0 % (largest 0.00 bps) |
| 5 min | expired | 4 | 2 | 1 | 0.4578 | 0.4871 | +6.39 % | too few sessions | 100.0 % / 100.0 % | 0.0 % (largest 0.00 bps) |
| 15 min | late | 3 | 2 | 1 | 1.0211 | 1.0610 | +3.91 % | too few sessions | 100.0 % / 100.0 % | 0.0 % (largest 0.00 bps) |
| 15 min | expired | 1 | 0 | 0 | - | - | - | - | - | - |
| 20 min | late | 3 | 1 | 1 | 0.8862 | 0.9515 | +7.37 % | too few sessions | 100.0 % / 100.0 % | 0.0 % (largest 0.00 bps) |
| 30 min | late | 3 | 1 | 1 | 1.6225 | 1.6416 | +1.18 % | too few sessions | 100.0 % / 100.0 % | 0.0 % (largest 0.00 bps) |
| 60 min | delayed origin (the delayed-feed evaluation) | 1 | 0 | 0 | - | - | - | - | - | - |
| 60 min | late | 1 | 0 | 0 | - | - | - | - | - | - |
| 120 min | delayed origin (the delayed-feed evaluation) | 2 | 0 | 0 | - | - | - | - | - | - |
| 240 min | delayed origin (the delayed-feed evaluation) | 2 | 0 | 0 | - | - | - | - | - | - |

**Timing**, in seconds after the mark (median / 90th percentile / largest); the live deadline is 60 s. When the origin bar reached the store is the feed's part; the trigger's start, the collection and the computation are the pipeline's.

| Trigger | Marks tried | Issued | Found the origin bar missing | Failed | Within the deadline | Issued after | Trigger started after | Collection | Computation | Origin bar stored after |
|---|---|---|---|---|---|---|---|---|---|---|
| dashboard Auto mode | 5 | 2 | 4 | 0 | 0 | 979 / 1127 / 1164 | 926 / 1085 / 1125 | - | 8 / 8 / 8 | 930 / 1088 / 1128 |
| by hand | 1 | 0 | 1 | 0 | 0 | - | - | - | - | - |
| not recorded (runs before triggers were) | 2 | 2 | 1 | 0 | 0 | 671 / 692 / 697 | - | - | - | 653 / 672 / 677 |

## Issues made under no rules (legacy)

| Horizon | Class | Issued | Scored | Sessions | v2 CRPS | Model CRPS | Model minus v2 | 95 % interval | 90 % band covers (v2 / model) | Origin bar revised |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 min | made under no rules | 2 | 2 | 1 | 0.5025 | 0.4856 | -3.35 % | too few sessions | 100.0 % / 100.0 % | 0.0 % (largest 0.00 bps) |
| 5 min | made under no rules | 2 | 2 | 1 | 0.7067 | 0.6550 | -7.32 % | too few sessions | 100.0 % / 100.0 % | 0.0 % (largest 0.00 bps) |
| 15 min | made under no rules | 2 | 2 | 1 | 1.6350 | 1.5608 | -4.54 % | too few sessions | 100.0 % / 100.0 % | 0.0 % (largest 0.00 bps) |
| 20 min | made under no rules | 2 | 2 | 1 | 1.9246 | 1.8114 | -5.88 % | too few sessions | 100.0 % / 100.0 % | 0.0 % (largest 0.00 bps) |
| 30 min | made under no rules | 2 | 2 | 1 | 2.6771 | 2.5268 | -5.62 % | too few sessions | 100.0 % / 100.0 % | 0.0 % (largest 0.00 bps) |
| 60 min | made under no rules | 2 | 2 | 1 | 7.9807 | 8.1815 | +2.52 % | too few sessions | 100.0 % / 100.0 % | 0.0 % (largest 0.00 bps) |
| 120 min | made under no rules | 1 | 1 | 1 | 6.3671 | 6.1281 | -3.75 % | too few sessions | 100.0 % / 100.0 % | 0.0 % (largest 0.00 bps) |

**Timing**, in seconds after the mark (median / 90th percentile / largest); the live deadline is none under these rules. When the origin bar reached the store is the feed's part; the trigger's start, the collection and the computation are the pipeline's.

| Trigger | Marks tried | Issued | Found the origin bar missing | Failed | Within the deadline | Issued after | Trigger started after | Collection | Computation | Origin bar stored after |
|---|---|---|---|---|---|---|---|---|---|---|
| not recorded (runs before triggers were) | 2 | 2 | 0 | 0 | - | 1233 / 1589 / 1679 | - | - | - | - |
