# Experiment hist_dev_v1

Scored by `forecaster/experiments.py` (result 128e7499-f507-45ff-89fc-eff69dbfda3a, code 83d75cb74966be946d4021fa4b7a9f6c5ca006ca). The manifest was registered before any score was computed; its hash is `a36a8aae6d694146`.

- **Purpose:** development - development data (guideline 4C): the structure rules were calibrated and the label disagreements inspected on these sessions, so the result identifies clear failures and a modest candidate; it is not a test - that is prospective.
- **Sessions:** 2025-09-02 to 2026-10-02 (274 scheduled), profile research_0929, historical replay.
- **Versions:** labels nq_prompt_v2_1_impl5, snapshots nq_evidence_v5_r0929, annotation nq_structure_rules_v4, matcher nq_match_p1_v2.
- **Arms:** A `nq_prior_p1_v1`; B `nq_baseline_p1_v1`.
- **Official run:** the first issued run of the session, profile and arm by the database issue time. Arms paired on the same analogue set: 274 sessions.
- **Primary:** direction_15m, multiclass log loss (natural log), lower is better. No floor or clipping at scoring: a realised class issued with probability 0 has infinite log loss; such cases are counted and the mean is taken over the finite ones (Brier is always finite).
- **Profitability:** not tested - no execution, costs or trading strategy; directional accuracy says nothing about profit.

## Coverage

| arm | case | no_run | no_outcome |
|---|---|---|---|
| A | 274 | 0 | 0 |
| B | 274 | 0 | 0 |

## Primary: direction_15m

Paired B-A on 273 common sessions (negative favours B):

| metric | A | B | difference | 95% interval | n |
|---|---|---|---|---|---|
| log loss (both finite) | 0.9860 | 1.0179 | 0.0319 | [-0.0023, 0.0652] | 271 |
| Brier sum | 0.6131 | 0.6266 | 0.0135 | [-0.0090, 0.0369] | 273 |
| accuracy (both classed) | 0.4016 | 0.4549 | - | - | 244 |

Infinite log loss (a realised class issued with probability 0): 0 only in A, 0 only in B, 2 in both.

## Every target

| target | arm | scored | log loss (finite) | infinite | Brier | accuracy | ambiguous | no realised label | no distribution | analogues | comparable weight |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `direction_15m` | A | 273 | 0.9860 | 2 | 0.6131 | 0.4016 | 29 | - | unavailable 1 | - | - |
| `direction_15m` | B | 273 | 1.0179 | 2 | 0.6266 | 0.4494 | 6 | - | unavailable 1 | 4.91 | 99.7 |
| `opening_bias_30m` | A | 273 | 0.9171 | 2 | 0.5813 | 0.4851 | 5 | - | unavailable 1 | - | - |
| `opening_bias_30m` | B | 273 | 0.9743 | 2 | 0.6145 | 0.4465 | 2 | - | unavailable 1 | 4.91 | 99.7 |
| `first_move_5m` | A | 117 | 0.7112 | 1 | 0.5302 | 0.4466 | 14 | ambiguous_intrabar 156 | unavailable 1 | - | - |
| `first_move_5m` | B | 117 | 0.6920 | 1 | 0.5113 | 0.5526 | 3 | ambiguous_intrabar 156 | unavailable 1 | 4.91 | 99.7 |
| `opening_type_15m` | A | 272 | 1.0960 | 5 | 0.5147 | 0.6900 | 1 | missing_reference 1 | unavailable 1 | - | - |
| `opening_type_15m` | B | 272 | 1.1687 | 5 | 0.5318 | 0.6863 | 1 | missing_reference 1 | unavailable 1 | 4.91 | 99.7 |
| `session_type_rth` | A | 166 | 1.4733 | 4 | 0.7793 | 0.3013 | 10 | missing_threshold 19, shortened_session 2, uncovered 86 | unavailable 1 | - | - |
| `session_type_rth` | B | 166 | 1.4993 | 4 | 0.7956 | 0.2609 | 5 | missing_threshold 19, shortened_session 2, uncovered 86 | unavailable 1 | 4.91 | 99.7 |
| `close_direction_rth` | A | 252 | 0.8740 | 2 | 0.5674 | 0.4310 | 20 | missing_threshold 19, shortened_session 2 | unavailable 1 | - | - |
| `close_direction_rth` | B | 252 | 0.9210 | 2 | 0.5947 | 0.4656 | 5 | missing_threshold 19, shortened_session 2 | unavailable 1 | 4.91 | 99.7 |
| `first_level_tested` | A | 211 | 2.1154 | 9 | 0.8833 | 0.2500 | 7 | ambiguous_intrabar 56, missing_reference 2, none_tested 4 | unavailable 1 | - | - |
| `first_level_tested` | B | 211 | 1.9734 | 9 | 0.8448 | 0.2696 | 7 | ambiguous_intrabar 56, missing_reference 2, none_tested 4 | unavailable 1 | 4.91 | 99.7 |

