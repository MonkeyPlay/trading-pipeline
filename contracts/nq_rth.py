# contracts/nq_rth.py
"""
The RTH analogue matcher's definition (docs/rth_analogues.md): which earlier NQ
sessions opened most like the session in progress, re-scored as each completed
minute of its regular session arrives, over its first hour.

This is a separate matcher from the pre-open one (contracts/nq_preopen.py,
matching/structural.py), under its own version and its own definition kind
('rth_matcher'), with its own tables (migrations 0024, 0025). Nothing about the
pre-open snapshot, cutoff, annotation, analogue set or forecast changes.

  RTH_MATCHER_VERSION   the version every new RTH set is stored under
  WEIGHTS, GROUPS       the eleven features and their weights (percent, sum 100)
  TOLERANCES            per feature, the difference at which it scores 0
  CALIBRATION           how, when and on which sessions the tolerances were set
  SCALED                features whose tolerance grows with the window's length
  MAX_MINUTES           the first hour: windows of 1..60 minutes (09:31..10:30 ET)
  CHECKPOINTS           15, 30 and 60 minutes: 09:45, 10:00, 10:30 ET (marked)
  ALWAYS_ISSUED         windows an issue stores even when it skipped their minute
  ISSUED_BY             auto (Auto mode), manual (a person), backfill (reconstruction)
  LIVE_MAX_LAG          a set issued by auto or manual within this long of its cutoff
                        is live; anything else a historical reconstruction (the
                        database decides, by its clock)

Similarity is agreement between two observed opening paths. It is never a
probability of anything, and nothing here is fitted to what happened afterwards.
"""

from __future__ import annotations

from datetime import timedelta
from fractions import Fraction
from typing import Any, Dict

from contracts.nq_prompt_v2 import _record

# nq_match_rth_v1 (registered 2026-10-09 09:35 UTC by the first rth-backfill, its 774 reconstructions kept): a bar
# was complete only once the next minute's bar was stored, so a missing minute also dropped the bar before it; live
# was decided by age alone; the calibration was described in prose only.
# nq_match_rth_v2 (the same day, after an outside review): a bar is complete once any later bar of the session is
# stored; a set records how it was issued (auto, manual, backfill - a backfill is never live) and when its inputs
# reached the store; the calibration's sample, method and values are part of the definition; the 45-minute window
# is always issued (the predefined evaluation's 10:15 cutoff, docs/rth_analogues.md). Weights, tolerances and
# features are v1's.
RTH_MATCHER_VERSION = "nq_match_rth_v2"
KIND = "rth_matcher"

MAX_MINUTES = 60                                  # 09:30 + 60 = 10:30 ET: automatic matching stops there
CHECKPOINTS = (15, 30, 60)                        # 09:45, 10:00 and 10:30 ET
ALWAYS_ISSUED = (15, 30, 45, 60)                  # stored even when the issue skipped their minute (bars late)
ISSUED_BY = ("auto", "manual", "backfill")
LIVE_ISSUERS = ("auto", "manual")
PROVISIONAL_MINUTES = 10                          # a window shorter than this is labelled provisional
TOP_ANALOGUES = 5
MIN_COMPARABLE = Fraction(75)                     # percent of the weight that must be comparable
LIVE_MAX_LAG = timedelta(minutes=30)              # also in migration 0025's trigger (tests check they agree)
RELVOL_SESSIONS = 20                              # relative volume: against the same window of the sessions before
RELVOL_MIN_SESSIONS = 10                          # ... of which at least this many hold the whole window
SCALE_MINUTES = 30                                # the window length the scaled tolerances are stated for

