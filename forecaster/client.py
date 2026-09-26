# forecaster/client.py
"""
LLM Client and Forecasting Engine for the Opening Forecast System.
Handles communication with LLM endpoints (Google Gemini by default, or Anthropic) and provides
a deterministic, analogue-driven fallback engine when running offline.
"""

import os
import json
import logging

from forecaster.prompts import construct_forecast_prompt, _fmt

logger = logging.getLogger(__name__)

REQUIRED_FORECAST_KEYS = ["opening_bias", "scenarios", "probabilities", "forecast_horizon"]

DEFAULT_MODEL = "gemini-2.5-flash"


class ForecastClient:
    def __init__(self, model_name=None, api_key=None, provider=None):
        self.model_name = model_name or os.getenv("LLM_MODEL", DEFAULT_MODEL)

        # GOOGLE_API_KEY is the google-genai SDK's own fallback name, so honour it too.
        gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")

        # Infer provider from an explicit arg, then the model name, then whichever key
        # exists. Gemini is the default.
        if provider:
            self.provider = provider
        elif "claude" in self.model_name.lower():
            self.provider = "anthropic"
        elif "gemini" in self.model_name.lower():
            self.provider = "gemini"
        elif anthropic_key and not gemini_key:
            self.provider = "anthropic"
        else:
            self.provider = "gemini"

        if api_key:
            self.api_key = api_key
        elif self.provider == "anthropic":
            self.api_key = anthropic_key
        else:
            self.api_key = gemini_key

    # ------------------------------------------------------------------
    # Offline / deterministic fallback
    # ------------------------------------------------------------------
    def _generate_mock_or_baseline_forecast(self, target_day, target_features, analogues):
        """
        Generates a mathematically grounded baseline forecast using the historical
        analogues' actual outcomes. Used whenever the LLM path is unavailable.
        """
        logger.info("Using analogue-driven deterministic baseline forecast engine.")
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
        Requests a forecast from the configured LLM. Falls back to the baseline
        statistical matching engine on any failure (no key, import error, bad JSON).

        ``instrument`` is a display label ('S&P 500 E-mini (ES)') naming which
        contract the snapshot belongs to, so the model is not told the wrong index.
        """
        try:
            prompt = construct_forecast_prompt(target_day, target_features, analogues, instrument)
        except Exception as e:  # never let prompt assembly kill the pipeline
            logger.warning(f"Prompt construction failed ({e}); using baseline engine.")
            return self._generate_mock_or_baseline_forecast(target_day, target_features, analogues)

        if not self.api_key:
            return self._generate_mock_or_baseline_forecast(target_day, target_features, analogues)

        try:
            if self.provider == "gemini":
                from google import genai
                from google.genai import types

                client = genai.Client(api_key=self.api_key)
                response = client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction="You are a quantitative market research analyst. Always respond with a single JSON object.",
                        response_mime_type="application/json",
                        temperature=0.2,
                    ),
                )
                raw_text = response.text

            elif self.provider == "anthropic":
                import anthropic

                client = anthropic.Anthropic(api_key=self.api_key)
                response = client.messages.create(
                    model=self.model_name,
                    max_tokens=1200,
                    system="You are a quantitative market research analyst. Always respond with a single JSON object and nothing else.",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                )
                raw_text = response.content[0].text
            else:
                raise ValueError(f"Unsupported provider: {self.provider}")

            parsed_json = json.loads(raw_text)
            for rk in REQUIRED_FORECAST_KEYS:
                if rk not in parsed_json:
                    raise KeyError(f"Missing required key '{rk}' in LLM forecast response.")
            return parsed_json

        except Exception as e:
            logger.warning(f"LLM forecast failed ({e}). Falling back to baseline matching engine.")
            return self._generate_mock_or_baseline_forecast(target_day, target_features, analogues)
