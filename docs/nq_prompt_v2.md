# NQ prompt-v2: stage 1 (definitions, cutoffs, evidence, outcomes)

Stage 1 of the *Nasdaq 100 Forecast Implementation Guideline* (28 September 2026),
built fresh on the codebase after the earlier forecasting code was removed. It
makes a pre-open snapshot and its realised outcome refer to exactly the same
question. Nothing here forecasts: stages 2-4 (analogue selection, the LLM
forecast, comparison) build on it.

## Sources

The source specifications are the pre-open prompt **P1** and the post-session
prompt **P2** (prompt version 2.1, definitions NQ-v2, overnight classification
ON-v1, revised 2026-09-15), in `prompts/source/` (renamed after their titles; the
manifest keeps the original file names), hashed in `prompts/manifest.json` (a test
checks the hashes). They were pasted into a chat session and saved as received, so
they are **not verified byte-for-byte** against the original
files (`byte_exact_original: false`). Replace them with the originals and update
the hashes when available. Adapted runtime prompts (stage 3) belong in
`prompts/runtime/`.

## What is built

| Piece | Where | Guideline |
|---|---|---|
| Definition registry: targets, canonical labels, windows, display mappings, T / B, profiles, calculation convention | [contracts/nq_prompt_v2.py](../contracts/nq_prompt_v2.py) | 1A, 1B |
| Evidence snapshot builder | [features/nq_evidence.py](../features/nq_evidence.py) | 1B, 1C |
| Outcome labels and measurements (pure) | [forecaster/labels_prompt_v2.py](../forecaster/labels_prompt_v2.py) | 1D |
| P2's 40-field realised-outcome record (display adapter) | [forecaster/outcome_display.py](../forecaster/outcome_display.py) | 1D |
| Store: version registry, snapshots, revisioned outcomes | [database/migrations/0009_journal_records.sql](../database/migrations/0009_journal_records.sql), [database/journal_store.py](../database/journal_store.py) | 1C, 1D |
| CLI | [scripts/nq_journal.py](../scripts/nq_journal.py) | 1E backfill |
| Tests | [tests/test_labels_prompt_v2.py](../tests/test_labels_prompt_v2.py), [tests/test_nq_journal.py](../tests/test_nq_journal.py) | 1E |
| Review set: selection, the Review page, verdict store | [forecaster/review_set.py](../forecaster/review_set.py), [dashboard/views/review.py](../dashboard/views/review.py), [database/migrations/0010_review_sets.sql](../database/migrations/0010_review_sets.sql) | 1E |
| Disagreement report against the old v5 labels | [scripts/label_disagreement_report.py](../scripts/label_disagreement_report.py), [docs/reports/](reports/) | 1E |

### Versions

| Version | Kind | What |
|---|---|---|
| `nq_prompt_v2_1_impl3` | labels | impl2 plus the supplementary descriptors of P2 sections 5 and 7 (MP-v1) and the first 15-minute pattern (FP-v1) - current, recorded for the 269 sessions |
| `nq_prompt_v2_1_impl2` | labels | NQ-v2 scored classifications, 30-minute bias, first level tested and the LO-v1 level outcomes (P2 sections 2-6) |
| `nq_prompt_v2_1_impl1` | labels | the first six targets only |
| `nq_conv_v1` | convention | what P1/P2 leave open (below) |
| `nq_evidence_v1_r0929` | snapshot | research profile, cutoff 09:29:00 ET |
| `nq_evidence_v1_o0927` | snapshot | operational profile, cutoff 09:27:00 ET |

Each label version keeps every rule of the one before unchanged (checked on the
stored sessions) and adds targets; the earlier versions stay with the outcomes
recorded under them.

Each is registered with a hash of its definition; re-registering a name with a
different definition is refused, so a changed rule needs a new version name. The
two profiles are separate snapshot versions because a different cutoff changes the
frozen ATRs and therefore T, B and every threshold-dependent label: compare arms
only within one profile.

### Targets and labels

