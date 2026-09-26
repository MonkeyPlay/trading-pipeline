/**
 * TradingView Lightweight Charts as a NiceGUI element.
 *
 * The Python side sends a *declarative spec* of what the chart should show and
 * this component reconciles it against what is already drawn. Nothing is torn
 * down for a settings change: the chart, its series and its primitives are
 * created once and then mutated in place, so the user's zoom and scroll survive
 * every interaction.
 *
 * Reconciliation prefers the cheapest operation that can produce the requested
 * state:
 *   - style only changed        -> series.applyOptions()
 *   - data grew at the right    -> series.update() per new/changed bar
 *   - data changed elsewhere    -> series.setData() on the *existing* series
 *
 * `update()` in Lightweight Charts can only touch the last bar or append after
 * it, so recomputing an indicator over the whole window genuinely requires
 * setData. Keeping the series object alive is what preserves the viewport.
 */

import "lightweight-charts";

const LWC = () => window.LightweightCharts;

/* ------------------------------------------------------------------ */
/* Band fill primitive                                                 */
/* ------------------------------------------------------------------ */

/**
 * Lightweight Charts has no fill-between-series, so this series primitive
 * paints the channel between two price arrays itself. Runs of consecutive
 * defined points are filled independently, so a gap in either edge breaks the
 * polygon instead of closing it across the hole.
 */
class BandFillRenderer {
  constructor(runs, color) {
    this._runs = runs;
    this._color = color;
  }

  draw(target) {
    target.useBitmapCoordinateSpace((scope) => {
      const ctx = scope.context;
      const hr = scope.horizontalPixelRatio;
      const vr = scope.verticalPixelRatio;
      ctx.save();
      ctx.fillStyle = this._color;
      for (const run of this._runs) {
        if (run.length < 2) continue;
        ctx.beginPath();
        ctx.moveTo(run[0].x * hr, run[0].upper * vr);
        for (let i = 1; i < run.length; i++) {
          ctx.lineTo(run[i].x * hr, run[i].upper * vr);
        }
        for (let i = run.length - 1; i >= 0; i--) {
          ctx.lineTo(run[i].x * hr, run[i].lower * vr);
        }
        ctx.closePath();
        ctx.fill();
      }
      ctx.restore();
    });
  }
}

class BandFillPaneView {
  constructor(source) {
    this._source = source;
    this._runs = [];
  }

  update() {
    const src = this._source;
    this._runs = [];
    if (!src._chart || !src._series) return;

    const timeScale = src._chart.timeScale();
    const series = src._series;
    let run = [];

    for (const point of src._points) {
      const x = timeScale.timeToCoordinate(point.time);
      const upper = point.upper == null ? null : series.priceToCoordinate(point.upper);
      const lower = point.lower == null ? null : series.priceToCoordinate(point.lower);
      if (x === null || upper === null || lower === null) {
        if (run.length > 1) this._runs.push(run);
        run = [];
        continue;
      }
      run.push({ x, upper, lower });
    }
    if (run.length > 1) this._runs.push(run);
  }

  renderer() {
    return new BandFillRenderer(this._runs, this._source._color);
  }

  // Under the candles, so a translucent channel never washes them out.
  zOrder() {
    return "bottom";
  }
}

class BandFill {
  constructor(color) {
    this._color = color;
    this._points = [];
    this._paneViews = [new BandFillPaneView(this)];
  }

  attached({ chart, series, requestUpdate }) {
    this._chart = chart;
    this._series = series;
    this._requestUpdate = requestUpdate;
  }

  detached() {
    this._chart = null;
    this._series = null;
    this._requestUpdate = null;
  }

  setPoints(points) {
    this._points = points;
    if (this._requestUpdate) this._requestUpdate();
  }

  setColor(color) {
    this._color = color;
    if (this._requestUpdate) this._requestUpdate();
  }

  updateAllViews() {
    this._paneViews.forEach((view) => view.update());
  }