# Feature -> weight in percent (exact, sum 100). The developing opening path carries 65, its location against the
# day's VWAP and the frozen pre-open levels 20, relative volume 5, the pre-open context itself 10.
WEIGHTS: Dict[str, Fraction] = {
    "path": Fraction(25), "net_move": Fraction(15), "range": Fraction(10),
    "deepest_pullback": Fraction(15, 2), "largest_recovery": Fraction(15, 2),
    "vs_vwap": Fraction(10), "in_overnight_range": Fraction(5), "vs_prev_close": Fraction(5),
    "relative_volume": Fraction(5),
    "gap": Fraction(5), "overnight_range": Fraction(5),
}
GROUPS = {
    "Opening path": ["path", "net_move", "range", "deepest_pullback", "largest_recovery"],
    "Location": ["vs_vwap", "in_overnight_range", "vs_prev_close"],
    "Volume": ["relative_volume"],
    "Pre-open context": ["gap", "overnight_range"],
}
LABELS = {
    "path": "Path from the open", "net_move": "Net move", "range": "Range so far",
    "deepest_pullback": "Deepest pullback", "largest_recovery": "Largest recovery",
    "vs_vwap": "vs VWAP", "in_overnight_range": "In overnight range", "vs_prev_close": "vs prev. close",
    "relative_volume": "Relative volume", "gap": "Opening gap", "overnight_range": "Overnight range",
}
# How a value is shown: 'atr' in daily-ATR units, 'fraction' of the overnight range, 'ratio' (volume)
UNITS = {f: "atr" for f in WEIGHTS} | {"in_overnight_range": "fraction", "relative_volume": "ratio"}

# Per feature, the absolute difference at which its score reaches 0 (score = max(0, 1 - |a - b| / tolerance)); for
# the SCALED features the value is the one at SCALE_MINUTES, multiplied by sqrt(minutes / SCALE_MINUTES) - an opening
# path's spread grows roughly with the square root of its length. Set once (2026-10-09) from the spread of the feature
# values themselves (CALIBRATION, matching/rth.calibrate) - so a typical pair of sessions scores about 0.5 on a
# feature. No outcome and no continuation after any cutoff was looked at; but the sessions of the sample did set the
# scales, so a set of a session inside it is a description, not a forward test.
TOLERANCES: Dict[str, Fraction] = {
    "path": Fraction("0.44"), "net_move": Fraction("0.53"), "range": Fraction("0.27"),
    "deepest_pullback": Fraction("0.28"), "largest_recovery": Fraction("0.26"),
    "vs_vwap": Fraction("0.49"), "in_overnight_range": Fraction("1.0"), "vs_prev_close": Fraction("1.1"),
    "relative_volume": Fraction("0.44"), "gap": Fraction("0.94"), "overnight_range": Fraction("0.55"),
}
SCALED = ("path", "net_move", "range", "deepest_pullback", "largest_recovery")
CALIBRATION: Dict[str, Any] = {
    "computed": "2026-10-09", "store": "the production store as of 2026-10-09 (bars and nq_evidence_v5_r0929 "
                                       "snapshots); reproduce with nq_journal.py rth-calibrate",
    "method": "for each feature, the median absolute difference over every pair of sessions at a 30-minute window "
              "(path: the root mean square difference of the two paths); tolerance = 2 x median, to two "
              "significant figures",
    "sample": "every NQ session with a pre-open snapshot of nq_evidence_v5_r0929, a valid frozen daily ATR and a "
              "whole confirmed 30-minute window",
    "first_session": "2025-09-29", "last_session": "2026-10-07", "sessions": 258,
    "medians": {"path": "0.2224", "net_move": "0.2648", "range": "0.1365", "deepest_pullback": "0.1398",
                "largest_recovery": "0.1300", "vs_vwap": "0.2461", "in_overnight_range": "0.5036",
                "vs_prev_close": "0.5374", "relative_volume": "0.2194", "gap": "0.4676", "overnight_range": "0.2768"},
    "inputs_looked_at": "feature values at the 30-minute cutoff only: no outcome, no bar after any cutoff",
    "scope": "sets of sessions from first_session to last_session use scales those same sessions set (and later "
             "ones): descriptive reconstruction, not a faithful forward test. A forward evaluation uses sessions "
             "after last_session only, or recalibrates on its training history alone under a new version",
}