| Target | Labels | Display (realised / predicted) |
|---|---|---|
| `first_move_5m` | `up_first`, `down_first`, `neither` | Up / Down / Two-sided |
| `direction_15m` | `bullish`, `bearish`, `neutral_band` | Two-sided for the band |
| `opening_type_15m` | `sweep_low_rebound`, `sweep_high_reverse`, `opening_drive_up`, `opening_drive_down`, `two_sided_whipsaw`, `range` | P1/P2 option names |
| `opening_bias_30m` | `bullish`, `bearish`, `neutral_band` | Two-sided realised, **Neutral** predicted |
| `close_direction_rth` | `bullish`, `bearish`, `neutral_band` | Two-sided for the band |
| `session_type_rth` | `reversal_day`, `bull_trend_day`, `bear_trend_day`, `two_sided_volatile_day`, `range_day` | P1/P2 option names |
| `first_level_tested` | `on_high`, `on_low`, `prev_rth_high`, `prev_rth_low`, `prev_rth_close`, `overnight_open`, `vwap` | ON High, ..., VWAP; the price is the measurement `first_level_price` |
| `first_level_outcome` | LO-v1 below, without `not_tested` | First Level Outcome, over [09:30, 09:45) |
| `on_high_outcome`, `on_low_outcome`, `prev_rth_high_outcome`, `prev_rth_low_outcome` | `not_tested`, `test_rejection`, `break_acceptance`, `break_reclaim_acceptance`, `break_without_acceptance` | Not tested / Test and rejection / Break and acceptance / Break–reclaim–acceptance / Break without acceptance, over the standard RTH |
| `ib_direction` | `bullish`, `bearish`, `neutral_band` | Initial Balance Direction: 10:29 close - O against T |
| `opening_drive_strength` | `strong`, `moderate` | only for an opening drive (efficiency >= 0.80 strong); otherwise `not_applicable`, shown blank |
| `opening_range_extension`, `ib_extension` | `up`, `down`, `both_sides`, `none` | strict breaches of the 15-minute / IB high and low to the close |
| `gap_outcome` | `no_material_gap`, `full_gap_fill`, `partial_gap_fill`, `gap_and_go` | against the frozen previous RTH close, B and T |
| `session_high_timing`, `session_low_timing` | `opening_15m`, `morning`, `midday`, `afternoon`, `closing_30m` | first bar reaching the RTH extreme |
| `morning_pullback` | `none`, `minor`, `moderate`, `deep` | MP-v1 |
| `afternoon_continuation` | `bullish`, `bearish`, `none` | morning (T) and 14:00-close (B) directions |
| `opening_direction_matched`, `direction_15m_matched` | `yes`, `no`, `mixed` | the 30-minute / 15-minute direction against the close direction |
| `trend_persistence` | `high`, `moderate`, `low` | E >= 0.60 / >= 0.35 / below |
| `first_15m_pattern` | `drive_continuation`, `fade_reversal`, `v_shape_reversal`, `double_top_bottom`, `balanced_rotation`, `spike_and_channel`, `choppy` | the FP-v1 convention (below); unavailable unless exactly one fits |

The extension, gap, timing, afternoon, matched and trend-persistence fields apply
to standard sessions only. Window measurements - first-15-minute, 30-minute, IB
and RTH highs / lows / closes, the opening confirmation time, the first level
price, the cutoff VWAP and the MP-v1 leg - are stored beside the labels; each needs
only its own window, so an IB high survives a later gap.

A value that cannot be established is `None` with a reason: `missing_bars`,
`missing_threshold`, `missing_reference`, `ambiguous_intrabar`,
`shortened_session`, `uncovered` (a complete standard session that no P2
session-type rule fits - recorded apart from missing data), and for the levels
`coincident_levels` (several candidates at the price reached first; the price is
kept), `none_tested` (no candidate reached in the complete window),
`approach_unresolved` (the level equals O), `no_rejection` (reached, never closed
back), `upstream_unavailable` (a field that depends on an unavailable label),
`not_applicable` (drive strength without a drive), `late_leg_extreme` (MP-v1's
leg extreme at or after 11:58) and `ambiguous_pattern` (more than one FP-v1 pattern
fits). Missing is never a
market class. The database checks every stored label against the registered
vocabulary and reasons.

