# forecaster/instrument_inventory.py
"""
The instrument inventory (ML forecaster brief, point 2): what the database actually holds
for every collected instrument, measured from the stored bars rather than assumed.

    python scripts/nq_journal.py instrument-inventory      # writes docs/reports/instrument_inventory.md

Per instrument: asset type, exchange, currency and time zone (contracts), the registered
asset source (freshness limit, roll rule, proxy), the bar intervals and date range, the
trading days stored and their completeness (session_days: bars stored against bars
expected), when bars carry a receipt time from live collection (bars.first_stored_at
within LIVE_WINDOW of the bar's end) and the observed feed delay on those days, the
contract history, the hours it trades (first and last bar of the day, New York time),
and whether it has a completed bar just before each candidate forecast cutoff - in the
stored history (as a reconstruction) and on the days collected live (as delivered:
received by the cutoff).

Read-only. Nothing here decides which instruments the model uses; contracts/nq_ml.py
does, with its reasons.
"""

from __future__ import annotations

import statistics
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional, Sequence

from features import calendar as cal

LIVE_WINDOW = timedelta(hours=2)          # a bar stored later than this after its end was not collected live
CUTOFFS = (time(9, 15), time(9, 29))
RECENT_LIVE_DAYS = 10                     # the feed delay is measured over the latest days collected live


def _q(conn, sql: str, *args) -> List[tuple]:
    return conn.execute(sql, args).fetchall()


def _pct(xs: Sequence[float], p: float) -> Optional[float]:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]


def symbols(conn) -> List[str]:
    return [r[0] for r in _q(conn, "SELECT DISTINCT symbol FROM active_contracts ORDER BY symbol;")]


