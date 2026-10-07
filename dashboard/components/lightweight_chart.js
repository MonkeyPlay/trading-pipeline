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
/* Time shade primitive                                                */
/* ------------------------------------------------------------------ */

/**
 * Paints full-height background bands over time ranges - the minutes shown
 * around the regular session. Each range runs from its first bar to its last,
 * widened by half a bar on either side so adjacent bars are covered edge to edge.
 */
class TimeShadeRenderer {
  constructor(rects, color) {
    this._rects = rects;
    this._color = color;
  }

  draw(target) {
    target.useBitmapCoordinateSpace((scope) => {
      const ctx = scope.context;
      const hr = scope.horizontalPixelRatio;
      ctx.save();
      ctx.fillStyle = this._color;
      for (const r of this._rects) {
        ctx.fillRect(Math.round(r.x0 * hr), 0, Math.max(1, Math.round((r.x1 - r.x0) * hr)),
                     scope.bitmapSize.height);
      }
      ctx.restore();
    });
  }
}

class TimeShadePaneView {
  constructor(source) {
    this._source = source;
    this._rects = [];
  }

  update() {
    const src = this._source;
    this._rects = [];
    if (!src._chart) return;
    const timeScale = src._chart.timeScale();
    const half = (timeScale.options().barSpacing || 6) / 2;
    for (const range of src._ranges) {
      const a = timeScale.timeToCoordinate(range.from);
      const b = timeScale.timeToCoordinate(range.to);
      if (a === null || b === null) continue;
      this._rects.push({ x0: Math.min(a, b) - half, x1: Math.max(a, b) + half });
    }
  }

  renderer() {
    return new TimeShadeRenderer(this._rects, this._source._color);
  }

  zOrder() {
    return "bottom";
  }
}

class TimeShade {
  constructor() {
    this._ranges = [];
    this._color = "rgba(120, 123, 134, 0.14)";
    this._paneViews = [new TimeShadePaneView(this)];
  }

  attached({ chart, requestUpdate }) {
    this._chart = chart;
    this._requestUpdate = requestUpdate;
  }

  detached() {
    this._chart = null;
    this._requestUpdate = null;
  }

