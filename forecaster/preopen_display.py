# forecaster/preopen_display.py
"""
P1's 47-field pre-open record (section 9) - the display / export adapter of the
guideline's Appendix B, kept apart from P2's 40-field outcome record
(forecaster/outcome_display.py), which never overwrites it.

``p1_record(snapshot, annotation, analogue_set, forecast=None)`` returns ``(provenance, rows)``:
one provenance line (versions, origin, target date, the actual cutoff and its
price, integrity) and ``[(property, value, basis), ...]`` in P1's order. Each field
comes from the component Appendix B makes its owner:

  1-14   the snapshot: identity, calendar and frozen references and ATRs. Price at
         09:29 is never the cutoff price renamed; 2-Min ATR at 09:29 is shown only
         for a profile whose cutoff is 09:29, else unavailable under that name
  15-24  the structure annotation (one protocol; P1's vocabularies)
  25-36, 42-45  the forecast: one stored run, read by its run id
                 (forecaster/forecast_display.py); without a run, unavailable
  37-41  the analogue set: count (0 when searched with none qualifying, unavailable
         when not searched), dates, individual scores, their arithmetic mean
  46-47  Event Risk and Event Notes from the annotation (EV-v1)

Values follow P1: numbers only, prices at two decimals, similarities as whole
percentages (a Notion export would write decimal fractions), analogue dates as
"YYYY-MM-DD (Weekday)". A contaminated snapshot gives no rows (P1's stop rule).
"""

from datetime import date
from typing import Any, Dict, List, Optional, Tuple


UNAVAILABLE = "Unavailable"
NO_FORECAST = "no forecast run given"

P1_FIELDS = [
    "Day", "Weekday", "Contract", "Previous RTH High", "Previous RTH Low", "Previous RTH Close", "Overnight Open",
    "ON High", "ON Low", "Premarket High", "Premarket Low", "Price at 09:29", "14-Day Daily ATR",
    "2-Min ATR at 09:29", "Overnight Structure", "Short-Term Structure", "5-Minute Trend", "15-Minute Trend",
    "Higher-Timeframe Bias", "Price vs Long MA", "Long MA Slope", "Fast MA Alignment", "Premarket Pattern",
    "Chop Score", "Predicted Opening Bias", "Expected First Move", "Most Likely Opening Type",
    "Predicted First 15-Minute Direction", "Predicted RTH Session Type", "Predicted RTH Close Direction",
    "Bullish Probability", "Bearish Probability", "Choppy Probability", "Expected First Level Tested",
    "Expected First Level Price", "Forecast Confidence", "Historical Analogue Count", "Analogue Sessions Relation",
    "Analogue Sessions", "Analogue Session Similarity Scores", "Analogue Similarity Score",
    "Expected Upside Target 1", "Expected Upside Target 2", "Expected Downside Target 1",
    "Expected Downside Target 2", "Event Risk", "Event Notes",
]
_REFERENCES = {"Previous RTH High": "prev_rth_high", "Previous RTH Low": "prev_rth_low",
               "Previous RTH Close": "prev_rth_close", "Overnight Open": "overnight_open", "ON High": "on_high",
               "ON Low": "on_low", "Premarket High": "premarket_high", "Premarket Low": "premarket_low",
               "Price at 09:29": "price_at_0929"}
_FORECAST = P1_FIELDS[24:36] + P1_FIELDS[41:45]


def _price(value) -> str:
    return f"{float(value):.2f}"


def _reference(refs: Dict[str, Any], name: str) -> Tuple[str, str]:
    ref = refs.get(name) or {}
    if ref.get("status") == "valid" and ref.get("value") is not None:
        return _price(ref["value"]), "frozen at the cutoff (exact)"
    return UNAVAILABLE, ref.get("detail") or ref.get("status") or "not in the snapshot"


def _day_label(d: str) -> str:
    return f"{d} ({date.fromisoformat(d).strftime('%A')})"