T = max(1, ceil(0.5 x frozen 2-minute ATR)), B = max(1, ceil(0.05 x frozen daily
ATR)), in points. The ATRs are kept as **exact fractions** (Wilder smoothing
divides by 14 at every step, so any rounded value could land on the wrong side of
an integer ceiling), and the ratio tests (drive efficiency, E, CL) are made by
cross-multiplication, so every comparison is exact.

### Snapshots

`build_snapshot(conn, day, profile)` reads, in one repeatable-read transaction,
only bars that ended by the cutoff, and archives what it used: the overnight
window's 1m bars and their complete 2m / 5m / 15m clock buckets, the previous
session's RTH 1m bars, the per-session daily-ATR inputs, the day's economic
calendar rows and coverage, and the other instruments' last observations. Bars
after the cutoff cannot change a snapshot (tested). It records the input cutoff,
the last completed bar and the source payload hash.

Without real-time receipts a snapshot is a `historical_reconstruction` with
point-in-time status `unverified_historical`; the database allows `verified` only
for a `live_capture`, and a live capture only before 09:30. The real-time streamer
was removed, so live capture comes back with stage 3.

### Outcomes

`compute_outcome(snapshot, bars)` sees the frozen snapshot and the realised 1m
bars only - no prediction. P2's ordered rules use three-valued logic: a rule that
cannot be decided makes the label `None` when it could change the winner, never a
fall-through to a lower rule. Outcomes are computed two hours after the scheduled
close and become a new `outcome_revision` only when they change (a vendor
revision, say); earlier revisions stay.

## Conventions (`nq_conv_v1`)

Decisions P1/P2 leave to the implementation, recorded in the convention and label
definitions:

- **Daily ATR:** Wilder ATR(14) on RTH sessions [09:30, scheduled close), each
  session on its active contract with the previous close from the same contract;
  a session without every RTH minute is skipped; the most recent 70 valid true
  ranges, seeded with the mean of the first 14 (searched over at most 105
  sessions). Chart parity with a TradingView daily ATR is **not verified** -
  TradingView may use the Globex day or a continuous contract.
- **2-minute ATR:** Wilder ATR(14) on 2m buckets anchored on even ET minutes,
  built from the snapshot contract's 1m bars in the overnight window and complete
  by the cutoff (at 09:29 the last is 09:26-09:28); 70 true ranges. Never the 1m
  ATR rescaled.
- **References:** previous RTH high / low / close need every RTH minute of the
  previous scheduled session (same contract); ON high / low need 90% of the
  overnight minutes [18:00 the day before, cutoff); overnight open is the bar
  starting 18:00 exactly; the cutoff price is the last complete 1m close if at most
  5 minutes old. **Price at 09:29 is always unavailable** (neither profile observes
  09:29:00-09:29:59) and **premarket high / low are unavailable** (no premarket
  window defined). Moving averages are not configured.
- **First level tested (P2 section 6):** the candidates are the snapshot's ON high
  / low, previous-RTH high / low / close, overnight open, and the **frozen cutoff
  VWAP** - sum(hlc3 x volume) / sum(volume) over the snapshot's archived overnight
  1m bars [18:00, cutoff), computed exactly by the labels from the stored snapshot
  (unavailable under 90% overnight coverage). Premarket high / low, Long MA and
  round numbers / other named levels are not in the candidate set. A level is
  reached by a 1m bar with low <= level <= high, so a level the opening gap crossed
  without an RTH trade there is not reached. Within one bar price is taken to trade
  through every price between the bar's open and its extremes: a level equal to the
  open is first, then the nearest on its side; levels on both sides of the open are
  `ambiguous_intrabar`. Any unavailable candidate makes the first level unavailable
  (it could have been first).
- **LO-v1:** the original side is the side of O (a level equal to O has no
  resolvable approach); a breach is a 1m **close** strictly beyond the level, so a
  wick through or an equal close is a test, not a break; test and rejection needs a
  later close strictly back on the original side; acceptance is the window's final
  three 1m closes strictly on one side, and those bars must be stored. Not tested
  needs the complete window and applies to the four session references only.
