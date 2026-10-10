# dashboard/theme.py
"""
The dashboard's look, "daylight recorder": chart paper and ink, with colour kept for meaning.

Every colour the pages use is named here. The HTML parts read them as CSS variables (``apply`` defines
``--tp-paper`` ... on every page), so an inline style names a role - ``theme.MUTED``, ``var(--tp-sheet)`` -
rather than a hex. The canvas-drawn parts, Lightweight Charts and ECharts, cannot read CSS variables and take
the hex values (``CHART``, the constants below).

Provenance has one grammar on every page: observed is solid ink; recorded (issued at the time and read back
from the journal) a solid line; recomputed (from the bars stored now) dashed and hollow; a forecast is grey fog;
a time the clock has passed but the delayed feed has not is hatched signal red. Signal red is otherwise kept for
"now" and for anything that sends a request or costs money.

Candles are ink: an up bar hollow, a down bar solid, which leaves colour free for the arms and the levels.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Palette (day). Muted text is 5.8:1 on paper; the arm colours were validated
# with the dataviz palette checks against SHEET: B, N and M all pairs (worst
# CVD dE 11.0, normal-vision dE 19.3); P is at the CVD floor against B (7.8),
# so, as before, it is used only beside its label (the arm tiles), never in an
# overlaid chart.
# ---------------------------------------------------------------------------

PAPER = "#E9EDEB"     # the page ground
SHEET = "#F6F8F7"     # panels and the chart
INK = "#16212A"       # text, candles, anything observed
INK2 = "#4E5C66"      # secondary text, levels, axes
RULE = "#C8D0CC"      # hairlines and the grid
GRID = "#DDE3E0"      # the chart's grid, quieter than RULE
TINT = "#DDE3E0"      # selected rows, the pre-open, tracks
SIGNAL = "#C8321F"    # now, waiting for bars, anything that costs money
AMBER = "#A35A00"     # a warning that is not an error (late, development data)

ARM_COLOR = {"A": INK2, "B": "#3346B8", "N": "#A35A00", "M": "#00806A", "P": "#8E3A8A"}

# Lines on the chart. The moving averages keep the TradingView script's hues (TEMA purple, the trend EMA blue,
# the trigger EMA orange), darkened to read on paper; the levels keep theirs (previous session sky, overnight
# orange, premarket violet, VWAP gold).
TEMA = "#7B4BB0"
EMA_TREND = "#5C7A99"
EMA_TRIGGER = "#C77418"
PREV_RTH = "#1F7FB8"
OVERNIGHT = "#B8650E"
PREMARKET = "#A0489F"
VWAP = "#8F7400"
PROJECTION = "#0A8FA3"
MUTED_CANDLE = "#A3ADB2"

# The comparison tables' cells: blue where an analogue agrees, red where it differs, amber partly (blue and
# orange-red stay apart for colour-blind readers, unlike green and red); grey where it cannot be compared.
AGREE = "rgba(51, 70, 184, 0.16)"
DIFFER = "rgba(200, 50, 31, 0.16)"
PARTLY = "rgba(163, 90, 0, 0.2)"
NOT_COMPARABLE = "rgba(78, 92, 102, 0.08)"

FONT = "'Archivo', 'Segoe UI', system-ui, sans-serif"
# Archivo carries a width axis: expanded (125 %) for dates, times and titles, semi-condensed (85 %) for dense
# tables. Its variable font (weight 100-900, width 62-125 %) is vendored in static/fonts under the SIL Open Font
# License 1.1 (OFL.txt beside it) and served by the dashboard itself at FONT_ROUTE (app.py), so the pages load
# nothing from the network.
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "fonts")
FONT_ROUTE = "/static/fonts"

# Inline-style snippets for the pages.
MUTED = "color:var(--tp-ink2)"
PANEL = "var(--tp-sheet)"


def rgb(hex_colour: str) -> str:
    """'#16212A' -> '22, 33, 42', for the canvas code that composes rgba() itself."""
    h = hex_colour.lstrip("#")
    return ", ".join(str(int(h[i:i + 2], 16)) for i in (0, 2, 4))


def rgba(hex_colour: str, alpha: float) -> str:
    return f"rgba({rgb(hex_colour)}, {alpha})"


# What the chart component (lightweight_chart.js) draws with.
CHART = {
    "background": SHEET,
    "text": INK2,
    "ink": INK,
    "grid": GRID,
    "border": RULE,
    "font": FONT,
    "up_body": SHEET,          # hollow
    "up_border": INK,
    "down_body": INK,          # solid
    "down_border": INK,
    "legend_value": INK,
    "legend_label": INK2,
    "release_rgb": rgb(INK2),
    "shade": rgba(INK2, 0.08),
}

# ECharts (the coverage map, the arm radar): the same ink on the same sheet.
ECHART_TOOLTIP = {"backgroundColor": SHEET, "borderColor": RULE, "textStyle": {"color": INK, "fontSize": 11}}

_CSS = f"""
@font-face {{ font-family:'Archivo'; src:url('{FONT_ROUTE}/Archivo-Variable.woff2') format('woff2');
  font-weight:100 900; font-stretch:62% 125%; font-style:normal; font-display:swap; }}
