# Instrument inventory

Measured from the database (forecaster/instrument_inventory.py), read-only. Receipt times count only when a bar was stored within 2 hours of its end (collected live); earlier history is a reconstruction, stored after the fact. Feed delay = receipt time minus the bar's end, the median of the daily medians over the latest 10 days collected live. 'At the cutoff' = a completed 1m bar ending in the 15 minutes before it; 'delivered' = such a bar received by the cutoff on a day collected live.

| instrument | type / exchange | value | days stored | complete days | missing bars | receipt times from (days) | feed delay (median, worst day) | first / last bar ET (median) | at 09:15: bar / delivered | at 09:29: bar / delivered | decision |
|---|---|---|---|---:|---:|---|---|---|---|---|---|
| 10Y | FUT CBOT | yield | 2025-09-03 to 2026-10-09 (278) | 278/278 | 0.0 % | 2026-10-06 (3) | 15.3 min, 51.5 min | 18:04 / 16:59 | 278/278 · 2/3 | 278/278 · 2/3 | included (optional) |
| DX | FUT NYBOT | price | 2025-09-12 to 2026-10-09 (271) | 271/271 | 0.0 % | 2026-10-06 (3) | 20.0 min, 54.3 min | 20:00 / 16:59 | 271/271 · 0/3 | 271/271 · 0/3 | included (optional) |
| ES | FUT CME | price | 2025-06-18 to 2026-10-09 (330) | 326/330 | 0.1 % | 2026-10-06 (3) | 15.2 min, 51.3 min | 18:00 / 16:59 | 279/279 · 2/3 | 279/279 · 2/3 | included (required) |
| IWM | STK SMART | - | 2025-05-23 to 2026-10-09 (347) | 347/347 | 0.0 % | 2026-10-06 (3) | 20.1 min, 55.8 min | 18:00 / 17:59 | 279/279 · 0/3 | 279/279 · 0/3 | excluded |
| NQ | FUT CME | price | 2025-06-18 to 2026-10-09 (330) | 326/330 | 0.1 % | 2026-10-06 (3) | 10.7 min, 15.2 min | 18:00 / 16:59 | 279/279 · 2/3 | 279/279 · 2/3 | target |
| QQQ | STK SMART | - | 2025-02-12 to 2026-10-09 (417) | 417/417 | 0.0 % | 2026-10-06 (3) | 19.9 min, 55.6 min | 18:00 / 17:59 | 279/279 · 0/3 | 279/279 · 0/3 | excluded |
| RTY | FUT CME | price | 2025-09-18 to 2026-10-09 (267) | 266/267 | 0.1 % | 2026-10-06 (3) | 15.2 min, 51.0 min | 18:00 / 16:59 | 267/267 · 2/3 | 267/267 · 2/3 | included (optional) |
| SMH | STK SMART | price | 2025-01-31 to 2026-10-09 (425) | 425/425 | 0.0 % | 2026-10-06 (3) | 20.0 min, 46.4 min | 18:00 / 17:59 | 279/279 · 0/3 | 279/279 · 0/3 | excluded |
| SPY | STK SMART | - | 2025-05-23 to 2026-10-09 (347) | 347/347 | 0.0 % | 2026-10-06 (3) | 20.0 min, 55.7 min | 18:00 / 17:59 | 279/279 · 0/3 | 279/279 · 0/3 | excluded |
| TNX | IND CBOE | yield | 2025-01-31 to 2026-10-09 (425) | 421/425 | 0.1 % | 2026-10-06 (3) | 15.2 min, 111.2 min | 08:20 / 14:59 | 279/279 · 0/3 | 279/279 · 0/3 | excluded |
| VIX | IND CBOE | index_level | 2024-07-29 to 2026-10-09 (553) | 553/553 | 0.0 % | 2026-10-06 (3) | 20.9 min, 55.1 min | 03:15 / 16:59 | 279/279 · 0/3 | 279/279 · 0/3 | included (optional) |
| VXN | IND CBOE | index_level | 2024-10-21 to 2026-10-09 (494) | 0/494 | 0.8 % | 2026-10-07 (2) | 15.2 min, 15.2 min | 09:31 / 15:59 | 0/279 · 0/2 | 0/279 · 0/2 | excluded |

## Contract history and rolls

