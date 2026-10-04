# forecaster/outcome_display.py
"""
The 40-field realised-outcome record of the post-session prompt P2 (section 9),
from a stored snapshot and its outcome - a display adapter, kept apart from the
numeric engine (forecaster/labels_prompt_v2.py).

``p2_record(snapshot, outcome)`` returns ``[(property, value), ...]`` in P2's
order. Labels use the prompts' display strings (contracts/nq_prompt_v2.display);
an unavailable value shows as "Unavailable"; Opening Drive Strength without a
qualifying drive is blank, as P2 asks. Prices show at two decimals (the stored
measurements stay exact).

Two fields are not P2 classifications and follow a documented display convention
(``CONFIDENCE_CONVENTION``):
  Realised Outcome Confidence  from coverage alone (P2: chart quality, coverage
                               and extraction certainty, never forecast success)
  Outcome Data Notes           definitions applied, data mode, coverage, the
                               reason for every unavailable field, MP-v1 details

First 15-Minute Pattern is a visual descriptor P2 gives no numeric definition for;
it follows the FP-v1 implementation convention (contracts/nq_prompt_v2.py).
"""

from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from contracts import nq_prompt_v2 as defs

UNAVAILABLE = defs.UNAVAILABLE_DISPLAY
_CENT = Decimal("0.01")          # NQ ticks are 0.25: two decimals show every price exactly (VWAP rounded)

CONFIDENCE_CONVENTION = (
    "Realised Outcome Confidence (implementation convention, 1-5, from 1m coverage only): 5 every minute of the "
    "scheduled session stored; 4 the first hour complete; 3 the first 15 minutes complete; 2 the 09:30 bar "
    "present; 1 no 09:30 bar."
)

# P2 section 9, in order: (property, source). A source is a label target, ('m', measurement key),
# or a special name.
P2_FIELDS: List[Tuple[str, Any]] = [
    ("Realised Opening Bias", "opening_bias_30m"),
    ("Realised First Move", "first_move_5m"),
    ("Realised Opening Type", "opening_type_15m"),
    ("Realised First Level Tested", "first_level_tested"),
    ("Realised First Level Price", ("m", "first_level_price")),
    ("First Level Outcome", "first_level_outcome"),
    ("Opening Confirmation Time", ("m", "opening_confirmation_time")),
    ("Opening Drive Strength", "opening_drive_strength"),
    ("First 15-Minute Direction", "direction_15m"),
    ("First 15-Minute High", ("m", "H15")),
    ("First 15-Minute Low", ("m", "L15")),
    ("First 15-Minute Close", ("m", "C15")),
    ("First 15-Minute Pattern", "first_15m_pattern"),
    ("First 30-Minute Direction", "opening_bias_30m"),
    ("First 30-Minute Range", ("m", "first_30m_range")),
    ("Initial Balance High", ("m", "IB_high")),
    ("Initial Balance Low", ("m", "IB_low")),
    ("Initial Balance Direction", "ib_direction"),
    ("Opening Range Extension Direction", "opening_range_extension"),
    ("Initial Balance Extension", "ib_extension"),
    ("RTH Open", ("m", "O")),
    ("RTH High", ("m", "RTH_high")),
    ("RTH Low", ("m", "RTH_low")),
    ("RTH Close", ("m", "RTH_close")),
    ("RTH Close Direction", "close_direction_rth"),
    ("RTH Session Type", "session_type_rth"),
    ("Gap Outcome", "gap_outcome"),
    ("ON High Outcome", "on_high_outcome"),
    ("ON Low Outcome", "on_low_outcome"),
    ("Previous RTH High Outcome", "prev_rth_high_outcome"),
    ("Previous RTH Low Outcome", "prev_rth_low_outcome"),
    ("Session High Timing", "session_high_timing"),
    ("Session Low Timing", "session_low_timing"),
    ("Morning Pullback Severity", "morning_pullback"),
    ("Afternoon Continuation", "afternoon_continuation"),
    ("Opening Direction Matched Session", "opening_direction_matched"),
    ("First 15-Minute Direction Matched Session", "direction_15m_matched"),
    ("Trend Persistence", "trend_persistence"),
    ("Realised Outcome Confidence", "confidence"),
    ("Outcome Data Notes", "notes"),
]