Paired differences, every target (no interval: fewer than two bootstrap blocks of pairs):

| target | pair | common | Brier diff | interval | log loss diff | interval | both finite |
|---|---|---|---|---|---|---|---|
| `direction_15m` | B-A | 273 | 0.0135 | [-0.0090, 0.0369] | 0.0319 | [-0.0023, 0.0652] | 271 |
| `opening_bias_30m` | B-A | 273 | 0.0332 | [0.0099, 0.0581] | 0.0572 | [0.0220, 0.0946] | 271 |
| `first_move_5m` | B-A | 117 | -0.0188 | [-0.0536, 0.0178] | -0.0191 | [-0.0537, 0.0176] | 116 |
| `opening_type_15m` | B-A | 272 | 0.0171 | [0.0013, 0.0349] | 0.0727 | [0.0242, 0.1234] | 267 |
| `session_type_rth` | B-A | 166 | 0.0163 | [-0.0095, 0.0417] | 0.0260 | [-0.0302, 0.0799] | 162 |
| `close_direction_rth` | B-A | 252 | 0.0274 | [0.0021, 0.0505] | 0.0470 | [0.0145, 0.0806] | 250 |
| `first_level_tested` | B-A | 211 | -0.0385 | [-0.0640, -0.0123] | -0.1420 | [-0.2298, -0.0545] | 202 |

## Where the differences sit (direction_15m)

By realised class:

| class | n | A mean p(realised) | B mean p(realised) | A accuracy | B accuracy |
|---|---|---|---|---|---|
| bullish | 123 | 0.4235 | 0.4298 | 0.2273 | 0.4000 |
| bearish | 118 | 0.4482 | 0.4550 | 0.6887 | 0.6207 |
| neutral_band | 32 | 0.1117 | 0.1184 | 0.0000 | 0.0000 |

By calendar month:

| group | common | A Brier | B Brier | A log loss (finite) | B log loss (finite) | A accuracy | B accuracy |
|---|---|---|---|---|---|---|---|
| 2025-09 | 20 | 0.7253 | 0.7175 | 0.9782 | 0.9437 | 0.4118 | 0.3333 |
| 2025-10 | 23 | 0.5656 | 0.5854 | 0.8851 | 0.9300 | 0.4783 | 0.4348 |
| 2025-11 | 19 | 0.6356 | 0.7067 | 1.0712 | 1.1917 | 0.4737 | 0.4737 |
| 2025-12 | 22 | 0.6451 | 0.5996 | 1.0786 | 1.0398 | 0.4545 | 0.5455 |
| 2026-01 | 20 | 0.6637 | 0.6399 | 1.0992 | 1.0971 | 0.3500 | 0.5000 |
| 2026-02 | 19 | 0.5326 | 0.5835 | 0.8359 | 0.9015 | 0.4211 | 0.3684 |
| 2026-03 | 22 | 0.6990 | 0.7222 | 1.1787 | 1.2819 | 0.2778 | 0.3636 |
| 2026-04 | 21 | 0.6146 | 0.6062 | 1.0090 | 1.0385 | 0.4444 | 0.6190 |
| 2026-05 | 20 | 0.5921 | 0.6336 | 0.9621 | 1.0180 | 0.2500 | 0.2632 |
| 2026-06 | 21 | 0.6155 | 0.7069 | 1.0098 | 1.0897 | 0.3333 | 0.3000 |
| 2026-07 | 22 | 0.5826 | 0.5469 | 0.9459 | 0.8896 | 0.4500 | 0.5455 |
| 2026-08 | 21 | 0.5576 | 0.5771 | 0.8928 | 0.9486 | 0.2353 | 0.5000 |
| 2026-09 | 21 | 0.5511 | 0.5416 | 0.8826 | 0.8769 | 0.5556 | 0.5500 |
| 2026-10 | 2 | 0.5228 | 0.5388 | 0.8205 | 0.8672 | 0.5000 | 0.5000 |