  setRanges(ranges, color) {
    this._ranges = ranges || [];
    if (color) this._color = color;
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
/* Density fan primitive                                               */
/* ------------------------------------------------------------------ */

/**
 * The price fan (dashboard/components/fan.py) to the right of its origin candle:
 * per future candle one column whose vertical gradient follows the forecast
 * distribution of its close - colour stops at the issued quantiles, opacity
 * alpha x exp(-z^2 / 2) x the column's confidence - so the most likely price is
 * the most opaque and the whole fan fades where the model's measured skill does.
 * The median (the origin's price: the fan has no drift), the origin and the
 * scheduled releases ahead are drawn with it. Under the candles, like the shades.
 *
 * The learned fan (fan.model, dashboard/components/fan.py): on the candles of
 * the horizons it passed on the holdout, a bracket of its own colour - the 5-95 %
 * whisker, the 25-75 % box and the median - beside the fog it is compared with.
 * A recorded forecast (the forward record's issue from this origin) is solid and
 * labelled "rec"; one computed from the bars stored now is hollow, its whiskers
 * dashed.
 */
class DensityFanRenderer {
  constructor(view) {
    this._v = view;
  }

  draw(target) {
    const v = this._v;
    const fan = v.fan;
    if (!fan || (!v.cols.length && !v.origin)) return;
    target.useBitmapCoordinateSpace((scope) => {
      const ctx = scope.context;
      const hr = scope.horizontalPixelRatio;
      const vr = scope.verticalPixelRatio;
      const height = scope.bitmapSize.height;
      ctx.save();
      for (const c of v.cols) {
        const x0 = Math.round((c.x - v.half) * hr);
        const x1 = Math.round((c.x + v.half) * hr);
        const top = c.ys[c.ys.length - 1] * vr;        // the highest quantile is the top of the column
        const bottom = c.ys[0] * vr;
        if (bottom - top < 1) continue;
        const gradient = ctx.createLinearGradient(0, top, 0, bottom);
        for (let k = 0; k < c.ys.length; k++) {
          const offset = Math.min(1, Math.max(0, (c.ys[k] * vr - top) / (bottom - top)));
          const alpha = fan.alpha * Math.exp(-0.5 * fan.z[k] * fan.z[k]) * c.conf;
          gradient.addColorStop(offset, `rgba(${fan.rgb}, ${alpha.toFixed(4)})`);
        }
        ctx.fillStyle = gradient;
        ctx.fillRect(x0, Math.round(top), Math.max(1, x1 - x0), Math.max(1, Math.round(bottom - top)));
      }

      // The origin: a hairline through its candle.
      if (v.origin) {
        ctx.fillStyle = `rgba(${fan.rgb}, 0.35)`;
        ctx.fillRect(Math.round(v.origin.x * hr), 0, Math.max(1, Math.round(hr)), height);
      }

      // The median, fading with the columns.
      ctx.lineWidth = Math.max(1, Math.round(hr));
      let prev = v.origin;
      for (const c of v.cols) {
        if (prev) {
          ctx.strokeStyle = `rgba(${fan.rgb}, ${(0.8 * c.conf).toFixed(3)})`;
          ctx.beginPath();
          ctx.moveTo(prev.x * hr, prev.ym * vr);
          ctx.lineTo(c.x * hr, c.ym * vr);
          ctx.stroke();
        }
        prev = c;
      }

      // The learned fan's brackets.
      if (v.marks.length) {
        const rgb = fan.model_rgb || "77, 182, 255";
        const w = Math.max(2, Math.round(v.half * 0.9 * hr));
        ctx.lineWidth = Math.max(1, Math.round(1.5 * hr));
        ctx.font = `${Math.round(10 * vr)}px ui-monospace, SFMono-Regular, Menlo, monospace`;
        ctx.textBaseline = "bottom";
        for (const m of v.marks) {
          const x = Math.round(m.x * hr);
          const y5 = m.ys[fan.band[0]] * vr, y95 = m.ys[fan.band[1]] * vr;
          const y25 = m.ys[fan.quartiles[0]] * vr, y75 = m.ys[fan.quartiles[1]] * vr;
          const y50 = m.ys[fan.median] * vr;
          ctx.strokeStyle = `rgba(${rgb}, 0.95)`;
          ctx.setLineDash(m.recorded ? [] : [Math.round(3 * vr), Math.round(2 * vr)]);
          ctx.beginPath();
          ctx.moveTo(x, y95); ctx.lineTo(x, y75);
          ctx.moveTo(x, y25); ctx.lineTo(x, y5);
          ctx.moveTo(x - w / 2, y95); ctx.lineTo(x + w / 2, y95);
          ctx.moveTo(x - w / 2, y5); ctx.lineTo(x + w / 2, y5);
          ctx.stroke();
          ctx.setLineDash([]);
          if (m.recorded) {
            ctx.fillStyle = `rgba(${rgb}, 0.18)`;
            ctx.fillRect(x - w, Math.min(y75, y25), 2 * w, Math.abs(y25 - y75));
          }
          ctx.strokeRect(x - w, Math.min(y75, y25), 2 * w, Math.abs(y25 - y75));
          ctx.beginPath();
          ctx.moveTo(x - w, y50); ctx.lineTo(x + w, y50);
          ctx.stroke();
          ctx.fillStyle = `rgba(${rgb}, 0.95)`;
          ctx.fillText(`${m.minutes}m${m.recorded ? " rec" : ""}`, x - w, y95 - Math.round(3 * vr));
        }
      }

      // Scheduled releases ahead: where the fan widens.
      ctx.font = `${Math.round(11 * vr)}px ui-monospace, SFMono-Regular, Menlo, monospace`;
      ctx.textBaseline = "bottom";
      for (const r of v.releases) {
        const x = Math.round(r.x * hr);
        ctx.fillStyle = "rgba(255, 167, 38, 0.55)";
        for (let y = 0; y < height; y += Math.round(6 * vr)) {
          ctx.fillRect(x, y, Math.max(1, Math.round(hr)), Math.round(3 * vr));
        }
        ctx.fillStyle = "rgba(255, 167, 38, 0.9)";
        ctx.fillText(r.label, x + Math.round(4 * hr), height - Math.round(6 * vr));
      }
      ctx.restore();
    });
  }
}

class DensityFanPaneView {
  constructor(source) {
    this._source = source;
    this.fan = null;
    this.cols = [];
    this.origin = null;
    this.releases = [];
    this.marks = [];
    this.half = 3;
  }

  update() {
    const src = this._source;
    const fan = src._fan;
    this.fan = fan;
    this.cols = [];
    this.origin = null;
    this.releases = [];
    this.marks = [];
    if (!fan || !src._chart || !src._series) return;
    const timeScale = src._chart.timeScale();
    const series = src._series;
    this.half = (timeScale.options().barSpacing || 6) / 2;
    for (const c of fan.columns) {
      const x = timeScale.timeToCoordinate(c.time);
      if (x === null) continue;
      const ys = c.q.map((p) => series.priceToCoordinate(p));
      if (ys.some((y) => y === null)) continue;
      this.cols.push({ x, ys, ym: ys[fan.median], conf: c.conf });
    }
    const ox = timeScale.timeToCoordinate(fan.origin.time);
    const oy = series.priceToCoordinate(fan.origin.price);
    if (ox !== null && oy !== null) this.origin = { x: ox, ym: oy };
    for (const r of fan.releases) {
      const x = timeScale.timeToCoordinate(r.time);
      if (x !== null) this.releases.push({ x, label: r.label });
    }
    for (const m of (fan.model && fan.model.marks) || []) {
      const x = timeScale.timeToCoordinate(m.time);
      if (x === null) continue;
      const ys = m.q.map((p) => series.priceToCoordinate(p));
      if (ys.some((y) => y === null)) continue;
      this.marks.push({ x, ys, minutes: m.minutes, recorded: fan.model.source === "recorded" });
    }
  }

  renderer() {
    return new DensityFanRenderer(this);
  }

  zOrder() {
    return "bottom";
  }
}

class DensityFan {
  constructor() {
    this._fan = null;
    this._paneViews = [new DensityFanPaneView(this)];
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

  setFan(fan) {
    this._fan = fan || null;
    if (this._requestUpdate) this._requestUpdate();
  }

  /** The column at chart time `time`, for the crosshair readout. */
  columnAt(time) {
    if (!this._fan) return null;
    return this._fan.columns.find((c) => c.time === time) || null;
  }

  /** The learned fan's bracket at chart time `time`, if one is drawn there. */
  markAt(time) {
    const model = this._fan && this._fan.model;
    if (!model) return null;
    return model.marks.find((m) => m.time === time) || null;
  }

  updateAllViews() {
    this._paneViews.forEach((view) => view.update());
  }

  paneViews() {
    return this._paneViews;
  }

  /** The price scale makes room for the fan's 5-95 % band over the visible columns, not its faint tails. */
  autoscaleInfo(startTimePoint, endTimePoint) {
    const fan = this._fan;
    if (!fan || !this._chart) return null;
    const timeScale = this._chart.timeScale();
    let lo = Infinity;
    let hi = -Infinity;
    for (const c of fan.columns.concat((fan.model && fan.model.marks) || [])) {
      const i = timeScale.timeToIndex(c.time, false);
      if (i === null || i < startTimePoint || i > endTimePoint) continue;
      lo = Math.min(lo, c.q[fan.band[0]]);
      hi = Math.max(hi, c.q[fan.band[1]]);
    }
    return Number.isFinite(lo) ? { priceRange: { minValue: lo, maxValue: hi } } : null;
  }
}

/* ------------------------------------------------------------------ */
/* Projection trend primitive                                          */
/* ------------------------------------------------------------------ */

/**
 * The projection trend (dashboard/components/projection.py): one dashed cubic
 * Bezier curve from the last candle shown, 15 minutes ahead - a visual extrapolation of
 * TEMA 14 and EMA 14, not a forecast. Its four control points come in (minutes after the
 * candle's start, price); x is placed through the time scale's logical index (minutes /
 * timeframe past the candle's index), so it reaches past the last candle and keeps its
 * place whatever the zoom - a Bezier curve maps to a Bezier curve under the chart's linear
 * scales, so drawing the mapped control points draws the same curve.
 */
class TrendProjectionRenderer {
  constructor(view) {
    this._v = view;
  }

  draw(target) {
    const v = this._v;
    if (!v.points) return;
    target.useBitmapCoordinateSpace((scope) => {
      const ctx = scope.context;
      const hr = scope.horizontalPixelRatio;
      const vr = scope.verticalPixelRatio;
      const [p0, p1, p2, p3] = v.points.map((p) => [p.x * hr, p.y * vr]);
      ctx.save();
      ctx.strokeStyle = `rgba(${v.rgb}, 0.95)`;
      ctx.lineWidth = Math.max(1, Math.round(2 * hr));
      ctx.setLineDash([Math.round(6 * hr), Math.round(4 * hr)]);
      ctx.beginPath();
      ctx.moveTo(p0[0], p0[1]);
      ctx.bezierCurveTo(p1[0], p1[1], p2[0], p2[1], p3[0], p3[1]);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = `rgba(${v.rgb}, 0.95)`;
      ctx.beginPath();
      ctx.arc(p3[0], p3[1], Math.max(2, Math.round(2.5 * hr)), 0, 2 * Math.PI);
      ctx.fill();
      ctx.font = `${Math.round(10 * vr)}px ui-monospace, SFMono-Regular, Menlo, monospace`;
      ctx.textBaseline = "middle";
      // To the right of the curve's end; pulled back only as far as the pane's right edge needs.
      const gap = Math.round(6 * hr);
      const width = ctx.measureText(v.label).width;
      const x = Math.min(p3[0] + gap, scope.bitmapSize.width - width - gap);
      ctx.fillText(v.label, x, p3[1]);
      ctx.restore();
    });
  }
}

class TrendProjectionPaneView {
  constructor(source) {
    this._source = source;
    this.points = null;
  }

  update() {
    const src = this._source;
    const p = src._projection;
    this.points = null;
    if (!p || !src._chart || !src._series) return;
    const xs = src.logicals();
    if (!xs) return;
    const timeScale = src._chart.timeScale();
    const points = p.control.map((c, i) => ({ x: timeScale.logicalToCoordinate(xs[i]),
                                              y: src._series.priceToCoordinate(c[1]) }));
    if (points.some((q) => q.x === null || q.y === null)) return;
    this.points = points;
    this.rgb = p.rgb;
    this.label = p.label;
  }

  renderer() {
    return new TrendProjectionRenderer(this);
  }

  zOrder() {
    return "top";
  }
}

class TrendProjection {
  constructor() {
    this._projection = null;
    this._paneViews = [new TrendProjectionPaneView(this)];
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

  setProjection(projection) {
    this._projection = projection || null;
    if (this._requestUpdate) this._requestUpdate();
  }

  /** Each control point's logical x: the origin candle's index plus minutes / timeframe. */
  logicals() {
    const p = this._projection;
    if (!p || !this._chart) return null;
    const i = this._chart.timeScale().timeToIndex(p.time, false);
    if (i === null) return null;
    return p.control.map((c) => i + c[0] / p.tf);
  }

  updateAllViews() {
    this._paneViews.forEach((view) => view.update());
  }

  paneViews() {
    return this._paneViews;
  }

  /** The price scale keeps the curve (its sampled range, not the control points) in view while its start is. */
  autoscaleInfo(startTimePoint, endTimePoint) {
    const xs = this.logicals();
    if (!xs || xs[0] < startTimePoint || xs[0] > endTimePoint) return null;
    const [lo, hi] = this._projection.range;
    return { priceRange: { minValue: lo, maxValue: hi } };
  }
}

/* ------------------------------------------------------------------ */
/* Diffing                                                             */
/* ------------------------------------------------------------------ */

const FIELDS = ["value", "open", "high", "low", "close", "color", "wickColor"];

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
/* Linked charts                                                       */
/* ------------------------------------------------------------------ */

/**
 * Charts sharing a `sync_group` show the same time of day. Each spec carries
 * its `anchor` - the chart's own day at 00:00 on its wall clock - so sessions
 * on different days line up by clock time, bar for bar where both have bars.
 *
 * The user moving any chart (drag, wheel, pinch) moves the others. Anything else
 * that moves the group's lead chart (`sync_lead`: a new day, Fit, a resize) moves
 * them too, and a follower that moves by itself (new data, a resize) returns to
 * the lead's view. A chart the user is moving ignores the others, so two charts
 * never pull against each other.
 */
const GROUPS = new Map();      // group name -> Set of mounted charts
const USER_MS = 400;           // a range change this soon after the user's pointer or wheel is the user's

/** The time at logical bar index `x` of time-sorted `points`, extrapolated beyond them. */
export function timeAt(points, x) {
  const n = points.length;
  if (n === 1) return points[0].time + x * 60;
  if (x <= 0) return points[0].time + x * (points[1].time - points[0].time);
  if (x >= n - 1) return points[n - 1].time + (x - (n - 1)) * (points[n - 1].time - points[n - 2].time);
  const i = Math.floor(x);
  return points[i].time + (x - i) * (points[i + 1].time - points[i].time);
}

/** The logical bar index of time `t` in time-sorted `points`: the inverse of timeAt. */
export function indexAt(points, t) {
  const n = points.length;
  if (n === 1) return (t - points[0].time) / 60;
  if (t <= points[0].time) return (t - points[0].time) / (points[1].time - points[0].time);
  if (t >= points[n - 1].time) {
    return n - 1 + (t - points[n - 1].time) / (points[n - 1].time - points[n - 2].time);
  }
  let lo = 0;
  let hi = n - 1;              // points[lo].time <= t < points[hi].time
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (points[mid].time <= t) lo = mid;
    else hi = mid;
  }
  return lo + (t - points[lo].time) / (points[hi].time - points[lo].time);
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
    // Charts with the same group show the same time of day (see "Linked charts").
    sync_group: { type: String, default: null },
    sync_lead: { type: Boolean, default: false },
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
      this.shade = new TimeShade();
      this.candles.attachPrimitive(this.shade);
      this.fan = new DensityFan();
      this.candles.attachPrimitive(this.fan);
      this.projection = new TrendProjection();
      this.candles.attachPrimitive(this.projection);
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
    this.pendingRange = null;
    this.anchor = null;
    this.userUntil = 0;
    this.following = false;
    if (this.sync_group) this.joinGroup();
    this.resizeObserver = new ResizeObserver(() => {
      if (performance.now() < this.refitUntil) {
        requestAnimationFrame(() => this.frame());
      }
    });
    this.resizeObserver.observe(this.$refs.chart);
    if (this.initial_spec) this.apply(this.initial_spec);
  },

  beforeUnmount() {
    if (this.sync_group) this.leaveGroup();
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

    /** Frames the chart: the pending visible range when one was asked for, else everything. */
    frame() {
      if (!this.chart) return;
      if (this.pendingRange) {
        try {
          this.chart.timeScale().setVisibleRange(this.pendingRange);
        } catch (e) {
          this.chart.timeScale().fitContent();
        }
      } else {
        this.chart.timeScale().fitContent();
      }
    },

    /** Shows all the data (the Fit button). */
    fit() {
      if (!this.chart) return;
      this.pendingRange = null;
      this.chart.timeScale().fitContent();
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
      // The time window being looked at, to restore after the data is swapped
      // (another timeframe has other bars, so the bar-index viewport would jump) -
      // moved on by `shift_by` seconds when a live session's newest candle moved on.
      let keptRange = spec.keep_view && hadData ? this.chart.timeScale().getVisibleRange() : null;
      if (keptRange && spec.shift_by) {
        keptRange = { from: keptRange.from + spec.shift_by, to: keptRange.to + spec.shift_by };
      }

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
      if (this.shade) this.shade.setRanges(spec.shades || [], spec.shade_color);
      if (this.fan) this.fan.setFan(spec.fan);
      if (this.projection) this.projection.setProjection(spec.projection);

      this.legendSpec = spec.legend || [];
      this.legend = this.legendSpec.map((item) => ({ ...item, value: null }));
      this.anchor = spec.anchor == null ? null : spec.anchor;

      // Frame the data: the lead chart's time of day when asked to follow it, else
      // a requested time window, else the window being looked at, else everything
      // the first time data arrives (or on request). Otherwise leave the user's
      // viewport alone - the point of reconciling in place.
      const lead = spec.follow ? this.peers().find((chart) => chart.sync_lead) : null;
      if (lead) {
        this.pendingRange = null;
        this.refitUntil = 0;
        this.showClock(lead.clockRange());
      } else if (spec.visible_range) {
        this.pendingRange = spec.visible_range;
        this.frame();
        this.refitUntil = performance.now() + 1500;
      } else if (keptRange) {
        this.pendingRange = keptRange;
        this.frame();
      } else if (!hadData || spec.fit) {
        this.pendingRange = null;
        this.frame();
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

    joinGroup() {
      if (!GROUPS.has(this.sync_group)) GROUPS.set(this.sync_group, new Set());
      GROUPS.get(this.sync_group).add(this);
      this.markUser = (event) => {
        if (event.type === "pointermove" && !event.buttons) return;   // hovering moves only the crosshair
        this.userUntil = performance.now() + USER_MS;
      };
      for (const type of ["pointerdown", "pointermove", "pointerup", "wheel", "touchstart", "touchmove", "touchend"]) {
        this.$refs.chart.addEventListener(type, this.markUser, { passive: true, capture: true });
      }
      this.chart.timeScale().subscribeVisibleLogicalRangeChange(this.onRange);
    },

    leaveGroup() {
      const group = GROUPS.get(this.sync_group);
      if (!group) return;
      group.delete(this);
      if (!group.size) GROUPS.delete(this.sync_group);
    },

    /** The other charts of the group that have data to line up with. */
    peers() {
      const group = this.sync_group ? GROUPS.get(this.sync_group) : null;
      return group ? [...group].filter((chart) => chart !== this && chart.ready()) : [];
    },

    ready() {
      return !!(this.chart && this.anchor != null && this.candlePoints && this.candlePoints.length);
    },

    /** The visible range as seconds after this chart's own midnight. */
    clockRange() {
      const range = this.chart.timeScale().getVisibleLogicalRange();
      if (!range) return null;
      return {
        from: timeAt(this.candlePoints, range.from) - this.anchor,
        to: timeAt(this.candlePoints, range.to) - this.anchor,
      };
    },

    /** Shows the clock range of another chart of the group - unless the user is moving this one. */
    showClock(range) {
      if (!range || !this.ready() || performance.now() < this.userUntil) return;
      const from = indexAt(this.candlePoints, range.from + this.anchor);
      const to = indexAt(this.candlePoints, range.to + this.anchor);
      const shown = this.chart.timeScale().getVisibleLogicalRange();
      if (shown && Math.abs(shown.from - from) < 1e-3 && Math.abs(shown.to - to) < 1e-3) return;
      this.following = true;
      try {
        this.chart.timeScale().setVisibleLogicalRange({ from, to });
      } finally {
        this.following = false;
      }
    },

    onRange() {
      if (this.following || !this.ready()) return;
      const now = performance.now();
      if (now < this.userUntil || this.sync_lead) {
        if (now < this.userUntil) this.userUntil = now + USER_MS;   // still moving: a drag, a kinetic scroll
        const range = this.clockRange();
        for (const peer of this.peers()) peer.showClock(range);
        return;
      }
      const lead = this.peers().find((chart) => chart.sync_lead);
      if (lead) this.showClock(lead.clockRange());        // a follower that moved by itself
    },

    onCrosshair(param) {
      if (!param || !param.time || !param.seriesData) {
        this.readout = "";
        this.legend = this.legendSpec.map((item) => ({ ...item, value: null }));
        return;
      }

      const bar = this.candles ? param.seriesData.get(this.candles) : null;
      const fmt = (v) => (v == null ? "—" : v.toFixed(2));
      const column = this.fan ? this.fan.columnAt(param.time) : null;
      if (bar && bar.open !== undefined) {
        this.readout =
          `O ${fmt(bar.open)}  H ${fmt(bar.high)}  L ${fmt(bar.low)}  C ${fmt(bar.close)}`;
      } else if (column) {
        const f = this.fan._fan;
        this.readout = `Fan +${column.minutes} min  5% ${fmt(column.q[f.band[0]])}  ` +
          `50% ${fmt(column.q[f.median])}  95% ${fmt(column.q[f.band[1]])}`;
        const mark = this.fan.markAt(param.time);
        if (mark) {
          this.readout += `  ·  learned +${mark.minutes} min  5% ${fmt(mark.q[f.band[0]])}  ` +
            `95% ${fmt(mark.q[f.band[1]])} (x${mark.multiplier.toFixed(2)})`;
          if (mark.base) {
            this.readout += `  ·  v2 +${mark.minutes} min  5% ${fmt(mark.base[f.band[0]])}  ` +
              `95% ${fmt(mark.base[f.band[1]])}`;
          }
          this.readout += f.model.source === "recorded"
            ? `  ·  recorded ${f.model.recorded_at_et} ET` : "  ·  computed, not recorded";
        }
      } else {
        this.readout = "";
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
