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
checks the hashes). Their author confirmed them as the original files on
2026-10-04 (`byte_exact_original: true`). The guideline's **Appendix A** - its
proposed runtime prompts A1 structure annotation, A2 forecast synthesis, A3
supplementary outcome annotation and the A4 interface sketch - is beside them
(`GUIDELINE APPENDIX A - RUNTIME PROMPTS.md`, pasted 2026-10-04, also hashed).
Runtime prompts adapted from it belong in `prompts/runtime/`, each under its own
version identifier.

## What is built

| Piece | Where | Guideline |
|---|---|---|
| Definition registry: targets, canonical labels, windows, display mappings, T / B, profiles, calculation convention | [contracts/nq_prompt_v2.py](../contracts/nq_prompt_v2.py) | 1A, 1B |
| Evidence snapshot builder | [features/nq_evidence.py](../features/nq_evidence.py) | 1B, 1C |
| Outcome labels and measurements (pure) | [forecaster/labels_prompt_v2.py](../forecaster/labels_prompt_v2.py) | 1D |
| P2's 40-field realised-outcome record (display adapter) | [forecaster/outcome_display.py](../forecaster/outcome_display.py) | 1D |
| Store: version registry, snapshots, revisioned outcomes | [database/migrations/0009_journal_records.sql](../database/migrations/0009_journal_records.sql), [database/journal_store.py](../database/journal_store.py) | 1C, 1D |
| CLI | [scripts/nq_journal.py](../scripts/nq_journal.py) | 1E backfill |
| Tests | [tests/test_labels_prompt_v2.py](../tests/test_labels_prompt_v2.py), [tests/test_nq_journal.py](../tests/test_nq_journal.py), [tests/test_snapshot_completeness.py](../tests/test_snapshot_completeness.py) | 1E |
| Review set: selection, the Review page, verdict store | [forecaster/review_set.py](../forecaster/review_set.py), [dashboard/views/review.py](../dashboard/views/review.py), [database/migrations/0010_review_sets.sql](../database/migrations/0010_review_sets.sql) | 1E |
| Disagreement report against the old v5 labels | [scripts/label_disagreement_report.py](../scripts/label_disagreement_report.py), [docs/reports/](reports/) | 1E |
| Disagreement report for a label revision (impl3 against impl5, rules v2 against v4) | [scripts/label_revision_report.py](../scripts/label_revision_report.py), [docs/reports/label_disagreement_impl3_vs_impl5.md](reports/label_disagreement_impl3_vs_impl5.md) | 1E |

### Versions