def inventory(conn, symbol: str, sessions_from: Optional[str] = None) -> Dict[str, Any]:
    """One instrument's inventory (see the module docstring)."""
    meta = _q(conn, "SELECT sec_type, exchange, currency, time_zone_id, count(*) OVER (), min(expiry) OVER (), "
                    "max(expiry) OVER () FROM contracts WHERE symbol = %s ORDER BY updated_at DESC LIMIT 1;", symbol)
    sec_type, exchange, currency, tz, n_contracts, first_expiry, last_expiry = meta[0] if meta else (None,) * 7
    src = _q(conn, "SELECT asset, value_kind, max_age_minutes, roll_rule, is_proxy, optional, description "
                   "FROM current_asset_sources WHERE symbol = %s ORDER BY asset LIMIT 1;", symbol)
    roll = _q(conn, "SELECT rule, count(*), min(trading_day), max(trading_day), count(DISTINCT contract_id) "
                    "FROM active_contracts WHERE symbol = %s GROUP BY rule ORDER BY count(*) DESC;", symbol)
    days = _q(conn, "SELECT s.trading_day, s.status, s.bar_count, s.expected_bar_count, s.fetched_at "
                    "FROM session_days s JOIN active_contracts a ON a.contract_id = s.contract_id "
                    "AND a.trading_day = s.trading_day AND a.symbol = %s "
                    "WHERE s.interval = '1m' AND s.price_type = 'TRADES' ORDER BY s.trading_day;", symbol)
    intervals = _q(conn, "SELECT b.interval, b.price_type, count(*) FROM bars b JOIN contracts k USING (contract_id) "
                         "WHERE k.symbol = %s GROUP BY 1, 2 ORDER BY 3 DESC;", symbol)
    expected = [(d[2] or 0, d[3]) for d in days if d[3]]
    complete = sum(1 for got, want in expected if got >= want)
    # receipt times: per day, the median delay of bars stored within the live window
    delays = _q(conn, """
        SELECT b.trading_day, percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM b.first_stored_at
               - (b.timestamp_utc + interval '1 minute'))), count(*) FILTER (WHERE b.first_stored_at IS NOT NULL),
               count(*)
          FROM bars b JOIN active_contracts a ON a.contract_id = b.contract_id AND a.trading_day = b.trading_day
         WHERE a.symbol = %s AND b.interval = '1m' AND b.price_type = 'TRADES'
           AND b.first_stored_at IS NOT NULL
           AND b.first_stored_at - (b.timestamp_utc + interval '1 minute') < %s
         GROUP BY b.trading_day ORDER BY b.trading_day;""", symbol, LIVE_WINDOW)
    live_days = [str(d[0]) for d in delays]
    recent = delays[-RECENT_LIVE_DAYS:]
    # the hours it trades and its bars just before each cutoff (the latest sessions)
    span = _q(conn, """
        SELECT b.trading_day, min(b.timestamp_utc), max(b.timestamp_utc), count(*)
          FROM bars b JOIN active_contracts a ON a.contract_id = b.contract_id AND a.trading_day = b.trading_day
         WHERE a.symbol = %s AND b.interval = '1m' AND b.price_type = 'TRADES'
           AND b.trading_day >= COALESCE(%s::date, '2000-01-01')
         GROUP BY b.trading_day ORDER BY b.trading_day;""", symbol, sessions_from)
    ny = lambda t: _utc(t).astimezone(cal.NY_TZ)
    starts = [ny(r[1]).strftime("%H:%M") for r in span]
    ends = [ny(r[2]).strftime("%H:%M") for r in span]
    at_cutoff: Dict[str, Any] = {}
    for c in CUTOFFS:
        have = delivered = considered = live_considered = 0
        ages = []
        for d in [str(r[0]) for r in span]:
            try:
                s = cal.session(d)
            except Exception:                                   # outside the calendar
                continue
            if not s.is_open:
                continue
            cutoff = cal.ny_instant(s.session_date, c)
            considered += 1
            row = _q(conn, """
                SELECT b.timestamp_utc, b.first_stored_at FROM bars b JOIN active_contracts a
                    ON a.contract_id = b.contract_id AND a.trading_day = b.trading_day
                 WHERE a.symbol = %s AND b.interval = '1m' AND b.price_type = 'TRADES'
                   AND b.timestamp_utc <= %s AND b.timestamp_utc > %s
                 ORDER BY b.timestamp_utc DESC LIMIT 1;""", symbol, cutoff - timedelta(minutes=1),
                     cutoff - timedelta(minutes=16))                # bars ending in the 15 minutes before it
            if row:
                have += 1
                ages.append((cutoff - (_utc(row[0][0]) + timedelta(minutes=1))).total_seconds() / 60)
            if d in live_days:
                live_considered += 1
                got = _q(conn, """
                    SELECT 1 FROM bars b JOIN active_contracts a
                        ON a.contract_id = b.contract_id AND a.trading_day = b.trading_day
                     WHERE a.symbol = %s AND b.interval = '1m' AND b.price_type = 'TRADES'
                       AND b.timestamp_utc <= %s AND b.timestamp_utc > %s AND b.first_stored_at <= %s LIMIT 1;""",
                         symbol, cutoff - timedelta(minutes=1), cutoff - timedelta(minutes=16), cutoff)
                delivered += bool(got)
        at_cutoff[c.strftime("%H:%M")] = {
            "sessions": considered, "with_bar_in_15min": have, "median_age_min": _pct(ages, 0.5),
            "live_days": live_considered, "delivered_by_cutoff": delivered}
    return {
        "symbol": symbol, "sec_type": sec_type, "exchange": exchange, "currency": currency, "time_zone": tz,
        "asset": src[0][0] if src else None, "value_kind": src[0][1] if src else None,
        "max_age_minutes": src[0][2] if src else None, "is_proxy": src[0][4] if src else None,
        "description": src[0][6] if src else None,
        "contracts": n_contracts, "expiries": [str(first_expiry), str(last_expiry)] if first_expiry else None,
        "roll": [{"rule": r[0], "days": r[1], "from": str(r[2]), "to": str(r[3]), "contracts": r[4]} for r in roll],
        "intervals": [{"interval": r[0], "price_type": r[1], "bars": r[2]} for r in intervals],
        "first_day": str(days[0][0]) if days else None, "last_day": str(days[-1][0]) if days else None,
        "days": len(days), "days_with_expected": len(expected), "days_complete": complete,
        "missing_bar_share": (None if not expected else
                              1 - sum(min(got, want) for got, want in expected) / sum(w for _, w in expected)),
        "statuses": _counts(d[1] for d in days),
        "live_days": len(live_days), "first_live_day": live_days[0] if live_days else None,
        "feed_delay_s": {"days": len(recent), "median_of_daily_medians": _pct([float(r[1]) for r in recent], 0.5),
                         "max_daily_median": max((float(r[1]) for r in recent), default=None)},
        "first_bar_et": {"median": _pct(starts, 0.5), "earliest": min(starts, default=None)},
        "last_bar_et": {"median": _pct(ends, 0.5), "latest": max(ends, default=None)},
        "at_cutoff": at_cutoff,
    }


