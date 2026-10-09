# RTH analogues: matching the opening as it develops

Matcher `nq_match_rth_v2` ([contracts/nq_rth.py](../contracts/nq_rth.py),
[matching/rth.py](../matching/rth.py), [forecaster/rth_analogues.py](../forecaster/rth_analogues.py)).

The pre-open analogues ([nq_prompt_v2.md](nq_prompt_v2.md#analogues-2b-2d)) are frozen at
09:29 ET. After the open they still describe the night, not the morning. The RTH set answers a
different question, again and again through the first hour: **which earlier sessions opened
most like this one, over the same minutes?**

It is a description of resemblance, not a forecast. Similarity scores are agreement between
two observed openings and are **never calibrated probabilities**. Whether the analogues'
continuations say anything about this session's is a separate question, with its own
[predefined evaluation](#the-predefined-evaluation).

The record is meant to answer, for every set: **what matched, using what information, at what
time.**

## Versions

| Version | Registered | What changed |
|---|---|---|
| `nq_match_rth_v1` | 2026-10-09 09:35 UTC, by the first `rth-backfill` (774 reconstructions at 15/30/60 minutes over 258 sessions, kept) | — |
| `nq_match_rth_v2` | on its first issue or backfill | After an outside review: confirmation by any later bar (v1 required the next minute's bar, so a missing minute also dropped the bar before it). Issuance mode and input receipt times recorded. The calibration's provenance is part of the definition. The 45-minute window is always issued. Weights, tolerances and features are unchanged. |

The dashboard and the CLI read v2 sets. v1's sets stay in the journal as history.

## What is unchanged

- **Pre-open path:** the pre-open snapshot, its 09:29 cutoff and its guards, the rule-based
  annotation, `nq_match_p1_v2`, its analogue sets and the forecasts built on them are all
  untouched.
- **Separate storage:** the RTH matcher has its own version, its own definition kind
  (`rth_matcher`) and its own tables (migrations 0024 and 0025). A pre-open set and an RTH
  set can never be confused, and no pre-open cutoff guard was loosened to make room for it.

## Timing

- **Clock:** America/New_York session times from the trading calendar, which handles
  daylight saving. The open is 13:30 UTC in summer and 14:30 UTC in winter.
- **Window:** expanding from the 09:30 open, never rolling. At *n* minutes, the target's
  first *n* confirmed one-minute RTH bars, [09:30, 09:30 + *n*), are compared with every
  candidate's first *n*. At 09:42 that means today's first 12 minutes against each earlier
  session's first 12.
- **Confirmed bars only:** the collector can store the bar that is still forming. A bar counts
  only once **any later bar of the same session** is stored. The window is the unbroken run of
  such bars from 09:30.
- **Where a window stops:** the reason is recorded on the set (`quality.stopped`,
  `stopped_at`) and shown:

  | State | Meaning |
  |---|---|
  | `awaiting_confirmation` | The next minute's bar is stored but nothing after it yet. It may still be forming. |
  | `not_stored` | The next minute's bar is not stored and nothing later is. The feed is behind. |
  | `gap` | The next minute's bar is missing while later bars are stored. This is a confirmed hole in the observed session: the window stops before it and nothing is filled in. |
  | `complete` | All 60 minutes are confirmed. |

  The last candle of the hour, 10:29, is confirmed by the 10:30 bar. If that bar is missing,
  any later bar of the session confirms it. Until one arrives, the window stays at 59 minutes,
  awaiting confirmation. All of this is tested.
- **Before the first bar:** until the 09:30 bar is confirmed (09:31 at the earliest, later on a
  delayed feed), there is no RTH set and the pre-open set stays on screen.
- **Refresh:** every confirmed minute, up to 60 (10:30 ET). Matching is independent of the
  chart's timeframe.
- **Checkpoints:** 15, 30 and 60 minutes (09:45, 10:00, 10:30 ET) are marked on the set's own
  row (`checkpoint`); no duplicate record is made. The 45-minute window (10:15, a cutoff of the
  predefined evaluation) is always issued too.
- **Stop:** nothing is issued automatically after 10:30. The last set stays available.

## Features and weights

**The unit:** every price difference is expressed in the session's own **daily Wilder
ATR(14), frozen in its pre-open snapshot**. That is known before the open. Nothing is ever
normalised by the session's eventual range, high, low or close.

| Group | Feature | Weight | Definition |
|---|---|---:|---|
| Opening path (65) | path | 25 | Root mean square, over the window's minutes, of the difference between the two sessions' (close<sub>i</sub> − RTH open) / ATR |
| | net move | 15 | (close at the cutoff − RTH open) / ATR |
| | range | 10 | (highest high − lowest low in the window) / ATR |
| | deepest pullback | 7.5 | Largest fall from a running high within the window, / ATR |
| | largest recovery | 7.5 | Largest rise from a running low within the window, / ATR |
| Location (20) | vs VWAP | 10 | (close − the Globex day's VWAP through the cutoff, the chart's VWAP) / ATR |
| | in overnight range | 5 | (close − frozen ON low) / (ON high − ON low), clipped to [−1, 2] |
| | vs prev. close | 5 | (close − frozen previous RTH close) / ATR |
| Volume (5) | relative volume | 5 | ln(window volume / mean volume of the same window over the 20 scheduled sessions before); not comparable unless 10 of them hold the whole window |
| Pre-open context (10) | gap | 5 | (RTH open − frozen previous RTH close) / ATR |
| | overnight range | 5 | (frozen ON high − frozen ON low) / ATR |

**Scoring:**

- **Feature score:** `max(0, 1 − |target − analogue| / tolerance)`.
- **Similarity:** `100 × Σ weight × score / comparable weight`.
- **Coverage floor:** at least 75 % of the weight must be comparable. A missing input (a
  frozen level that is not valid, too few sessions for relative volume) is neither a match
  nor a mismatch.

**Tolerances** are the difference at which a feature scores 0. For the five path features
(path, net move, range, pullback, recovery) the stated tolerance applies at 30 minutes and is
multiplied by √(*n* / 30) at other windows. An opening path's spread grows roughly with the
square root of its length; measured, the spread at 15 and 60 minutes is within about 10 % of
that rule.

| path | net move | range | pullback | recovery | vs VWAP | in ON range | vs prev. close | rel. volume | gap | ON range |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.44 | 0.53 | 0.27 | 0.28 | 0.26 | 0.49 | 1.0 | 1.1 | 0.44 | 0.94 | 0.55 |

### Calibration and its provenance

The calibration is stored with the matcher version (`DEFINITION["calibration"]`):

- **Method:** for each feature, the median absolute difference over every pair of sessions at a
  30-minute window (for path, the root mean square path difference). Tolerance = 2 × median, to
  two significant figures, so a typical pair scores about 0.5 on a feature.
- **Sample:** 258 NQ sessions, **2025-09-29 to 2026-10-07**. These are all the sessions with an
  `nq_evidence_v5_r0929` snapshot, a valid frozen daily ATR and a whole 30-minute window, in
  the production store as of 2026-10-09.
- **What was looked at:** feature values at the 30-minute cutoff only. No outcome and no bar
  after any cutoff.
- **Reproduce it:** `python scripts/nq_journal.py rth-calibrate --end 2026-10-07` prints the
  medians beside the registered tolerances. It stores nothing.

**What this means for the record:** those 258 sessions set the scales. A set of a session
inside the sample (`quality.in_calibration_sample`) therefore uses knowledge from later
sessions. That is fine for describing what matched, and the header says so. It is **not a
faithful historical forward test**. A forward evaluation uses only sessions after 2026-10-07,
or recalibrates on its own training history under a new version and freezes that for the
evaluation period.

## Pool and selection

- **Candidates:** every earlier scheduled NQ session (never the target date or later) that has:
  - a pre-open snapshot of the profile's version. The newest is used; the context reads only
    its daily ATR, previous RTH close and ON high / low;
  - a valid frozen daily ATR;
  - its whole window of confirmed bars on its active contract.
- **Re-scored every minute:** the whole pool is scored again at each window. It is not limited
  to the five pre-open analogues, so a session that opened unlike the pre-open reading can
  enter the top five at any minute.
- **Exclusions:** every excluded session is counted under its reason: `not_earlier`,
  `other_symbol`, `no_preopen_context`, `incomplete_window`, `low_coverage`.
- **Selection:** the five highest similarities. Ties go to the higher comparable weight, then
  the more recent session.

## Point in time

- **Same minute on both sides:** target and candidates stop at the same elapsed RTH minute.
  Frozen pre-open context only.
- **What came after is display only:** what a candidate did after the cutoff is never an
  input to a score, a rank or the set's identity. The Session Explorer draws it grey beside the
  session, after selection.
- **Tested:** changing the target's or a candidate's bars after minute *n* changes neither
  the ranking at *n* nor its input digest.

## Storage: `journal.rth_analogue_sets` and `journal.rth_analogue_members`

Each set records:

- its session, contract and matcher version;
- the pre-open snapshot its context came from;
- `elapsed_minutes` and `cutoff_at`;
- the input digest;
- the pool size, hash and exclusions;
- the target's features;
- the data quality: provisional or not, why the window stops, the newest bar stored when the
  set was made, and whether the session is in the calibration sample;
- the code revision;
- its provenance (below);
- its five members with ranks, similarities and per-feature components.

**Idempotent:** one set per (session, version, window, input digest). The digest covers the
target's window bars, context and features, and every scored candidate's session, context
snapshot and features. Repeated runs on unchanged inputs store nothing new.

**Revised data never overwrites:** a vendor revision of a bar inside a window, or a new
candidate session, gives a new digest and a new set beside the earlier one. The tables are
append-only.

### How a set was produced, and from what (migration 0025)

| Field | Meaning |
|---|---|
| `issued_by` | `auto` (Auto mode, after a successful collection), `manual` (a person: `rth-issue`, or Update data → Run collector) or `backfill` (`rth-backfill`). The 774 v1 sets were backfills and are marked so. |
| `created_at` | When the set was stored, by the **database clock** (a trigger). |
| `data_mode` | `live` only when issued by auto or manual within 30 minutes of the cutoff. Everything else is `historical_reconstruction`, and a backfill always is, however soon it runs. |
| `inputs_received_at` | When every input of the target was in the store: its window's bars, the bar that confirmed the last of them, its overnight bars and its snapshot. Its distance from the cutoff is the feed's delay. |
| `pool_received_at` | The same bound over every earlier session read. |
| `pit_status` | `verified`: every earlier input was in the store by the cutoff, and the target's within 30 minutes of it, as a live issue would have had them. Otherwise `unverified`, for example a reconstruction from bars stored or revised later, or with unknown receipt times. |

**Where the receipt times come from:** `bars.version_stored_at` is when a bar's current values
reached the store, and snapshots carry `built_at`. Migration 0023 reset every earlier value's
receipt time to unknown, and the collector stamps every value stored since. So a value without
one was in the store by the time 0023 was applied, and that time is used as its bound.

**Why this matters:** timing alone cannot show that historical inputs were available at a
claimed cutoff. These fields can, for every set issued from now on. A reconstruction of an old
session is honestly `unverified`.

### Two views of the record

| View | What it returns |
|---|---|
| **As issued** (`rth_set_issued`) | Of the live sets, the newest stored by the time replayed. A correction stored later, or a backfill, never appears here. With a window given, the newest live set of that window. |
| **Reconstructed** (`rth_set_at`) | The newest calculation of the longest stored window not past the minute replayed, live or not. It may be a correction stored hours later, and is labelled as such. |

Neither view ever returns a later window: a review at 09:45 never sees the 10:30 set. A test
covers the case where a later correction changes the rankings at an earlier cutoff:
Reconstructed shows the correction, As issued never does.

## Running it

- **Auto:** the dashboard's Auto runs `nq_journal.py rth-issue --by auto` after the collector.
  **Update data → Run collector** runs it with `--by manual`. Both run from the first confirmed
  RTH minute until 11:00 ET, and only when the collection succeeded
  (`dashboard/jobs.NEEDS_SUCCESS`).
- **What one issue stores:** the newest confirmed window, plus any of 15, 30, 45 and 60 not
  yet stored. If two bars arrive at once, the window between them is still saved.
- **Duplicate jobs:** one dashboard runs one job at a time, and only one process runs Auto. An
  issue also holds a database advisory lock: a second issue running at the same moment, from
  another process, stores nothing and says so.

```bash
python scripts/nq_journal.py rth-issue                                   # the session in progress (by hand)
python scripts/nq_journal.py rth-backfill --start 2025-09-02 --end 2026-10-08   # reconstructions at 15/30/60
python scripts/nq_journal.py rth-backfill --date 2026-10-07 --all-minutes       # every window of the first hour
python scripts/nq_journal.py rth-show --date 2026-10-07 --minute 15      # reconstructed view at 09:45
python scripts/nq_journal.py rth-show --date 2026-10-12 --view issued --at 10:05   # as issued by 10:05 ET
python scripts/nq_journal.py rth-calibrate --end 2026-10-07              # reproduce the calibration
```

The first `rth-issue` or `rth-backfill` registers `nq_match_rth_v2`. From then on its
definition is frozen: changed weights, tolerances, features or calibration need a new version
name.

## Dashboard

The Session Explorer's analogues section switches between **Pre-open set (saved)** and **RTH
set (evolving)**.

- **Which set shows:** until the session's first RTH set is stored, the pre-open set; after
  that, the RTH set, unless the other is picked.
- **Views:** **As issued** and **Reconstructed** are always switchable. A day with live sets
  opens on As issued; a day without any opens on Reconstructed. In playback, As issued shows
  what had been issued live by the candle played to. On a delayed feed that is an earlier
  window than the candle, and the header says so.
- **The header line:** "As issued: RTH analogues — first 23 minutes — data through 09:53 ET".
  It then says:
  - checkpoint or not;
  - provisional, for a window under 10 minutes;
  - how it was issued (by Auto, by hand, by a backfill) and when;
  - how long after the cutoff the window's bars were in the store;
  - whether its inputs are verified as of the cutoff;
  - whether its session is in the calibration sample;
  - why its window stops, if not complete;
  - for the session in progress, how far behind the clock the matches are.
- **Window:** **Follow** shows the newest window, or in playback the one replayed. Any stored
  window can be picked instead; checkpoints, live issues and revised windows are marked.
- **The comparison table:** every feature of the session and of each analogue, coloured by the
  feature's score.
- **The chart beside the session:** the analogue's day; its candles after the matched minutes
  are grey, and the caption says "matched 09:30–09:53 · grey: what followed, never matched".
- **When a set updates:** the analogue being compared is kept if the new set still holds it,
  and both charts keep their zoom.
- **Observed windows:** for the session in progress, a P1 target whose window has ended (first
  move 09:35, the 15-minute targets 09:45, opening bias 10:00) is marked as observed in the
  pre-open set's outcome rows and frequencies, and in the forecast's per-target table. It is
  no longer a forecast of anything still to come.

## The predefined evaluation

`rth_continuation_v1` is defined in
[contracts/rth_eval.py](../contracts/rth_eval.py), with a copy in
[rth_continuation_v1_definition.json](rth_continuation_v1_definition.json) (hash
`977d4c124a257b14`, pinned by a test). It was fixed **before any result exists**. Nothing
scores it yet and nothing registers it.

**The question:** at 09:45, 10:00 and 10:15 ET, do the analogues' **next 15 minutes** describe
the session's next 15 minutes better than the frozen pre-open analogue set and a same-clock
history? Movement size and direction are measured separately.

**Sample:**

- forward sessions only, after the calibration sample (after 2026-10-07);
- only cutoffs with a **live** set; a missing one is reported, never filled from a
  reconstruction;
- results are not looked at before 60 sessions hold all three cutoffs, and are then scored
  once.

**Forecasts:**

| Name | Role | Members |
|---|---|---|
| RTH-20 | Primary | The 20 highest similarities, recomputed with the frozen matcher. Accepted only when its input digest equals the live set's. |
| RTH-5 | Secondary | The five members of the set as issued, i.e. what was on screen. |
| PRE-5 | Baseline | The frozen pre-open analogue set. |
| CLOCK | Baseline | Every eligible earlier session over the same clock window. |

**Scores:**

- **Size:** fair ensemble CRPS of the absolute move.
- **Direction:** Brier score of a Laplace-smoothed up-share.
- **Signed:** a secondary CRPS of the signed move.

Moves are in each session's frozen daily ATR.

**Uncertainty and decision:**

- Paired per-session differences, averaged over the session's cutoffs.
- Intervals from a moving-block bootstrap over sessions.
- Four primary comparisons (RTH-20 against CLOCK and against PRE-5, for size and for
  direction), each at 98.75 %.
- A comparison shows an improvement only when its whole interval is below zero. Otherwise:
  "no sufficiently reliable improvement was established".
- The five displayed analogues are never treated as the probability model.

## What this is not

- **Not a forecast:** no outcome, frequency or path of the analogues is aggregated, overlaid
  or turned into one on the dashboard.
- **No direction claim:** earlier work found NQ first-hour direction unpredictable from pre-open
  matches, momentum and 15/30-minute matching (2026-09).
- **Usefulness is untested** until the predefined evaluation has its 60 forward sessions.