def p1_record(snapshot: Dict[str, Any], annotation: Optional[Dict[str, Any]],
              analogue_set: Optional[Dict[str, Any]],
              forecast: Optional[Dict[str, Any]] = None) -> Tuple[str, List[Tuple[str, str, str]]]:
    """``(provenance, [(property, value, basis), ...])`` for one stored snapshot (see the module docstring); with a
    forecast run, its annotation and analogue set must be the ones given."""
    if forecast is not None and (forecast["snapshot_id"] != snapshot["snapshot_id"]
                                 or forecast["annotation_id"] != (annotation or {}).get("annotation_id")
                                 or forecast["analogue_set_id"] != (analogue_set or {}).get("set_id")):
        raise ValueError(f"forecast run {forecast['run_id']} was issued on other evidence")
    p = snapshot["payload"]
    ident, cut, refs = p["identity"], p["cutoff"], p.get("references") or {}
    cp = refs.get("cutoff_price") or {}
    cutoff_price = (f"cutoff price {_price(cp['value'])} (close of the {cp.get('bar_start_at', '')[11:16]} UTC bar)"
                    if cp.get("status") == "valid" else "no valid cutoff price")
    provenance = (f"{ident['session_date']} {ident.get('local_symbol') or ''}: snapshot {snapshot['snapshot_version']} "
                  f"({cut['data_mode'].replace('_', ' ')}, {cut['pit_availability_status'].replace('_', ' ')}), "
                  f"actual cutoff {cut['cutoff_et']} ET, {cutoff_price}; annotation "
                  f"{annotation['protocol_version'] if annotation else 'none'}; analogues "
                  f"{analogue_set['matcher_version'] if analogue_set else 'not searched'}; "
                  + (f"forecast run {forecast['run_id']} ({forecast['lifecycle_status']}, "
                     f"{forecast['algorithm_version']}, {forecast['mode'].replace('_', ' ')})" if forecast
                     else "no forecast"))
    if annotation is not None and annotation["integrity_status"] != "ok":
        return provenance + " - CONTAMINATED: no record (P1 stop rule)", []

    out: Dict[str, Tuple[str, str]] = {
        "Day": (ident["session_date"], "session identity, exchange calendar"),
        "Weekday": (ident["weekday"], "calendar"),
        "Contract": (ident.get("local_symbol") or str(ident["contract_id"]),
                     f"active contract ({ident.get('active_contract_rule')})"),
    }
    for prop, name in _REFERENCES.items():
        out[prop] = _reference(refs, name)
    daily, two = p["atr"]["daily"], p["atr"]["two_minute"]
    out["14-Day Daily ATR"] = ((_price(daily["value"]), "Wilder ATR(14) of RTH sessions, frozen before the open")
                               if daily.get("status") == "valid" else (UNAVAILABLE, daily.get("status", "missing")))
    if cut["cutoff_et"] != "09:29":
        out["2-Min ATR at 09:29"] = (UNAVAILABLE, f"this profile's cutoff is {cut['cutoff_et']}, not 09:29")
    elif two.get("status") == "valid":
        out["2-Min ATR at 09:29"] = (_price(two["value"]), f"Wilder ATR(14) of 2m bars to {two['last_bucket_end'][11:16]}"
                                                          f" UTC, complete by the 09:29 cutoff")
    else:
        out["2-Min ATR at 09:29"] = (UNAVAILABLE, two.get("status", "missing"))

    fields = (annotation or {}).get("fields") or {}
    for prop in P1_FIELDS[14:24] + ["Event Risk", "Event Notes"]:
        f = fields.get(prop)
        if f is None:
            out[prop] = (UNAVAILABLE, "no structure annotation")
        elif f["value"] is None:
            out[prop] = (UNAVAILABLE, f["reason"] or f["status"])
        else:
            out[prop] = (str(f["value"]), f["basis"])
    if forecast is None:
        for prop in _FORECAST:
            out[prop] = (UNAVAILABLE, NO_FORECAST)
    else:
        from forecaster.forecast_display import forecast_rows
        out.update(forecast_rows(forecast))

    if analogue_set is None:
        for prop in P1_FIELDS[36:41]:
            out[prop] = (UNAVAILABLE, "the journal was not searched")
    else:
        members = analogue_set["members"]
        basis = f"{analogue_set['matcher_version']}: {analogue_set['pool_size']} earlier session(s) scored"
        out["Historical Analogue Count"] = (str(len(members)), basis)
        out["Analogue Sessions Relation"] = (UNAVAILABLE, "no Notion export: the relation is not written; dates "
                                                          "below")
        if members:
            out["Analogue Sessions"] = ("; ".join(_day_label(m["session_date"]) for m in members), basis)
            out["Analogue Session Similarity Scores"] = (
                "; ".join(f"{m['session_date']} — {float(m['similarity']):.0f}%" for m in members),
                "similarity over the comparable weight")
            out["Analogue Similarity Score"] = (f"{float(analogue_set['mean_similarity']):.0f}%",
                                                "arithmetic mean of the selected scores")
        else:
            for prop in ("Analogue Sessions", "Analogue Session Similarity Scores", "Analogue Similarity Score"):
                out[prop] = (UNAVAILABLE, "searched; none qualified")
    return provenance, [(prop, *out[prop]) for prop in P1_FIELDS]
