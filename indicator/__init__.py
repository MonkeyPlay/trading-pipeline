# indicator/__init__.py
"""
Chart indicators for the NQ Opening Forecast System.

Each indicator computes over an OHLC frame and returns an
:class:`~indicator.types.IndicatorResult`, which
:func:`~indicator.lwc.indicator_payload` turns into Lightweight Charts series.
"""

from indicator.auto_anchored_vwap import (
    ANCHOR_PERIOD_OPTIONS,
    CALC_MODE_OPTIONS,
    SOURCE_OPTIONS,
    AutoAnchoredVwapSettings,
)
from indicator.auto_anchored_vwap import compute as compute_auto_anchored_vwap
from indicator.lwc import indicator_payload, level_points, rgba, to_epoch
from indicator.tema_session import TemaSessionSettings
from indicator.tema_session import compute as compute_tema_session
from indicator.types import (
    LINE_STYLE_OPTIONS,
    IndicatorResult,
    LevelStyle,
    LineStyle,
    SessionLevel,
)

__all__ = [
    "ANCHOR_PERIOD_OPTIONS",
    "CALC_MODE_OPTIONS",
    "LINE_STYLE_OPTIONS",
    "SOURCE_OPTIONS",
    "AutoAnchoredVwapSettings",
    "IndicatorResult",
    "LevelStyle",
    "LineStyle",
    "SessionLevel",
    "TemaSessionSettings",
    "compute_auto_anchored_vwap",
    "compute_tema_session",
    "indicator_payload",
    "level_points",
    "rgba",
    "to_epoch",
]