:root {{
  --tp-paper:{PAPER}; --tp-sheet:{SHEET}; --tp-ink:{INK}; --tp-ink2:{INK2}; --tp-rule:{RULE};
  --tp-tint:{TINT}; --tp-signal:{SIGNAL}; --tp-amber:{AMBER};
}}
body {{ background:var(--tp-paper); color:var(--tp-ink); font-family:{FONT}; font-variant-numeric:tabular-nums; }}
.tp-x {{ font-stretch:125%; }}
.tp-c {{ font-stretch:85%; }}
.tp-muted {{ color:var(--tp-ink2); }}
.q-header {{ background:var(--tp-sheet) !important; color:var(--tp-ink) !important;
  border-bottom:1px solid var(--tp-rule); box-shadow:none; }}
.q-card {{ background:var(--tp-sheet); color:var(--tp-ink); border:1px solid var(--tp-rule); border-radius:10px;
  box-shadow:none; }}
.q-dialog .q-card {{ box-shadow:0 12px 40px rgba(22, 33, 42, 0.18); }}
.q-expansion-item {{ background:var(--tp-sheet); border:1px solid var(--tp-rule); border-radius:10px; }}
.q-expansion-item .q-item__label {{ font-stretch:125%; font-weight:700; letter-spacing:-0.01em; }}
.q-btn {{ font-weight:550; letter-spacing:0; text-transform:none; }}
.q-separator {{ background:var(--tp-rule); }}
.q-tab--active {{ color:var(--tp-ink); }}
.tp-nav .q-btn {{ border-radius:0; border-bottom:2px solid transparent; color:var(--tp-ink2); }}
.tp-nav .q-btn.tp-active {{ border-bottom-color:var(--tp-ink); color:var(--tp-ink); }}
:focus-visible {{ outline:2px solid {ARM_COLOR["B"]}; outline-offset:2px; }}

/* Buttons: 6 px corners, no Material shadow; an outline in the rule colour. */
.q-btn {{ border-radius:6px; }}
.q-btn--standard:before {{ box-shadow:none; }}
.q-btn--outline:before {{ border-color:var(--tp-rule); }}
/* Segmented controls (ui.toggle): a tinted track, the chosen option a white pill. */
.q-btn-toggle {{ background:var(--tp-tint); border-radius:8px; padding:2px; gap:2px; box-shadow:none; }}
.q-btn-toggle .q-btn {{ border-radius:6px !important; color:var(--tp-ink2); padding:0 12px; }}
.q-btn-toggle .q-btn.bg-primary {{ background:var(--tp-sheet) !important; color:var(--tp-ink) !important;
  box-shadow:0 0 0 1px var(--tp-rule); }}
/* Fields: outlined boxes on the sheet. */
.q-field--outlined .q-field__control {{ border-radius:6px; background:var(--tp-sheet); }}
.q-field--outlined .q-field__control:before {{ border-color:var(--tp-rule); }}
/* Section panels (ui.expansion): the title in the expanded width, no icon. */
.q-expansion-item__container > .q-item {{ padding:14px 18px; min-height:56px; }}
.q-expansion-item__container > .q-item .q-item__label {{ font-size:19px; }}
.q-expansion-item__content {{ padding:0 18px 16px; }}

