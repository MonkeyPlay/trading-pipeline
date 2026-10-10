# RTH analogues: matching the opening as it develops

Matcher `nq_match_rth_v2` ([contracts/nq_rth.py](../contracts/nq_rth.py),
[matching/rth.py](../matching/rth.py), [forecaster/rth_analogues.py](../forecaster/rth_analogues.py)).

The pre-open analogues ([nq_prompt_v2.md](nq_prompt_v2.md#analogues-2b-2d)) are frozen at
09:29 ET. After the open they still describe the night, not the morning. The RTH set answers a
different question, again and again through the first hour: **which earlier sessions opened
most like this one, over the same minutes?**

It is a description of resemblance, not a forecast. Similarity scores are agreement between
two observed openings and are **never calibrated probabilities**. Whether the analogues'
continuations say anything about this session's is a separate question, with two
evaluations fixed before their forward sample and collected side by side:

- [`rth_continuation_v2`](#the-research-evaluation-rth_continuation_v2): **research only** -
  forecast skill from delayed, cutoff-frozen inputs. "Eligible" there means eligible for that
  experiment, never timely for trading.
- [`rth_operational_v1`](#the-operational-evaluation-rth_operational_v1): **operational** - a
  15-minute window that starts after the forecast is stored.

The record is meant to answer, for every set: **what matched, using what information, at what
time.**

## Versions

| Version | Registered | What changed |
|---|---|---|
| `nq_match_rth_v1` | 2026-10-09 09:35 UTC, by the first `rth-backfill` (774 reconstructions at 15/30/60 minutes over 258 sessions, kept) | — |
| `nq_match_rth_v2` | on its first issue or backfill | After an outside review: confirmation by any later bar (v1 required the next minute's bar, so a missing minute also dropped the bar before it). Issuance mode and input receipt times recorded. The calibration's provenance is part of the definition. The 45-minute window is always issued. Weights, tolerances and features are unchanged. |
| `nq_match_rth_v3` | on its first issue or backfill (with migration 0031) | **The full session**, beside v2: the same matching from the open to the scheduled RTH close, each confirmed minute, with its own timing record and evaluation (`rth_session_v1`). v2 stays the first hour, unchanged. See [The full session](#the-full-session-nq_match_rth_v3). |

Who launched the v1 backfill is not confirmed: no Claude session on this machine ran it.

The CLI reads v2 sets. The dashboard shows a day's v2 sets; on a day without any it shows v1's
reconstructions for review, labelled as a superseded version. v1's sets stay in the journal as
history.

## What is unchanged

- **Pre-open path:** the pre-open snapshot, its 09:29 cutoff and its guards, the rule-based
  annotation, `nq_match_p1_v2`, its analogue sets and the forecasts built on them are all
  untouched.
- **Separate storage:** the RTH matcher has its own version, its own definition kind
  (`rth_matcher`) and its own tables (migrations 0024 to 0027). A pre-open set and an RTH
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

**Idempotent:** one set per (session, version, window, input digest) and kind of issue. The
digest covers the target's window bars, context and features, and every scored candidate's
session, context snapshot and features. Repeated issues, or repeated backfills, of unchanged
inputs store nothing new. An issue by Auto or by hand is still recorded when a backfill of the
same inputs came first, so the backfill never stands in for it; a backfill of inputs already
issued adds nothing (migration 0027).

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
  yet issued (a backfill does not count as issued). If two bars arrive at once, the window
  between them is still saved. For 15, 30 and 45 the run also stores both evaluations'
  forecasts.
- **Collection must be running:** sets are issued only while Auto runs during the first hour.
  Auto belongs to the dashboard process, so it keeps running with the browser tab closed, but
  not when the dashboard process stops. A session without Auto is counted `not_issued`.
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
python scripts/nq_journal.py rth-eval-status                             # both evaluations' health, never a score
python scripts/nq_journal.py rth-eval-score --version rth_operational_v1  # one scoring, at its endpoint only
```

The first `rth-issue` or `rth-backfill` registers `nq_match_rth_v2`, `rth_continuation_v2` and
`rth_operational_v1`. From then on they are frozen: changed weights, tolerances, features,
calibration or evaluation rules need a new version name.

Production runs from `~/trading_pipeline` on `main` (README, [Database](../README.md#database)): a
new migration reaches the store only through its explicit apply step.

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

## The research evaluation (`rth_continuation_v2`)

**Research only: forecast skill from delayed, cutoff-frozen inputs.** Its window is the 15
minutes after the matching cutoff, which on the delayed feed have mostly passed in the market
by the time the forecast is stored. "Eligible" means eligible for this experiment, never timely
for trading. Every status report and result carries that label, and shows each forecast's
actual issue delay and how much of its window was still ahead when it was stored.

`rth_continuation_v2` is defined in [contracts/rth_eval.py](../contracts/rth_eval.py), with a
copy in [rth_continuation_v2_definition.json](rth_continuation_v2_definition.json) (hash
`0308613ba27505d0`, pinned by a test). It is registered (kind `rth_evaluation`) by the first
RTH issue, in the same run and before that run stores any evaluation forecast. So it is fixed
before the forward sample starts.

`rth_continuation_v1` (hash `977d4c124a257b14`) was committed and superseded before any data.
It would have rebuilt RTH-20 at scoring time and had no rule for issue time.

**The question:** at 09:45, 10:00 and 10:15 ET, do the analogues' **next 15 minutes**
describe the session's next 15 minutes better than the frozen pre-open analogue set and a
same-clock history? Movement size and direction are measured separately.

### Forecasts are stored when issued

The run that issues a cutoff window's live RTH set also stores the evaluation's four forecasts
from the same ranking and inputs (`journal.rth_eval_forecasts`, migration 0026):

| Name | Role | Members |
|---|---|---|
| RTH-20 | Primary | The 20 highest similarities of the matcher's ranking at the cutoff |
| RTH-5 | Secondary | The live set's five members, i.e. what was on screen |
| PRE-5 | Baseline | The session's pre-open analogue set (`nq_match_p1_v2`), the newest stored at issue |
| CLOCK | Baseline | Every session of the matcher's scored pool at the cutoff |

- **What is stored for each member:** session, context snapshot, contract, similarity, an equal
  weight, and its own move over the same clock window from its bars as stored at issue. The
  versions and the pre-open set id are stored with the forecast.
- **Never rebuilt:** the scorer only adds the target's realised move. A later vendor revision
  or a bigger history changes nothing that was issued.
- **One per session and cutoff:** the first stored counts.

### Eligibility is apart from provenance

Who issued a set and when its inputs reached the store are provenance, recorded on the set.
Whether a forecast counts is decided separately, by the database's stamp of when the forecast
was stored:

- it counts only when stored **within 14 minutes of its cutoff**, so before its 15-minute
  window ends;
- a forecast stored after its window ends never counts;
- the limit is read from the registered definition.

**Why 14 minutes:** on the one session with receipt times (2026-10-07), first-hour bars reached
the store a median 10.2 minutes after their minute ended (90 % by 10.3, worst 16.0). A set for
cutoff *C* is therefore issued about *C* + 12 at the earliest.

**What that means:** on this delayed feed, every forecast arrives about 12 minutes into its
window. The evaluation measures the information at the cutoff, out of sample, since nothing
after the cutoff is read. It does **not** measure a tradeable lead time.

### Cases, endpoint and decision

- **Universe:** every scheduled NQ session from the registration day, at each cutoff.
- **Excluded cases:** each case is reported under the first reason that applies, in this order:

  | Reason | Meaning |
  |---|---|
  | `not_issued` | No forecast stored for the cutoff. |
  | `late` | Stored after the delay limit. |
  | `unverified_inputs` | The set's inputs were not verified as of the cutoff. |
  | `forecast_incomplete` | Too few members with a move: RTH-20 < 10, RTH-5 < 3, PRE-5 < 3, CLOCK < 30. |
  | `outcome_pending` | The window has not ended, or its bars are not confirmed yet. |
  | `outcome_missing` | A gap in the window's bars; never filled in. |

- **Counted sessions:** a session counts toward the endpoint when at least one of its cutoffs is
  scored. Its paired score difference is averaged over its scored cutoffs, so sessions weigh
  equally. Each cutoff is also reported alone, as a secondary result.
- **Endpoint:** 60 counted sessions, or 2027-06-30 if that comes first. At the end date there
  must be at least 30 counted sessions; otherwise the result is "insufficient" and no
  comparison is made.
- **Scored once:** `rth-eval-score` refuses before the endpoint and after it has run; the stored
  result stands. Sixty sessions is a checkpoint, not a promise of a conclusive result, and more
  data means a new version.
- **Before the endpoint:** `rth-eval-status` shows operational health only, never a score (see
  [Availability](#availability-at-every-checkpoint)).
- **Scores:**
  - **size:** fair ensemble CRPS of the absolute move;
  - **direction:** Brier score of a Laplace-smoothed up-share;
  - **signed:** a secondary CRPS of the signed move.

  Moves are in each session's frozen daily ATR.
- **Decision:** four primary comparisons: RTH-20 against CLOCK and against PRE-5, for size and
  for direction. Each uses a circular moving-block bootstrap over sessions at 98.75 %.
  - An improvement only when the whole interval is below zero.
  - Otherwise: "no sufficiently reliable improvement was established".
  - Size and direction are concluded separately.
  - The five displayed analogues are never the probability model.

## The operational evaluation (`rth_operational_v1`)

Defined in [contracts/rth_operational.py](../contracts/rth_operational.py), with a copy in
[rth_operational_v1_definition.json](rth_operational_v1_definition.json) (hash
`1ad913e1838ef25c`, pinned by a test). It is collected beside v2 from the same issues and does
not interrupt it.

**Not v2 relabelled:** it has its own forecast construction.

- **The target window:** [*S*, *S* + 15 min), where *S* is the start of the second full minute
  after the forecast is built, using the issue run's clock. So the window starts at least a
  minute after the forecast exists. It must end by 11:30 ET; otherwise no forecast is made
  (`not_issued`). It never starts before the matched minutes end.
- **Only what is known at issuance:** the forecast reads the set's ranking (bars to its
  cutoff), the pre-open set, and earlier sessions' bars. It never reads the target's own bars
  after the cutoff.
- **All four arms aligned to the target:** RTH-20, RTH-5, PRE-5 and CLOCK as for v2, but every
  member's move is over the same clock minutes [*S*, *S* + 15) of its own session.
- **Stored as issued:** in `journal.rth_eval_forecasts`, with *S*, the matching cutoff and the
  build time. Its `cutoff_at` holds *S*, the start of its target window.
- **Eligibility:** the database stamps the forecast and counts it only when stored at or before
  *S*. The registered definition's `max_issue_delay_s` is 0.
- **Shown with every forecast:** its lead (*S* minus when it was stored) and how old its
  information was (*S* minus the matching cutoff). On the ~10-minute feed the information is
  about 15 minutes old when the window starts.
- **Shared with v2:** cases, scores, aggregation, endpoint, bootstrap and decision rule.

**The other route:** with a real-time feed, v2's cutoff-based forecasts could instead be judged
under a much tighter delivery limit. That needs a new, predefined version, and the feed is your
call.

## Availability at every checkpoint

A scored subset must never hide frequent unavailable forecasts. So `rth-eval-status`, and the
stored result of each scoring, report per checkpoint (09:45, 10:00, 10:15), over every
scheduled opportunity that is decided:

- **Counts:** the opportunities, scored, and each reason with its rate (not issued, late,
  unverified inputs, incomplete forecast, outcome missing);
- **Issue delay** after the matching cutoff: median, 90th percentile and maximum;
- **Window still ahead** when stored: how much of the 15-minute window had not yet happened;
- **Lead to the window's start** and the **age of the information** at it.

The 60-session endpoint counts sessions with at least one scored checkpoint. So the three
checkpoints can end with different sample sizes, and reaching 60 does not by itself make the
evidence at any one checkpoint adequate. Each per-checkpoint result carries its own *n*, and
is secondary.

## The full session (`nq_match_rth_v3`)

Defined in [contracts/nq_rth.py](../contracts/nq_rth.py) (`SESSION_DEFINITION`). It is the same
matching through the whole regular session. At 12:17 ET it compares today's 09:30–12:17 with
the same 167 minutes of every earlier eligible session: "RTH analogues — first 167 minutes —
data through 12:17 ET". It is issued beside v2. **v2 keeps the first hour and stays the
only input of `rth_continuation_v2` and `rth_operational_v1`.** v3 feeds only its own
evaluation, `rth_session_v1`.

### What carries over from v2, and what does not

- **Unchanged:** features, weights, coverage floor, pool rules (the whole earlier pool
  re-scored at each window, never only yesterday's or the pre-open five), the five displayed
  analogues, frozen pre-open context and ATR units, the Globex VWAP anchor, the strict gap
  rule, and the confirmation rule.
- **The window:** n = 1..L, where L comes from the trading calendar: 390 minutes, or 210 on a
  13:00 ET early close. It ends at the scheduled RTH close, never at the 17:00 futures halt.
  Holidays have no session; daylight saving moves the UTC instants, not the length.
- **The last RTH bar:** like every bar, it counts once a later bar of the same trading day is
  stored. The futures trade on after the cash close, so the 16:00 bar normally confirms 15:59.
  Without such a bar, the window waits at 389 minutes ("awaiting confirmation").
- **Short sessions:** an early-close session holds no window past 210 minutes. For longer
  windows it is excluded (`incomplete_window`) and never filled in. Relative volume uses the
  sessions that hold the window.
- **Tolerances past 60 minutes:** a descriptive extension, not validated (`TOLERANCE_SCOPE`).
  The registered tolerances were calibrated at 30 minutes and checked at 15 and 60. Measured
  on the calibration's own sessions (`rth-calibrate --minutes 30,60,120,180,240,300,389`), the
  rule drifts with the window:

  | Window (minutes) | Path features' typical spread ÷ tolerance in use | vs VWAP | In overnight range | vs prev. close |
  |---:|---:|---:|---:|---:|
  | 30 | 1.00 | 1.00 | 1.01 | 0.98 |
  | 60 | 0.89–0.97 | 1.08 | 1.17 | 1.05 |
  | 120 | 0.70–0.82 | 1.14 | 1.36 | 1.14 |
  | 240 | 0.60–0.69 | 1.26 | 1.57 | 1.20 |
  | 389 | 0.55–0.62 | 1.19 | 1.66 | 1.27 |

  Late in the session the path tolerances are therefore about 1.6–1.8 times too wide, so path
  features score generously, while the fixed location tolerances are up to 1.7 times too
  tight. v3's long windows weigh the features differently from the 30-minute calibration.
  They describe resemblance, and are not a validated similarity. Phase-dependent tolerances,
  calibrated on training sessions only and frozen, would be a new version.
- **Expanding only:** a rolling 30- or 60-minute "recent path" component could weigh an
  afternoon reversal more. That is a research hypothesis, not part of v3.

### Issuing through the session

- **Schedule:** Auto runs `rth-issue` after each successful collection, from the open's first
  confirmed minute until 30 minutes after the scheduled close. The 30 minutes is a grace for
  the delayed feed's last bars, not a forecast rule. v2 still stops at 11:00 ET
  (`first_hour_due`); `session_due` covers v3; the dashboard schedules by either. A dashboard
  started before this change keeps its old schedule (the first hour) until restarted.
- **What one issue stores:**
  - the newest confirmed window;
  - every `rth_session_v1` cutoff window (each 30 minutes, 10:00–15:30) not stored yet and
    still within 30 minutes of its cutoff.

  Nothing is stored past the close. After the close the last set stands and is marked
  "RTH closed".
- **Bounded catch-up:** windows confirmed between two issues are not stored afterwards under
  a made-up issue time. They go into `journal.rth_issue_misses` with the reason:
  - `coalesced`: bars arrived together within one issue interval;
  - `not_running`: no issue ran while they were current;
  - `expired`: an evaluation cutoff never issued within its 30 minutes.

  So stale work never queues up, and the session's record shows what was missed.
- **Cost, measured on the production store** (read-only, 2026-10-09, a full replay of
  2026-10-08's 390 windows):
  - a whole issue: p50 0.74 s, p99 0.76 s;
  - ranking and building one window: 35 ms at p50 and 71 ms at worst;
  - peak memory about 76 MB.

  The earlier sessions' inputs are cached in `data/rth_cache` under a per-day fingerprint (bar
  count, latest receipt, latest bar; the snapshot's id), and only changed days are re-read. A
  warm load takes 0.66 s against 2.97 s uncached. A test checks that the cached and fresh loads
  are identical and that a revised bar invalidates its day. Overnight sums are exact (numeric),
  so the same bars always give the same inputs and digest.

### The timing record (migration 0031)

Every v3 set stores, beside v2's provenance:

| Field | Meaning |
|---|---|
| `cutoff_at` | the data cutoff: the end of the last matched bar |
| `confirmed_by_start`, `confirmed_received_at` | the bar that confirmed the last matched bar, and when it reached the store |
| `inputs_received_at` | when every input of the set was in the store |
| `computation_started_at` | when the issuing run started computing |
| `created_at` | when the database stored it (its clock) |
| `session_minutes` | the session's scheduled RTH length |
| `issue_class` | decided by the database: **timely** (issued by Auto or by hand within 2 minutes of its last input reaching the store), **late** (issued live but later: a catch-up) or **reconstruction** (a backfill, or stored more than 30 minutes after its cutoff) |

- **"Timely" means as current as the feed allows.** On the delayed feed a timely set still
  describes the market as it was about 11 minutes earlier, and the panel says how far behind
  the clock it is.
- **v1/v2 limit:** v1/v2 sets stay limited to 60 minutes, now by name in the database.

### Dashboard

- **Which matcher:** the RTH panel shows the full-session sets when the day has them. A day with
  both offers **Full session / First hour**.
- **Header:** "As issued: RTH analogues — first 167 minutes — data through 12:17 ET", then:
  - how and when it was issued;
  - timely, late or reconstruction;
  - the feed's delay;
  - whether the inputs are verified;
  - "RTH closed" on the last window;
  - the tolerance note past 60 minutes;
  - the matcher version.
- **Live status:** for the session in progress it says how far behind the clock the matches
  are, and **Stale** when no newer set was stored for 3 minutes (Auto off, or the collection
  failing).
- **Playback:** As issued only ever shows sets stored by the time replayed. Reconstructed shows
  the latest calculation and says so.
- **Kept:** chart zoom and the chosen analogue are kept as before. Nothing about the analogues'
  continuations is aggregated or overlaid.

### The full-session evaluation (`rth_session_v1`)

Defined in [contracts/rth_session.py](../contracts/rth_session.py). It is registered by the
first v3 issue, before that run stores a forecast. It follows `rth_operational_v1`'s
construction:

- **When:** at every 30-minute cutoff from 10:00 to 15:30 ET.
- **Target window:** the forecast's window is [S, S + h). S is the start of the second full
  minute after the forecast is built, and h is 15 minutes (primary), 5, 30 or 60.
- **The close:** a window that would cross the close is **never forecast and never shortened**.
  At 15:30 only 5 and 15 minutes fit; on an early close, nothing after 13:00.
- **Arms:** RTH-20 (primary, a sample size fixed in advance), RTH-5, PRE-5 and CLOCK. Each
  member's move is over the same clock minutes of its own session.
- **Eligibility:** a forecast counts only if the database stamped it by S.
- **Scores and decision:** as the other evaluations: size CRPS and direction Brier, sessions
  weighed equally (a dense cutoff grid adds no precision), and a circular moving-block
  bootstrap over sessions at 98.75 %.
- **Endpoint:** 60 counted sessions or 2027-06-30, scored once.
- **Secondary results:** per-horizon and per-phase (morning, midday, afternoon) results are
  exploratory.
- **Before the endpoint:** `rth-eval-status --version rth_session_v1` shows availability per
  cutoff and horizon, never a score.

**Deployment** needs migration 0031. The steps, the Monday checks and the rollback are in
[reports/deployment_plan_2026-10-10.md](reports/deployment_plan_2026-10-10.md).

**What to expect.** The ML study ([reports/ml_study.md](reports/ml_study.md), section 4) rebuilt
RTH-20 at these cutoffs for 198 earlier sessions:

- its member frequencies scored worse than the same-clock history on direction, at every horizon;
- a variant adding the last 30 minutes' path did not help;
- a same-clock up-share itself carries a little estimation noise, so a direction win over CLOCK
  alone should be checked against p(up) = 0.5.

The prospective result is what counts.

```bash
python scripts/nq_journal.py rth-issue                                        # both matchers, as Auto does
python scripts/nq_journal.py rth-backfill --version nq_match_rth_v3 --date 2026-10-08 [--all-minutes]
python scripts/nq_journal.py rth-show --date 2026-10-12 --view issued --at 12:17   # as issued by 12:17 ET
python scripts/nq_journal.py rth-calibrate --end 2026-10-07 --minutes 30,60,120,240,389   # the drift above
python scripts/nq_journal.py rth-eval-status --version rth_session_v1
```

## What this is not

- **Not a forecast:** no outcome, frequency or path of the analogues is aggregated, overlaid
  or turned into one on the dashboard.
- **No direction claim:** earlier work found NQ first-hour direction unpredictable from pre-open
  matches, momentum and 15/30-minute matching (2026-09). ml_study_v1 (2026-10-10) found no
  directional edge at rolling RTH horizons either.
- **Usefulness is untested** until the evaluations reach their endpoints.
