# forecaster/prompts.py
"""
Prompt template generator for the Opening Forecast System.
Creates structured, context-rich prompts for Large Language Models (LLMs)
to predict opening trading scenarios based on mathematical features and historical analogues.
"""

# Used when the caller does not name the instrument, so the prompt never claims
# the wrong index. Reads as "... the US equity index futures opening session".
_DEFAULT_INSTRUMENT = "US equity"


def _fmt(value, spec=".2f"):
    """Formats a number, returning 'N/A' for None / non-numeric values."""
    try:
        return format(float(value), spec)
    except (TypeError, ValueError):
        return "N/A"


def _vix_line(features, label="- Pre-Open VIX:      "):
    """
    The pre-open VIX context line, or '' when VIX was not collected for that
    session — an absent line is better than one reading 'N/A'. ``label`` carries
    the caller's own indentation so the line aligns with its neighbours.
    """
    level = features.get("vix_pre_open")
    if level is None:
        return ""
    change = features.get("vix_change")
    suffix = f" ({float(change):+.2f} vs prior close)" if change is not None else ""
    return f"\n{label}{_fmt(level)}{suffix}"


def _estimated_open(features):
    prev_close = features.get("previous_rth_close")
    gap = features.get("gap")
    try:
        return format(float(prev_close) + float(gap), ".2f")
    except (TypeError, ValueError):
        return "N/A"


def construct_forecast_prompt(target_day, target_features, analogues, instrument=None):
    """
    Assembles a prompt string structured to keep the LLM focused on evidence-based analysis.
    Passes pre-open features, analogue days, and their historical outcomes,
    requiring a strictly validated JSON output format.

    ``instrument`` is a display label such as 'S&P 500 E-mini (ES)'. Analogues are
    always drawn from the same contract, so one label describes the whole prompt.
    """
    tf = target_features or {}
    instrument_label = instrument or _DEFAULT_INSTRUMENT

    target_info = f"""
INSTRUMENT: {instrument_label}
TARGET SESSION: {target_day}
=====================================================
- Previous RTH High:  {_fmt(tf.get('previous_rth_high'))}
- Previous RTH Low:   {_fmt(tf.get('previous_rth_low'))}
- Previous RTH Close: {_fmt(tf.get('previous_rth_close'))}
- Overnight High:     {_fmt(tf.get('overnight_high'))}
- Overnight Low:      {_fmt(tf.get('overnight_low'))}
- Overnight Range:    {_fmt(tf.get('overnight_range'))}
- RTH Opening Price:  {_estimated_open(tf)} (Estimated based on Opening Gap)
- Opening Gap:        {_fmt(tf.get('gap'))}
- Pre-Open Direction: {tf.get('pre_open_direction', 'N/A')}
- Current VWAP:       {_fmt(tf.get('vwap'))}
- Hist Volatility:    {_fmt(tf.get('historical_volatility'), '.4f')}{_vix_line(tf)}
=====================================================
"""

    analogues_section = ""
    for idx, a in enumerate(analogues or [], start=1):
        snap = a.get("snapshot_data", {})
        outcome = snap.get("outcome", {}) or {}

        analogues_section += f"""
ANALOGUE #{idx}: Date={a.get('match_date')} (Similarity Score: {_fmt(a.get('similarity_score'), '.4f')})
-----------------------------------------------------
Pre-Open Context:
  - Prev Close: {_fmt(snap.get('previous_rth_close'))} | Gap: {_fmt(snap.get('gap'))} ({snap.get('pre_open_direction', 'N/A')})
  - Overnight Range: {_fmt(snap.get('overnight_range'))} | Hist Vol: {_fmt(snap.get('historical_volatility'), '.4f')}{_vix_line(snap, label='  - Pre-Open VIX: ')}
Realized Outcomes (What actually happened):
  - First 15-Min High/Low/Close: {_fmt(outcome.get('first_15_minute_high'))} / {_fmt(outcome.get('first_15_minute_low'))} / {_fmt(outcome.get('first_15_minute_close'))}
  - First 30-Min High/Low/Close: {_fmt(outcome.get('first_30_minute_high'))} / {_fmt(outcome.get('first_30_minute_low'))} / {_fmt(outcome.get('first_30_minute_close'))}
  - Initial Balance (1st Hr) High/Low: {_fmt(outcome.get('initial_balance_high'))} / {_fmt(outcome.get('initial_balance_low'))}
  - RTH Session High/Low/Close: {_fmt(outcome.get('rth_high'))} / {_fmt(outcome.get('rth_low'))} / {_fmt(outcome.get('rth_close'))}
-----------------------------------------------------
"""

    prompt = f"""You are an elite quantitative microstructural analyst specializing in the {instrument_label} index futures opening session.

Your task is to analyze the opening context for the target session {target_day} on {instrument_label} by evaluating its pre-open metrics against the provided historical analogue days and their realized outcomes.

{target_info}

HISTORICAL ANALOGUES FOR COMPARATIVE CONTEXT:
{analogues_section}

Based strictly on this structured evidence:
1. Identify the most likely opening scenario for the first 30-60 minutes of trading (e.g., Open Drive, Open Test-Drive, Open Rejection-Reverse, or Open Auction).
2. Assign probability distributions to key directional paths (Bullish Continuation, Mean Reversion/Gap Fill, Bearish Rejection).
3. Specify price triggers (levels that confirm a scenario) and invalidation targets.

CRITICAL REQUIREMENT: Your mathematical and scenario assertions must be directly supported by the relative behavior of the historical analogues provided. Do not use generic training assumptions.

You must respond ONLY with a valid JSON document matching this structure:
{{
  "opening_bias": "BULLISH" | "BEARISH" | "NEUTRAL",
  "scenarios": {{
    "primary_scenario": {{
      "name": "e.g., Mean Reversion/Gap Fill",
      "description": "Provide a 2-sentence microstructural explanation based on matching analogues.",
      "triggers": ["level 1", "level 2"],
      "invalidations": ["level 1"]
    }},
    "alternative_scenario": {{
      "name": "e.g., Open Drive / Trend Continuation",
      "description": "Provide a 1-sentence alternative path.",
      "triggers": ["level"],
      "invalidations": ["level"]
    }}
  }},
  "probabilities": {{
    "bullish_continuation_pct": 35.0,
    "mean_reversion_gap_fill_pct": 50.0,
    "bearish_rejection_pct": 15.0
  }},
  "forecast_horizon": "First 60 minutes (Initial Balance window)",
  "analogue_rationale": "Provide a brief explanation of how the top historical analogues guided this forecast."
}}

Respond only with JSON. Do not write preambles, markdown fences, or explanations outside the JSON object block."""

    return prompt
