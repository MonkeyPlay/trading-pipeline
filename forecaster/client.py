# forecaster/client.py
"""
The analogue opening forecast used by the dashboard's Session Explorer and
scripts/daily_forecast.py: a deterministic engine that turns the realised first
hour of the closest historical sessions into an opening bias, two scenarios and
outcome frequencies. The analogues themselves are chosen by
forecaster/analogue.py.

It runs entirely offline: no language model and no external API is called. The
trained per-target forecasts of the v2 contract live in forecaster/models_v2.py
(scripts/nq_forecast_v2.py).

analogue_baseline_v2 (this version) measures every analogue over the horizon
the forecast states - the first 60 minutes, 09:30 open to 10:29 close - and
derives the bias from the same frequencies it shows. analogue_baseline_v1 took
the bias from the sign of the gap alone and the frequencies from the analogues'
RTH close against the previous close, so it could show a bearish bias next to a
mostly bullish distribution.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

MODEL_VERSION = "analogue_baseline_v2"
# The v1 predictions table records a prompt version; no prompt exists any more.
PROMPT_VERSION = "none"
REQUIRED_FORECAST_KEYS = ["opening_bias", "scenarios", "probabilities", "forecast_horizon"]

HORIZON = "First 60 minutes: 09:30 open to 10:29 close"
# A first-hour move within +/- FLAT_BAND of the day's volatility scale is flat.
FLAT_BAND = 0.10
_BIAS = {"up": "BULLISH", "down": "BEARISH", "flat": "NEUTRAL"}


def _fmt(value, spec=".2f"):
    """Formats a number, returning 'N/A' for None / non-numeric values."""
    try:
        return format(float(value), spec)
    except (TypeError, ValueError):
        return "N/A"


def classify_move(normalized_move: float) -> str:
    """'up', 'down' or 'flat' for a first-hour move in units of the day's volatility scale."""
    if normalized_move > FLAT_BAND:
        return "up"
    if normalized_move < -FLAT_BAND:
        return "down"
    return "flat"


class ForecastClient:
    def __init__(self, model_name=None):
        self.model_name = model_name or MODEL_VERSION

    def get_forecast(self, target_day, target_features, analogues, instrument=None,
                     scale: Optional[float] = None, scale_name: str = "daily volatility",
                     method: str = "", pool: Optional[int] = None) -> Dict[str, Any]:
        """
        The forecast for ``target_day`` from ``analogues`` (each with
        ``outcome['label']`` in up / down / flat, see forecaster/analogue.py).
        ``scale`` is the target day's volatility scale in points (ATR, or previous
        close x historical volatility), used to state the flat band in points;
        ``method`` and ``pool`` describe how the analogues were chosen.
        """
        logger.info(f"Analogue forecast for {instrument or 'instrument'} {target_day} "
                    f"from {len(analogues or [])} analogues.")
        tf = target_features or {}
        labels = [a["outcome"]["label"] for a in (analogues or []) if a.get("outcome")]
        n = len(labels)
        counts = {k: labels.count(k) for k in ("up", "flat", "down")}
        if n:
            up_pct = round(100.0 * counts["up"] / n, 1)
            down_pct = round(100.0 * counts["down"] / n, 1)
            flat_pct = round(100.0 - up_pct - down_pct, 1)
        else:
            up_pct = flat_pct = down_pct = None

        # The bias is the most frequent first-hour outcome of the analogues; a tie
        # between the leaders (or no analogues) is no bias.
        if n:
            top = max(counts.values())
            leaders = [k for k, v in counts.items() if v == top]
            bias = _BIAS[leaders[0]] if len(leaders) == 1 else "NEUTRAL"
        else:
            bias = "UNKNOWN"

        prev_close, gap = tf.get("previous_rth_close"), tf.get("gap")
        ovn_high, ovn_low = tf.get("overnight_high"), tf.get("overnight_low")
        tally = (f"{counts['up']} of {n} matched sessions rose, {counts['down']} fell and "
                 f"{counts['flat']} stayed within ±{FLAT_BAND:.2f} {scale_name} in the first hour")
        if bias == "BULLISH":
            primary = {
                "name": "Analogues lean up",
                "description": f"{tally}. Opening gap {_fmt(gap)} points.",
                "triggers": [f"Break above overnight high of {_fmt(ovn_high)}"],
                "invalidations": [f"Loss of previous close {_fmt(prev_close)}"],
            }
        elif bias == "BEARISH":
            primary = {
                "name": "Analogues lean down",
                "description": f"{tally}. Opening gap {_fmt(gap)} points.",
                "triggers": [f"Break below overnight low of {_fmt(ovn_low)}"],
                "invalidations": [f"Reclaim of previous close {_fmt(prev_close)}"],
            }
        elif n:
            primary = {
                "name": "No clear lean",
                "description": f"{tally}. Opening gap {_fmt(gap)} points; range trade within the "
                               f"overnight limits is as likely as a break.",
                "triggers": [f"Rejection of overnight high {_fmt(ovn_high)} or low {_fmt(ovn_low)}"],
                "invalidations": ["Sustained break of the overnight high/low range"],
            }
        else:
            primary = {
                "name": "No analogues",
                "description": "No earlier session with a measurable first hour was found to compare with.",
                "triggers": [], "invalidations": [],
            }

        probabilities = {
            # Keys kept from v1 for the stored rows and the evaluation page.
            "bullish_continuation_pct": up_pct,
            "mean_reversion_gap_fill_pct": flat_pct,
            "bearish_rejection_pct": down_pct,
            "matches": n,
            "flat_band_points": round(FLAT_BAND * scale, 2) if scale else None,
        }
        pool_text = f" among {pool} earlier sessions" if pool else ""
        return {
            "opening_bias": bias,
            "scenarios": {
                "primary_scenario": primary,
                "alternative_scenario": {
                    "name": "Mean Reversion / Counter-Trend",
                    "description": f"Failure at the first-hour extremes and a return toward {_fmt(prev_close)}.",
                    "triggers": ["Failure to hold first 15-minute high/low limits"],
                    "invalidations": ["Sustained volume expansion beyond RTH levels"],
                },
            },
            "probabilities": probabilities,
            "forecast_horizon": HORIZON,
            "analogue_rationale": (
                f"The {n} closest sessions{pool_text} by {method or 'pre-open similarity'}; the "
                f"probabilities are how their first hour went, and the bias is the most frequent "
                f"outcome. Pre-open data carries little information about direction, so treat a lean "
                f"as weak; similar pre-open volatility does point to a similar range."
            ),
        }