def _counts(xs) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for x in xs:
        out[str(x)] = out.get(str(x), 0) + 1
    return out


def _utc(value) -> datetime:
    from datetime import timezone
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    t = datetime.fromisoformat(str(value).replace(" ", "T").replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def report(rows: Sequence[Dict[str, Any]], decisions: Dict[str, Dict[str, str]]) -> str:
    """The inventory as markdown, with each instrument's inclusion decision and reason (contracts/nq_ml.py)."""
    pct = lambda x: "-" if x is None else f"{100 * x:.1f} %"
    lines = ["# Instrument inventory", "",
             "Measured from the database (forecaster/instrument_inventory.py), read-only. Receipt times count only "
             "when a bar was stored within 2 hours of its end (collected live); earlier history is a "
             "reconstruction, stored after the fact. Feed delay = receipt time minus the bar's end, the median of "
             f"the daily medians over the latest {RECENT_LIVE_DAYS} days collected live. 'At the cutoff' = a "
             "completed 1m bar ending in the 15 minutes before it; 'delivered' = such a bar received by the cutoff "
             "on a day collected live.", "",
             "| instrument | type / exchange | value | days stored | complete days | missing bars | receipt times "
             "from (days) | feed delay (median, worst day) | first / last bar ET (median) | at 09:15: bar / "
             "delivered | at 09:29: bar / delivered | decision |",
             "|---|---|---|---|---:|---:|---|---|---|---|---|---|"]
    for r in rows:
        fd = r["feed_delay_s"]
        delay = ("-" if fd["median_of_daily_medians"] is None else
                 f"{fd['median_of_daily_medians'] / 60:.1f} min, {fd['max_daily_median'] / 60:.1f} min")
        cut = lambda c: (f"{r['at_cutoff'][c]['with_bar_in_15min']}/{r['at_cutoff'][c]['sessions']} · "
                         f"{r['at_cutoff'][c]['delivered_by_cutoff']}/{r['at_cutoff'][c]['live_days']}")
        d = decisions.get(r["symbol"], {})
        lines.append(f"| {r['symbol']} | {r['sec_type']} {r['exchange']} | {r['value_kind'] or '-'} | "
                     f"{r['first_day']} to {r['last_day']} ({r['days']}) | {r['days_complete']}/"
                     f"{r['days_with_expected']} | {pct(r['missing_bar_share'])} | {r['first_live_day'] or '-'} "
                     f"({r['live_days']}) | {delay} | {r['first_bar_et']['median']} / {r['last_bar_et']['median']} | "
                     f"{cut('09:15')} | {cut('09:29')} | {d.get('decision', '-')} |")
    lines += ["", "## Contract history and rolls", ""]
    for r in rows:
        rolls = "; ".join(f"{x['rule']} ({x['contracts']} contract(s), {x['from']} to {x['to']})" for x in r["roll"])
        name = r["description"] or r["asset"]
        lines.append(f"- **{r['symbol']}**" + (f" ({name})" if name else "") + f": {r['contracts']} contract(s) "
                     f"stored; {rolls or 'no assignment'}"
                     + ("" if r["max_age_minutes"] is None else
                        f"; the asset registry's freshness limit {r['max_age_minutes']} min")
                     + ("; a proxy" if r["is_proxy"] else "") + ".")
    lines += ["", "## Inclusion and exclusion", ""]
    for r in rows:
        d = decisions.get(r["symbol"], {})
        lines.append(f"- **{r['symbol']}: {d.get('decision', 'not assessed')}.** {d.get('reason', '')}")
    return "\n".join(lines) + "\n"