| Version | Kind | What |
|---|---|---|
| `nq_prompt_v2_1_impl5` | labels | guideline revision 2: FL-v3 (both sides of one bar's open `ambiguous_intrabar`, the nearest kept as `first_level_estimate`; impl4's candidates and precedence), OS-v2 (a level the opening gap crossed can be swept), LO-v2 (a wick beyond a level is a breach); candidates read from the snapshot's frozen list - current |
| `nq_prompt_v2_1_impl4` | labels | impl3 with first level tested under FL-v2: Premarket High / Low and the Long MA as candidates, coincident candidates named by precedence, both sides of one bar resolved by the nearest to the open (estimated, flagged); registered, never recorded |
| `nq_prompt_v2_1_impl3` | labels | impl2 plus the supplementary descriptors of P2 sections 5 and 7 (MP-v1) and the first 15-minute pattern (FP-v1) |
| `nq_prompt_v2_1_impl2` | labels | NQ-v2 scored classifications, 30-minute bias, first level tested and the LO-v1 level outcomes (P2 sections 2-6) |
| `nq_prompt_v2_1_impl1` | labels | the first six targets only |
| `nq_conv_v5` | convention | strict completeness (guideline revision 2, 1B): every window needs every minute exactly once, the ATRs and the Long MA unbroken runs of complete buckets, no coverage share anywhere; adds the frozen VWAP, Long MA and first-level candidate list - current |
| `nq_conv_v4` | convention | v3 plus the premarket window [08:00 ET, cutoff) for Premarket High / Low |
| `nq_conv_v3` | convention | v2 plus the five prior sessions' RTH prices on the snapshot contract (HTB-v1) |
| `nq_conv_v2` | convention | what P1/P2 leave open (below): v1 plus P1 section 8's events (EV-v1) and the TradingView moving averages |
| `nq_conv_v1` | convention | v1: events of the session's calendar day only, no moving averages |
| `nq_evidence_v5_r0929` | snapshot | research profile, cutoff 09:29:00 ET, under `nq_conv_v5` - current |
| `nq_evidence_v5_o0927` | snapshot | operational profile, cutoff 09:27:00 ET, under `nq_conv_v5` - registered, never built (a live-latency fallback) |
| `nq_evidence_v4_r0929` / `_o0927` | snapshot | the same under `nq_conv_v4`; registered, never built |
| `nq_evidence_v3_r0929` / `_o0927` | snapshot | the same under `nq_conv_v3` (no premarket window) |
| `nq_evidence_v2_r0929` / `_o0927` | snapshot | the same under `nq_conv_v2`; the 274 stored v2 snapshots, their annotations, analogue sets and the first pre-open review set stay as they are |
| `nq_evidence_v1_r0929` / `_o0927` | snapshot | the same under `nq_conv_v1`; the 269 stored v1 snapshots and the review set stay as they are |
| `nq_structure_rules_v4` | annotation | the rule-based pre-open structure annotation (stage 2, below): v3 over complete windows only - current |
| `nq_structure_rules_v3` | annotation | v2's labels, HTB-v1 worded as exclusive bands, its basis naming a close beyond the range; never stored |
| `nq_structure_rules_v2` | annotation | v1 plus Higher-Timeframe Bias (HTB-v1) |
| `nq_structure_rules_v1` | annotation | the same without Higher-Timeframe Bias |
| `nq_structure_llm_v2` | annotation | the Claude structure annotation (Appendix A, A1), `claude-opus-5-5`, Higher-Timeframe Bias from HTB-v1 - built, waiting for an API key |
| `nq_structure_llm_v1` | annotation | registered, never run: Claude judged Higher-Timeframe Bias itself |
| `nq_match_p1_v1` | matcher | P1 section 7's analogue rubric (stage 2B / 2C) |

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
| `first_level_tested` | `on_high`, `on_low`, `prev_rth_high`, `prev_rth_low`, `prev_rth_close`, `overnight_open`, `vwap`, `premarket_high`, `premarket_low`, `long_ma` | ON High, ..., Long MA; the price is the measurement `first_level_price` (impl2-impl3: the first seven) |
| `first_level_outcome` | LO-v2 below, without `not_tested` | First Level Outcome, over [09:30, 09:45) |
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

## Conventions (`nq_conv_v1`; `nq_conv_v2` adds events and moving averages)

Decisions P1/P2 leave to the implementation, recorded in the convention and label
definitions:

- **Completeness (`nq_conv_v5`, guideline revision 2, 1B, your decision
  2026-10-04):** a frozen value needs its whole window - every minute present
  exactly once - or it is unavailable with the gap named; nothing is computed from
  what is left, and no coverage share is used anywhere. Each 2m / 5m / 15m bucket
  row carries a `complete` flag (its minutes all present, none twice); a bucket
  missing a minute is kept and flagged, never filled. An ON or premarket window
  that is not whole keeps its observed extremes only as `provisional_high` /
  `provisional_low` diagnostics. On the 274 sessions this changes two: 2025-12-11
  and 2026-09-10 each miss the 18:00 minute, so their ON high / low, VWAP, Long MA
  and the overnight and MA structure fields are unavailable (v1-v4 used 90% of the
  overnight minutes).
- **Daily ATR:** Wilder ATR(14) on RTH sessions [09:30, scheduled close), each
  session on its active contract with the previous close from the same contract;
  the 70 true ranges of exactly the 70 sessions before the target, each needing
  every RTH minute of itself and of the session before, seeded with the mean of
  the first 14. One incomplete session leaves it unavailable (`incomplete_history`);
  v1-v4 skipped such sessions and searched back up to 105. Chart parity with a
  TradingView daily ATR is **not verified** - TradingView may use the Globex day or
  a continuous contract.
- **2-minute ATR:** Wilder ATR(14) on 2m buckets anchored on even ET minutes,
  built from the snapshot contract's 1m bars in the overnight window and complete
  by the cutoff (at 09:29 the last is 09:26-09:28); 70 true ranges from the last 71
  buckets, which must be consecutive, complete and end at the last bucket before the
  cutoff. Never the 1m ATR rescaled.