By daily-ATR tercile (volatility):

| group | common | A Brier | B Brier | A log loss (finite) | B log loss (finite) | A accuracy | B accuracy |
|---|---|---|---|---|---|---|---|
| high | 85 | 0.5934 | 0.6018 | 0.9666 | 0.9792 | 0.3662 | 0.4940 |
| low | 85 | 0.6125 | 0.6233 | 0.9976 | 1.0305 | 0.4578 | 0.4643 |
| middle | 85 | 0.6057 | 0.6292 | 0.9923 | 1.0522 | 0.3733 | 0.4167 |
| no daily ATR | 18 | 0.7444 | 0.7464 | 0.9947 | 0.9749 | 0.4000 | 0.3125 |

## Reliability (direction_15m)

The issued probability of each class in equal bins against how often the class was realised. Five analogues and a few hundred sessions do not establish calibration (4D).

A, bullish: 0.3-0.4 n=30 p=0.38 obs=0.60; 0.4-0.5 n=234 p=0.43 obs=0.44; 0.5-0.6 n=5 p=0.51 obs=0.40; 0.6-0.7 n=3 p=0.64 obs=0.33; 0.9-1.0 n=1 p=1.00 obs=0.00
A, bearish: 0.0-0.1 n=1 p=0.00 obs=1.00; 0.1-0.2 n=1 p=0.17 obs=1.00; 0.2-0.3 n=3 p=0.25 obs=0.33; 0.3-0.4 n=2 p=0.35 obs=0.50; 0.4-0.5 n=208 p=0.44 obs=0.44; 0.5-0.6 n=58 p=0.51 obs=0.40
A, neutral_band: 0.0-0.1 n=51 p=0.08 obs=0.16; 0.1-0.2 n=220 p=0.13 obs=0.11; 0.2-0.3 n=2 p=0.23 obs=0.00
B, bullish: 0.1-0.2 n=1 p=0.20 obs=1.00; 0.2-0.3 n=21 p=0.24 obs=0.43; 0.3-0.4 n=79 p=0.33 obs=0.47; 0.4-0.5 n=83 p=0.42 obs=0.39; 0.5-0.6 n=56 p=0.52 obs=0.48; 0.6-0.7 n=26 p=0.62 obs=0.54; 0.7-0.8 n=6 p=0.72 obs=0.50; 0.9-1.0 n=1 p=1.00 obs=0.00
B, bearish: 0.0-0.1 n=1 p=0.00 obs=1.00; 0.1-0.2 n=1 p=0.18 obs=1.00; 0.2-0.3 n=17 p=0.22 obs=0.47; 0.3-0.4 n=50 p=0.33 obs=0.28; 0.4-0.5 n=92 p=0.43 obs=0.49; 0.5-0.6 n=78 p=0.53 obs=0.42; 0.6-0.7 n=28 p=0.63 obs=0.50; 0.7-0.8 n=6 p=0.74 obs=0.33
B, neutral_band: 0.0-0.1 n=132 p=0.06 obs=0.13; 0.1-0.2 n=112 p=0.16 obs=0.09; 0.2-0.3 n=26 p=0.26 obs=0.19; 0.3-0.4 n=3 p=0.36 obs=0.00
