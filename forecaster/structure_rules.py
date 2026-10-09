# forecaster/structure_rules.py
"""
Rule-based pre-open structure annotation (protocol nq_structure_rules_v4,
contracts/nq_preopen.py). It reads one frozen snapshot only and returns
contracts.nq_preopen.ANNOTATION_SCHEMA.

``annotate(snapshot)`` is pure and deterministic. Every field carries its value
(or None with a reason), a status, the evidence ids it used - ``bar:<tf>:<start>``,
``ref:<name>``, ``event:<source>:<key>``, all inside the snapshot - and a short
basis with the numbers. The numeric layer (moving averages on 2m bars, 2/2 swing
points on 5m bars, window statistics) is kept in ``measurements`` so a later
annotator can be given the same evidence.

The thresholds are a trial convention (RULES), not P1's text. Every field needs
its whole window (v4): an unbroken run of complete buckets - every minute present
once - or it is unavailable, never classified from what is left.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from contracts import nq_preopen as pre
from contracts.nq_prompt_v2 import canonical_json
from features import calendar as cal
from features.calculations import calculate_moving_averages
from features.nq_evidence import EARNINGS_SOURCE

MINUTES = {"2m": 2, "5m": 5, "15m": 15}
_UTC = timezone.utc

# The thresholds of RULES (contracts/nq_preopen.py), in the order the rules use them.
REVERSAL_LEG, REVERSAL_RECOVERY, REVERSAL_MIDDLE = 0.50, 0.60, (0.15, 0.85)
TREND_EFFICIENCY, TREND_CLOSE_LOCATION = 0.50, 0.70
RANGE_EFFICIENCY, RANGE_ALTERNATIONS = 0.35, 2
CHOP_CROSSINGS, CHOP_CONTAINED, CHOP_CONTAINED_SHARE, CHOP_WICK_SHARE = 4, 0.70, 0.45, 0.55


@dataclass(frozen=True)
class Bar:
    start: datetime
    minutes: int
    o: float
    h: float
    l: float  # noqa: E741
    c: float
    complete: bool = True       # every minute of the bucket present exactly once

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.minutes)

    @property
    def id(self) -> str:
        return f"bar:{self.minutes}m:{self.start.strftime('%Y-%m-%dT%H:%MZ')}"


@dataclass(frozen=True)
class Swing:
    kind: str           # high | low
    index: int          # position in the 5m bars
    price: float
    bar: Bar
    confirmed_at: datetime


def _ts(value: str) -> datetime:
    return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_UTC)


def _bars(payload: Dict[str, Any], tf: str) -> List[Bar]:
    """The ``tf`` buckets of the snapshot; ``complete`` from the row's flag (nq_conv_v5) or, on older rows without
    it, from the count of 1m bars (which cannot see a duplicate minute)."""
    rows = (payload.get("bars") or {}).get(tf) or []
    return sorted((Bar(_ts(r[0]), MINUTES[tf], float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                       bool(r[7]) if len(r) > 7 else len(r) > 6 and r[6] == MINUTES[tf]) for r in rows),
                  key=lambda b: b.start)


def last_start(cutoff: datetime, tf: str) -> datetime:
    """The start of the last ``tf`` bucket that ends by the cutoff."""
    n = MINUTES[tf]
    epoch = int(cutoff.timestamp()) // 60 - n
    return datetime.fromtimestamp((epoch - epoch % n) * 60, _UTC)


def unbroken(bars: Sequence[Bar], first: datetime, last: datetime, tf: str) -> Optional[str]:
    """Why the ``tf`` buckets of [first, last] are not all present and complete (None: they are)."""
    step = timedelta(minutes=MINUTES[tf])
    run = {b.start: b for b in bars if first <= b.start <= last}
    expected = int((last - first) / step) + 1
    if len(run) < expected:
        gone = next(first + i * step for i in range(expected) if first + i * step not in run)
        return (f"{expected - len(run)} of {expected} {tf} buckets from {first.strftime('%H:%MZ')} missing "
                f"(first {gone.strftime('%H:%MZ')})")
    bad = [b.start.strftime("%H:%MZ") for b in run.values() if not b.complete]
    return f"incomplete {tf} bucket(s) {', '.join(bad[:3])}" if bad else None


def _num(value) -> Optional[float]:
    return None if value is None else float(value)


def _field(value=None, status="classified", reason=None, evidence=(), basis="") -> Dict[str, Any]:
    return {"value": value, "status": status if value is not None or status != "classified" else "unavailable",
            "reason": reason, "evidence_ids": list(dict.fromkeys(evidence)), "basis": basis}


def _missing(reason: str, evidence=()) -> Dict[str, Any]:
    return _field(None, "unavailable", reason, evidence, "")


def _r(x: Optional[float], nd: int = 2) -> Optional[float]:
    return None if x is None else round(x, nd)


def _sign_changes(values: Sequence[float]) -> List[int]:
    """Positions i where the sign of values[i] differs from the last non-zero value before it."""
    out, last = [], 0
    for i, v in enumerate(values):
        s = (v > 0) - (v < 0)
        if s == 0:
            continue
        if last and s != last:
            out.append(i)
        last = s
    return out


# --------------------------------------------------------------------------
# Numeric layer
# --------------------------------------------------------------------------

def swings(bars: Sequence[Bar], left: int = 2, right: int = 2) -> List[Swing]:
    """2/2 swing points: above the ``left`` bars before and at least the ``right`` after (low mirrored)."""
    out = []
    for i in range(left, len(bars) - right):
        b, before, after = bars[i], bars[i - left:i], bars[i + 1:i + 1 + right]
        confirmed = bars[i + right].end
        if b.h > max(x.h for x in before) and b.h >= max(x.h for x in after):
            out.append(Swing("high", i, b.h, b, confirmed))
        if b.l < min(x.l for x in before) and b.l <= min(x.l for x in after):
            out.append(Swing("low", i, b.l, b, confirmed))
    return out


def moving_averages(bars2: Sequence[Bar]) -> pd.DataFrame:
    """The TradingView lines (nq_conv_v2) on the 2m bars: columns tema, ema_trend, ema_trigger."""
    df = pd.DataFrame({"timestamp_utc": [b.start for b in bars2], "close": [b.c for b in bars2]})
    return calculate_moving_averages(df).reset_index(drop=True)


def _stats(bars: Sequence[Bar], close: float) -> Dict[str, float]:
    hi, lo = max(b.h for b in bars), min(b.l for b in bars)
    rng = hi - lo
    net = close - bars[0].o
    return {"open": bars[0].o, "close": close, "high": hi, "low": lo, "range": rng, "net": net,
            "efficiency": abs(net) / rng if rng > 0 else 0.0}


# --------------------------------------------------------------------------
# Fields
# --------------------------------------------------------------------------

def _overnight_structure(on: List[Bar], sw: List[Swing], close: float, broken: Optional[str]):
    if broken:
        return _missing(f"the overnight window is incomplete: {broken}"), {}
    if len(on) < 12:
        return _missing(f"{len(on)} 5m bars: under 12"), {}
    st = _stats(on, close)
    H, L, R = st["high"], st["low"], st["range"]
    ids = [on[0].id]
    if R == 0:
        return _field("Range", evidence=ids, basis="zero overnight range"), st
    i_hi = next(i for i, b in enumerate(on) if b.h == H)
    i_lo = next(i for i, b in enumerate(on) if b.l == L)
    st["close_location"] = (close - L) / R
    ids += [on[i_hi].id, on[i_lo].id]

    span = (on[-1].end - on[0].start).total_seconds()

    def reversal(up: bool):
        i_ext = i_lo if up else i_hi
        ext = L if up else H
        position = (on[i_ext].start - on[0].start).total_seconds() / span
        if not REVERSAL_MIDDLE[0] <= position <= REVERSAL_MIDDLE[1]:
            return None
        prior = on[:i_ext + 1]
        start_ext = max(b.h for b in prior) if up else min(b.l for b in prior)
        leg = abs(start_ext - ext)
        recovered = (close - ext) if up else (ext - close)
        pre_swing = [s for s in sw if s.kind == ("high" if up else "low") and s.index < i_ext]
        if leg < REVERSAL_LEG * R or recovered < REVERSAL_RECOVERY * leg or not pre_swing:
            return None
        level = pre_swing[-1]
        brk = next((j for j in range(i_ext + 1, len(on)) if (on[j].c > level.price if up else on[j].c < level.price)),
                   None)
        if brk is None:
            return None
        confirm = next((s for s in sw if s.kind == ("low" if up else "high") and s.index > brk
                        and (s.price > ext if up else s.price < ext)), None)
        if confirm is None:
            return None
        return (f"{'decline' if up else 'advance'} {leg:.2f} ({leg / R:.0%} of R) to {ext:.2f}, recovered "
                f"{recovered:.2f}, close {on[brk].c:.2f} broke the swing {'high' if up else 'low'} "
                f"{level.price:.2f}, then a {'higher low' if up else 'lower high'} {confirm.price:.2f}",
                [level.bar.id, on[brk].id, confirm.bar.id])

    v, inv = reversal(True), reversal(False)
    if v and inv:
        return _field("Mixed", evidence=ids + v[1] + inv[1], basis=f"both a V-reversal ({v[0]}) and an Inverted-V "
                                                                    f"({inv[0]}) qualify"), st
    if v or inv:
        name, (text, ev) = ("V-reversal", v) if v else ("Inverted-V", inv)
        return _field(name, evidence=ids + ev, basis=text), st
    last_low = next((s for s in reversed(sw) if s.kind == "low"), None)
    last_high = next((s for s in reversed(sw) if s.kind == "high"), None)
    E, CL = st["efficiency"], st["close_location"]
    basis = f"net {st['net']:.2f} over R {R:.2f}: E {E:.2f}, close location {CL:.2f}"
    if st["net"] > 0 and E >= TREND_EFFICIENCY and CL >= TREND_CLOSE_LOCATION and (last_low is None or close > last_low.price):
        return _field("Uptrend", evidence=ids + ([last_low.bar.id] if last_low else []),
                      basis=basis + (f", above the last swing low {last_low.price:.2f}" if last_low else "")), st
    if st["net"] < 0 and E >= TREND_EFFICIENCY and CL <= 1 - TREND_CLOSE_LOCATION and (last_high is None or close < last_high.price):
        return _field("Downtrend", evidence=ids + ([last_high.bar.id] if last_high else []),
                      basis=basis + (f", below the last swing high {last_high.price:.2f}" if last_high else "")), st
    zones = []
    for b in on:
        top, bottom = b.h >= L + 0.75 * R, b.l <= L + 0.25 * R
        if top != bottom and (not zones or zones[-1] != ("top" if top else "bottom")):
            zones.append("top" if top else "bottom")
    st["alternations"] = len(zones) - 1
    if E < RANGE_EFFICIENCY and len(zones) - 1 >= RANGE_ALTERNATIONS:
        return _field("Range", evidence=ids, basis=basis + f", {len(zones) - 1} alternations between the top and "
                                                           f"bottom quarters"), st
    return _field("Mixed", evidence=ids, basis=basis + f", {max(len(zones) - 1, 0)} alternations: no rule fits"), st


def _swing_window(bars5: List[Bar], cutoff: datetime) -> Optional[str]:
    """The 5m buckets of the last three hours, with the two before them that a swing in the window is judged
    against: all present and complete (None), or why not."""
    first = cutoff - timedelta(minutes=180)
    first = first + timedelta(minutes=-first.minute % 5) - timedelta(minutes=5 * 2)
    return unbroken(bars5, first, last_start(cutoff, "5m"), "5m")


def _premarket_pattern(bars5: List[Bar], sw: List[Swing], close: float, cutoff: datetime, T: Optional[int],
                       on_value: Optional[str]):
    if T is None:
        return _missing("T unavailable")
    broken = _swing_window(bars5, cutoff)
    if broken:
        return _missing(f"the final three hours are incomplete: {broken}")
    hour_start, ctx_start = cutoff - timedelta(minutes=60), cutoff - timedelta(minutes=180)
    hour = [b for b in bars5 if b.start >= hour_start]
    ctx = [b for b in bars5 if ctx_start <= b.start < hour_start]
    h, c = _stats(hour, close), _stats(ctx, ctx[-1].c)
    dir_hour = (1 if h["net"] > 0 else -1) if abs(h["net"]) >= 2 * T and h["efficiency"] >= 0.50 else 0
    dir_ctx = (1 if c["net"] > 0 else -1) if abs(c["net"]) >= 2 * T and c["efficiency"] >= 0.40 else 0
    ctx_from = "the context"
    if dir_ctx == 0:
        dir_ctx = {"Uptrend": 1, "V-reversal": 1, "Downtrend": -1, "Inverted-V": -1}.get(on_value, 0)
        ctx_from = f"the overnight structure ({on_value})" if dir_ctx else "no direction"
    ids = [hour[0].id, hour[-1].id, ctx[0].id, ctx[-1].id]
    basis = (f"hour net {h['net']:.2f} (E {h['efficiency']:.2f}, range {h['range']:.2f}), context net {c['net']:.2f} "
             f"(E {c['efficiency']:.2f}); 2T = {2 * T}; direction before the hour from {ctx_from}")
    if dir_hour:
        side = "Bullish" if dir_hour > 0 else "Bearish"
        if dir_ctx != -dir_hour:
            return _field(f"{side} continuation", evidence=ids, basis=basis)
        ctx_sw = [s for s in sw if ctx_start <= s.bar.start < hour_start and s.kind == ("high" if dir_hour > 0 else "low")]
        level = ctx_sw[-1].price if ctx_sw else (c["high"] if dir_hour > 0 else c["low"])
        broke = close > level if dir_hour > 0 else close < level
        ids += [ctx_sw[-1].bar.id] if ctx_sw else []
        if broke:
            return _field(f"{side} reversal", evidence=ids, basis=basis + f"; cutoff price beyond the context's "
                                                                          f"structure level {level:.2f}")
        return _field("Mixed", evidence=ids, basis=basis + f"; a {side.lower()} hour within an intact opposite "
                                                           f"structure (level {level:.2f} not broken)")
    if h["efficiency"] < 0.35 or (abs(h["net"]) < 2 * T and h["range"] <= 4 * T):
        return _field("Range", evidence=ids, basis=basis + f"; no directional progress (4T = {4 * T})")
    return _field("Mixed", evidence=ids, basis=basis + "; no rule fits")


def _short_term_structure(bars5: List[Bar], sw: List[Swing], cutoff: datetime):
    broken = _swing_window(bars5, cutoff)
    if broken:
        return _missing(f"the final three hours are incomplete: {broken}")
    win = [s for s in sw if s.bar.start >= cutoff - timedelta(minutes=180)]
    highs, lows = [s for s in win if s.kind == "high"], [s for s in win if s.kind == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return _missing(f"{len(highs)} swing high(s) and {len(lows)} swing low(s) confirmed in the last 3 hours")
    ids = [s.bar.id for s in highs[-2:] + lows[-2:]]
    basis = (f"swing highs {highs[-2].price:.2f} -> {highs[-1].price:.2f}, swing lows {lows[-2].price:.2f} -> "
             f"{lows[-1].price:.2f}")
    if highs[-1].price > highs[-2].price and lows[-1].price > lows[-2].price:
        return _field("Higher highs / Higher Lows", evidence=ids, basis=basis)
    if highs[-1].price < highs[-2].price and lows[-1].price < lows[-2].price:
        return _field("Lower highs / Lower lows", evidence=ids, basis=basis)
    return _field("Mixed", evidence=ids, basis=basis)


def _trend(bars: List[Bar], tf: str, cutoff: datetime, n: int = 12):
    last = last_start(cutoff, tf)
    window_start = last - timedelta(minutes=(n - 1) * MINUTES[tf])
    broken = unbroken(bars, window_start, last, tf)
    if broken:
        return _missing(f"the last {n} {tf} buckets are incomplete: {broken}")
    win = [b for b in bars if window_start <= b.start <= last]
    st = _stats(win, win[-1].c)
    E = st["efficiency"]
    side = "bullish" if st["net"] > 0 else "bearish"
    if st["range"] == 0 or E < 0.30:
        value = "Neutral"
    elif E >= 0.60:
        value = side.capitalize()
    else:
        value = f"Neutral-{side}"
    return _field(value, evidence=[win[0].id, win[-1].id],
                  basis=f"last {len(win)} {tf} bars: net {st['net']:.2f} over range {st['range']:.2f}, E {E:.2f}")


def _ma_fields(bars2: List[Bar], close: float, cutoff: datetime, T: Optional[int],
               on_start: datetime) -> Tuple[Dict[str, Any], dict]:
    out: Dict[str, Any] = {}
    win_start = cutoff - timedelta(minutes=30)
    names = ("Price vs Long MA", "Long MA Slope", "Fast MA Alignment", "Chop Score")
    broken = unbroken(bars2, on_start, last_start(cutoff, "2m"), "2m")
    if broken:
        return {k: _missing(f"the moving averages' 2m history is incomplete: {broken}") for k in names}, {}
    if len(bars2) < 116:
        return {k: _missing(f"{len(bars2)} 2m bars: too few for EMA(100)") for k in names}, {}
    ma = moving_averages(bars2)
    idx = [i for i, b in enumerate(bars2) if b.start >= win_start]
    last = idx[-1]
    win = [bars2[i] for i in idx]
    ema, tema, trig = ([float(x) for x in ma[col]] for col in ("ema_trend", "tema", "ema_trigger"))
    ids = [win[0].id, win[-1].id]
    m = {"ema100": _r(ema[last]), "tema": _r(tema[last]), "ema14_sma3": _r(trig[last]),
         "ema100_30m_ago": _r(ema[idx[0] - 1]), "bars_in_window": len(idx)}

    d = close - ema[last]
    cross = _sign_changes([bars2[i].c - ema[i] for i in idx])
    if abs(d) <= pre.PRICE_LOCATION["at_points"]:
        v = "At"
    elif len(cross) >= 2:
        v = "Crossing"
    elif len(cross) == 1:
        v = "Above/crossing" if d > 0 else "Crossing/below"
    else:
        v = "Above" if d > 0 else "Below"
    out["Price vs Long MA"] = _field(v, evidence=ids + ["ref:cutoff_price"],
                                     basis=f"cutoff {close:.2f} vs EMA(100) {ema[last]:.2f} ({d:+.2f}); "
                                           f"{len(cross)} crossing(s) in 30 min")
    if T is None:
        out["Long MA Slope"] = _missing("T unavailable")
        flat = None
    else:
        change = ema[last] - ema[idx[0] - 1]
        flat = abs(change) < T
        out["Long MA Slope"] = _field("Flat" if flat else ("Rising" if change > 0 else "Falling"), evidence=ids,
                                      basis=f"EMA(100) {change:+.2f} over 30 min against T = {T}")
    g = [tema[i] - trig[i] for i in idx]
    gc = _sign_changes(g)
    if len(gc) >= 2 or (gc and gc[-1] >= len(g) - 3) or g[-1] == 0:
        v = "Mixed"
    else:
        v = "Bullish" if g[-1] > 0 else "Bearish"
    out["Fast MA Alignment"] = _field(v, evidence=ids, basis=f"TEMA {tema[last]:.2f} vs EMA(14) {trig[last]:.2f}; "
                                                             f"{len(gc)} cross(es) in 30 min")
    if flat is None:
        out["Chop Score"] = _missing("Long MA Slope unavailable")
    else:
        # the fast TEMA hugs price, so its crossings are left out: the slower line and the long MA
        crossings = len(_sign_changes([bars2[i].c - trig[i] for i in idx])) + len(cross)
        pairs = [(a, b) for a, b in zip(win, win[1:]) if b.h > b.l]
        contained = sum(1 for a, b in pairs
                        if max(0.0, min(a.h, b.h) - max(a.l, b.l)) >= CHOP_CONTAINED * (b.h - b.l))
        ranged = [b for b in win if b.h > b.l]
        wicky = sum(1 for b in ranged if (b.h - max(b.o, b.c)) + (min(b.o, b.c) - b.l) >= 0.5 * (b.h - b.l))
        contained_share = contained / len(pairs) if pairs else 0.0
        wick_share = wicky / len(ranged) if ranged else 0.0
        points = {"flat_long_ma": bool(flat), "repeated_crossings": crossings >= CHOP_CROSSINGS,
                  "overlap_and_wicks": contained_share >= CHOP_CONTAINED_SHARE and wick_share >= CHOP_WICK_SHARE}
        out["Chop Score"] = _field(sum(points.values()), evidence=ids,
                                   basis=f"flat long MA {points['flat_long_ma']}, {crossings} crossings of EMA(14) and "
                                         f"EMA(100), {contained_share:.2f} of bars inside the previous one, long-wick "
                                         f"share {wick_share:.2f}")
        m.update(crossings=crossings, contained_share=_r(contained_share, 3), wick_share=_r(wick_share, 3))
    return out, m


def htb_bands(above: bool, below: bool, p: Fraction, m: Fraction) -> List[str]:
    """
    Every HTB-v1 band the position and momentum fall in, as the user stated them
    (exclusive momentum bands): Bullish above the 5-session high or in the upper
    third with m >= +0.5; Neutral-bullish in the upper third with +0.15 < m < +0.5;
    Neutral in the middle third or with |m| <= 0.15; the bearish ones mirrored.
    Thirds are of the range, inclusive at 2/3 and 1/3; above / below are outside it.
    """
    half, small = Fraction(1, 2), Fraction(15, 100)
    inside = not (above or below)
    upper = inside and p >= Fraction(2, 3)
    lower = inside and p <= Fraction(1, 3)
    middle = inside and not (upper or lower)
    bands = []
    if above or (upper and m >= half):
        bands.append("Bullish")
    if below or (lower and m <= -half):
        bands.append("Bearish")
    if upper and small < m < half:
        bands.append("Neutral-bullish")
    if lower and -half < m < -small:
        bands.append("Neutral-bearish")
    if middle or abs(m) <= small:
        bands.append("Neutral")
    return bands


def htb_label(above: bool, below: bool, p: Fraction, m: Fraction) -> Optional[str]:
    """The HTB-v1 label: the one band that applies; a breakout beyond the range with |m| <= 0.15 (Bullish or
    Bearish and Neutral at once) stays the breakout; no band (location and momentum disagree) is None."""
    bands = htb_bands(above, below, p, m)
    return bands[0] if bands else None


def higher_timeframe_bias(payload: Dict[str, Any]) -> Dict[str, Any]:
    """HTB-v1 (contracts/nq_preopen.py): the cutoff price within the five prior sessions' range, and the momentum
    since the open five sessions back in daily ATRs; exact arithmetic."""
    prior = (payload.get("prior_sessions") or {}).get("sessions") or []
    valid = [x for x in prior if x["status"] == "valid"]
    if len(valid) < pre.HTB_SESSIONS or len(prior) < pre.HTB_SESSIONS:
        return _missing(f"{len(valid)} of {pre.HTB_SESSIONS} prior sessions verified (every RTH minute bar on the "
                        f"snapshot contract)")
    cp = (payload.get("references") or {}).get("cutoff_price") or {}
    A = (payload.get("thresholds") or {}).get("A")
    if cp.get("status") != "valid" or A is None:
        return _missing("no valid cutoff price" if cp.get("status") != "valid" else "daily ATR unavailable")
    close, A = Decimal(str(cp["value"])), Fraction(A)
    high, low = max(Decimal(str(x["high"])) for x in valid), min(Decimal(str(x["low"])) for x in valid)
    start = valid[0]
    m = Fraction(close - Decimal(str(start["open"]))) / A
    p = Fraction(close - low) / Fraction(high - low) if high > low else Fraction(1, 2)
    value = htb_label(close > high, close < low, p, m)
    where = ("above the range" if close > high else "below the range" if close < low else
             "upper third" if p >= Fraction(2, 3) else "lower third" if p <= Fraction(1, 3) else "middle third")
    basis = (f"cutoff {close} against the 5-session range {low}-{high} ({where}, position {float(p):.2f}); "
             f"momentum {float(m):+.2f} ATR from the {start['open']} open of {start['session_date']}")
    ids = ["ref:cutoff_price"] + [f"prior:{x['session_date']}" for x in valid]
    if value is None:
        return _field(None, "unavailable", "uncovered: location and momentum disagree", ids, basis)
    return _field(value, evidence=ids, basis=basis)


def price_location(refs: Dict[str, Any], close: Optional[float]) -> Dict[str, Optional[str]]:
    out = {}
    for name in pre.PRICE_LOCATION["levels"]:
        ref = refs.get(name) or {}
        level = _num(ref.get("value")) if ref.get("status") == "valid" else None
        if close is None or level is None:
            out[name] = None
        elif abs(close - level) <= pre.PRICE_LOCATION["at_points"]:
            out[name] = "At"
        else:
            out[name] = "Above" if close > level else "Below"
    return out


def event_risk(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    ev = payload.get("events") or {}
    covered = set(ev.get("covered_sources") or [])
    missing = sorted(set(pre.EVENT_RISK["sources"]) - covered)
    if missing:
        reason = f"no calendar coverage for {', '.join(missing)}"
        return _missing(reason), _missing(reason)
    close = _ts(payload["schedule"]["scheduled_close_at"])
    rows = [e for e in ev.get("events", []) if e["source"] in pre.EVENT_RISK["sources"]
            and (e["source"] == EARNINGS_SOURCE or _ts(e["scheduled_at"]) < close)]
    high = [e for e in rows if e["tier"] == "high" and e["source"] != EARNINGS_SOURCE]
    moderate = [e for e in rows if e["tier"] == "moderate" and e["source"] != EARNINGS_SOURCE]
    earnings = [e for e in rows if e["source"] == EARNINGS_SOURCE]
    value = "High-risk" if high else "Reduced-confidence" if (moderate or earnings) else "Normal"
    ids = [f"event:{e['source']}:{e['event_key']}" for e in rows]
    day = payload["identity"]["session_date"]

    def note(e):
        t = _ts(e["scheduled_at"]).astimezone(cal.NY_TZ)
        when = t.strftime("%H:%M") if t.date().isoformat() == day else t.strftime("%a %H:%M")
        return f"{when} ET {e['name']} ({'released pre-open' if e['before_cutoff'] else 'upcoming'})"

    basis = (f"{len(high)} high-tier, {len(moderate)} moderate release(s), {len(earnings)} material earnings "
             f"release(s); every source covered")
    notes = "; ".join(note(e) for e in rows) or "No material scheduled release or material earnings release"
    return (_field(value, evidence=ids, basis=basis),
            _field(notes, evidence=ids, basis=f"{pre.EVENT_RISK_VERSION} window"))


def _overnight_gap(payload: Dict[str, Any], on: List[Bar], on_start: datetime, cutoff: datetime) -> Optional[str]:
    """Why the overnight window [18:00, cutoff) is not complete (None: every minute once, every 5m bucket whole)."""
    cov = (payload.get("bars") or {}).get("coverage") or {}
    whole = cov.get("complete") if "complete" in cov else (cov.get("ratio") is not None
                                                           and Decimal(str(cov["ratio"])) == 1)
    if not whole:
        return f"{cov.get('minutes', '?')} of {cov.get('expected_minutes', '?')} minutes (ratio {cov.get('ratio')})"
    return unbroken(on, on_start, last_start(cutoff, "5m"), "5m")


def after_cutoff(payload: Dict[str, Any], cutoff: datetime) -> List[str]:
    """Items after the cutoff that the snapshot must not hold."""
    bad = []
    for tf in ("1m",) + tuple(MINUTES):
        n = MINUTES.get(tf, 1)
        for r in (payload.get("bars") or {}).get(tf) or []:
            if _ts(r[0]) + timedelta(minutes=n) > cutoff:
                bad.append(f"bar:{tf}:{r[0]}")
    for e in (payload.get("events") or {}).get("events", []):
        if e["source"] == EARNINGS_SOURCE and _ts(e["scheduled_at"]) >= cutoff:
            bad.append(f"event:{e['source']}:{e['event_key']}")
    return bad


# --------------------------------------------------------------------------
# Annotation
# --------------------------------------------------------------------------

def annotate(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """The rule-based structure annotation of one stored snapshot ({'payload', ...})."""
    p = snapshot["payload"]
    cutoff = _ts(p["cutoff"]["input_cutoff_at"])
    refs = p.get("references") or {}
    cp = refs.get("cutoff_price") or {}
    close = _num(cp.get("value")) if cp.get("status") == "valid" else None
    T = (p.get("thresholds") or {}).get("T")
    T = None if T is None else int(T)
    out: Dict[str, Any] = {"protocol_version": pre.RULES_PROTOCOL_VERSION, "annotator": "rules"}

    bad = after_cutoff(p, cutoff)
    if bad:
        out.update(integrity_status="contaminated", fields={}, price_location={},
                   measurements={"after_cutoff": bad[:20]})
        return seal(out)
    if close is None:
        reason = f"no valid cutoff price ({cp.get('status')})"
        fields = {name: _missing(reason) for name in pre.FIELDS}
        fields["Event Risk"], fields["Event Notes"] = event_risk(p)
        out.update(integrity_status="ok", fields=fields, price_location=price_location(refs, None), measurements={})
        return seal(out)

    bars5, bars2, bars15 = _bars(p, "5m"), _bars(p, "2m"), _bars(p, "15m")
    on_start = _ts(p["schedule"]["overnight_start_at"])
    on = [b for b in bars5 if b.start >= on_start]
    sw = swings(on)
    fields: Dict[str, Any] = {}
    fields["Overnight Structure"], on_stats = _overnight_structure(on, sw, close, _overnight_gap(p, on, on_start,
                                                                                                cutoff))
    fields["Premarket Pattern"] = _premarket_pattern(on, sw, close, cutoff, T, fields["Overnight Structure"]["value"])
    fields["Short-Term Structure"] = _short_term_structure(on, sw, cutoff)
    fields["5-Minute Trend"] = _trend(on, "5m", cutoff)
    fields["15-Minute Trend"] = _trend([b for b in bars15 if b.start >= on_start], "15m", cutoff)
    fields["Higher-Timeframe Bias"] = higher_timeframe_bias(p)
    ma_fields, ma_meas = _ma_fields([b for b in bars2 if b.start >= on_start], close, cutoff, T, on_start)
    fields.update(ma_fields)
    fields["Event Risk"], fields["Event Notes"] = event_risk(p)
    fields = {name: fields[name] for name in pre.FIELDS}

    out.update(
        integrity_status="ok", fields=fields, price_location=price_location(refs, close),
        measurements={
            "cutoff_price": close, "T": T,
            "overnight": {k: _r(v, 4) for k, v in on_stats.items()},
            "swings_5m": [[s.kind, s.bar.start.strftime("%Y-%m-%dT%H:%MZ"), s.price,
                           s.confirmed_at.strftime("%Y-%m-%dT%H:%MZ")] for s in sw],
            "moving_averages_2m": ma_meas,
        })
    return seal(out)


def seal(annotation: Dict[str, Any]) -> Dict[str, Any]:
    """Validates the vocabularies and adds the output hash."""
    for name, f in annotation["fields"].items():
        allowed = pre.FIELDS[name]["values"]
        assert f["status"] in pre.FIELD_STATUSES, (name, f["status"])
        assert f["value"] is None or allowed == "text" or f["value"] in allowed, (name, f["value"])
        assert (f["value"] is None) == (f["status"] != "classified"), (name, f)
    annotation["output_hash"] = hashlib.sha256(canonical_json(annotation).encode()).hexdigest()
    return annotation