- **References:** previous RTH high / low / close need every RTH minute of the
  previous scheduled session (same contract); ON high / low need every overnight
  minute [18:00 the day before, cutoff) exactly once; overnight open is the bar
  starting 18:00 exactly; the cutoff price is the last complete 1m close if at most
  5 minutes old. **Price at 09:29 is always unavailable** (neither profile observes
  09:29:00-09:29:59): the 09:29 candle closes at 09:30:00, the open, so no pre-open
  snapshot can hold it; the research cutoff is 09:29:00, the close of the 09:28
  candle. **Premarket high / low** (`nq_conv_v4`, the user's TradingView premarket
  session 08:00-09:30 cut at the cutoff): the 1m bars of [08:00 ET, cutoff) - 08:00
  to 09:28 at the 09:29 cutoff - every minute (`nq_conv_v4` took 90%); unavailable
  in v1-v3. Moving averages: none in v1; v2 adds the TradingView lines (stage 2
  below). From `nq_conv_v5` the snapshot also freezes the **cutoff VWAP** (exact, every
  overnight minute), the **Long MA at the cutoff** (`moving_averages.long_ma_at_cutoff`:
  EMA(100) of every 2m bucket from 18:00, all complete and consecutive - the line
  the structure annotation uses) and the **first-level candidate list**
  (`first_level_candidates`: each candidate's price or why it is unavailable).
- **First level tested (P2 section 6):** the candidates are the snapshot's ON high
  / low, previous-RTH high / low / close, overnight open, the **cutoff VWAP** -
  sum(hlc3 x volume) / sum(volume) over the overnight 1m bars [18:00, cutoff),
  exact - and from impl4 the premarket high / low and the **cutoff Long MA**. impl5
  reads them from the snapshot's frozen candidate list (`nq_conv_v5`); on an older
  snapshot it derives the VWAP and Long MA from the archived bars under the same
  completeness rule. Round numbers / other named levels are not in the candidate
  set. A level is reached by a 1m bar with low <= level <= high, so a level the
  opening gap crossed without an RTH trade there is not reached. Within one bar a
  level equal to the bar's open is first, then the nearest on its side. Any
  unavailable candidate makes the first level unavailable (it could have been
  first).
- **FL-v3 (impl5, your decision 2026-10-04 under guideline revision 2):** levels
  reached on both sides of one bar's open are `ambiguous_intrabar` - a 1m bar does
  not show whether its high or its low came first. The level nearest the open is
  kept on file as a measurement, `first_level_estimate` / `first_level_estimate_price`
  (none for an exact tie in distance above and below), never as the label; P2's
  Outcome Data Notes name it. Candidates at one price are one level, named by
  precedence - previous RTH high / low / close, ON high / low, premarket high / low,
  overnight open, VWAP, Long MA - so a premarket high equal to the ON high is the ON
  high; `first_level_coincident` keeps the others. On the 274 sessions: named on 212
  (Long MA 55, premarket low 32, ON high 29, VWAP 22, premarket high 22, ON low 19,
  previous RTH high 13, low 9, close 8, overnight open 3), ambiguous 56 (each with an
  estimate), none reached 4, a missing candidate 2; 45 named by precedence.
  History: impl2-impl3 had the first seven candidates and left candidates at one
  price `coincident_levels` (233 named); impl4 (FL-v2, never recorded) took the
  nearest level as the label, flagged `estimated` (268 named, 56 of them estimated).
- **LO-v2 (impl5, guideline revision 2):** the original side is the side of O (a
  level equal to O has no resolvable approach); a breach is a **trade** strictly
  beyond the level - the wick; returns and acceptance use closes. Test and rejection
  is an exact touch with no trade beyond, then a later close strictly back on the
  original side; acceptance is the window's final three 1m closes strictly on one
  side, and those bars must be stored. Not tested needs the complete window and
  applies to the four session references only. LO-v1 (impl2-impl3) took a breach
  as a 1m close beyond the level, so a wick through that closed back was a test and
  rejection; on the 274 sessions LO-v2 moves 45 labels, all such wicks becoming a
  break (mostly break-reclaim-acceptance).
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
  least T" is low <= level - T, by an RTH bar; the reclaim must be a later bar than
  the first breach. **OS-v2 (impl5, guideline revision 2):** a level the opening
  gap crossed is a sweep reference like any other - the gap alone is never the
  breach, but a later RTH breach by T and a reclaim close make it a sweep, and the
  09:29 bar is not needed. impl2-impl4 left a level strictly between the 09:29 close
  and O out (no session of the 274 changes). A missing sweep reference only blocks
  the label when a sweep was possible at all (the window went below O - T for a
  support, above O + T for a resistance).

## Running it

```bash
python scripts/nq_journal.py outcomes --start 2025-09-01 --end 2026-09-25   # label stored snapshots under the current version
python scripts/nq_journal.py backfill --start 2025-09-01 --end 2026-09-25   # snapshot + outcome per session
python scripts/nq_journal.py backfill --start 2025-09-01 --end 2026-09-25 --profile operational_0927
python scripts/nq_journal.py show --date 2026-09-24                         # snapshot + P2's 40-field record
python scripts/nq_journal.py catch-up                                       # every final session not yet stored
```

**The collector keeps the journal current.** After every full collection
(`python -m collector.ib_collector` over all configured instruments, each future on
its front contract - so `run_pipeline.sh` too) it runs `catch-up`
([forecaster/journal.py](../forecaster/journal.py)): a research-profile snapshot and
outcome for every session since the journal's first that is final (two hours past
its close) and has no snapshot yet, outcomes for stored snapshots still without
one, and a re-check of the sessions the collector re-downloads (a vendor revision
becomes a new outcome revision). A stored snapshot is never rebuilt. A session
whose snapshot cannot be built is logged and retried next run. `--no-journal`
skips the step; a `--symbol` subset or a pinned contract skips it too, since
snapshots also read the context instruments.