DEFINITION = {
    "purpose": "which earlier NQ sessions opened most like the session in progress, over its first hour - "
               "a description of resemblance, not a forecast",
    "window": "expanding from the 09:30 ET open (America/New_York, the trading calendar's DST-aware instants): at "
              "n minutes both sessions' first n completed 1-minute RTH bars, [09:30, 09:30 + n); never a rolling "
              "window; n = 1..60",
    "completed_bar": "a stored 1-minute bar counts once any later bar of the same session is stored (the collector "
                     "may store the bar still forming); the window is the unbroken run of such bars from 09:30. Where "
                     "it stops is recorded: awaiting_confirmation (the next minute's bar stored, nothing later yet), "
                     "not_stored (the feed is behind), gap (the next minute's bar missing while later bars are "
                     "stored - a confirmed hole, never filled)",
    "unit": "every price difference is in the session's own frozen daily Wilder ATR(14) from its pre-open snapshot "
            "(atr.daily.exact) - known before the open; never the session's eventual range, high, low or close",
    "features": {
        "path": "root mean square over the window's minutes of the difference of the two sessions' "
                "(close_i - RTH open) / ATR",
        "net_move": "(close at the cutoff - RTH open) / ATR",
        "range": "(highest high - lowest low in the window) / ATR",
        "deepest_pullback": "largest fall from a running high within the window, / ATR",
        "largest_recovery": "largest rise from a running low within the window, / ATR",
        "vs_vwap": "(close at the cutoff - VWAP of the Globex day through the cutoff, hlc3 x volume) / ATR",
        "in_overnight_range": "(close at the cutoff - frozen ON low) / (ON high - ON low), clipped to [-1, 2]",
        "vs_prev_close": "(close at the cutoff - frozen previous RTH close) / ATR",
        "relative_volume": "ln(volume of the window / mean volume of the same window over the 20 scheduled "
                           "sessions before, on their active contracts); not comparable unless 10 of them hold the "
                           "whole window",
        "gap": "(RTH open - frozen previous RTH close) / ATR",
        "overnight_range": "(frozen ON high - frozen ON low) / ATR",
    },
    "weights": {k: str(v) for k, v in WEIGHTS.items()},
    "groups": GROUPS,
    "tolerances": {k: str(v) for k, v in TOLERANCES.items()},
    "calibration": CALIBRATION,
    "scaled": list(SCALED),
    "scale": f"a scaled feature's tolerance is the stated one x sqrt(n / {SCALE_MINUTES})",
    "feature_score": "max(0, 1 - |target - analogue| / tolerance)",
    "comparable": "a feature counts when both sessions have a value; a missing input (a frozen level not valid, "
                  "too few sessions for relative volume) is neither a match nor a mismatch",
    "coverage": "comparable_weight = the weights of the comparable features; candidates under 75 are rejected",
    "score": "similarity = 100 x sum(weight x feature score) / comparable_weight - a resemblance score, not a "
             "calibrated probability",
    "pool": "every earlier scheduled NQ session (never the target date or later) with a pre-open snapshot of the "
            "profile's version (its newest: the context reads only the daily ATR, previous RTH close and ON high / "
            "low), a valid frozen daily ATR and its whole window of completed bars on its active contract; "
            "re-scored in full at every cutoff (not limited to the pre-open analogues)",
    "selection": "the 5 highest similarities; ties by higher comparable_weight, then the more recent session",
    "point_in_time": "target and candidates stop at the same elapsed RTH minute; frozen pre-open context only; "
                     "what a candidate did after the cutoff is attached for display after selection and is never "
                     "an input to a score, a rank or the set's identity",
    "identity": "a set is stored once per session, version, window and input digest (the target's window bars "
                "and features and every candidate's features); a revised input makes a new set beside the old",
    "issue": "an issue (auto: Auto mode after a successful collection; manual: a person) stores the newest window "
             "and any of 15, 30, 45 and 60 minutes not yet stored; a backfill stores reconstructions. data_mode, by "
             "the database clock: 'live' only when issued by auto or manual within 30 minutes of the cutoff, else "
             "'historical_reconstruction' - a backfill always",
    "provenance": "each set records when its inputs' values reached the store (bars.version_stored_at; a value "
                  "without one was stored before migration 0023 was applied; snapshots' built_at): the target's "
                  "window, confirming bar, overnight bars and snapshot (inputs_received_at - its distance from the "
                  "cutoff is the feed's delay) and every earlier session read (pool_received_at); pit_status "
                  "'verified' when every earlier input was in the store by the cutoff and the target's within 30 "
                  "minutes of it (as a live issue would have had them), else 'unverified'",
    "views": "as issued: of the live sets, the newest stored by the time replayed; reconstructed: the newest set "
             "of the longest window not past the minute replayed, any mode - labelled as such",
    "provisional": f"a window under {PROVISIONAL_MINUTES} minutes is labelled provisional",
    "checkpoints": list(CHECKPOINTS),
    "always_issued": list(ALWAYS_ISSUED),
}


def matcher_record() -> Dict[str, Any]:
    return _record(RTH_MATCHER_VERSION, KIND, DEFINITION)
