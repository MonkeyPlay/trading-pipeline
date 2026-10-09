# Estimated pre-open issuance times (reconstructed)

**Estimates, not demonstrated delivery.** Arm D is not connected to the scheduled issuance: the A/B times are the recorded ends of the Auto runs that stored each cutoff's confirming bar, and the D times add D's measured generation time to them. Demonstrated delivery is a live capture's 'delivered' step (nq_journal.py live --with-d, forecaster/live_synthesis.py). Ten sessions can inform the operational design - cutoff, deadline, late policy; they cannot establish reliable predictive performance.

Measured from the bars' receipt times and the Auto runs' recorded ends (forecaster/timeliness.py); nothing forecast, scored or sent. Deadline: the open, 09:30 ET. A hit needs a non-negative slack; a morning nothing was collected counts as a miss; sessions without receipt times are listed, not counted.

Arm D's generation time (nq_synthesis_p1_v5 only): no nq_synthesis_p1_v5 run measured yet, so no D estimate: D's columns stay empty until one is.
For reference only, not used above: nq_synthesis_p1_v4 took 25 to 30 s (median 27 s, 3 run(s)).

| cutoff | scheduled | measured | missed (not collected) | A/B in time | A/B + D in time (fastest / median / slowest D) | data ready after the cutoff, min (fastest / median / slowest) | A/B slack to the open, min (least / median / most) |
|---|---:|---:|---:|---|---|---|---|
| 09:15 | 10 | 2 | 1 | 2 of 3 | - | +11.2 / +11.2 / +11.2 | +3.5 / +3.5 / +3.5 |
| 09:20 | 10 | 2 | 1 | 0 of 3 | - | +11.2 / +11.2 / +11.2 | -1.6 / -1.6 / -1.6 |
| 09:25 | 10 | 2 | 1 | 0 of 3 | - | +11.2 / +11.2 / +11.2 | -6.6 / -6.6 / -6.6 |
| 09:29 | 10 | 2 | 1 | 0 of 3 | - | +11.2 / +11.2 / +11.2 | -10.7 / -10.6 / -10.5 |

Per session (minutes; slack to the open, negative = after it):

- 09:15, 2026-09-28: not measurable (no receipt times)
- 09:15, 2026-09-29: not measurable (no receipt times)
- 09:15, 2026-09-30: not measurable (no receipt times)
- 09:15, 2026-10-01: not measurable (no receipt times)
- 09:15, 2026-10-02: not measurable (no receipt times)
- 09:15, 2026-10-05: not measurable (no receipt times)
- 09:15, 2026-10-06: not measurable (no receipt times)
- 09:15, 2026-10-07: data ready 11.2 after the cutoff, A/B slack +3.5
- 09:15, 2026-10-08: missed (nothing collected live: data stored 20.9 h after the cutoff)
- 09:15, 2026-10-09: data ready 11.2 after the cutoff, A/B slack +3.5
- 09:20, 2026-09-28: not measurable (no receipt times)
- 09:20, 2026-09-29: not measurable (no receipt times)
- 09:20, 2026-09-30: not measurable (no receipt times)
- 09:20, 2026-10-01: not measurable (no receipt times)
- 09:20, 2026-10-02: not measurable (no receipt times)
- 09:20, 2026-10-05: not measurable (no receipt times)
- 09:20, 2026-10-06: not measurable (no receipt times)
- 09:20, 2026-10-07: data ready 11.2 after the cutoff, A/B slack -1.6
- 09:20, 2026-10-08: missed (nothing collected live: data stored 20.8 h after the cutoff)
- 09:20, 2026-10-09: data ready 11.2 after the cutoff, A/B slack -1.6
- 09:25, 2026-09-28: not measurable (no receipt times)
- 09:25, 2026-09-29: not measurable (no receipt times)
- 09:25, 2026-09-30: not measurable (no receipt times)
- 09:25, 2026-10-01: not measurable (no receipt times)
- 09:25, 2026-10-02: not measurable (no receipt times)
- 09:25, 2026-10-05: not measurable (no receipt times)
- 09:25, 2026-10-06: not measurable (no receipt times)
- 09:25, 2026-10-07: data ready 11.2 after the cutoff, A/B slack -6.6
- 09:25, 2026-10-08: missed (nothing collected live: data stored 20.7 h after the cutoff)
- 09:25, 2026-10-09: data ready 11.2 after the cutoff, A/B slack -6.6
- 09:29, 2026-09-28: not measurable (no receipt times)
- 09:29, 2026-09-29: not measurable (no receipt times)
- 09:29, 2026-09-30: not measurable (no receipt times)
- 09:29, 2026-10-01: not measurable (no receipt times)
- 09:29, 2026-10-02: not measurable (no receipt times)
- 09:29, 2026-10-05: not measurable (no receipt times)
- 09:29, 2026-10-06: not measurable (no receipt times)
- 09:29, 2026-10-07: data ready 11.2 after the cutoff, A/B slack -10.5
- 09:29, 2026-10-08: missed (nothing collected live: data stored 20.7 h after the cutoff)
- 09:29, 2026-10-09: data ready 11.2 after the cutoff, A/B slack -10.7