Migration 0009 (the `journal` schema) is applied by the first process that
starts after it is pulled - the dashboard and the collector migrate on start.
A new label version needs no new snapshots: `outcomes` labels the stored ones.
A new convention does: impl5 under `nq_conv_v5` is recorded by `catch-up` (or the
collector's journal step), which builds a `nq_evidence_v5_r0929` snapshot for every
final session, labels it, annotates it under `nq_structure_rules_v4` and matches it.
The older versions' records stay as they are. Read
[docs/reports/label_disagreement_impl3_vs_impl5.md](reports/label_disagreement_impl3_vs_impl5.md)
first; `python scripts/label_revision_report.py` regenerates it.

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
the other 10 spread the set over the year. Reviewed 2026-10-04: all 25 sessions, every field agreed,
nothing flagged. The dashboard's **Review** page shows
each session's chart with the frozen levels the labels used (previous RTH close /
high / low, ON high / low, the cutoff VWAP) and O +/- T, beside P2's 40-field
record; every field counts as agreed unless marked disagree or unsure, with a
note. Verdicts are append-only rows (migration 0010, `journal.review_verdicts`;
`journal.review_latest` holds the one that counts). The review checks that the
labels mean what P1 / P2 say - not whether anything predicts them. Pre-open
classifications join the review with stage 2.

## Stage 2: pre-open structure and structural analogues

Built on 2026-10-04. Definitions in [contracts/nq_preopen.py](../contracts/nq_preopen.py).

- **Fields and vocabularies.** The P1 properties an annotation fills: Overnight
  Structure (ON-v1), Premarket Pattern, Short-Term Structure, 5- and 15-Minute
  Trend, Higher-Timeframe Bias, Price vs Long MA, Long MA Slope, Fast MA
  Alignment, Chop Score, Event Risk and Event Notes. Values are P1's own lists
  where it gives one, otherwise the live Notion Weekday Trades options P1 points to,
  copied read-only to [contracts/weekday_trades_schema.json](../contracts/weekday_trades_schema.json)
  (fetched 2026-10-04). Price location (P1 section 7) is the cutoff price Above /
  At / Below each of the five levels, At within one point.
- **Moving averages** (`nq_conv_v2`): the user's TradingView indicator on 2-minute
  bars - the long MA is EMA(100), the fast pair TEMA(14) + SMA(3) (faster) over
  EMA(14) + SMA(3) (slower). Computed from the snapshot's 2m bars, seeded at
  18:00; by the final pre-open hour the seed weighs under 0.05% in the EMA(100).