/* The session day as the page's heading (components/session_bar.py). */
.tp-day {{ font-stretch:125%; font-size:30px; font-weight:720; letter-spacing:-0.02em; line-height:1.1;
  cursor:pointer; white-space:nowrap; }}
/* Keys, arm marks and probability bars (views/candles.py, views/forecast.py). */
.tp-key {{ display:inline-flex; align-items:center; gap:6px; white-space:nowrap; font-size:12.5px; }}
.tp-mark {{ display:inline-flex; align-items:center; justify-content:center; width:24px; height:24px;
  border-radius:12px; border:2px solid; font-weight:700; font-size:12px; box-sizing:border-box; flex:none; }}
.tp-pbar {{ display:block; height:4px; border-radius:2px; background:var(--tp-tint); margin-top:4px; }}
.tp-pbar > span {{ display:block; height:4px; border-radius:2px; }}

/* The session timeline (components/session_timeline.py). */
.tp-tl {{ position:relative; min-width:980px; height:150px; font-size:12.5px; line-height:1.3; }}
.tp-tl > * {{ position:absolute; }}
.tp-tl .seg {{ top:0; color:var(--tp-ink2); white-space:nowrap; }}
.tp-tl .track {{ left:0; right:0; top:51px; border-top:2px dotted var(--tp-ink2); opacity:.55; }}
.tp-tl .bars {{ left:0; top:47px; height:10px; border-radius:5px; background:var(--tp-ink); }}
.tp-tl .wait {{ top:47px; height:10px; opacity:.7;
  background:repeating-linear-gradient(135deg, var(--tp-signal) 0 2px, transparent 2px 6px); }}
.tp-tl .now {{ top:20px; height:48px; border-left:2px solid var(--tp-signal); }}
.tp-tl .nowlabel {{ top:18px; padding-left:8px; white-space:nowrap; display:flex; gap:10px; align-items:baseline; }}
.tp-tl .nowlabel.right {{ transform:translateX(-100%); padding-left:0; padding-right:8px; }}
.tp-tl .nowlabel b {{ font-stretch:125%; font-size:15px; color:var(--tp-signal); }}
.tp-tl .nowlabel .lag {{ color:var(--tp-signal); }}
.tp-tl .gate {{ top:46px; width:12px; height:12px; margin-left:-6px; transform:rotate(45deg); box-sizing:border-box;
  border:2px solid var(--tp-paper); background:var(--tp-ink); }}
.tp-tl .gate.waiting {{ background:var(--tp-paper); border-color:var(--tp-signal); }}
.tp-tl .gate.ahead {{ background:var(--tp-paper); border-color:var(--tp-ink2); }}
.tp-tl .tick {{ top:62px; height:8px; border-left:1px solid var(--tp-ink2); }}
.tp-tl .tick.low {{ height:46px; }}
.tp-tl .label {{ top:72px; white-space:nowrap; padding:0 6px; background:var(--tp-paper); z-index:1; }}
.tp-tl .label.low {{ top:110px; }}
.tp-tl .label.right {{ text-align:right; transform:translateX(-100%); }}
.tp-tl .label b {{ font-stretch:125%; font-size:15px; margin-right:4px; }}
.tp-tl .state {{ display:block; color:var(--tp-ink2); }}
.tp-tl .state.observed {{ color:var(--tp-ink); font-weight:600; }}
.tp-tl .state.waiting {{ color:var(--tp-signal); }}
"""


def apply() -> None:
    """The theme for the page being built: fonts, the CSS variables and Quasar's brand colours."""
    from nicegui import ui

    ui.add_css(_CSS)
    ui.colors(primary=INK, secondary=INK2, accent=ARM_COLOR["B"], dark=INK, positive=ARM_COLOR["M"],
              negative=SIGNAL, info=ARM_COLOR["B"], warning=AMBER)