def confidence(measurements: Dict[str, Any], labels: Dict[str, Any]) -> int:
    """CONFIDENCE_CONVENTION, from the coverage the measurements record."""
    if measurements.get("O") is None:
        return 1
    if measurements.get("rth_bars") == measurements.get("rth_minutes"):
        return 5
    if measurements.get("IB_high") is not None:
        return 4
    if measurements.get("H15") is not None:
        return 3
    return 2


def _notes(snapshot: Dict[str, Any], outcome: Dict[str, Any], label_version: str) -> str:
    m, labels = outcome["measurements"], outcome["labels"]
    parts = [f"Definitions {defs.DEFINITIONS} ({label_version}), {defs.LEVEL_OUTCOME_CONVENTION}, "
             f"{defs.PULLBACK_RUBRIC}, {defs.PATTERN_CONVENTION} (pattern convention, not P2); "
             f"{snapshot['data_mode'].replace('_', ' ')} from 1m bars, snapshot "
             f"{snapshot['snapshot_version']} (T={m.get('T')}, B={m.get('B')}); RTH coverage "
             f"{m.get('rth_bars')}/{m.get('rth_minutes')} minutes."]
    gaps = [f"{defs.TARGETS[t]['realised_property']}: {v['reason']}"
            for t, v in labels.items() if v["label"] is None and v["reason"] != "not_applicable"]
    if gaps:
        parts.append("Unavailable - " + "; ".join(gaps) + ".")
    first = labels.get("first_level_tested", {}).get("label")
    if first is not None and (m.get("first_level_order") == "estimated" or m.get("first_level_coincident")):
        shown = defs.display("first_level_tested", first)
        if m.get("first_level_order") == "estimated":
            parts.append(f"{defs.FIRST_LEVEL_CONVENTION}: {shown} is the level nearest the open of the "
                         f"{m.get('first_level_bar')} bar, which reached levels on both sides - an estimated order.")
        if m.get("first_level_coincident"):
            also = ", ".join(defs.display("first_level_tested", c) for c in m["first_level_coincident"])
            parts.append(f"{defs.FIRST_LEVEL_CONVENTION}: {also} at the same price, named {shown} by precedence.")
    mp = m.get("mp_v1")
    if mp and mp.get("direction") != "two_sided":
        parts.append(f"MP-v1 {mp['direction']} morning: leg {mp['leg_start']} to {mp['leg_extreme']} "
                     f"({mp['leg_extreme_at']}), pullback {mp['pullback']}, retracement "
                     f"{float(mp['retracement']) * 100:.1f}%.")
    return " ".join(parts)


def p2_record(snapshot: Dict[str, Any], outcome: Dict[str, Any],
              label_version: str = defs.LABEL_VERSION) -> List[Tuple[str, str]]:
    """The 40 P2 outcome properties as (property, display value), in P2's order."""
    labels, m = outcome["labels"], outcome["measurements"]
    out: List[Tuple[str, str]] = []
    for prop, source in P2_FIELDS:
        if isinstance(source, tuple):
            value: Optional[Any] = m.get(source[1])
            if value is None:
                out.append((prop, UNAVAILABLE))
            elif source[1] == "opening_confirmation_time":
                out.append((prop, value))
            else:
                out.append((prop, str(Decimal(str(value)).quantize(_CENT))))
        elif source == "confidence":
            out.append((prop, str(confidence(m, labels))))
        elif source == "notes":
            out.append((prop, _notes(snapshot, outcome, label_version)))
        else:
            v = labels[source]
            if v["label"] is None and v["reason"] == "not_applicable":
                out.append((prop, ""))
            else:
                out.append((prop, defs.display(source, v["label"])))
    return out