- **Event Risk (EV-v1)** meets P1 section 8 with a source for each part:
  - Scheduled releases: FOMC decisions and minutes, CPI, payrolls, PPI and JOLTS
    (existing), ISM by rule, and now **BEA** GDP and Personal Income and Outlays
    (PCE) and **Census** advance retail sales
    ([data/economic_calendar.csv](../data/economic_calendar.csv)). BEA's past
    releases are taken at the times its release archive shows them published - its
    2025 schedule still lists the Q3 advance GDP the shutdown cancelled - and later
    ones from its schedule; retail sales from Census's retail release schedule
    (which has the February and March 2026 reports its indicator calendar lacks).
  - Material Nasdaq-100 earnings: the ten largest constituents' 8-K Item 2.02
    filings from SEC EDGAR at their acceptance time
    ([database/earnings.py](../database/earnings.py); set `SEC_USER_AGENT` to
    "name e-mail" as SEC asks). Coverage reaches a date only when a fetch ran after
    its cutoff; a failed fetch records none.
  - The rule: High-risk for a high-tier release from the previous session's close
    to the session's close; else Reduced-confidence for a moderate release or a
    material earnings release published since the previous close (single-name
    repricing at the open); else Normal - only when every source covers the
    session, otherwise unavailable. Event Notes list each event with its ET time,
    released pre-open or upcoming. Unscheduled shocks have no source and are not
    detected; P1 bases Normal on scheduled risk.
  - Snapshots (`nq_evidence_v2_*`) hold scheduled releases from the previous
    session's close to the end of the day and earnings only up to the cutoff.
- **Rule-based structure annotation** (`nq_structure_rules_v4`,
  [forecaster/structure_rules.py](../forecaster/structure_rules.py)): the stand-in
  for the Claude structure annotation (Appendix A, A1) until the API is in place.
  It reads one frozen snapshot only and returns the shape A1 will return - per
  field a value or null with a reason, a status, evidence ids inside the snapshot
  and a short basis with the numbers - plus the price location and the numeric
  layer (2/2 swing points on 5m bars confirmed by the cutoff, the moving averages,
  window statistics) in `measurements`. A target-session item after the cutoff
  makes it `contaminated`, with no classifications. Its thresholds are a trial
  convention (`RULES`), not P1's text: P1 does not quantify dominant, meaningful,
  flat or frequent. Higher-Timeframe Bias follows your rule HTB-v1 (below).
  On the 274 stored sessions it gives Overnight Structure Mixed 80, Uptrend 54,
  V-reversal 48, Downtrend 34, Inverted-V 33, Range 25. From v4 every field needs
  its whole window as an unbroken run of complete buckets (`nq_conv_v5`): the
  overnight structure every minute of [18:00, cutoff); the premarket pattern and
  short-term structure the 5m buckets of the last three hours and the two before
  them (the swing look-back); each trend its 12 buckets ending at the last one
  before the cutoff; the four MA fields every 2m bucket from 18:00. v1-v3 used 90% of
  the overnight minutes, 80% of the premarket hour and 10 of 12 trend bars; v4
  changes the two sessions with a missing 18:00 minute (their overnight structure
  and MA fields become unavailable) and nothing else.
- **Store**: `journal.structure_annotations` (migration 0011, append-only), one row
  per snapshot, protocol and output; a rules protocol may annotate a snapshot only
  once. The collector's journal step loads the calendar, refreshes the earnings and
  annotates every snapshot without an annotation.