  paneViews() {
    return this._paneViews;
  }
}

/* ------------------------------------------------------------------ */
/* Diffing                                                             */
/* ------------------------------------------------------------------ */

const FIELDS = ["value", "open", "high", "low", "close", "color"];

function samePoint(a, b) {
  if (a.time !== b.time) return false;
  for (const field of FIELDS) {
    if (a[field] !== b[field]) return false;
  }
  return true;
}

/**
 * Orders two Lightweight Charts times, which are either UNIX seconds or
 * "YYYY-MM-DD" business days. Returns NaN for anything else, so a caller that
 * cannot prove the ordering falls back to a full replace.
 */
function compareTime(a, b) {
  if (typeof a === "number" && typeof b === "number") return a - b;
  if (typeof a === "string" && typeof b === "string") return a < b ? -1 : a > b ? 1 : 0;
  return NaN;
}

/**
 * Decides how to get from `previous` to `next`.
 *
 * Returns `{mode: "none"}` when they already match, `{mode: "update", from}`
 * when `next` only revises its final bar and/or appends after it, and
 * `{mode: "set"}` when an earlier bar moved and only a full replace will do.
 *
 * `update()` refuses to touch anything before the series' last bar, so the
 * update path is taken only when the replacement tail is provably not older.
 * A two-point horizontal level whose right edge moves left — which is what
 * resampling 1m to 5m does to it — has to go through setData.
 */
export function planDataChange(previous, next) {
  if (!previous || !previous.length) return { mode: "set" };
  if (!next.length) return { mode: "set" };
  if (next.length < previous.length) return { mode: "set" };

  let i = 0;
  for (; i < previous.length; i++) {
    if (!samePoint(previous[i], next[i])) break;
  }
  if (i === previous.length && next.length === previous.length) return { mode: "none" };

  // Revising the final bar is allowed; anything earlier is not.
  if (i < previous.length - 1) return { mode: "set" };

  const lastTime = previous[previous.length - 1].time;
  const order = compareTime(next[i].time, lastTime);
  if (Number.isNaN(order)) return { mode: "set" };
  if (i === previous.length) return order > 0 ? { mode: "update", from: i } : { mode: "set" };
  return order >= 0 ? { mode: "update", from: i } : { mode: "set" };
}

function applyData(series, previous, next) {
  const plan = planDataChange(previous, next);
  if (plan.mode === "none") return plan.mode;
  if (plan.mode === "set") {
    series.setData(next);
    return plan.mode;
  }
  for (let i = plan.from; i < next.length; i++) {
    series.update(next[i]);
  }
  return plan.mode;
}

function shallowEqual(a, b) {
  if (a === b) return true;
  if (!a || !b) return false;
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  for (const key of keys) {
    if (a[key] !== b[key]) return false;
  }
  return true;
}

/* ------------------------------------------------------------------ */
/* Component                                                           */
/* ------------------------------------------------------------------ */