- **Descriptors (P2 sections 5 and 7):** opening confirmation time is the close
  time of the first 1m bar closing strictly beyond O + T / O - T in the first 15
  minutes; an extension breach is a trade strictly beyond the boundary, and "both
  sides" is final even when bars are missing, any other category needs the complete
  window; for the gap outcome "moved toward P" is a trade strictly beyond O on P's
  side, and it is a standard-session field; timing takes the first bar reaching the
  exact extreme; MP-v1 takes the first occurrence of the leg extreme and the
  pullback from the bars strictly after it, with its bands compared exactly.
- **The P2 record:** two fields follow a display convention, not P2: Realised
  Outcome Confidence comes from coverage only (5 every minute stored, 4 the first
  hour complete, 3 the first 15 minutes, 2 the 09:30 bar, 1 none), and Outcome Data
  Notes lists the definitions, data mode, coverage, the reason for every
  unavailable field and the MP-v1 leg.
- **First 15-Minute Pattern (FP-v1, an implementation convention):** P2 describes
  the pattern visually and maps only three cases, which FP-v1 applies first: an
  opening drive is a drive continuation and a sweep a V-shape reversal. Otherwise
  the shapes are read in proportions of the 15-minute range R, as on a chart:
  v-shape (the extreme opposite the close in bars 3-11, the close in the far fifth
  and beyond O by T), fade (the extreme in bars 0-2, the close in the opposite
  third, not retested), double top / bottom (two tests within 0.1R at least 4 bars
  apart, a 0.3R pullback, the close beyond it), spike and channel (from the near
  fifth, half the range in bars 0-2, the far extreme in bars 12-14, the close in
  the far fifth), balanced rotation (|C - O| <= 0.2R, R >= 2T, three midpoint
  crossings), choppy (|C - O| <= 0.2R, eight reversals, not a rotation). P2's "only
  when unambiguous" holds: no fit is `uncovered`, several `ambiguous_pattern`. A
  first draft measured in T fired on noise (T is small against the opening
  range: a fade on 25% of sessions, 19% ambiguous); in proportions of R, 54% of
  the 269 sessions get a pattern, 4% are ambiguous. The review set is where FP-v1
  is checked against your reading of the charts.
- **Opening-type details:** reach is inclusive (high >= level); "breached by at
  least T" is low <= level - T; the reclaim must be a later bar than the first
  breach; a level strictly between the 09:29 close and O was crossed by the gap
  and is not a sweep reference. A missing sweep reference only blocks the label
  when a sweep was possible at all (the window went below O - T for a support,
  above O + T for a resistance).

## Running it

```bash
python scripts/nq_journal.py outcomes --start 2025-09-01 --end 2026-09-25   # label stored snapshots under the current version
python scripts/nq_journal.py backfill --start 2025-09-01 --end 2026-09-25   # snapshot + outcome per session
python scripts/nq_journal.py backfill --start 2025-09-01 --end 2026-09-25 --profile operational_0927
python scripts/nq_journal.py show --date 2026-09-24                         # snapshot + P2's 40-field record
```

Migration 0009 (the `journal` schema) is applied by the first process that
starts after it is pulled - the dashboard and the collector migrate on start.
A new label version needs no new snapshots: `outcomes` labels the stored ones.

## Findings (research profile)

Read-only, nothing stored:

- **First move is ambiguous on 7 of 18 sessions.** T is half the 2-minute ATR of
  the quiet overnight session (T = 6-10 points), while the 09:30 1m bar alone
  often spans 30-50 points and reaches both O + T and O - T. P2 then requires
  finer data or Unavailable. With 1-minute bars only, about 40% of first moves
  cannot be resolved; seconds or ticks would be needed.
- **Opening type is `two_sided_whipsaw` on 14 of 18 sessions** for the same reason:
  with T that small, most first 15 minutes reach both sides.
- **Session type is `uncovered` on 5 of 18 sessions:** complete sessions none of
  the five P2 rules fits (for example a directional close with 0.35 <= E < 0.60).

These are properties of the NQ-v2 definitions as written, not of the code. T stays
on the 2-minute ATR and 1 minute is the finest resolution (decided 2026-10-03), so
such sessions stay unavailable.