- **Higher-Timeframe Bias (HTB-v1, your rule):** the cutoff price against the
  highest RTH high H5 and lowest RTH low L5 of the previous five completed sessions,
  on the snapshot contract (the snapshot freezes their RTH open / high / low / close,
  `prior_sessions`, `nq_conv_v3`), and the momentum m = (cutoff - the RTH open five
  sessions back) / the frozen daily ATR. Thirds of the range by position
  p = (cutoff - L5) / (H5 - L5): upper p >= 2/3, lower p <= 1/3, inside the range.
  Exclusive bands: Bullish above H5, or the upper third with m >= +0.5;
  Neutral-bullish the upper third with +0.15 < m < +0.5; Neutral the middle third,
  or |m| <= 0.15; the bearish ones mirrored. Fewer than five verified prior sessions
  (every RTH minute on the contract) or no daily ATR: unavailable. Two cases are not
  settled by the bands: a close beyond the range with |m| <= 0.15 is in the breakout
  band and Neutral at once - the breakout wins (it does not occur in the 274
  sessions); the upper third with m < -0.15, or the lower third with m > +0.15, is in
  no band and is unavailable as `uncovered`. `nq_structure_rules_v2` worded the same
  rule as an ordered list (a breakout, then Neutral, then the neutral-bullish /
  -bearish bands) with identical labels; `nq_structure_rules_v3` registers the
  exclusive wording and names a close beyond the range as such in the basis. On the 274 sessions: Bullish 99, Neutral 77, Bearish
  55, Neutral-bullish 11, Neutral-bearish 4, unavailable 28 (19 without a daily ATR
  in September 2025, 9 uncovered). Not a matcher input.
- **Claude structure annotation** (`nq_structure_llm_v2`,
  [forecaster/structure_llm.py](../forecaster/structure_llm.py)), built and tested
  against a stand-in client; it runs once `ANTHROPIC_API_KEY` is in `.env`. The
  system prompt [prompts/runtime/structure_annotation_v2.md](../prompts/runtime/structure_annotation_v2.md)
  quotes Appendix A's A1 and P1 section 4 verbatim and adds the conventions and
  vocabularies; its hash is part of the registered protocol, so an edit needs a new
  version. The user message is the evidence bundle - references, the overnight 5m
  and 15m bars, the last 45 2m bars with the three lines, the confirmed swing
  points, every item with an id - and the answer must follow the
  structure_annotation JSON schema (structured outputs). `claude-opus-5-5`, effort
  high. Claude annotates the nine descriptive fields; Event Risk, Event Notes,
  Higher-Timeframe Bias and the price location come from the same rules as the
  rule-based protocol. Validation:
  allowed values, null exactly when unavailable (with a reason), every evidence id in
  the bundle. Every request is an attempt in `journal.annotation_attempts`, failures
  included; only a valid answer becomes an annotation. Live requests have the
  server-side refusal fallback on, but an answer another model served is kept as an
  attempt, not as an annotation of this protocol. A snapshot holding anything after
  its cutoff is never sent. The historical backfill goes through the Batch API at
  half price; `--estimate` sizes it first.

### Analogues (2B-2D)

[matching/structural.py](../matching/structural.py), matcher `nq_match_p1_v1`:

- **Rubric (P1 section 7, exact weights):** price location against each session's
  own five levels 6% each; Overnight and Short-Term Structure 12.5% each; 5- and
  15-Minute Trend, Price vs Long MA, Long MA Slope 6.25% each; Premarket Pattern and
  Chop Score 5% each; Event Risk 10%. Categorical equality 1 / 0; Chop Score
  max(0, 1 - |a - b| / 3). A feature counts only when both sessions have a
  classified value under the same annotation protocol and snapshot version;
  comparable weight under 75% rejects a candidate; similarity = 100 x weighted
  matches / comparable weight.
- **Pool and selection:** earlier NQ sessions only, never the target or later; the
  five highest similarities, ties by comparable weight, then the more recent
  session, then the snapshot id; no minimum similarity; zero analogues allowed.
  Exclusions are counted by reason.
- **Outcomes after selection (2C):** each analogue's latest stage-1 outcome; per P1
  target the class counts over the analogues with a label and that denominator (an
  analogue without one is kept, not replaced), the unweighted mean similarity, and
  a separately named smoothed baseline (count + 5 x prior) / (n + 5), the prior from
  every earlier session's label; no analogue label gives the prior only, said so.
- **Store (2D):** `journal.analogue_sets` / `journal.analogue_members` (migration
  0012, append-only): pool size and hash, exclusions, per-feature components,
  similarity and coverage per member, the outcome revisions used and the outcome
  summary; a set is new only when its pool or its outcome revisions change.
  Historical sets are marked `historical_reconstruction`: their outcomes were
  computed after the fact.
