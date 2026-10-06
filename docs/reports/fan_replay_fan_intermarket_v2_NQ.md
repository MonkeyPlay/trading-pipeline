# Live-style replay: fan_intermarket_v2, NQ

Code 70ef93ebe654; data `873e55e084d746bf`; candidates `lin_pois_ivx`, `gbm_own_ivx` (each trained on its check's training rows).

Sessions (predefined from what was known before each opened): release 2026-03-06, 2026-05-01, 2026-07-02; volatile 2026-03-24, 2026-06-10, 2026-06-11; ordinary 2026-07-06, 2026-03-23, 2026-03-27. Origins (ET): 19:00, 03:00, 08:25, 09:28, 10:30, 15:30 - the forecast issued as the origin's bar closes.

**Passed.**

| Check | What | Compared | Failed | Largest relative difference |
|---|---|---|---|---|
| features | every feature from the panel as of the issuance, against the research table | 7,182 | 0 | 0.00e+00 |
| forming bar | the same issuance 30 s into the next minute (its bar still forming), against the above | 7,182 | 0 | 0.00e+00 |
| baseline | fan_rw_v2's variance to every horizon from load_days(as_of), against the cached frame | 594 | 0 | 0.00e+00 |
| multiplier | each candidate on the replayed features and variance, against the research batch | 882 | 0 | 2.85e-16 |
| alone | one origin predicted alone, against the same origin inside the session's batch | 882 | 0 | 2.85e-16 |

Not replayable: a bar stored as preliminary (is_completed = 0, within 2 hours of collection) may be revised by a later collection; history holds only the revised value. The forward record logs the live inputs.
