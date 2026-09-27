# tests/test_first_hour.py
"""First-hour forecast (09:30-10:30) from pre-open matches: extraction of a session's
first hour, the forecast's widths, break probabilities and fan, and the backtest."""

import numpy as np
import pandas as pd
import pytest

from forecaster import first_hour as fh

OPEN = pd.Timestamp("2026-06-10 13:30", tz="UTC")        # 09:30 ET


def _bars(closes, highs=None, lows=None, start=OPEN, skip=()):
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(opens, closes) if highs is None else np.asarray(highs, float)
    lows = np.minimum(opens, closes) if lows is None else np.asarray(lows, float)
    idx = pd.date_range(start, periods=len(closes), freq="1min")
    df = pd.DataFrame({"timestamp_utc": idx.strftime("%Y-%m-%d %H:%M:%S"), "open": opens, "high": highs,
                       "low": lows, "close": closes})
    return df.drop(index=list(skip)).reset_index(drop=True)


def test_break_above_after_the_opening_range():
    # 15 flat minutes between 100 and 101, then a climb from minute 20.
    closes = [100.5] * 20 + list(np.linspace(101.5, 110, 40))
    highs = [101.0] * 15 + [100.8] * 5 + [c + 0.2 for c in closes[20:]]
    lows = [100.0] * 15 + [100.2] * 5 + [c - 0.2 for c in closes[20:]]
    h = fh.first_hour(_bars(closes, highs, lows), OPEN, "2026-06-10", 7)
    assert (h.or_high, h.or_low) == (101.0, 100.0)
    assert h.first_break == "above" and h.break_minute == 20 and h.contract_id == 7
    assert h.width == pytest.approx((110.2 - 100.0) / 100.5 * 100)
    assert len(h.path) == 60 and h.path[0] == pytest.approx(0.0)


def test_no_break_and_same_minute_break():
    flat = [100.5] * 60
    h = fh.first_hour(_bars(flat, [101.0] * 60, [100.0] * 60), OPEN, "d")
    assert h.first_break == "none" and h.break_minute is None
    highs, lows, closes = [101.0] * 60, [100.0] * 60, [100.5] * 60
    highs[30], lows[30], closes[30] = 102.0, 99.0, 99.5        # both sides in one minute: its close decides
    assert fh.first_hour(_bars(closes, highs, lows), OPEN, "d").first_break == "below"


def test_missing_bars():
    closes = [100.0 + 0.01 * i for i in range(60)]
    assert fh.first_hour(_bars(closes, skip=range(40, 50)), OPEN, "d") is None      # 50 of 60 < MIN_BARS
    assert fh.first_hour(_bars(closes, skip=[0]), OPEN, "d") is None                # no 09:30 bar
    gap = fh.first_hour(_bars(closes, skip=[30, 31]), OPEN, "d")
    assert gap is not None and gap.path[30] == gap.path[29]                          # previous close repeated


def _hour(day, width, side, path_end=0.0):
    return fh.FirstHour(session_date=day, open=100.0, or_high=100.5, or_low=99.5, high=100 + width / 2,
                        low=100 - width / 2, close=100 + path_end, path=np.linspace(0, path_end, 60),
                        or_width=width / 2, width=width, first_break=side, break_minute=20)


def test_forecast_widths_shrunk_breaks_and_fan():
    hours = {f"2026-01-{i + 1:02d}": _hour(f"2026-01-{i + 1:02d}", 1.0 + i / 10, "above" if i % 3 else "below",
                                          path_end=0.1 * i) for i in range(30)}
    ranked = sorted(hours, reverse=True)                      # any order; the first 20 are the matches
    fc = fh.forecast("2026-02-02", ranked, hours, anchor=20000.0, k=20)
    matches = [hours[d] for d in ranked[:20]]
    assert fc["n"] == 20 and fc["dates"] == ranked[:20]
    assert fc["width_pct"][50] == pytest.approx(np.median([m.width for m in matches]))
    assert fc["width_pts"][50] == pytest.approx(fc["width_pct"][50] / 100 * 20000.0)
    assert fc["naive_width_pct"] == pytest.approx(np.median([h.width for h in hours.values()][-40:]))
    counts = fc["first_break_counts"]
    assert sum(counts.values()) == 20
    base = fc["base_rates"]
    for side in fh.SIDES:                                       # shrunk toward the base rates
        raw = counts[side] / 20
        assert min(raw, base[side]) - 1e-9 <= fc["first_break"][side] <= max(raw, base[side]) + 1e-9
    assert sum(fc["first_break"].values()) == pytest.approx(1.0)
    assert fc["fan_pct"][10][-1] <= fc["fan_pct"][50][-1] <= fc["fan_pct"][90][-1]
    assert fc["fan_price"][50][0] == pytest.approx(20000.0)
    # later sessions are never used, and too few matches give no forecast
    assert fh.forecast("2026-01-05", ranked, hours, None) is None


