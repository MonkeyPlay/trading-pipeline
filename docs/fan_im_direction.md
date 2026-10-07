# Intermarket Direction (`fan_im_dir_v1`)

Asked 2026-10-07, after `fan_direction_v1` and `fan_cond_ema_v1` found no directional improvement in
NQ's own prices ([docs/fan_direction.md](fan_direction.md), [docs/fan_cond_ema.md](fan_cond_ema.md)).
Those experiments read only NQ. This one asks: **do recent moves of the related index futures,
ES and RTY, and NQ's performance relative to ES, add information about NQ's next 5 and 15
minutes beyond NQ's own recent prices?** The model may find catch-up, continued divergence or
nothing; nothing is hard-coded. (Using VXN to set the fan's *width* was a different question,
answered in the intermarket fan experiment.) The production fan is untouched.

[forecaster/fan_im_direction.py](../forecaster/fan_im_direction.py), `scripts/fan.py im-dir`;
tests [tests/test_fan_im_direction.py](../tests/test_fan_im_direction.py).

## The definition (fixed before the run)

Hash **`2171e8f4da7adde0`**, written to [fan_im_dir_v1_definition.json](fan_im_dir_v1_definition.json)
before any real-data result. `im-dir` refuses to run if the code's definition differs.

**Machinery: Conditional EMA Direction's, unchanged** (definition `3ec8838a731fb229`). That
covers one shared fan per block (`lin_pois_ivx` refitted on earlier sessions, `fan_rw_v2`'s sigma
and shape), shallow gradient boosting per horizon (depth {2, 3} x {50, 150} trees, shrinkage
{0.25, 0.5, 1}, exactly zero as an option), and selection on the validation sessions by the
shifted fan's CRPS. Activation needs the best option's session-level interval against zero to
lie wholly below zero at 99.58 % (95 %, Bonferroni over the 12 options). It also fixes the five
chronological blocks (150 sessions, all development) and the decision rule.

**Arms.**

| Arm | Inputs |
|---|---|
| A `zero` | none: the centred fan |
| B `own` | NQ's 1-, 5-, 15-minute returns, VWAP distance, `rv15`, `rv60`, `vol60`, phase (the conditional-EMA context arm) |
| C `intermarket` | B plus the intermarket set |

The essential comparison is C against B.

**The intermarket set (14).** Each market's last close known at the end of each minute; sd60 is
each market's own trailing 60-minute volatility of 1-minute returns.

- ES and RTY returns over the last 1, 5 and 15 minutes, over their own sd60 x sqrt(w).
- NQ relative to ES over 5 and 15 minutes: NQ's move minus beta x ES's move, over NQ's
  sd60 x sqrt(w). Beta is the OLS slope of NQ's on ES's 1-minute returns over the 20 sessions
  before (minutes where both closed a bar), missing with fewer than 10.
- Co-movement: the sign of NQ's 15-minute return where it equals ES's, else 0 (diverging); and the
  correlation of their 1-minute returns over the last 60 minutes.
- ES's and RTY's relative volume (`vol60`) and volatility over usual (`rv15`).

RTY's inputs are missing before its first complete session (2025-09-18), read as missing.

**Data check (before the definition).** In the stored history the three markets' bars line up.
NQ's 1-minute return correlates 0.943 with ES's at lag 0, and at most 0.016 at one minute's lead
or lag; for RTY 0.798 and at most 0.032. Every market has a bar in every minute outside the daily
halt (2026-03 and 2026-04 checked). That is reassuring but not proof. The bars carry market
timestamps from IB's historical data, not the times they were received; matching timestamps and
strong same-minute correlation do not show that the values were available at the same moment, or
that a market's value was never stale in real time. Receipt times are recorded only by the
forward record (bar receipts, from 2026-10-06), not for this history. A live feed delayed
differently per market can create a lead-lag of its own. Any live use would need synchronised
feeds, and this account's IB data is delayed.

**Decision.** Pass at a horizon: C - A and C - B both have their 97.5 % interval wholly below zero
(Bonferroni over the two horizons); the experiment passes if either horizon does. Fail: at both
horizons C's mean CRPS is not below both A's and B's, meaning no incremental intermarket benefit.
Otherwise inconclusive. After a pass: freeze, then fresh, timely forward data on synchronised
feeds before any overlay.

