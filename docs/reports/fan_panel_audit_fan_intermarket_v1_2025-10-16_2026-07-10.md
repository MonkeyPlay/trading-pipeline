# Point-in-time panel audit: fan_intermarket_v1, 2025-10-16 to 2026-07-10

181 development sessions (development), every collected instrument on the target's minute grid (forecaster/fan_panel.py). A value at a minute is the close of the instrument's last bar closed by then, on the day's active contract; its age is the minutes since that bar closed.

Code 197791214fb9; loaded in 4.4 s.

## Coverage and trading hours

| Instrument | Bars per session (median) | Trades (ET, most minutes in most sessions) | Sessions with no bar | Not complete in the store |
|---|---|---|---|---|
| DX | 1260 | 20:00-17:00 | none | none |
| ES | 1380 | 18:00-17:00 | none | none |
| NQ | 1380 | 18:00-17:00 | none | none |
| 10Y | 1376 | 18:00-17:00 | none | none |
| IWM | 960 | 18:00-20:00, 04:00-18:00 | none | none |
| QQQ | 960 | 18:00-20:00, 04:00-18:00 | none | none |
| RTY | 1380 | 18:00-17:00 | none | none |
| SMH | 960 | 18:00-20:00, 04:00-18:00 | none | none |
| SPY | 960 | 18:00-20:00, 04:00-18:00 | none | none |
| TNX | 400 | 08:15-15:00 | none | none |
| VIX | 810 | 03:15-09:15, 09:30-17:00 | none | none |
| VXN | 389 | 09:30-16:00 | none | none |

## How old the value is (median minutes since the last bar closed)

| Instrument | 18:00 | 03:00 | 08:00 | 09:29 | 09:45 | 12:00 | 16:00 | 16:59 |
|---|---|---|---|---|---|---|---|---|
| DX | 1.0 h | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| ES | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| NQ | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 10Y | 1.0 h | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| IWM | 0 | 7.0 h | 0 | 0 | 0 | 0 | 0 | 0 |
| QQQ | 0 | 7.0 h | 0 | 0 | 0 | 0 | 0 | 0 |
| RTY | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| SMH | 0 | 7.0 h | 0 | 0 | 0 | 0 | 0 | 0 |
| SPY | 0 | 7.0 h | 0 | 0 | 0 | 0 | 0 | 0 |
| TNX | 3.0 h | 12.0 h | 17.0 h | 0 | 0 | 0 | 1.0 h | 2.0 h |
| VIX | 1.0 h | 10.0 h | 0 | 15 | 0 | 0 | 0 | 0 |
| VXN | 2.0 h | 11.0 h | 16.0 h | 17.5 h | 0 | 0 | 1 | 1.0 h |

A stale value is carried, never filled in: features read its age beside it (chunk 4).