def test_backtest_walks_forward(monkeypatch):
    from forecaster.analogue import MATCH_FEATURES
    days = [f"2026-{m:02d}-{d:02d}" for m in (1, 2, 3) for d in range(1, 29)]
    rng = np.random.default_rng(3)
    hours, snaps = {}, []
    for day in days:
        vol = float(rng.uniform(0.5, 2.0))
        hours[day] = _hour(day, vol * float(rng.uniform(0.9, 1.1)), str(rng.choice(fh.SIDES[::2])))
        snaps.append({"session_date": day, "features": {k: vol for k in MATCH_FEATURES}, "reference_values": {}})
    monkeypatch.setattr(fh, "load_first_hours", lambda conn, symbol: hours)
    monkeypatch.setattr(fh.store, "snapshots_before", lambda conn, before, fv, symbol: snaps)
    r = fh.backtest(None, "NQ", k=20, min_history=40)
    assert r["sessions"] == len(days) - 40
    # the range follows the pre-open volatility, so both estimators beat the usual range
    for key in ("range_matches", "range_regression"):
        assert r[key]["gain"] > 2 * r[key]["gain_se"]
    # the side is random here: no gain beyond noise
    assert abs(r["first_break"]["gain"]) < 3 * r["first_break"]["gain_se"] + 0.02
    text = fh.format_backtest(r)
    assert "regression" in text and "calibrated" in text


def test_a_first_hour_that_never_moved_is_not_a_session():
    # placeholder bars: 60 identical prices (the backtest used to divide by its zero range)
    assert fh.first_hour(_bars([100.0] * 60, [100.0] * 60, [100.0] * 60), OPEN, "d") is None



def _record(day, actual, matches, regression, usual, z80, z50):
    return {"day": day, "actual": actual, "estimates": {"matches": matches, "regression": regression, "usual": usual},
            "z80": z80, "z50": z50, "first_break": "above", "p_matches": {}, "p_base": {}}


def test_calibration_uses_earlier_sessions_only():
    recs = [_record(f"2026-01-{i + 1:02d}", 1.0, 1.5, 1.05, 1.2, z80=1.0 + i / 100, z50=0.5 + i / 100)
            for i in range(fh.MIN_CALIBRATION + 5)]
    assert fh.calibration(recs, "2026-01-10")["method"] == "matches"       # too few earlier: defaults
    assert fh.calibration(recs, "2026-01-10")["fan80"] == 1.0
    c = fh.calibration(recs, "2026-12-31")
    assert c["n"] == len(recs) and c["method"] == "regression"             # smallest error: |log 1/1.05|
    assert c["band50"] == pytest.approx(abs(np.log(1 / 1.05)))
    z80 = np.array([r["z80"] for r in recs])
    assert c["fan80"] == pytest.approx(np.quantile(z80, 0.8))
    # a later session never enters an earlier day's calibration
    later = recs + [_record("2027-01-01", 1.0, 1.0, 9.0, 9.0, 99.0, 99.0)]
    assert fh.calibration(later, "2026-12-31") == c


def test_calibrated_fan_and_side_residuals():
    fan = {10: np.array([-2.0]), 25: np.array([-1.0]), 50: np.array([0.0]), 75: np.array([0.5]), 90: np.array([1.0])}
    wide = fh.calibrated_fan(fan, 2.0, 1.5)
    assert (wide[10][0], wide[25][0], wide[50][0], wide[75][0], wide[90][0]) == (-4.0, -1.5, 0.0, 0.75, 2.0)
    ends = {q: v[0] for q, v in fan.items()}
    assert fh._z(0.5, ends, 10, 90) == pytest.approx(0.5)            # above the median: the upper half counts
    assert fh._z(-1.0, ends, 10, 90) == pytest.approx(0.5)           # below: the lower half
    assert fh._z(1.0, ends, 25, 75) == pytest.approx(2.0)
