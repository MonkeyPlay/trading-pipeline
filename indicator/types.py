# indicator/types.py
"""
Shared result types for the indicator package.

Indicators return an :class:`IndicatorResult` so the rendering layer can draw any
of them without knowing which indicator produced the data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

LINE_STYLE_OPTIONS = ("Solid", "Dashed", "Dotted")
DASH_BY_NAME = {"Solid": "solid", "Dashed": "dash", "Dotted": "dot"}


@dataclass(frozen=True)
class LineStyle:
    """Presentation for one continuous series plotted over the candles."""

    label: str
    color: str
    width: float = 1.0
    dash: str = "solid"
    # Set to "tonexty" to shade the gap against the preceding series, so a band's
    # upper edge must be registered immediately before its lower edge.
    fill: Optional[str] = None
    fill_color: Optional[str] = None
    legend_group: Optional[str] = None
    show_legend: bool = True


@dataclass(frozen=True)
class LevelStyle:
    """Presentation shared by every horizontal level in a result."""

    width: int = 1
    dash: str = "solid"
    show_labels: bool = True
    label_offset: int = 5


@dataclass(frozen=True)
class SessionLevel:
    """A horizontal level anchored at a bar and extended to the right edge."""

    key: str
    label: str
    value: float
    color: str
    anchor: Optional[pd.Timestamp] = None


@dataclass
class IndicatorResult:
    """
    Output of an indicator over one bar frame.

    ``lines`` is indexed by the timestamps of the input frame, so callers can
    reindex it onto a narrower display window after computing over a longer
    warm-up range.
    """

    lines: pd.DataFrame = field(default_factory=pd.DataFrame)
    line_styles: Dict[str, LineStyle] = field(default_factory=dict)
    levels: List[SessionLevel] = field(default_factory=list)
    level_style: LevelStyle = field(default_factory=LevelStyle)
    bar_delta: Optional[pd.Timedelta] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def reindex_lines(self, timestamps) -> "IndicatorResult":
        """Returns a copy whose line series cover only ``timestamps``."""
        if self.lines.empty:
            return self
        return IndicatorResult(
            lines=self.lines.reindex(pd.DatetimeIndex(timestamps)),
            line_styles=self.line_styles,
            levels=self.levels,
            level_style=self.level_style,
            bar_delta=self.bar_delta,
            meta=self.meta,
        )
