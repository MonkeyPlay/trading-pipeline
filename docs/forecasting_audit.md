# Forecasting system audit (2026-10-09)

**Scope:** the whole forecasting system in `trading-pipeline`, from bar collection to the
dashboard and the evaluations.

**The question:** does the LLM improve timely, out-of-sample predictions beyond the
numerical models and the historical analogues, and is any improvement worth its delay, cost
and complexity?

Predictive value, explanation quality and operational usefulness are judged separately
throughout.

## The answer, as far as the evidence goes

**The LLM's predictive value is not established, in either direction.**

- **Too few runs:** arm C (Claude annotates two pre-open fields) has 5 runs and arm D (Claude
  synthesises the forecast) has 5, spread over four versions.
- **No usable evidence:** every one is a historical replay of a session the model could
  remember, and none has been scored under a registered rule.
- **The model D must beat is weak:** arm B (deterministic analogues) does no better than plain
  historical frequencies (arm A) except on *which reference level is tested first*. The bar
  the LLM has to clear is therefore "frequencies", and no LLM evidence bears on it yet.
- **Cost and speed (measured):** at effort medium, D takes about 27 s and costs about
  $0.11–0.13 a request, and C about 23 s and $0.08–0.10. Two of three D v4 answers were
  usable.

**None of it arrives in time on the current feed.**

- **The feed:** bars reach the store about 10 minutes after their minute ends (measured).
- **The official pre-open forecast:** issued 11–20 minutes after its 09:29 cutoff, i.e. after
  the open. By then its first-move window is over and its 15-minute windows are mostly over.
- **No live forecast ever:** the one live capture (2026-10-06) never reached its cutoff.
- **What timeliness needs:** either a real-time feed (your call) or forecasts from an earlier
  cutoff, evaluated as separate candidates with less information.

**Collect before claiming.** The forward studies collect from today:

- the RTH research (`rth_continuation_v2`) and operational (`rth_operational_v1`) evaluations,
  issued live by Auto. The first session (Oct 9) is complete: 60 of 60 windows and 3 of 3
  cases in each evaluation, none missing. Health checks are logged in
  `docs/reports/rth_evaluation_health.md` (counts and timing, never a score);
- the fan's forward record (since 2026-10-06).

An LLM comparison needs its own registered forward experiment (proposed below). Running it
costs about $0.12 a session for D, plus C's pool backfill, so whether and when to start it is
your call.

## 1. Production baseline (verified today)

| Item | State |
|---|---|
| Deployed revision | `d588004`, through `scripts/deploy.sh` to `~/trading_pipeline_prod` (logged in `logs/deployments.log`) |
| Production process | **switched 13:03 UTC, verified from /proc:** dashboard pid 295083 (`.venv/bin/python -m dashboard.app`) has its working directory in `~/trading_pipeline_prod`, listens on 127.0.0.1:8080 and holds the one Auto lock. Its Auto collector runs there. The checkout is at `d588004` with no local changes. The main checkout is development again: merging into it no longer deploys anything |
| Schema | v27 in production. Routine processes only check it; the production checkout's check passes |
| Shared state | the Auto lock, `.env`, `.venv`, logs and caches are linked, so only one dashboard can run Auto |
| Tests | 453 pass on `d588004` in a clean worktree |
| Snapshot-readiness fix | your commit `db83643` is exactly the Oct-7 staged diff (hash `dfa5e76a70c2c3f6`). Reviewed: it doesn't move the evidence cutoff or disguise late issuance, because the snapshot stays a reconstruction stamped when built. **One defect, fixed in `d588004`:** it accepted the cutoff bar while that bar could still be forming; now a later bar must confirm it |
| Git history | the earlier 446-test runs used `21e0b8d` plus the then-staged Oct-7 files. A clean checkout of `21e0b8d` alone also passed 446/446 |

**Found and fixed during deployment (P0).**

- **What broke:** the Auto collection of 2026-10-09 stored 2026-10-08's bars only as warm-up
  days, with no active-contract row. Oct 8 had been missed while nothing collected.
- **Why it mattered:** without that row, nothing that joins on the active contract reads the
  day. Today's daily ATR (70 sessions) was invalid, which would have blocked every RTH set
  today, since the ATR is the matcher's unit, and the A/B thresholds.
