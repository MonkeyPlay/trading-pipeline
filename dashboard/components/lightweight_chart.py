# dashboard/components/lightweight_chart.py
"""
NiceGUI element wrapping TradingView's Lightweight Charts.

The element is deliberately thin: it owns no chart state. Callers build a
complete spec of what the chart should show (see :mod:`dashboard.components.spec`)
and hand it over; the Vue component works out which parts actually changed and
mutates the live chart in place.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from nicegui.element import Element


class LightweightChart(
    Element,
    component="lightweight_chart.js",
    dependencies=["lib/lightweight-charts.standalone.js"],
):
    """A candlestick chart with a volume pane and arbitrary overlay series."""

    def __init__(
        self,
        *,
        height: int = 620,
        volume_ratio: float = 0.22,
        show_volume: bool = True,
        show_candles: bool = True,
        spec: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__()
        self._props["height"] = height
        self._props["volume_ratio"] = volume_ratio
        self._props["show_volume"] = show_volume
        self._props["show_candles"] = show_candles
        # Drawn when the browser mounts the chart, so a chart created inside an
        # event handler shows its data without waiting for a later apply().
        self._props["initial_spec"] = spec
        self.classes("w-full")

    def apply(self, spec: Dict[str, Any]) -> None:
        """
        Pushes a desired-state spec to the browser.

        Safe to call on every interaction: unchanged series are left untouched,
        style-only changes become ``applyOptions``, and appended bars become
        ``update`` rather than a full ``setData``.
        """
        # Kept current without an update() round trip, so a remount (a reconnect,
        # or a chart not yet mounted when this runs) draws the latest state.
        self._props["initial_spec"] = spec
        self.run_method("apply", spec)