export default {
  template: `
    <div class="nq-chart-root" style="position:relative;width:100%;">
      <div ref="chart" style="width:100%;"></div>
      <div v-if="legend.length || readout" class="nq-chart-legend" style="
        position:absolute;top:8px;left:12px;z-index:3;pointer-events:none;
        font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;">
        <div v-if="readout" style="color:#d1d4dc;margin-bottom:2px;white-space:nowrap;">
          {{ readout }}
        </div>
        <div v-for="item in legend" :key="item.key" style="white-space:nowrap;">
          <span :style="{color:item.color}">&#9632;</span>
          <span style="color:#b2b5be;"> {{ item.label }}</span>
          <span v-if="item.value !== null" style="color:#d1d4dc;"> {{ item.value }}</span>
        </div>
      </div>
    </div>
  `,

  props: {
    height: { type: Number, default: 620 },
    volume_ratio: { type: Number, default: 0.22 },
    show_volume: { type: Boolean, default: true },
    show_candles: { type: Boolean, default: true },
    // The spec to draw on mount. A chart created in response to a user action is
    // not mounted yet when the server's first apply() would arrive, so that call
    // would be lost; the server keeps this prop equal to the last spec it applied.
    initial_spec: { type: Object, default: null },
  },

  data() {
    return {
      legend: [],
      readout: "",
    };
  },

  mounted() {
    const lwc = LWC();
    this.series = {};          // key -> { api, points, style, pane }
    this.bands = {};           // key -> { primitive, host, spec }
    this.spec = { series: {}, bands: {} };
    this.candlePoints = null;
    this.volumePoints = null;
    this.legendSpec = [];

    this.chart = lwc.createChart(this.$refs.chart, {
      autoSize: true,
      layout: {
        background: { color: "#161a25" },
        textColor: "#b2b5be",
        attributionLogo: false,
        panes: { separatorColor: "#2a2e39", separatorHoverColor: "#363a45" },
      },
      grid: {
        vertLines: { color: "#1f2430" },
        horzLines: { color: "#1f2430" },
      },
      crosshair: { mode: lwc.CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#2a2e39", scaleMargins: { top: 0.08, bottom: 0.08 } },
      timeScale: {
        borderColor: "#2a2e39",
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 6,
      },
    });
    this.$refs.chart.style.height = `${this.height}px`;

    // A line-only chart (the evaluation view) creates no candle series at all:
    // an empty candlestick series still tries to draw a last-value label and
    // throws when it has no value to show.
    if (this.show_candles) {
      this.candles = this.chart.addSeries(lwc.CandlestickSeries, {
        upColor: "#26a69a",
        downColor: "#ef5350",
        wickUpColor: "#26a69a",
        wickDownColor: "#ef5350",
        borderVisible: false,
        priceFormat: { type: "price", precision: 2, minMove: 0.25 },
      });
    }

    if (this.show_volume) {
      this.volume = this.chart.addSeries(
        lwc.HistogramSeries,
        { priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false },
        1,
      );
      this.applyPaneSizing();
    }

    this.chart.subscribeCrosshairMove(this.onCrosshair);
    this.refitUntil = 0;
    this.resizeObserver = new ResizeObserver(() => {
      if (performance.now() < this.refitUntil) {
        requestAnimationFrame(() => this.chart && this.chart.timeScale().fitContent());
      }
    });
    this.resizeObserver.observe(this.$refs.chart);
    if (this.initial_spec) this.apply(this.initial_spec);
  },

  beforeUnmount() {
    if (this.resizeObserver) this.resizeObserver.disconnect();
    if (this.chart) {
      this.chart.remove();
      this.chart = null;
    }
  },

  methods: {
    applyPaneSizing() {
      const panes = this.chart.panes();
      if (panes.length < 2) return;
      const volume = Math.max(0.05, Math.min(0.6, this.volume_ratio));
      panes[0].setStretchFactor(1 - volume);
      panes[1].setStretchFactor(volume);
    },

    /** Reconciles the whole chart against a desired-state spec. */
    apply(spec) {
      if (!this.chart) return;

      if (spec.options) this.chart.applyOptions(spec.options);
      if (spec.height && spec.height !== this.height) {
        this.$refs.chart.style.height = `${spec.height}px`;
      }

      const hadData =
        (this.candlePoints && this.candlePoints.length) || Object.keys(this.series).length;

      if (spec.candles && this.candles) {
        applyData(this.candles, this.candlePoints, spec.candles);
        this.candlePoints = spec.candles;
      }
      if (spec.volume && this.volume) {
        applyData(this.volume, this.volumePoints, spec.volume);
        this.volumePoints = spec.volume;
      }

      this.reconcileSeries(spec.series || {});
      this.reconcileBands(spec.bands || {});

      this.legendSpec = spec.legend || [];
      this.legend = this.legendSpec.map((item) => ({ ...item, value: null }));

      // Frame the data the first time it arrives; afterwards leave the user's
      // viewport alone, which is the whole point of reconciling in place.
      if (!hadData || spec.fit) {
        this.chart.timeScale().fitContent();
        // A chart that has only just been laid out may still be growing to its
        // final width; resizing keeps the bar spacing, which would leave the
        // fitted data bunched at the right. Refit on resizes shortly after.
        this.refitUntil = performance.now() + 1500;
      }
    },

    reconcileSeries(wanted) {
      const lwc = LWC();

      for (const key of Object.keys(this.series)) {
        if (!(key in wanted)) {
          this.detachBandsFor(key);
          this.chart.removeSeries(this.series[key].api);
          delete this.series[key];
        }
      }

      for (const [key, next] of Object.entries(wanted)) {
        const style = next.style || {};
        const options = {
          color: style.color,
          lineWidth: style.width || 1,
          lineStyle: style.dash || 0,
          title: style.title || "",
          priceLineVisible: false,
          lastValueVisible: !!style.axis_label,
          crosshairMarkerVisible: false,
          visible: style.visible !== false,
          lineVisible: style.line_visible !== false,
        };

        let entry = this.series[key];
        if (!entry) {
          const api = this.chart.addSeries(lwc.LineSeries, options, style.pane || 0);
          entry = { api, points: null, style: options };
          this.series[key] = entry;
          api.setData(next.points);
          entry.points = next.points;
          continue;
        }

        if (!shallowEqual(entry.style, options)) {
          entry.api.applyOptions(options);
          entry.style = options;
        }
        applyData(entry.api, entry.points, next.points);
        entry.points = next.points;
      }
    },

    detachBandsFor(seriesKey) {
      for (const [key, band] of Object.entries(this.bands)) {
        if (band.hostKey === seriesKey) {
          band.host.detachPrimitive(band.primitive);
          delete this.bands[key];
        }
      }
    },

    reconcileBands(wanted) {
      for (const key of Object.keys(this.bands)) {
        if (!(key in wanted)) {
          const band = this.bands[key];
          band.host.detachPrimitive(band.primitive);
          delete this.bands[key];
        }
      }

      for (const [key, spec] of Object.entries(wanted)) {
        const upper = this.series[spec.upper];
        const lower = this.series[spec.lower];
        if (!upper || !lower) continue;

        const points = [];
        const lowerByTime = new Map(lower.points.map((p) => [p.time, p.value]));
        for (const point of upper.points) {
          const other = lowerByTime.get(point.time);
          points.push({
            time: point.time,
            upper: point.value == null ? null : point.value,
            lower: other == null ? null : other,
          });
        }

        let band = this.bands[key];
        if (!band) {
          const primitive = new BandFill(spec.color);
          upper.api.attachPrimitive(primitive);
          band = { primitive, host: upper.api, hostKey: spec.upper };
          this.bands[key] = band;
        } else if (band.hostKey !== spec.upper) {
          band.host.detachPrimitive(band.primitive);
          upper.api.attachPrimitive(band.primitive);
          band.host = upper.api;
          band.hostKey = spec.upper;
        }
        band.primitive.setColor(spec.color);
        band.primitive.setPoints(points);
      }
    },

    onCrosshair(param) {
      if (!param || !param.time || !param.seriesData) {
        this.readout = "";
        this.legend = this.legendSpec.map((item) => ({ ...item, value: null }));
        return;
      }

      const bar = this.candles ? param.seriesData.get(this.candles) : null;
      if (bar) {
        const fmt = (v) => (v == null ? "—" : v.toFixed(2));
        this.readout =
          `O ${fmt(bar.open)}  H ${fmt(bar.high)}  L ${fmt(bar.low)}  C ${fmt(bar.close)}`;
      }

      this.legend = this.legendSpec.map((item) => {
        const entry = this.series[item.key];
        const point = entry ? param.seriesData.get(entry.api) : null;
        const value = point && point.value != null ? point.value.toFixed(2) : null;
        return { ...item, value };
      });
    },
  },
};
