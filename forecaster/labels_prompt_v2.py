# forecaster/labels_prompt_v2.py
"""
Realised NQ-v2 outcome labels (label version contracts/nq_prompt_v2.LABEL_VERSION):
the five scored classifications of the post-session prompt P2, the 30-minute
opening bias, the first level tested and the LO-v1 level outcomes, from a frozen
evidence snapshot and the session's realised 1m bars.

``compute_outcome(snapshot, bars)`` is pure. It reads the snapshot's frozen
thresholds (T, B, A) and sweep references - never a prediction, an explanation or
anything else about a forecast - measures the windows, then classifies:

  first_move_5m      which of O+T / O-T is reached first in [09:30, 09:35)
  direction_15m      C15 - O against T
  opening_type_15m   P2's ordered list (sweeps, drives, whipsaw, range)
  opening_bias_30m   C30 - O against T
  close_direction_rth  RTH close - O against B (standard sessions)
  session_type_rth   P2's ordered list over the complete RTH (standard sessions)
  first_level_tested the first frozen reference reached in [09:30, 09:45)
  first_level_outcome  LO-v1 for that level over [09:30, 09:45)
  *_outcome          LO-v1 for ON high / low and previous-RTH high / low over the
                     standard RTH
  descriptors        P2 sections 5 and 7: IB direction, opening drive strength,
                     range extensions, gap outcome, high / low timing, MP-v1
                     morning pullback, afternoon continuation, matched-session
                     fields, trend persistence, and the first 15-minute pattern
                     by the FP-v1 convention

Measurements (window highs / lows / closes, opening confirmation time, ...) are
kept alongside; the 40-field P2 table is forecaster/outcome_display.py.

Ordered rules use three-valued logic: a rule that cannot be decided (a missing
reference, a missing 09:29 bar for the gap rule) makes the label None when it
could change the winner - a lower rule is never chosen past it. Every None
carries a reason (contracts/nq_prompt_v2.REASONS). The decisions the prompts
leave open are contracts/nq_prompt_v2.LABEL_CONVENTIONS.

Comparing labels with predictions is a separate step (scoring), not done here.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence, Tuple

from contracts import nq_prompt_v2 as defs
from features import calendar as cal
from features.nq_evidence import dec, iso

MINUTE = timedelta(minutes=1)
SETTLE = timedelta(hours=2)        # the collector stores bars within 2h of a session as not completed
_EFF_MIN = Decimal("0.60")
_E_TREND, _CL_HIGH, _CL_LOW, _E_FLAT = Decimal("0.60"), Decimal("0.80"), Decimal("0.20"), Decimal("0.35")
_SHOW = Decimal("0.000001")
_STRONG = Decimal("0.80")
_E_HIGH, _E_MODERATE = Decimal("0.60"), Decimal("0.35")
_MP_NONE, _MP_MINOR, _MP_MODERATE = Decimal("0.10"), Decimal("0.382"), Decimal("0.618")
_NOON, _TWO_PM, _IB_END = 150, 270, 60          # minutes after 09:30


@dataclass(frozen=True)
class RBar:
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


def _label(label=None, reason=None, detail=None) -> Dict[str, Any]:
    return {"label": label, "reason": reason, "detail": detail}


def _and(*xs):
    """Kleene AND over True / False / None (unknown)."""
    if any(x is False for x in xs):
        return False
    return None if any(x is None for x in xs) else True


def _or(*xs):
    """Kleene OR over True / False / None (unknown)."""
    if any(x is True for x in xs):
        return True
    return None if any(x is None for x in xs) else False


def _s(value) -> Optional[str]:
    return None if value is None else str(value)


def session_finalised(session_date, now: datetime) -> bool:
    """True once the session's scheduled close is two hours past (the collector's revision window)."""
    s = cal.session(session_date)
    return s.scheduled_close_at is not None and now >= s.scheduled_close_at + SETTLE


def load_realised_bars(conn, snapshot: Dict[str, Any]) -> List[RBar]:
    """The 1m bars of the snapshot's contract from 09:29 to the scheduled close (realised data)."""
    s = cal.session(snapshot["session_date"])
    rows = conn.execute(
        "SELECT timestamp_utc, open, high, low, close FROM bars WHERE contract_id = %s AND interval = '1m' "
        "AND price_type = 'TRADES' AND timestamp_utc >= %s AND timestamp_utc < %s ORDER BY timestamp_utc;",
        (snapshot["contract_id"], s.rth_open_at - MINUTE, s.scheduled_close_at),
    ).fetchall()
    return [RBar(datetime.strptime(str(r[0])[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc),
                 dec(r[1]), dec(r[2]), dec(r[3]), dec(r[4])) for r in rows]


def _show_fraction(v: Fraction) -> str:
    return str((Decimal(v.numerator) / Decimal(v.denominator)).quantize(_SHOW))


def _vwap(payload: Dict[str, Any]) -> Optional[Fraction]:
    """The frozen cutoff VWAP: sum(hlc3 x volume) / sum(volume) over the snapshot's archived
    overnight 1m bars, exactly; None under 90% overnight coverage or without volume."""
    bars = payload.get("bars") or {}
    coverage = (bars.get("coverage") or {}).get("ratio")
    if coverage is None or Decimal(str(coverage)) < defs.ON_MIN_COVERAGE:
        return None
    pv, vol = Fraction(0), Fraction(0)
    for _, _o, h, low, c, v in bars.get("1m", []):
        pv += (Fraction(dec(h)) + Fraction(dec(low)) + Fraction(dec(c))) / 3 * v
        vol += v
    return None if vol == 0 else pv / vol


def _long_ma(payload: Dict[str, Any], overnight_start: datetime) -> Optional[Fraction]:
    """The frozen cutoff Long MA: EMA(100) of the snapshot's overnight 2m bars at the last one (nq_conv_v2), computed
    as the structure annotation computes it; None under 100 bars. Kept to six decimals."""
    from forecaster.structure_rules import _bars, moving_averages
    bars = [b for b in _bars(payload, "2m") if b.start >= overnight_start]
    if len(bars) < 100:
        return None
    value = float(moving_averages(bars)["ema_trend"].iloc[-1])
    return Fraction(Decimal(repr(value)).quantize(_SHOW))


class _Session:
    """The realised bars of one session indexed by minute from the open (-1 = the 09:29 bar)."""

    def __init__(self, snapshot: Dict[str, Any], bars: Sequence[RBar]):
        payload = snapshot["payload"]
        self.session = cal.session(payload["identity"]["session_date"])
        self.open_at = self.session.rth_open_at
        self.n = int((self.session.scheduled_close_at - self.open_at).total_seconds() // 60)
        self.by_min: Dict[int, RBar] = {}
        for b in bars:
            m = int((b.start - self.open_at).total_seconds() // 60)
            if -1 <= m < self.n and (b.start - self.open_at).total_seconds() % 60 == 0:
                self.by_min[m] = b
        th = payload["thresholds"]
        self.T = None if th.get("T") is None else Decimal(th["T"])
        self.B = None if th.get("B") is None else Decimal(th["B"])
        self.A = None if th.get("A") is None else Fraction(th["A"])      # exact 'num/den'
        self.refs = {name: dec(payload["references"].get(name, {}).get("value"))
                     for name in defs.SWEEP_REFERENCES}
        self.prev_close_ref = payload["references"].get("prev_rth_close", {}).get("value")
        # first-level candidates as exact values (None: unavailable) and their display prices
        self.levels: Dict[str, Optional[Fraction]] = {}
        self.level_shown: Dict[str, Optional[str]] = {}
        for name in defs.FIRST_LEVEL_CANDIDATES:
            if name in ("vwap", "long_ma"):
                v = _vwap(payload) if name == "vwap" else _long_ma(payload, self.session.overnight_start_at)
                self.levels[name] = v
                self.level_shown[name] = None if v is None else _show_fraction(v)
            else:
                d = dec(payload["references"].get(name, {}).get("value"))
                self.levels[name] = None if d is None else Fraction(d)
                self.level_shown[name] = None if d is None else str(d)
        first = self.by_min.get(0)
        self.O = None if first is None else first.open
        last_pre = self.by_min.get(-1)
        self.pre_close = None if last_pre is None else last_pre.close

    def at(self, minute: int) -> str:
        return (self.open_at + minute * MINUTE).astimezone(cal.NY_TZ).strftime("%H:%M")

    def window(self, a: int, b: int) -> Tuple[List[RBar], List[int]]:
        present = [self.by_min[m] for m in range(a, b) if m in self.by_min]
        return present, [m for m in range(a, b) if m not in self.by_min]

    def missing_detail(self, missing: List[int]) -> str:
        shown = ", ".join(self.at(m) for m in missing[:5])
        return f"{len(missing)} bar(s) missing ({shown}{', ...' if len(missing) > 5 else ''})"


# --------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------

def _first_move(x: _Session, meas: Dict[str, Any]) -> Dict[str, Any]:
    if x.T is None:
        return _label(reason="missing_threshold", detail="T unavailable (no frozen 2-minute ATR)")
    if x.O is None:
        return _label(reason="missing_bars", detail="no 09:30 bar: O unknown")
    up, down = x.O + x.T, x.O - x.T
    for m in range(0, 5):
        bar = x.by_min.get(m)
        if bar is None:
            return _label(reason="missing_bars", detail=f"{x.at(m)} bar missing before either threshold was reached")
        hit_up, hit_down = bar.high >= up, bar.low <= down
        if hit_up and hit_down:
            return _label(reason="ambiguous_intrabar", detail=f"both thresholds reached in the {x.at(m)} bar")
        if hit_up or hit_down:
            meas["first_move_bar"] = x.at(m)
            return _label("up_first" if hit_up else "down_first")
    return _label("neither")


def _direction(x: _Session, close_minute: int, threshold: Optional[Decimal], name: str,
               meas: Dict[str, Any], key: str) -> Dict[str, Any]:
    if threshold is None:
        return _label(reason="missing_threshold", detail=f"{name} unavailable")
    if x.O is None:
        return _label(reason="missing_bars", detail="no 09:30 bar: O unknown")
    bar = x.by_min.get(close_minute)
    if bar is None:
        return _label(reason="missing_bars", detail=f"no {x.at(close_minute)} bar")
    meas[key] = str(bar.close)
    diff = bar.close - x.O
    return _label("bullish" if diff > threshold else "bearish" if diff < -threshold else "neutral_band")


def opening_type_rules(x: _Session):
    """
    ``(unavailable, rules)``: the opening type's early-unavailable label (no T, no O,
    an incomplete window) or None, and every P2 rule in order as ``(label, result,
    notes)`` with result True / False / None (undecidable) - range, the last rule,
    is implied. Exposed for the disagreement report; ``_opening_type`` decides.
    """
    if x.T is None:
        return _label(reason="missing_threshold", detail="T unavailable (no frozen 2-minute ATR)"), []
    if x.O is None:
        return _label(reason="missing_bars", detail="no 09:30 bar: O unknown"), []
    bars, missing = x.window(0, 15)
    if missing:
        return _label(reason="missing_bars", detail="first 15 minutes incomplete: " + x.missing_detail(missing)), []
    O, T = x.O, x.T
    H, L, C = max(b.high for b in bars), min(b.low for b in bars), bars[-1].close

    def crossed_by_gap(v: Decimal, notes: List[str]):
        if x.pre_close is None:
            notes.append("no 09:29 bar: whether the opening gap crossed a reference is unknown")
            return None
        return min(x.pre_close, O) < v < max(x.pre_close, O)

    def swept(name: str, v: Optional[Decimal], low_side: bool, notes: List[str]):
        if v is None:
            # Breaching a support below O by T needs L15 < O - T (a resistance: H15 > O + T);
            # without that, no value of the missing level could make this a sweep.
            if (L >= O - T) if low_side else (H <= O + T):
                return False
            notes.append(f"{name} unavailable")
            return None
        if (v >= O) if low_side else (v <= O):
            return False                       # not a support (resistance) of this open
        breach = next((i for i, b in enumerate(bars) if (b.low <= v - T if low_side else b.high >= v + T)), None)
        reclaimed = breach is not None and any((b.close > v if low_side else b.close < v) for b in bars[breach + 1:])
        gap = crossed_by_gap(v, notes) if reclaimed else False
        return _and(None if gap is None else not gap, reclaimed)

    rules = [
        ("sweep_low_rebound",
         lambda n: _and(_or(*[swept(name, v, True, n) for name, v in x.refs.items()]), C > O + T)),
        ("sweep_high_reverse",
         lambda n: _and(_or(*[swept(name, v, False, n) for name, v in x.refs.items()]), C < O - T)),
        # efficiency >= 0.60 tested as |C - O| >= 0.60 (H - L): exact, and no division by a zero range
        ("opening_drive_up", lambda n: C > O + T and H > L and abs(C - O) >= _EFF_MIN * (H - L) and O - L <= T),
        ("opening_drive_down", lambda n: C < O - T and H > L and abs(C - O) >= _EFF_MIN * (H - L) and H - O <= T),
        ("two_sided_whipsaw", lambda n: H >= O + T and L <= O - T),
    ]
    out = []
    for label, rule in rules:
        notes: List[str] = []
        out.append((label, rule(notes), notes))
    return None, out


def _opening_type(x: _Session, meas: Dict[str, Any]) -> Dict[str, Any]:
    unavailable, rules = opening_type_rules(x)
    if unavailable is not None:
        return unavailable
    bars, _ = x.window(0, 15)
    H, L, C = max(b.high for b in bars), min(b.low for b in bars), bars[-1].close
    eff = abs(C - x.O) / (H - L) if H > L else None
    meas.update(H15=str(H), L15=str(L), C15=str(C),
                efficiency_15m=None if eff is None else str(eff.quantize(_SHOW)))
    for label, result, notes in rules:
        if result is True:
            return _label(label)
        if result is None:
            reason = "missing_reference" if any("unavailable" in u for u in notes) else "missing_bars"
            return _label(reason=reason, detail=f"rule {label} undecidable: " + "; ".join(dict.fromkeys(notes)))
    return _label("range")


def session_type_rules(x: _Session, direction: Optional[str]):
    """
    ``(unavailable, rules)``: the session type's early-unavailable label or None, and
    the five P2 rules in order as ``(label, result)`` over the complete standard RTH
    (a zero-range session: only range_day holds). Exposed for the disagreement
    report; ``_session_type`` decides.
    """
    if x.session.schedule != "full":
        return _label(reason="shortened_session", detail=f"{x.session.schedule} session"), []
    if x.A is None or x.B is None:
        return _label(reason="missing_threshold", detail="A / B unavailable (no frozen daily ATR)"), []
    if x.O is None:
        return _label(reason="missing_bars", detail="no 09:30 bar: O unknown"), []
    bars, missing = x.window(0, x.n)
    if missing:
        return _label(reason="missing_bars", detail="RTH incomplete: " + x.missing_detail(missing)), []
    ib = bars[:60]
    ib_high, ib_low = max(b.high for b in ib), min(b.low for b in ib)
    high, low, close = max(b.high for b in bars), min(b.low for b in bars), bars[-1].close
    R = high - low
    if R == 0:                            # P2: a fully observed zero-range session is a range day
        return None, [("reversal_day", False), ("bull_trend_day", False), ("bear_trend_day", False),
                      ("two_sided_volatile_day", False), ("range_day", True)]
    # E = |close - O| / R and CL = (close - low) / R, compared by cross-multiplication (exact)
    move, location = abs(close - x.O), close - low
    quarter = x.A / 4
    return None, [
        ("reversal_day", (Fraction(ib_low) <= Fraction(x.O) - quarter and direction == "bullish")
         or (Fraction(ib_high) >= Fraction(x.O) + quarter and direction == "bearish")),
        ("bull_trend_day", direction == "bullish" and move >= _E_TREND * R and location >= _CL_HIGH * R),
        ("bear_trend_day", direction == "bearish" and move >= _E_TREND * R and location <= _CL_LOW * R),
        ("two_sided_volatile_day", Fraction(R) >= x.A and move < _E_FLAT * R),
        ("range_day", Fraction(R) < x.A and move < _E_FLAT * R),
    ]


def _session_type(x: _Session, close_dir: Dict[str, Any], meas: Dict[str, Any]) -> Dict[str, Any]:
    unavailable, rules = session_type_rules(x, close_dir["label"])
    if unavailable is not None:
        return unavailable
    bars, _ = x.window(0, x.n)
    ib = bars[:60]
    high, low, close = max(b.high for b in bars), min(b.low for b in bars), bars[-1].close
    R = high - low
    meas.update(IB_high=str(max(b.high for b in ib)), IB_low=str(min(b.low for b in ib)), IB_close=str(ib[-1].close),
                RTH_high=str(high), RTH_low=str(low), RTH_close=str(close), R=str(R))
    if R != 0:
        meas.update(E=str((abs(close - x.O) / R).quantize(_SHOW)), CL=str(((close - low) / R).quantize(_SHOW)))
    winner = next((label for label, result in rules if result), None)
    if winner is not None:
        return _label(winner)
    return _label(reason="uncovered", detail="complete session; no P2 session-type rule applies")


def _first_level(x: _Session, meas: Dict[str, Any]) -> Dict[str, Any]:
    """
    P2 section 6 under FL-v2: the first candidate reached in [09:30, 09:45), by 1m bar and order within it.
    Within a bar the level nearest the open is first - observed when every level reached lies on one side of the
    open, estimated when both sides were reached (first_level_order); candidates at one price are named by
    FIRST_LEVEL_PRECEDENCE (first_level_coincident lists the others).
    """
    if x.O is None:
        return _label(reason="missing_bars", detail="no 09:30 bar: O unknown")
    missing = [n for n, v in x.levels.items() if v is None]
    if missing:
        return _label(reason="missing_reference",
                      detail="unavailable candidate(s) could have been reached first: " + ", ".join(missing))
    for m in range(0, 15):
        bar = x.by_min.get(m)
        if bar is None:
            return _label(reason="missing_bars", detail=f"{x.at(m)} bar missing before any level was reached")
        low, high, op = Fraction(bar.low), Fraction(bar.high), Fraction(bar.open)
        reached = {n: v for n, v in x.levels.items() if low <= v <= high}
        if not reached:
            continue
        both_sides = any(v > op for v in reached.values()) and any(v < op for v in reached.values())
        nearest = min(abs(v - op) for v in reached.values())
        first = [n for n, v in reached.items() if abs(v - op) == nearest]
        if len({reached[n] for n in first}) > 1:          # one above and one below at the same distance
            meas.update(first_level_price=None, first_level_bar=x.at(m))
            return _label(reason="ambiguous_intrabar",
                          detail=f"levels above and below the {x.at(m)} bar's open at the same distance: "
                                 + ", ".join(sorted(first)))
        first.sort(key=defs.FIRST_LEVEL_PRECEDENCE.index)
        meas.update(first_level_price=x.level_shown[first[0]], first_level_bar=x.at(m),
                    first_level_order="estimated" if both_sides and reached[first[0]] != op else "observed",
                    first_level_coincident=first[1:])
        return _label(first[0])
    meas["first_level_price"] = None
    return _label(reason="none_tested", detail="no candidate level reached in the complete first 15 minutes")


def _level_outcome(x: _Session, level: Optional[Fraction], name: str, a: int, b: int,
                   allow_not_tested: bool) -> Dict[str, Any]:
    """LO-v1 for one level over minutes [a, b) of the session (contracts/nq_prompt_v2.LABEL_CONVENTIONS)."""
    if level is None:
        return _label(reason="missing_reference", detail=f"{name} unavailable")
    if x.O is None:
        return _label(reason="missing_bars", detail="no 09:30 bar: O unknown")
    O = Fraction(x.O)
    if level == O:
        return _label(reason="approach_unresolved", detail=f"{name} equals the opening trade")
    from_above = O > level

    def beyond(close: Decimal) -> bool:
        return Fraction(close) < level if from_above else Fraction(close) > level

    def back(close: Decimal) -> bool:
        return Fraction(close) > level if from_above else Fraction(close) < level

    bars, missing = x.window(a, b)
    if any(beyond(bar.close) for bar in bars):
        final = [x.by_min.get(m) for m in range(b - 3, b)]
        if any(bar is None for bar in final):
            return _label(reason="missing_bars", detail="the window's final three 1m bars are not all stored")
        if all(beyond(bar.close) for bar in final):
            return _label("break_acceptance")
        if all(back(bar.close) for bar in final):
            return _label("break_reclaim_acceptance")
        return _label("break_without_acceptance")
    if missing:
        return _label(reason="missing_bars", detail="cannot rule out a test or breach: " + x.missing_detail(missing))
    touched = next((i for i, bar in enumerate(bars)
                    if (Fraction(bar.low) <= level if from_above else Fraction(bar.high) >= level)), None)
    if touched is None:
        if allow_not_tested:
            return _label("not_tested")
        return _label(reason="no_rejection", detail=f"{name} not reached in the window")
    if any(back(bar.close) for bar in bars[touched:]):
        return _label("test_rejection")
    return _label(reason="no_rejection", detail=f"{name} reached without a later close back on the original side")


# --------------------------------------------------------------------------
# Supplementary descriptors (P2 sections 5 and 7)
# --------------------------------------------------------------------------

def _measure(x: _Session, meas: Dict[str, Any]) -> None:
    """Window extremes and closes, each needing only its own window (never a threshold)."""
    def extremes(a, b):
        bars, missing = x.window(a, b)
        return (None, None) if missing or not bars else (max(b.high for b in bars), min(b.low for b in bars))

    def close(m):
        bar = x.by_min.get(m)
        return None if bar is None else bar.close

    h15, l15 = extremes(0, 15)
    h30, l30 = extremes(0, 30)
    ib_h, ib_l = extremes(0, _IB_END)
    meas.update(H15=_s(h15), L15=_s(l15), C15=_s(close(14)), C30=_s(close(29)),
                first_30m_range=None if h30 is None else str(h30 - l30),
                IB_high=_s(ib_h), IB_low=_s(ib_l), IB_close=_s(close(_IB_END - 1)))
    if x.session.schedule == "full":
        rth_h, rth_l = extremes(0, x.n)
        meas.update(RTH_high=_s(rth_h), RTH_low=_s(rth_l), RTH_close=_s(close(x.n - 1)))
    else:
        meas.update(RTH_high=None, RTH_low=None, RTH_close=None)


def _confirmation_time(x: _Session, first_move: Dict[str, Any]) -> Optional[str]:
    """Close time of the first 1m bar in [09:30, 09:45) closing strictly beyond O+T (up) / O-T (down)."""
    if first_move["label"] not in ("up_first", "down_first"):
        return None
    up = first_move["label"] == "up_first"
    for m in range(0, 15):
        bar = x.by_min.get(m)
        if bar is None:
            return None
        if (bar.close > x.O + x.T) if up else (bar.close < x.O - x.T):
            return x.at(m + 1)
    return None


def _drive_strength(x: _Session, opening_type: Dict[str, Any], meas: Dict[str, Any]) -> Dict[str, Any]:
    if opening_type["label"] is None:
        return _label(reason="upstream_unavailable", detail=f"opening type unavailable ({opening_type['reason']})")
    if opening_type["label"] not in ("opening_drive_up", "opening_drive_down"):
        return _label(reason="not_applicable", detail="no qualifying opening drive")
    h, low, c = Decimal(meas["H15"]), Decimal(meas["L15"]), Decimal(meas["C15"])
    return _label("strong" if abs(c - x.O) >= _STRONG * (h - low) else "moderate")


def _standard(x: _Session) -> Optional[Dict[str, Any]]:
    """The unavailable label of a full-session descriptor on a shortened session, else None."""
    if x.session.schedule != "full":
        return _label(reason="shortened_session", detail=f"{x.session.schedule} session")
    return None


def _extension(x: _Session, high: Optional[str], low: Optional[str], a: int, what: str) -> Dict[str, Any]:
    """Strict breaches of a range's high / low from minute ``a`` to the close."""
    if high is None or low is None:
        return _label(reason="missing_bars", detail=f"{what} range incomplete")
    hi, lo = Decimal(high), Decimal(low)
    bars, missing = x.window(a, x.n)
    up, down = any(b.high > hi for b in bars), any(b.low < lo for b in bars)
    if up and down:
        return _label("both_sides")
    if missing:
        return _label(reason="missing_bars", detail="cannot establish the final category: " + x.missing_detail(missing))
    return _label("up" if up else "down" if down else "none")


def _gap_outcome(x: _Session) -> Dict[str, Any]:
    p = dec(x.prev_close_ref)
    if p is None:
        return _label(reason="missing_reference", detail="previous RTH close unavailable")
    if x.B is None or x.T is None:
        return _label(reason="missing_threshold", detail="B / T unavailable")
    if x.O is None:
        return _label(reason="missing_bars", detail="no 09:30 bar: O unknown")
    gap = x.O - p
    if abs(gap) <= x.B:
        return _label("no_material_gap")
    up = gap > 0
    bars, missing = x.window(0, x.n)
    if any((b.low <= p) if up else (b.high >= p) for b in bars):
        return _label("full_gap_fill")
    if missing:
        return _label(reason="missing_bars", detail="P not reached in the stored bars, session incomplete: "
                                                    + x.missing_detail(missing))
    if any((b.low < x.O) if up else (b.high > x.O) for b in bars):
        return _label("partial_gap_fill")
    if any((b.high >= x.O + x.T) if up else (b.low <= x.O - x.T) for b in bars):
        return _label("gap_and_go")
    return _label(reason="uncovered", detail="never toward P and never T beyond O")


def _timing(x: _Session, high: bool) -> Dict[str, Any]:
    bars, missing = x.window(0, x.n)
    if missing:
        return _label(reason="missing_bars", detail="RTH incomplete: " + x.missing_detail(missing))
    values = [b.high if high else b.low for b in bars]
    first = values.index(max(values) if high else min(values))
    return _label(next(name for name, end in defs.TIMING_BINS if first < end))


def _morning_pullback(x: _Session, meas: Dict[str, Any]) -> Dict[str, Any]:
    """MP-v1 (P2 section 7)."""
    if x.T is None:
        return _label(reason="missing_threshold", detail="T unavailable")
    if x.O is None:
        return _label(reason="missing_bars", detail="no 09:30 bar: O unknown")
    bars, missing = x.window(0, _NOON)
    if missing:
        return _label(reason="missing_bars", detail="morning incomplete: " + x.missing_detail(missing))
    diff = bars[-1].close - x.O
    if -x.T <= diff <= x.T:
        span = max(b.high for b in bars) - min(b.low for b in bars)
        meas["mp_v1"] = {"direction": "two_sided", "range": str(span)}
        if span <= 2 * x.T:
            return _label("none")
        return _label(reason="uncovered", detail="two-sided morning wider than 2T")
    bearish = diff < 0
    lows, highs = [b.low for b in bars], [b.high for b in bars]
    if bearish:
        i = lows.index(min(lows))
        start, end = max(highs[:i + 1]), lows[i]
    else:
        i = highs.index(max(highs))
        start, end = min(lows[:i + 1]), highs[i]
    if i >= _NOON - 2:
        return _label(reason="late_leg_extreme", detail=f"leg extreme at {x.at(i)}")
    after = bars[i + 1:]
    pull = max(b.high for b in after) if bearish else min(b.low for b in after)
    leg, back = abs(start - end), abs(pull - end)
    meas["mp_v1"] = {"direction": "bearish" if bearish else "bullish", "leg_start": str(start),
                     "leg_extreme": str(end), "leg_extreme_at": x.at(i), "pullback": str(pull),
                     "retracement": str((back / leg).quantize(_SHOW))}
    if back < _MP_NONE * leg:
        return _label("none")
    if back < _MP_MINOR * leg:
        return _label("minor")
    if back <= _MP_MODERATE * leg:
        return _label("moderate")
    return _label("deep")


def _afternoon(x: _Session) -> Dict[str, Any]:
    if x.T is None or x.B is None:
        return _label(reason="missing_threshold", detail="T / B unavailable")
    noon, two, last = x.by_min.get(_NOON - 1), x.by_min.get(_TWO_PM), x.by_min.get(x.n - 1)
    if x.O is None or noon is None or two is None or last is None:
        return _label(reason="missing_bars", detail="needs the 09:30, 11:59, 14:00 and 15:59 bars")

    def direction(diff, band):
        return "bullish" if diff > band else "bearish" if diff < -band else "neutral_band"

    morning, afternoon = direction(noon.close - x.O, x.T), direction(last.close - two.open, x.B)
    return _label(morning if morning == afternoon and morning != "neutral_band" else "none")


def _matched(early: Dict[str, Any], close: Dict[str, Any], name: str) -> Dict[str, Any]:
    if early["label"] is None or close["label"] is None:
        return _label(reason="upstream_unavailable", detail=f"{name} or the RTH close direction unavailable")
    if "neutral_band" in (early["label"], close["label"]):
        return _label("mixed")
    return _label("yes" if early["label"] == close["label"] else "no")


_F = {k: Decimal(v) for k, v in (("tenth", "0.1"), ("fifth", "0.2"), ("third", "0.3"), ("fade", "0.4"),
                                    ("half", "0.5"), ("open_room", "0.6"), ("far", "0.8"))}


def _first_15m_pattern(x: _Session, opening_type: Dict[str, Any]) -> Dict[str, Any]:
    """FP-v1 (contracts/nq_prompt_v2.TARGETS['first_15m_pattern']): P2's mappings first, then exactly one shape."""
    if x.T is None:
        return _label(reason="missing_threshold", detail="T unavailable")
    if x.O is None:
        return _label(reason="missing_bars", detail="no 09:30 bar: O unknown")
    bars, missing = x.window(0, 15)
    if missing:
        return _label(reason="missing_bars", detail="first 15 minutes incomplete: " + x.missing_detail(missing))
    kind = opening_type["label"]
    if kind is None:
        return _label(reason="upstream_unavailable",
                      detail=f"opening type unavailable ({opening_type['reason']}): drive or sweep unknown")
    if kind in ("opening_drive_up", "opening_drive_down"):          # P2: a qualifying drive
        return _label("drive_continuation")
    if kind in ("sweep_low_rebound", "sweep_high_reverse"):         # P2: a clear sweep-and-return
        return _label("v_shape_reversal")
    O, T = x.O, x.T
    highs, lows, closes = [b.high for b in bars], [b.low for b in bars], [b.close for b in bars]
    H, L, C = max(highs), min(lows), closes[-1]
    R = H - L
    if R == 0:
        return _label(reason="uncovered", detail="zero range")
    i_high, i_low, f = highs.index(H), lows.index(L), _F
    o_pos, c_pos = O - L, C - L                      # positions in the range, in points (compare with f * R)

    def double(top: bool) -> bool:
        near = [i for i in range(15) if (highs[i] >= H - f["tenth"] * R if top else lows[i] <= L + f["tenth"] * R)]
        for i in near:
            for j in near:
                if j - i < 4:
                    continue
                if top:
                    trough = min(lows[i + 1:j])
                    if H - trough >= f["third"] * R and C < trough:
                        return True
                else:
                    peak = max(highs[i + 1:j])
                    if peak - L >= f["third"] * R and C > peak:
                        return True
        return False

    def crossings(values, level) -> int:
        signs = [v > level for v in values if v != level]
        return sum(a != b for a, b in zip(signs, signs[1:]))

    def retested(top: bool, after: int) -> bool:
        return any((highs[k] >= H - f["tenth"] * R) if top else (lows[k] <= L + f["tenth"] * R)
                   for k in range(after + 4, 15))

    steps = [b - a for a, b in zip(closes, closes[1:]) if b != a]
    reversals = sum((a > 0) != (b > 0) for a, b in zip(steps, steps[1:]))
    flat = abs(C - O) <= f["fifth"] * R
    rotation = flat and R >= 2 * T and crossings(closes, (H + L) / 2) >= 3
    fits = {
        # the turn mid-window: extreme opposite the close in bars 3-11, a return across the range beyond O + T
        "v_shape_reversal": (3 <= i_low <= 11 and o_pos >= f["fade"] * R and c_pos >= f["far"] * R and C > O + T)
        or (3 <= i_high <= 11 and o_pos <= f["open_room"] * R and c_pos <= f["fifth"] * R and C < O - T),
        # the opening move (extreme in bars 0-2) faded to the far third, the extreme not retested
        "fade_reversal": (i_high <= 2 and o_pos <= f["open_room"] * R and c_pos <= R / 3 and i_low > i_high
                          and not retested(True, i_high))
        or (i_low <= 2 and o_pos >= f["fade"] * R and c_pos >= 2 * R / 3 and i_high > i_low
            and not retested(False, i_low)),
        "double_top_bottom": double(True) or double(False),
        # from the bottom (top) fifth: half the range within bars 0-2, the extreme in bars 12-14, the close in the
        # far fifth (an opening drive is drive continuation, above)
        "spike_and_channel": (o_pos <= f["fifth"] * R and max(highs[:3]) - L >= f["half"] * R and i_high >= 12
                              and c_pos >= f["far"] * R)
        or (o_pos >= f["far"] * R and H - min(lows[:3]) >= f["half"] * R and i_low >= 12 and c_pos <= f["fifth"] * R),
        "balanced_rotation": rotation,
        "choppy": flat and reversals >= 8 and not rotation,
    }
    matched = [name for name, ok in fits.items() if ok]
    if len(matched) == 1:
        return _label(matched[0])
    if not matched:
        return _label(reason="uncovered", detail="no FP-v1 pattern fits")
    return _label(reason="ambiguous_pattern", detail="several FP-v1 patterns fit: " + ", ".join(matched))


def _trend_persistence(x: _Session, meas: Dict[str, Any]) -> Dict[str, Any]:
    if x.O is None or meas.get("RTH_high") is None:
        return _label(reason="missing_bars", detail="RTH incomplete")
    high, low, close = Decimal(meas["RTH_high"]), Decimal(meas["RTH_low"]), Decimal(meas["RTH_close"])
    r, move = high - low, abs(close - x.O)
    if r == 0:
        return _label("low")
    return _label("high" if move >= _E_HIGH * r else "moderate" if move >= _E_MODERATE * r else "low")


def compute_outcome(snapshot: Dict[str, Any], bars: Sequence[RBar]) -> Dict[str, Any]:
    """
    ``{'labels': {target: {'label', 'reason', 'detail'}}, 'measurements': {...},
    'digest'}`` of one snapshot ({'snapshot_id', 'payload'}) from its realised 1m bars.
    """
    x = _Session(snapshot, bars)
    meas: Dict[str, Any] = {"O": _s(x.O), "pre_open_close": _s(x.pre_close), "T": _s(x.T), "B": _s(x.B),
                            "A": _s(x.A), "rth_minutes": x.n,
                            "rth_bars": sum(1 for m in x.by_min if m >= 0)}
    _measure(x, meas)
    labels = {"first_move_5m": _first_move(x, meas),
              "direction_15m": _direction(x, 14, x.T, "T", meas, "C15"),
              "opening_type_15m": _opening_type(x, meas),
              "opening_bias_30m": _direction(x, 29, x.T, "T", meas, "C30")}
    if x.session.schedule != "full":
        labels["close_direction_rth"] = _label(reason="shortened_session", detail=f"{x.session.schedule} session")
    else:
        labels["close_direction_rth"] = _direction(x, x.n - 1, x.B, "B", meas, "RTH_close")
    labels["session_type_rth"] = _session_type(x, labels["close_direction_rth"], meas)

    meas["vwap"] = x.level_shown["vwap"]
    labels["first_level_tested"] = first = _first_level(x, meas)
    if first["label"] is None:
        labels["first_level_outcome"] = _label(reason="upstream_unavailable",
                                               detail=f"first level tested unavailable ({first['reason']})")
    else:
        labels["first_level_outcome"] = _level_outcome(x, x.levels[first["label"]], first["label"], 0, 15, False)
    for target, ref in defs.LEVEL_OUTCOME_REFERENCES.items():
        if x.session.schedule != "full":
            labels[target] = _label(reason="shortened_session", detail=f"{x.session.schedule} session")
        else:
            labels[target] = _level_outcome(x, x.levels[ref], ref, 0, x.n, True)

    meas["opening_confirmation_time"] = _confirmation_time(x, labels["first_move_5m"])
    labels["ib_direction"] = _direction(x, _IB_END - 1, x.T, "T", meas, "IB_close")
    labels["opening_drive_strength"] = _drive_strength(x, labels["opening_type_15m"], meas)
    labels["first_15m_pattern"] = _first_15m_pattern(x, labels["opening_type_15m"])
    labels["morning_pullback"] = _morning_pullback(x, meas)
    short = _standard(x)
    full_session = {
        "opening_range_extension": lambda: _extension(x, meas["H15"], meas["L15"], 15, "first 15-minute"),
        "ib_extension": lambda: _extension(x, meas["IB_high"], meas["IB_low"], _IB_END, "initial balance"),
        "gap_outcome": lambda: _gap_outcome(x),
        "session_high_timing": lambda: _timing(x, True),
        "session_low_timing": lambda: _timing(x, False),
        "afternoon_continuation": lambda: _afternoon(x),
        "opening_direction_matched": lambda: _matched(labels["opening_bias_30m"], labels["close_direction_rth"],
                                                      "opening bias"),
        "direction_15m_matched": lambda: _matched(labels["direction_15m"], labels["close_direction_rth"],
                                                  "first 15-minute direction"),
        "trend_persistence": lambda: _trend_persistence(x, meas),
    }
    for target, compute in full_session.items():
        labels[target] = short or compute()
    assert set(labels) == set(defs.TARGETS)

    used = sorted(x.by_min.values(), key=lambda b: b.start)
    digest = hashlib.sha256(defs.canonical_json({
        "snapshot_id": snapshot.get("snapshot_id"),
        "bars": [[iso(b.start), b.open, b.high, b.low, b.close] for b in used],
    }).encode()).hexdigest()
    return {"labels": labels, "measurements": meas, "digest": digest}