- **10Y** (Micro 10-Year Yield futures, front month): 15 contract(s) stored; front of FGHJKMNQUVXZ, roll 3d before expiry (14 contract(s), 2025-09-03 to 2026-10-09); the asset registry's freshness limit 30 min; a proxy.
- **DX** (ICE US Dollar Index futures, front contract): 8 contract(s) stored; front of HMUZ, roll 10d before expiry (5 contract(s), 2025-09-12 to 2026-10-09); the asset registry's freshness limit 30 min; a proxy.
- **ES** (S&P 500 E-mini, front contract): 26 contract(s) stored; front of HMUZ, roll 8d before expiry (6 contract(s), 2025-06-18 to 2026-10-09); the asset registry's freshness limit 5 min.
- **IWM**: 1 contract(s) stored; single contract (1 contract(s), 2024-07-29 to 2026-10-09).
- **NQ** (Nasdaq-100 E-mini, front contract): 17 contract(s) stored; front of HMUZ, roll 8d before expiry (6 contract(s), 2025-06-18 to 2026-10-09); the asset registry's freshness limit 5 min.
- **QQQ**: 1 contract(s) stored; single contract (1 contract(s), 2024-07-29 to 2026-10-09).
- **RTY** (Russell 2000 E-mini, front contract): 15 contract(s) stored; front of HMUZ, roll 8d before expiry (5 contract(s), 2025-09-18 to 2026-10-09); the asset registry's freshness limit 5 min.
- **SMH** (VanEck Semiconductor ETF (regular close -> premarket)): 1 contract(s) stored; single contract (1 contract(s), 2024-07-29 to 2026-10-09); the asset registry's freshness limit 30 min.
- **SPY**: 1 contract(s) stored; single contract (1 contract(s), 2024-07-29 to 2026-10-09).
- **TNX** (10-year Treasury yield (Cboe TNX, yield x10)): 1 contract(s) stored; single contract (1 contract(s), 2024-07-29 to 2026-10-09); the asset registry's freshness limit 30 min.
- **VIX** (Cboe VIX spot index): 1 contract(s) stored; single contract (1 contract(s), 2024-07-29 to 2026-10-09); the asset registry's freshness limit 20 min.
- **VXN** (Cboe VXN spot index): 1 contract(s) stored; single contract (1 contract(s), 2024-07-29 to 2026-10-09); the asset registry's freshness limit 20 min.

## Inclusion and exclusion

- **10Y: included (optional).** Micro 10-Year Yield futures - rates. Trades overnight, unlike the cash TNX index (from 08:20 ET); quoted in yield, so changes are in yield units, not log returns.
- **DX: included (optional).** ICE US Dollar Index futures - the dollar. The only currency series collected (the cash DXY is not licensed to IB); a futures proxy; its feed is about 20 minutes late, so live it is often stale.
- **ES: included (required).** S&P 500 futures - the closest related market; NQ's beta and residual move against it. Same exchange, session and calendar as NQ, complete 1m history from 2025-06; the market NQ is most correlated with, so NQ's move net of it is the cleanest relative-performance measure.
- **IWM: excluded.** The cash ETF of RTY's index: redundant with RTY, thin premarket prints.
- **NQ: target.** the instrument forecast (direction_15m)
- **QQQ: excluded.** The cash ETF of NQ's own index: no information NQ futures lack, and thin premarket prints.
- **RTY: included (optional).** Russell 2000 futures - small caps; breadth and divergence. Same exchange and session as NQ; differs from it more than ES does (small caps vs. large tech), which is what a divergence measure needs; history from 2025-09-18, so earlier sessions are missing and imputed.
- **SMH: excluded.** Semiconductors are a large part of NQ; premarket ETF prints are thin and its feed about 20 minutes late - a candidate for a later feature set, not the compact first one.
- **SPY: excluded.** The cash ETF of ES's index: redundant with ES, thin premarket prints.
- **TNX: excluded.** The cash 10-year yield index starts at 08:20 ET; the 10-year yield future (10Y) covers the overnight.
- **VIX: included (optional).** Cboe VIX index - implied volatility. The only volatility series with pre-open values (global trading hours until 09:15 ET); its 09:14 bar is the latest value at a 09:29 cutoff, closed in between.
- **VXN: excluded.** No pre-open values (first bar 09:31 ET); VIX stands for implied volatility.
