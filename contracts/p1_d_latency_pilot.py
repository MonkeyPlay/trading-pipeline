# contracts/p1_d_latency_pilot.py
"""
The v5 latency pilot (review of 2026-10-09): five explicitly approved arm D requests,
before the live A/B/D comparison is frozen, to fill the empty D columns of the
timeliness estimates (forecaster/timeliness.py) with v5's own generation times.

  sessions     five fixed sessions before any confirmatory sample, on the 09:15
               candidate profile (the bundle the live comparison would send); a
               historical replay of sessions whose outcomes are known - which is why
               nothing is scored
  approval     one run started by hand: "send" typed at a terminal, or a one-time
               dashboard approval for exactly these sessions (forecaster/approvals.py)
  cap          at most MAX_REQUESTS requests and MAX_USD in all. Requests go one at a
               time; before each, the pilot's spend so far (its answered requests at
               list prices) plus that request at its worst (its whole 64,000-token
               cap: about $1.34) must stay within MAX_USD - so the cap holds even if
               every request ran to its limit. v4's requests cost $0.11-0.13, so five
               fit; a request that would not is not sent
  measured     per request: duration (the run's generation_completed_at minus
               generation_started_at), the validation outcome (issued, invalid,
               failed and why), the tokens by kind and their cost at list prices
  not measured no predictive score is computed or read; five requests are first
               observations of v5's timing, not an estimate of its tail latency
  exclusion    the pilot's sessions are never in the confirmatory sample: the live
               comparison is prospective, and its definition names them as excluded
"""

from __future__ import annotations

from contracts import nq_forecast as fc

NAME = "p1_d_latency_pilot_v1"
PROFILE = "candidate_0915"
SESSIONS = ("2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09")
ALGORITHM = fc.SYNTHESIS_VERSION
MAX_REQUESTS = 5
MAX_USD = 2.00
# list prices per million tokens, as the plans use (forecaster/llm_arms._cost); cache writes and reads at the usual
# 1.25x and 0.1x of the input price
USD_PER_MTOK = {"input_tokens": 4.0, "cache_creation_input_tokens": 5.0, "cache_read_input_tokens": 0.4,
                "output_tokens": 20.0}