- **Dashboard:** **Analogues** (`/analogues`) puts a session and its analogues side
  by side - each feature cell a match, a mismatch, partly similar or not comparable -
  with the similarity and coverage of each; outcomes stay hidden until "Show
  outcomes", so the page first serves the outcome-blind check of why each analogue
  qualifies. Clicking a date charts that session's own pre-open (its own contract
  and prices, never rebased). **Pre-open review** (`/preopen-review`) is the
  outcome-blind review of the annotations: the overnight chart to the cutoff with
  the three lines, every field with the numbers behind it, a verdict and a note per
  field (`journal.annotation_review_*`).

```bash
python -m database.events                                   # the calendar (also run by the collector)
python -m database.earnings                                 # earnings from EDGAR (also run by the collector)
python scripts/nq_journal.py annotate --start 2025-09-01 --end 2026-10-02
python scripts/nq_journal.py match                          # analogue sets (also run by the collector)
python scripts/nq_journal.py analogues --date 2026-10-02 [--outcomes]
python scripts/nq_journal.py annotation-review-set --name preopen_review_v1
python scripts/nq_journal.py annotation-review-report --name preopen_review_v1
python scripts/nq_journal.py annotate-llm --start 2025-09-01 --end 2026-10-02 --estimate
python scripts/nq_journal.py annotate-llm --start 2025-09-01 --end 2026-10-02 --batch
python scripts/nq_journal.py match --protocol llm           # analogues over Claude's annotations
python scripts/nq_journal.py show --date 2026-10-02         # snapshot, annotation and outcome
```

The collector's journal step does everything above except the review set and the
Claude requests, which cost money and are started by hand.

### P1's 47-field pre-open record (Appendix B)

[forecaster/preopen_display.py](../forecaster/preopen_display.py) is the display /
export adapter Appendix B asks for: P1 section 9's 47 properties in order, each
from the component Appendix B makes its owner, with a provenance line (versions,
origin, target date, the actual cutoff and its price). It is kept apart from P2's
40-field outcome record and never overwrites it. `nq_journal.py show` prints it and
the Analogues page has it under "P1 pre-open record".

| P1 fields | Owner here | Status |
|---|---|---|
| 1-3 Day, Weekday, Contract | snapshot identity, exchange calendar | done |
| 4-7 Previous RTH High / Low / Close, Overnight Open | snapshot references on the snapshot contract | done |
| 8-9 ON High / Low | snapshot references, every overnight minute (`nq_conv_v5`) | done |
| 10-11 Premarket High / Low | snapshot references, every minute of [08:00 ET, cutoff) (`nq_conv_v4`, `nq_conv_v5`) | done |
| 12 Price at 09:29 | neither profile observes 09:29:00-09:29:59; the cutoff price is a separate field, never renamed | unavailable by design |
| 13-14 Daily ATR, 2-Min ATR at 09:29 | snapshot ATRs; the 09:27 profile shows 2-Min ATR at 09:29 unavailable | done |
| 15-19 structure, trends, Higher-Timeframe Bias | structure annotation (rules now, Claude later); HTB-v1 | done |
| 20-22 MA fields | 2m TradingView lines + the rules convention | done |
| 23-24 Premarket Pattern, Chop Score | outcome-blind annotation | done |
| 25-36 predictions, probabilities, first level, confidence | the forecast | stage 3 |
| 37-41 analogue count, relation, dates, scores, mean | matcher `nq_match_p1_v1`; the Notion relation is not written (no export) | done, relation unavailable |
| 42-45 targets | the forecast | stage 3 |
| 46-47 Event Risk, Event Notes | EV-v1 | done |

The database names the counterparts by what they are - `references.cutoff_price`,
`atr.two_minute` with its last bucket - not by a time they were not observed at. A
Notion export is not built; it would verify the live schema first (the read-only
copy is [contracts/weekday_trades_schema.json](../contracts/weekday_trades_schema.json)).

## Not built yet

- Running the Claude structure annotation (needs `ANTHROPIC_API_KEY`), and your
  verdicts on the pre-open review set.

- The operational profile (09:27) is registered but not caught up automatically;
  run `backfill --profile operational_0927` when live runs need it.
- Live capture with receipt provenance, the 09:29:50 deadline and the
  inference-attempt log (needs a real-time feed again; stage 3).
- Scoring predictions against outcomes (nothing predicts yet).
