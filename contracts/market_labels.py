# contracts/market_labels.py
"""
The NQ-v2 label conventions parameterised by market (market_labels_v1), so the pooled ML bundle can
label ES's and RTY's sessions from their own frozen references, thresholds, tick conventions,
calendar and outcomes - never by copying NQ's labels or levels onto them.

Everything that is not a market parameter is the registered label version itself
(contracts/nq_prompt_v2.LABEL_VERSION, computed by forecaster/labels_prompt_v2.compute_outcome): the
targets, windows, class order, boundaries, ordered rules, early-close rules, ties and unavailable
reasons. A market changes only:

  symbol              whose bars, active contracts and frozen snapshot are read
                      (features/nq_evidence.build_snapshot builds the same payload for any symbol)
  tick                the exchange's minimum price increment (documentation; prices are compared
                      exactly as stored)
  threshold_increment T and B are rounded UP to a multiple of this many points
  threshold_minimum   and are at least this many points

The NQ parameters reproduce the registered thresholds exactly - T = max(1, ceil(0.5 x the frozen
two-minute ATR)), B = max(1, ceil(0.05 x the frozen daily ATR)), whole points
(contracts/nq_prompt_v2.threshold_t / threshold_b) - and tests/test_ml_bundle.py checks that the
parameterised NQ path gives the stored labels. A whole point is 4 NQ ticks but 4 ES ticks on a
two-minute ATR about a quarter of NQ's, and 10 RTY ticks on one of one or two points: rounding those
to whole points would move T by up to half its size, so ES and RTY round to their own tick instead.
That is a declared convention of the pooled training labels, not a change to NQ's.

A is the frozen daily ATR (exact) in every market. The calendar is the CME equity-index calendar
(features/calendar.py): NQ, ES and RTY share RTH, the Globex day, holidays and early closes.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from fractions import Fraction
from typing import Any, Dict, Optional

from contracts import nq_prompt_v2 as defs

VERSION = "market_labels_v1"


@dataclass(frozen=True)
class MarketConvention:
    symbol: str
    tick: str                      # exact decimal string
    threshold_increment: str       # exact decimal string, points
    threshold_minimum: str         # exact decimal string, points
    note: str


MARKETS: Dict[str, MarketConvention] = {
    "NQ": MarketConvention("NQ", "0.25", "1", "1",
                           "the registered convention: whole points, at least one (nq_prompt_v2.threshold_t / _b)"),
    "ES": MarketConvention("ES", "0.25", "0.25", "0.25",
                           "rounded up to ES's tick, at least one tick: a whole point would be a quarter of a "
                           "typical two-minute ATR"),
    "RTY": MarketConvention("RTY", "0.1", "0.1", "0.1",
                            "rounded up to RTY's tick, at least one tick: a whole point would be most of a typical "
                            "two-minute ATR"),
}


def _round_up(x: Fraction, inc: Fraction, minimum: Fraction) -> Fraction:
    return max(minimum, math.ceil(x / inc) * inc)


def _shown(x: Fraction):
    """An exact threshold as stored in a snapshot payload: an int for a whole number of points (NQ's registered
    form), else its exact decimal string."""
    if x.denominator == 1:
        return int(x)
    from decimal import Decimal
    return str(Decimal(x.numerator) / Decimal(x.denominator))


def threshold_t(market: str, two_minute_atr) -> Optional[Any]:
    """T for ``market`` from its frozen two-minute ATR (exact rational), None without it."""
    if two_minute_atr is None:
        return None
    m = MARKETS[market]
    return _shown(_round_up(Fraction(two_minute_atr) / 2, Fraction(m.threshold_increment),
                            Fraction(m.threshold_minimum)))


def threshold_b(market: str, daily_atr) -> Optional[Any]:
    """B for ``market`` from its frozen daily ATR (exact rational), None without it."""
    if daily_atr is None:
        return None
    m = MARKETS[market]
    return _shown(_round_up(Fraction(daily_atr) / 20, Fraction(m.threshold_increment),
                            Fraction(m.threshold_minimum)))


def thresholds(market: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """The market's T, B and A from a snapshot payload's frozen ATRs."""
    atr = payload.get("atr") or {}
    two = (atr.get("two_minute") or {}).get("exact")
    daily = (atr.get("daily") or {}).get("exact")
    return {"T": threshold_t(market, two), "B": threshold_b(market, daily), "A": daily}


def record() -> Dict[str, Any]:
    return defs._record(VERSION, "convention", {
        "label_version": defs.LABEL_VERSION, "convention_version": defs.CONVENTION_VERSION,
        "markets": {k: asdict(v) for k, v in MARKETS.items()},
        "thresholds": {"T": "max(threshold_minimum, ceil(0.5 x the market's frozen two-minute ATR / increment) x "
                            "increment) points", "B": "max(threshold_minimum, ceil(0.05 x the market's frozen daily "
                                                      "ATR / increment) x increment) points",
                       "A": "the market's frozen daily ATR"},
        "unchanged": "every target, window, class order, boundary, ordered rule, early-close rule, tie and unavailable "
                     "reason of the label version; the market's own snapshot (references, first-level candidates, "
                     "ATRs) built by features/nq_evidence.build_snapshot for its symbol",
        "calendar": "the CME equity-index calendar (features/calendar.py), shared by NQ, ES and RTY",
        "nq_parity": "the NQ parameters reproduce nq_prompt_v2.threshold_t / threshold_b exactly, so the parameterised "
                     "NQ path gives the stored labels",
    })