- **The fix (`d588004`):** a run's window now reaches back to the first day after the
  symbol's last recorded active contract, at most 10 days.
- **Verified:** the next Auto run assigned Oct 8 for all 12 symbols, took the missing Oct 8
  snapshot, and today's ATR is valid again (393.58 from 70 true ranges).

## 2. Pipeline map

**Status** is one of: production (runs automatically), on demand (run by hand), research
(collected or scored for evaluation, no decision value yet), visual (drawn, not validated) or
archived.

| Component | Inputs and versions | Cutoff → target / horizon | Issuance and runtime | Missing data / failure | Evidence of usefulness | Status |
|---|---|---|---|---|---|---|
| Collection | IB historical 1-minute bars, 12 symbols; receipt times `first_stored_at` / `version_stored_at` (0022/0023) | — | Auto every minute (collection median 21 s, p95 23 s); daily run with refetch | The newest bar may be forming; missed days are now caught up (≤ 10 days) | Feed delay median 10.2 min (bar end → stored) | production |
| Pre-open evidence snapshot | `nq_evidence_v5_r0929`, convention v5; bars to the cutoff, references, ATRs, thresholds T/B/A, events, intermarket | 09:29 ET | From 09:31 ET once every symbol was fetched after then and the cutoff bar is confirmed: **11–20 min after the cutoff** on this feed | Incomplete windows flagged, never filled; duplicates possible (2026-10-07 has two) | — | production (reconstruction) |
| Rule annotation | `nq_structure_rules_v4` (P1 fields from the snapshot) | 09:29 | With the snapshot, about 10 ms | Contamination stops the forecast | Reviewed by you (stage 1/2 sets) | production |
| Pre-open analogues (B's evidence) | `nq_match_p1_v2`, 14 rubric features, top 5, 75 % coverage | 09:29 → P1 targets | With the snapshot | Coverage floor; duplicate sessions excluded | hist_dev_v1 and p1_pool_tuning_v1: no gain over frequencies except first level tested | production |
| Arm A: frequencies | `nq_prior_p1_v1` | 09:29 → 7 P1 targets | about 10 ms | Prior-only; zero-probability classes possible early (2–9 infinite log-loss cases a target) | The benchmark | production (historical replay) |
| Arm B: smoothed analogues | `nq_baseline_p1_v1`: (count + 5·prior)/(n + 5) | same | about 10 ms | Prior-only without analogues | Worse than A on opening bias, opening type and close direction (Brier, development data); better only on first level tested | production (historical replay) |
| Arm C: Claude annotation | `nq_structure_restricted_v3` (2 fields) → `nq_restricted_p1_v3` | same | Manual approval; about 23 s a request (medium) | Matches only among sessions Claude annotated (5): a prior-only fallback, not an LLM effect | none (5 runs) | on demand |
| Arm D: Claude synthesis | `nq_synthesis_p1_v4`, **v5 in the audit branch** (adds B and A) | same | Manual approval; about 27 s (medium) | Invalid answers kept as runs (1 of 3 at v4) | none (5 runs) | on demand |
| Forecast now (preview) | Snapshot as of now, rules, analogues, arms A and B, in memory | as of now (≤ 09:29) | Auto before 09:31; about 2 s | Incomplete pre-open by design, labelled | none | production (preview, never stored) |
| Live capture | `nq_issue_live_v2`, receipts, 09:29:50 deadline | 09:29 | Run by hand before the open | Needs a real-time feed | **never issued** (1 attempt, no events) | inactive |
| Outcome labels | `nq_prompt_v2_1_impl5` | → realised P2 labels | Two hours after the close; revisioned | ambiguous_intrabar, missing_threshold, shortened_session, uncovered: explicit, never scored as a class | Stage-1 review 25/25 agreed | production |
| RTH analogues | `nq_match_rth_v2` (11 features, daily-ATR unit, calibration stored) | each confirmed minute to 10:30 → resemblance (no target) | Auto, 09:31–11:00 ET, after a successful collection | Confirmation, gap and awaiting states recorded; provenance and `pit_status` | descriptive; usefulness per the two RTH evaluations | production (from today) |
| RTH research evaluation | `rth_continuation_v2` | cutoff → next 15 min | Stored with the set; eligible ≤ 14 min | — | collecting from today | research |
| RTH operational evaluation | `rth_operational_v1` | the 2nd full minute after the build → next 15 min | Stored with the set; eligible only by the window's start | — | collecting from today | research |
| Benchmark fan | `fan_rw_v2` (intraday variance, events, fat tails) | any minute → every later minute | Auto on the dashboard | — | Better than a flat random walk (CRPS −0.25 % to −0.44 %, intervals below zero) | production (visual + forward) |
| Learned fan brackets | `fan_intermarket_v2_lin_pois_ivx_frozen`, 5 and 15 min | any minute → 5 / 15 min | Drawn on the session | — | Holdout pass: −0.13 % (5 min) and −0.14 % (15 min) CRPS: real, tiny, size only | production (visual) + forward record |
| Fan forward record | `..._forward_v2` | marks every 15 min and at 09:29 | Auto | stale or late marks recorded | collecting since 2026-10-06 (55 issues), not inspected | research |
| Projection trend | TEMA/EMA extrapolation | last candle → 15 min | Drawn | — | none; labelled "a visual extrapolation, not a forecast" | visual |
| Direction experiments | fan_direction_v1, fan_cond_ema_v1, fan_im_dir_v1, fan_first_hit_v1 | — | — | — | No reliable directional gain; first hit: size only | archived |
| Grading radar / experiments page | experiments.py, grading.py | — | — | Counts infinite log loss | — | production (display) |

## 3. Correctness and availability findings

Ordered by severity, with their status.

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | A missed session got its bars but no active-contract row, so its snapshot was never taken and today's daily ATR was invalid (no RTH sets, no A/B thresholds today) | P0 | **fixed and deployed** (`d588004`); Oct 8 recovered |
| 2 | Snapshot readiness accepted a cutoff bar that could still be forming | P0 | **fixed and deployed** (`d588004`) |
| 3 | Auto migrated production from the development working tree | P0 | **fixed and deployed** (`eac151c`): routine processes check the schema; `scripts/deploy.sh` migrates |
| 4 | A new bar's two receipt times came from two clock readings (test flaked 4/30 on clean HEAD; 695 production bars differ by microseconds) | P2 | **fixed and deployed** (`f2526bf`); the 695 rows are left as they are |
| 5 | **Arm D was asked to forecast RTH close direction (needs B) and session type (needs A and B) from a bundle that carried only T**, although its prompt said B was there. B and A are frozen pre-open, so available at issuance. Units: index points (B = max(1, ceil(0.05 × daily ATR)); A = daily ATR). Eligibility: 19 of 279 sessions have neither | P1 | **fixed in the audit branch** (`nq_synthesis_p1_v5`): B and A in the session block, said to be distances, and the two targets ineligible without them. Verified on real sessions, offline |
| 6 | LLM jobs shared the dashboard's one job runner, so a C/D job (up to a 30-minute batch wait) stopped Auto: no collection, no RTH recording | P1 | **fixed in the audit branch**: LLM jobs on their own runner |
| 7 | Arm C's annotations all ran before arm D's requests, though D never reads them | P2 | **fixed in the audit branch**: live requests run side by side (about 27 s a session instead of about 50 s) |
| 8 | Neutral classes are named as paths: "Two-sided" (and P1's "Choppy") for a net change within ±T or ±B; the first move's "neither" (no ±T touch) shown as "Two-sided" | P2 | **fixed in the audit branch** for display and in D v5's prompt; registered definitions and stored labels unchanged |
| 9 | **The official pre-open forecasts are not timely on this feed:** stored 11 min (Oct 6) and 20 min (Oct 7) after the 09:29 cutoff, so after the open, with the first-move window already over | P1 | **open, needs your decision.** The audit branch now shows each run's issue time against the cutoff and the open, and marks targets whose window had ended ("a record, not a forecast"). See section 7 for the options |
| 10 | 2026-10-07 holds two v5 snapshots (the first froze without the cutoff price). The pre-open matcher excludes the session as `duplicate_session` from every later pool | P2 | **open:** a consequence of #2, now prevented; the duplicate stays (append-only). Excluding it is the registered rule |
| 11 | "Verified" on an RTH set covers its window bars, the confirming bar, overnight bars, snapshot, and every earlier session's first-hour and overnight bars and snapshot. It is an availability condition ("in the store by then"); it does not make 10-minute-old information current | — | verified; the wording in the header and docs says so |
| 12 | The operational window S is fixed from the issue run's clock before any member is measured (S = the 2nd full minute after it, 60–120 s ahead at every second of a minute, tested). Slow storage cannot move it: the database stamps such a forecast ineligible. All four arms use the same S | — | verified |
| 13 | Arm C's pool is the sessions Claude annotated (5), so its "analogues" are mostly the prior. A C-vs-B difference would measure coverage, not the LLM | P1 for the evaluation | **open:** compare C with B restricted to the same pool, or backfill C's pool first (about $70 batch). See section 5 |
| 14 | Zero-probability failures exist in arms A and B (2–9 a target). Finite-only log loss is not the overall loss | P2 | handled: every scorer counts infinite cases explicitly; Brier is reported beside it |
| 15 | Ambiguous intrabar outcomes (about 40 % of first moves), missing data and shortened sessions stay explicit reasons, never scored as a class | — | verified |
| 16 | As issued and reconstruction are kept apart: append-only sets and runs, `issued_by`, `data_mode`, and the RTH views (As issued / Reconstructed) | — | verified |

**The serialized LLM requests (audited offline; nothing was sent).**

- **What D receives:** the real D v5 request for 2026-10-07 has these sections: session
  (T 7, B 20, A 380.64), completeness, references, 15-minute bars, final 2-minute bars, the
  annotation, price location, the analogues with their outcomes, the baseline, the
  candidates, eligibility and the targets.
- **Size:** about 19,500 characters, plus a 10,100-character system prompt.
- **Dates:** no date-like string appears in the request. Blinding removes the obvious route,
  but it doesn't prove the model can't recognise a remembered session from its price shape,
  so historical LLM replay stays development evidence.

## 4. Numerical benchmarks

All on development data (historical replay; these sessions set the rules). Compared on
identical targets, horizons and cutoffs; no single combined score.

| Benchmark | Result |
|---|---|
| **B vs A** (`hist_dev_v1`, 274 sessions; Brier, B minus A) | 15-minute direction +0.014 [−0.009, +0.037]. Opening bias +0.033 [+0.010, +0.058], worse. Opening type +0.017 [+0.001, +0.035], worse. Close direction +0.027 [+0.002, +0.051], worse. First level tested −0.039 [−0.064, −0.012], **better**. First move −0.019 (n = 117, inconclusive) |
| **Tuned analogues vs A** (`p1_pool_tuning_v1`; definition `a695e53c1a7d4f1b` committed before the run; pool sizes {5, 10, 20, 40} × pseudo-counts {2, 5, 10, 20}, walk-forward, training only) | Tuning drifts to the heaviest smoothing, i.e. towards A. 15-minute direction: tuned − A +0.001 [−0.014, +0.017]. Only first level tested gains: −0.047 [−0.079, −0.016]. **No reliable improvement over the frequencies** ([report](reports/p1_pool_tuning_v1.md)) |
| **The five shown versus the probability sample** | The five on screen *are* B's probability sample (K = 5). Tuning prefers K = 10–40 with heavy smoothing for the directional targets, so five examples should not be the model. The RTH evaluations already separate them (RTH-20 primary, RTH-5 secondary) |
| **Fan** | `fan_rw_v2` beats a flat random walk (CRPS −0.25 % to −0.44 %); the learned fan beats `fan_rw_v2` on its sealed holdout by −0.13 % (5 min) and −0.14 % (15 min). Size skill, no direction |
| **RTH** | Forward only: no result before the endpoint |
| **Directional research** | fan_direction_v1, fan_cond_ema_v1 and fan_im_dir_v1: no sufficiently reliable directional improvement was established. Archived; reopen only for a new, specific hypothesis with a bounded test |

## 5. Isolating the LLM's contribution

### `p1_d_research_v1`: defined, deferred

Second review, same day. D answers the cleanest question: does Claude's synthesis improve the
numerical forecast it is handed? C adds annotation coverage and pool composition, which makes
attribution harder, so C and its pool backfill are deferred.

**Deferred by the third review: not registered, nothing spent.** It would show whether D adds
statistical information to *delayed* snapshots. The agreed priority is whether D improves
forecasts *delivered before trading begins*, which is the live comparison below. The design
stays available as a separate experiment.

- **Arms:** A, B and D v5, all on the session's official 09:29 snapshot. That gives identical
  evidence cutoffs and target windows; D's only extra numbers are the thresholds B's rules
  already use.
- **Pairs:** D − B (the question), D − A and B − A, using the manifest's own `pairs`.
- **Primary target and score, explicitly:** `direction_15m`, scored by the multiclass Brier
  score in the experiments' convention: the *unhalved* sum over the classes,
  Σ_c (p_c − [c realised])², from 0 to 2 per session. Log loss is a companion score. The
  manifest used to inherit log loss as "primary" while its decision used Brier; that is fixed.
- **0.01 means an absolute reduction** of the mean per-session Brier score on that 0–2 scale
  (0.005 on the halved 0–1 scale). It is not a relative reduction.
- **Decision:** D is better only if *both* D − B and D − A are at most −0.01 and each whole
  95 % interval lies below zero. Secondary targets are reported, never decisive.
- **Research only:** the 09:29 snapshot exists only after the open on this feed, and D runs when
  approved. It measures skill from cutoff-frozen evidence, never timeliness.
- **Three controls, kept separate:** the definition (`experiment-register --design d-research`
  writes one row and sends nothing; a test proves no Claude client is built), activation (none:
  D runs only from a run a person starts and confirms) and budget (each approval's own
  `max_requests`; about $7 for 60 sessions).

### Estimated issuance times (reconstructed, not demonstrated delivery)

`nq_journal.py timeliness` writes `docs/reports/preopen_timeliness.md`. **These are
estimates.** D is not connected to the scheduled issuance, so its times are the Auto run's
end plus D's measured generation time. Delivery is demonstrated only by a live capture's
`delivered` step (below).

- **D's generation time** now comes from the current version only (v5). No v5 run has been
  measured yet, so the D columns are empty. v4's 25–30 s (3 runs) is shown for reference and
  not used. Measuring v5 needs D runs, which are paid requests and your call.
- **Variability:** every column now shows fastest / median / slowest, and each session is
  listed.

| Cutoff | Measured sessions | A/B vs the open (each session) |
|---|---|---|
| 09:15 | 2 (Oct 7, Oct 9) | +3.5, +3.5 min |
| 09:20 | 2 | −1.6, −1.6 min |
| 09:25 | 2 | −6.6, −6.6 min |
| 09:29 | 2 | −10.5, −10.7 min |

Data were ready 11.2 min after the cutoff both times. Oct 8 counts as a miss (nothing
collected that morning), and earlier sessions have no receipt times.

**Collecting ten sessions** needs nothing new: receipt times and Auto run ends are recorded on
every morning Auto runs (the run log is append-only). Run `timeliness` over the range once ten
sessions are measured, which is 2026-10-21 at the earliest if every morning collects. Ten
sessions can inform the operational design (cutoff, deadline, late policy). They cannot
establish reliable predictive performance.

### The live D path (built and tested; not deployed)

`nq_journal.py live --with-d` (branch `live-d`, `forecaster/live_synthesis.py`, migration
0028). Verified only against simulated provider answers; no paid request was made.

- **Approval:** one Claude request per session, confirmed by hand (typed `send`) or by a
  one-time dashboard approval scoped to `live-d`, that day and that profile. Without one, the
  capture records `synthesis_skipped` and sends nothing. A bad approval stops the command
  before it connects to IB.
- **Immutable evidence:** D's request is built from the live B run's own evidence (the same
  snapshot, annotation and analogue set ids; the frozen evidence is identical apart from the
  versions it names). It is recorded in the inference ledger before it is sent, and a snapshot
  that already has a request is never sent again. Runs, evidence and predictions are
  append-only.
- **Deadline:** the database's (09:29:50 ET, `nq_issue_live_v2`). It stamps the issue time and
  turns a run inserted later into `late`. The capture acknowledges an issued D after the
  commit, as it does A and B.
- **The forecast in force:** awaited until the deadline, then recorded as a `delivered` step
  with the database's clock: the first timely run of D, B, A, else none, with the reason
  (e.g. "B timely (D invalid)").
- **Late results:** an answer after the deadline is still stored, for up to 120 s, and the
  database makes it late. It never replaces the forecast in force. With no answer by then, a
  failed run records that.
- **Tested fallbacks to B:** an invalid answer, a provider error, no approval, a deadline
  already passed, and no answer at all.

### The live A/B/D comparison: frozen once the timing supports a cutoff

When ten or more measured sessions support a cutoff, freeze one definition with that cutoff
and begin the prospective test. Proposed content:

- **Fixed in advance:** the cutoff, the deadline, the model and prompt (`nq_synthesis_p1_v5`
  at effort medium), the late policy above, and the delivery order D → B → A.
- **The capture's freshness budget must fit the feed.** `nq_issue_live_v2` waits at most 20 s
  after the cutoff for the bar ending at it. On this feed that bar arrives about 11 minutes
  later, so every live capture goes stale, as on 2026-10-06, with or without D. Two ways out:
  - an earlier-cutoff profile with a budget that fits the measured delay (e.g. a 09:15 cutoff
    with about 13 minutes), as a new snapshot profile and issue policy;
  - a real-time feed (your call; it is a paid subscription).
- **Arms:** A, B and D, all from the same live snapshot at that cutoff. C stays out (pool
  composition).
- **Cases:** every scheduled session is a case. A session without a timely D is counted, never
  dropped.
- **Primary target and score:** `direction_15m`, unhalved multiclass Brier.
- **Skill criterion:** D must improve on **both** A and B by an absolute 0.01 or more, with each
  95 % block-bootstrap interval below zero, paired on the sessions where D was timely.
- **Availability criterion, alongside:** D's on-time share over all scheduled sessions. The
  threshold is yours to set when freezing; 90 % is my suggestion.
- **Also reported:** the delivered system (D else B) against B on every session, i.e. what you
  would actually have seen at the open.
- **Operational point:** under the manual-only rule, each session's D request needs your
  approval before the capture starts. The dashboard has no live-with-D button yet.

| Arm | What it is | State |
|---|---|---|
| A | historical frequencies | 279 sessions (replay) |
| B | deterministic annotation, matching, probabilities | 279 sessions (replay) |
| C | Claude replaces 2 annotation fields, same downstream | 5 runs; pool of 5 |
| D | Claude synthesises from B's frozen evidence | 5 runs (v3/v4); v5 ready |
| E | Claude *explains* B's unchanged predictions | not built |

**What a fair forward comparison needs, with C too (later).** Proposed as `p1_llm_forward_v1`,
superseded for D by the live A/B/D comparison above, and to be registered
with `experiments.experiment_manifest` (it accepts any arms) before its first session:

- **Arms and sample:** A, B, D v5, and C *with B restricted to C's own annotated pool* as its
  control (so pool coverage is not read as an LLM effect). Prospective sessions only, from
  registration.
- **Official run:** the first issued run of each arm. Every scheduled session is a case:
  late, failed, invalid, unavailable and abstained runs are counted.
- **The delivered system is an arm of its own:** "D if valid, else B", because that is what
  you would actually see.
- **Primary measure:** paired unhalved Brier on `direction_15m`, with an absolute minimum
  improvement of 0.01 agreed in advance; the other P1 targets and log loss (infinite cases
  counted) are secondary.
- **D's departures from B:** for D, its departures from B (including probability shifts that
  keep B's class) scored separately: did moving away from B help?
- **Operational measures:** on-time share, latency, failures and cost per usable forecast.
- **Uncertainty:** a block bootstrap over sessions with a stated comparison budget
  (Bonferroni over the primary comparisons). Checkpoint-specific sample sizes are reported.
- **If D gets more numbers:** if D is ever given numerical information B lacks, a non-LLM
  model given the same information must be an arm before any gain is credited to the LLM.
- **E is about communication, not skill:**
  - **Factual correctness:** every claim checked against the cited evidence ids, automatically
    where possible.
  - **Clarity and time to understand:** rated by you, blind to the arm.
  - Never inferred from prediction accuracy.

**Why it is not registered here.** Its sample means sending Claude requests every session
(about $0.12 for D, more for C), and requests are manual only by your rule. Registering
commits you to running it, so the definition is ready but the decision is yours.

## 6. Evidence inventory

**Development data.** Every result so far is development data:

- A/B: 279 sessions replayed;
- hist_dev_v1;
- p1_pool_tuning_v1;
- the fan checks and holdout (spent once);
- the 774 RTH v1 reconstructions;
- the 5 C and 5 D runs.

**Untouched forward data.** Collecting now:

- RTH v2 and the operational RTH evaluation, from today;
- the fan forward record, since 2026-10-06.

No predictive result of these has been looked at, and none will be before its endpoint.
Operational health only: `rth-eval-status` and the fan forward panel.

**No forward LLM data exists.**

## 7. Speed and cost

Measured, not estimated:

| Step | Median | p95 / max |
|---|---|---|
| Bar end → stored (feed delay, first hour, 2026-10-07) | 10.2 min | 10.3 / 16.0 min |
| Auto run (collection, journal, forward) | 26–27 s | 31–35 / 81 s |
| … collection step | 21 s | 23 / 77 s |
| … fan forward step | 4 s | 10 / 16 s |
| Preview step | 2 s | 2 s |
| Pre-open snapshot + A/B after the cutoff (same day) | 11 min (Oct 6), 20 min (Oct 7) | — |
| A, B, C-run computation | about 10–20 ms | — |
| RTH issue (load 2 s + about 10 ms a set) | about 3 s | live measurement from today |
| Arm C request, medium / xhigh | 23 s / 161–227 s | — |
| Arm D request, medium / xhigh | 27 s / 59 s | 30 / 81 s |
| Tokens per D request (medium) | about 10k in (about 4.7k cacheable), about 3.1k out | — |
| Cost per request (medium, measured 2026-10-05) | C $0.08–0.10, D $0.11–0.13 | xhigh: C $0.46–0.63, D $0.22–0.25 |
| Usable D answers (v4) | 2 of 3 (about $0.18 per usable forecast) | — |
| Dashboard rendering | not measured | — |

**What limits timeliness.** It isn't computation; it's the feed. A 09:29 forecast cannot
exist before about 09:41, and an LLM adds about 27 s on top. Options, each a separate
candidate with less or different information:

1. **A real-time data subscription.** Your decision, cost outside this project.
2. **An earlier cutoff,** e.g. 09:15 (whose bars arrive about 09:26), registered as its own
   profile and evaluated against the 09:29 forecast as a different candidate.
3. **Keep 09:29 as a research record,** as now, labelled as such.

**Done for speed:** the separate LLM runner and parallel C/D (audit branch).

**Not done: compact-request and model-setting benchmarks.** These need Claude requests, which
your rule keeps manual. The offline request sizes above are the starting point.

## 8. Interface

**Done.**

- **RTH set header:** says live or reconstruction, issued by whom, feed delay, verified
  inputs, the calibration sample, the confirmation state, and the minutes still ahead.
- **Evaluation status:** v2 labelled research-only everywhere ("forecast skill from delayed,
  cutoff-frozen inputs"). Per checkpoint it shows every opportunity, reason rates, delay,
  window still ahead and lead.
- **Pre-open set and forecast table on the session in progress:** targets whose window is over
  are marked observed.
- **Stored runs (audit branch):** the issue time against the cutoff and the open, and targets
  already over at issue marked "a record, not a forecast".
- **Neutral classes (audit branch):** named for what they are.
- **The projection trend:** stays labelled a visual extrapolation.

**Recommended next.** Not built in this pass:

1. **One timeliness badge per forecast.** Timely operational (stored before its window),
   research-eligible delayed, reconstruction, or observed outcome. Use one vocabulary across
   the forecast panel, RTH panel and fan.
2. **D against B, side by side.** Per target, B's class and distribution, D's, the shift, and
   the evidence ids D cited for it. Today the radar shows each arm, but not what D *changed*.
3. **Three confidence notions kept apart.** Measured calibration (from a scored experiment;
   none yet), statistical uncertainty (denominators, intervals) and the LLM's 1–5
   self-rating, which is labelled "the model's rating, not calibration".
4. **Show B's numbers immediately, add LLM output when validated.** Never hold the numbers
   for the LLM.

## 9. Decisions by component

| Component | Recommendation |
|---|---|
| Collection, receipts, catch-up | **retain** (fixed today) |
| Deployment (`deploy.sh`, schema check) | **retain**; run production only from the deployed checkout |
| Pre-open snapshot, labels, rule annotation | **retain** |
| Arm A (frequencies) | **retain** as the benchmark everything must beat |
| Arm B (smoothed analogues) | **improve or narrow:** keep for first level tested, where it beats A. For the directional and regime targets it does not beat A even tuned, so show A's numbers there, or B with heavy smoothing labelled "≈ frequencies" |
| Arm C (Claude annotation) | **research-only.** Its pool makes it untestable until backfilled; low priority |
| Arm D (Claude synthesis) | **research-only** until `p1_llm_forward_v1` runs. Deploy v5, since v4 omits the thresholds two targets need |
| Arm E (explanation) | **explanation-only**, if you want it: build it as communication and evaluate it as communication |
| Forecast now (preview) | **retain**, labelled incomplete |
| Live capture | **retire or revive.** Revive only with a real-time feed |
| Official pre-open runs | **retain as a research record.** On this feed they are not forecasts; the interface now says so |
| RTH analogues | **retain** (descriptive). Usefulness per the two forward evaluations |
| RTH evaluations | **retain**, untouched until their endpoints |
| Benchmark fan | **retain** |
| Learned fan brackets | **retain** (passed, tiny), size only |
| Fan forward record | **retain**, untouched |
| Projection trend | **visual only**, labelled |
| Direction experiments | **archived** |

**Established now:**
- B adds predictive value over frequencies only for the first level tested.
- Tuning does not change that.
- The fan's size skill is real but small.
- On this feed, no pre-open forecast is timely.

**Needs future observations:**
- Whether the LLM adds anything (`p1_llm_forward_v1`, when you start it).
- Whether the RTH analogues' continuations do (the two RTH evaluations, at their endpoints).
- Whether an earlier cutoff gives a timely forecast that is still useful (a new profile, if you
  want it).

## What changed in the code during this audit

| Change | Commit | Deployed |
|---|---|---|
| Receipt times from one clock reading | `f2526bf` | yes |
| RTH analogues, evaluations, deployment mechanism | `e249a66`…`69f86e6` | yes |
| Snapshot readiness (yours) | `db83643` | yes |
| Confirmed cutoff bar; missed-session catch-up | `d588004` | **yes, the running revision** |
| p1_pool_tuning_v1 (definition, then scorer and report) | `35b629f`, `0afae63` | no (research) |
| Synthesis v5, separate LLM runner, parallel C/D, honest labels, issue timing | `f300143` | **no.** Deploy after today's session: v5 registers on the first Auto journal step after deployment |
| C's own database connection when parallel; D-only research design; experiment pairs; timeliness measurement | `b8b5786` | **no**, with the above |
| Live D path (migration 0028), timeliness as estimates with spreads, explicit D research decision rule, pair labels in experiment reports | this commit (branch `live-d`) | **no**: needs its own deployment (schema v28) after `b8b5786` |

**The combined revision, rechecked before deployment:**

- **Every definition the code registers** (19) hashes the same as at `d588004`, except the
  intended `nq_synthesis_p1_v4` → `v5`. The RTH matcher, both RTH evaluations and the label
  definitions are unchanged.
- **Determinism:** on real data (2026-10-07, read-only), the RTH sets' input digests, members,
  `pit_status`, and both evaluations' forecast digests (operational at a fixed build time) are
  identical at both revisions.
- **Parallel C/D:** C's requests use a database connection of their own, closed after. C's
  requests are counted before D's, so together they never exceed the approval's
  `max_requests` (tested at three budgets). They run on the LLM runner, apart from Auto.