**Diagnostics** (explanation only): by phase, by co-movement (together up / together down /
diverging), and by divergence, in terciles of |NQ relative to ES over 15 minutes| from each block's
training rows.

**Verification** (tests): no input at a minute changes when a later bar of NQ, ES or RTY changes;
beta reads only earlier sessions; a planted relationship carried by ES's 5-minute return is
recovered, activates C and not B, and passes; two no-signal controls activate nothing and fail.

## Result (run once, 2026-10-07): **PASS by the declared rule, at 5 minutes only - marginal, from one block, and not reproduced by the procedure on current data**

[Report](reports/fan_im_dir_v1_fan_intermarket_v2_NQ.md).

| | 5 min (secondary) | 15 min (primary) |
|---|---|---|
| C `intermarket` - A `zero`, CRPS | -0.013 %; 97.5 % interval [-0.00094, -0.00002] bps | 0 (never activated) |
| C - B `own`, CRPS | the same (B never activated) | 0 |
| Verdict | **pass** | not below both |

- **One activation carries it.** Of 20 fits (5 blocks x 2 arms x 2 horizons) one activated: the
  intermarket arm at 5 minutes in check 1 (trained to 2026-03-02, scored 2026-03-03 to 04-14).
  There it improved CRPS by 0.057 % (19 of 30 sessions better, 11 worse) and the Brier score
  significantly. On the other 120 sessions every arm issued the centred fan, so the pooled
  difference is exactly zero there. The pooled interval excludes zero by 0.00002 bps.
- **The shift is tiny:** where active, 0.024 of the fan's sigma, 0.18 bps (under half an NQ point).
  P(up) stayed between 0.49 and 0.51. Coverage unchanged (90 % band 89.4 %).
- **NQ's own inputs nearly matched it.** In check 1 the own arm's best validation option gained
  more (-0.070 % against -0.057 %) but its interval was wider and it stayed off. At an uncorrected
  95 %, both arms would also have activated at 5 minutes in check 2.
- **Where it gained** (diagnostics, explanation only): most in the opening hour, and when NQ had
  diverged most from ES over 15 minutes (top tercile).
- **Not reproduced on current data.** Fitted by the defined procedure on all 303 sessions to
  2026-10-05 (validation 2026-07-10 to 10-05, 61 sessions), no arm activates. The intermarket
  arm's best 5-minute option gains 0.015 % there, with an interval across zero. The last two
  blocks (trained to 2026-07-10 and 2026-08-21) did not activate either.

**Reading.** The declared rule was met, so this is recorded as a pass. It is a weak one:

- the rule pooled four blocks of exact zeros with one active block, so a single 30-session
  period could carry it;
- it is the secondary horizon;
- it is the third directional experiment on these sessions today, and the Bonferroni correction
  covered only the two horizons within this one.

The 15-minute primary found nothing.

## Decision (2026-10-07): option 3 - not actionable, the fan stays centred

The formal pass stands under its registered rule and is kept as recorded. It is **not** a
readiness to deploy:

- the primary 15-minute horizon never activated;
- the 5-minute result comes entirely from one historical block;
- the procedure fitted on current data issues zero shift;
- the Bonferroni correction covered only this experiment's two horizons, not the sequence of
  directional experiments.

Check 1's fitted model was not chosen: it was the one successful fit, and selecting it after
seeing the results would add another selection step. Nothing is frozen and no forward
evaluation runs. The centre-shift experiments (`fan_direction_v1`, `fan_cond_ema_v1`,
`fan_im_dir_v1`) are archived with their negative and marginal findings. The next question is a
different one: first-hit probabilities ([docs/fan_first_hit.md](fan_first_hit.md)).

## The options that were considered

The definition says a pass is frozen and evaluated on fresh, timely forward data before any
overlay. Frozen as specified today, the procedure issues **zero shift**: it would forward-test
the current fan against itself. The options:

1. **Forward-test the procedure.** Freeze the code and definition, refit by the procedure as each
   new 30-session block completes, and score each block once. This is honest, but about six weeks per
   block, and on today's evidence it will mostly issue zero.
2. **Forward-test check 1's fitted model** (trained to 2026-03-02): the only one that activated.
   It is seven months old, and that period's relationship did not hold up in later validation.
3. **Record the pass as not actionable** and leave the fan centred, the default this work assumes.

No overlay is built in any case. The live use the review describes would also need ES, RTY and NQ
on synchronised real-time feeds; this account's feed is delayed.