On the 269 snapshots stored by 2026-10-03 (2025-09-02 to 2026-09-25), impl2
reproduces impl1's six targets on every session, identifies the **first level
tested on 87%** (VWAP 27%, ON high 17%, ON low 12%, previous-RTH high 10%, ...;
unavailable: none tested 9%, both sides within one bar 5%, a missing candidate 1%,
coincident levels 0.4%), and resolves the four session-reference outcomes on every
standard session (not tested 43-64%, then break and acceptance and
break-reclaim-acceptance; test and rejection is rare because any close beyond the
level counts as a breach).

impl3 keeps every impl2 label on all 269 sessions. Its descriptors: IB direction
bullish 51% / bearish 44%; a qualifying drive (and so a drive strength) on 13%;
the opening range extended on both sides on 51%; gap outcome full fill 42%,
partial fill 42%, no material gap 7%, gap-and-go one session; MP-v1 minor 37%,
deep 27%, moderate 19%, none 7%, leg extreme too late 7%; afternoon continuation
none 58%; trend persistence about a third each. About 7% of the threshold-dependent
fields are unavailable on the first weeks of September 2025, before the daily ATR
had 70 sessions.

## Disagreement with the old labels (1E)

[docs/reports/label_disagreement_v5_vs_nq_v2.md](reports/label_disagreement_v5_vs_nq_v2.md)
(and the CSV beside it) compares the old `nq_labels_v5_candidate` - restored from
the pre-0008 backup into a scratch database - with the NQ-v2 labels on the 269
sessions both have, and explains every disagreement ([how to regenerate it](reports/README.md)):

- v5 replayed on today's bars with its own code reproduces all 1,614 stored v5
  labels, so no bars were revised: every difference is a definition. The frozen
  daily ATR A agrees exactly on all 250 sessions both have, so `nq_conv_v1`
  reproduces the old feature contract's A.
- First move (159 disagreements), 15-minute, IB and close direction: all explained
  by the threshold - T (3-18 points across the sessions) against v5's 0.10 A (about
  25-70), B (13-35) against v5's 0.20 A (about four times B). With v5's threshold the NQ-v2 rule gives v5's
  label every time; same-minute double touches never needed v5's tie rule.
- Opening type (189): v5's `mixed` has no NQ-v2 class (96); v5 range where both
  O +/- T were reached becomes a whipsaw (43); v5 drives whose counter-excursion
  exceeds T fail as NQ-v2 drives (28); NQ-v2 checks sweeps before the whipsaw (15).
- Session type (146): v5's `mixed` (83); P2's reversal coming first (22); complete
  sessions no P2 rule fits (`uncovered`, 25); P2's trend, volatile and range rules
  failing on E or CL (16).
- 19 September 2025 sessions have no v5 label (its A was not available yet) but an
  NQ-v2 opening label.

## Review set (1E)

```bash
python scripts/nq_journal.py review-set --name stage1_review_v1     # done 2026-10-04: 25 sessions
python -m dashboard.app                                              # then the Review page
python scripts/nq_journal.py review-report --name stage1_review_v1  # agreement per field, every flag
```

`stage1_review_v1` holds 25 of the 269 sessions, chosen by
[forecaster/review_set.py](../forecaster/review_set.py): greedy coverage of every
target's labels and unavailable reasons (142 classes, all covered by the first 15
picks), early closes, contract rolls and calendar quarters, rare classes first;
the other 10 spread the set over the year. The dashboard's **Review** page shows
each session's chart with the frozen levels the labels used (previous RTH close /
high / low, ON high / low, the cutoff VWAP) and O +/- T, beside P2's 40-field
record; every field counts as agreed unless marked disagree or unsure, with a
note. Verdicts are append-only rows (migration 0010, `journal.review_verdicts`;
`journal.review_latest` holds the one that counts). The review checks that the
labels mean what P1 / P2 say - not whether anything predicts them. Pre-open
classifications join the review with stage 2.

## Not built yet

- Live capture with receipt provenance, the 09:29:50 deadline and the
  inference-attempt log (needs a real-time feed again; stage 3).
- Your verdicts on `stage1_review_v1` (the set and the page are ready).
- Scoring predictions against outcomes (nothing predicts yet).
