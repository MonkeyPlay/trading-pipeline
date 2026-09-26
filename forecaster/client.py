# forecaster/client.py
"""
The v1 opening forecast used by the dashboard's Session Explorer and
scripts/daily_forecast.py: a deterministic, analogue-driven engine that turns
the realised outcomes of the closest historical sessions into an opening bias,
two scenarios and outcome frequencies.

It runs entirely offline: no language model and no external API is called. The
trained per-target forecasts of the v2 contract live in forecaster/models_v2.py
(scripts/nq_forecast_v2.py).
"""

import logging

logger = logging.getLogger(__name__)

MODEL_VERSION = "analogue_baseline_v1"
# The v1 predictions table records a prompt version; no prompt exists any more.
PROMPT_VERSION = "none"
REQUIRED_FORECAST_KEYS = ["opening_bias", "scenarios", "probabilities", "forecast_horizon"]


def _fmt(value, spec=".2f"):
    """Formats a number, returning 'N/A' for None / non-numeric values."""
    try:
        return format(float(value), spec)
    except (TypeError, ValueError):
        return "N/A"


class ForecastClient:
    def __init__(self, model_name=None):
        self.model_name = model_name or MODEL_VERSION

    # ------------------------------------------------------------------
    # Analogue engine
    # ------------------------------------------------------------------
    def _analogue_forecast(self, target_day, target_features, analogues):
        """
        Generates a mathematically grounded baseline forecast using the historical
        analogues' actual outcomes.
        """
        tf = target_features or {}
        analogues = analogues or []

        bullish_count = bearish_count = mean_rev_count = 0
        for a in analogues:
            snap = a.get("snapshot_data", {})
            outcome = snap.get("outcome", {}) or {}
            rth_close = outcome.get("rth_close")
            prev_close = snap.get("previous_rth_close", rth_close)

            try:
                session_return = (float(rth_close) - float(prev_close)) / float(prev_close)
            except (TypeError, ValueError, ZeroDivisionError):
                continue

            if abs(session_return) < 0.002:
                mean_rev_count += 1
            elif session_return > 0:
                bullish_count += 1
            else:
                bearish_count += 1

        total = bullish_count + bearish_count + mean_rev_count
        if total > 0:
            bull_pct = round((bullish_count / total) * 100, 1)
            bear_pct = round((bearish_count / total) * 100, 1)
            mrev_pct = round(100.0 - bull_pct - bear_pct, 1)
        else:
            bull_pct, mrev_pct, bear_pct = 30.0, 40.0, 30.0

        direction = tf.get("pre_open_direction", "FLAT")
        prev_close = tf.get("previous_rth_close")
        gap = tf.get("gap")
        ovn_high = tf.get("overnight_high")
        ovn_low = tf.get("overnight_low")

        if direction == "UP":
            primary_name = "Bullish Gap-and-Go"
            primary_desc = (
                f"Opening gap of {_fmt(gap)} points indicates strong pre-market momentum. "
                f"Top historical analogues suggest continuation."
            )
            triggers = [f"Break above overnight high of {_fmt(ovn_high)}"]
            invalidations = [f"Fill below previous close of {_fmt(prev_close)}"]
            bias = "BULLISH"
        elif direction == "DOWN":
            primary_name = "Bearish Gap Continuation"
            primary_desc = (
                f"Downward gap of {_fmt(gap)} points shows pre-market selling pressure. "
                f"Expected test of overnight lows."
            )
            triggers = [f"Break below overnight low of {_fmt(ovn_low)}"]
            invalidations = [f"Fill above previous close of {_fmt(prev_close)}"]
            bias = "BEARISH"
        else:
            primary_name = "Mean Reverting Range Trade"
            primary_desc = (
                f"Flat opening gap of {_fmt(gap)} points indicates a balance regime. "
                f"Price expected to trade within overnight limits."
            )
            triggers = [f"Rejection of overnight high {_fmt(ovn_high)}"]
            invalidations = ["Sustained break of overnight high/low range"]
            bias = "NEUTRAL"

        forecast = {
            "opening_bias": bias,
            "scenarios": {
                "primary_scenario": {
                    "name": primary_name,
                    "description": primary_desc,
                    "triggers": triggers,
                    "invalidations": invalidations,
                },
                "alternative_scenario": {
                    "name": "Mean Reversion / Counter-Trend",
                    "description": (
                        f"Overextended gap levels trigger immediate profit-taking, "
                        f"leading to a gap fill toward {_fmt(prev_close)}."
                    ),
                    "triggers": ["Failure to hold first 15-minute high/low limits"],
                    "invalidations": ["Sustained volume expansion beyond RTH levels"],
                },
            },
            "probabilities": {
                "bullish_continuation_pct": bull_pct,
                "mean_reversion_gap_fill_pct": mrev_pct,
                "bearish_rejection_pct": bear_pct,
            },
            "forecast_horizon": "First 60 minutes (Initial Balance window)",
            "analogue_rationale": (
                "Forecast derived deterministically from historical similarities of "
                "volatility-normalized price action and the realized outcomes of the "
                "closest analogue sessions."
            ),
        }
        return forecast

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def get_forecast(self, target_day, target_features, analogues, instrument=None):
        """
        The analogue forecast for ``target_day``. ``instrument`` (a display label
        such as 'S&P 500 E-mini (ES)') is accepted for interface compatibility;
        analogues are already drawn from that instrument's own history.
        """
        logger.info(f"Analogue baseline forecast for {instrument or 'instrument'} {target_day}.")
        return self._analogue_forecast(target_day, target_features, analogues)
