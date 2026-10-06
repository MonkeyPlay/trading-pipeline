# Features: fan_intermarket_v2, target NQ

137 features (forecaster/fan_features.py, format 3); code 70ef93ebe654. Measured on 153 development sessions before the first check (2025-07-21 to 2026-03-02), origins every 5 minutes, 15 minutes ahead: 41,768 rows.

**Coverage:** the share of rows with a value. **Rank correlation** (Spearman) with |z|, the realised move in the baseline's sigmas: positive means the baseline is too narrow when the feature is high. A univariate screen, not a model - a feature can matter only together with others.

## By group

| Group | Features | Median coverage | Strongest | Its rank correlation |
|---|---|---|---|---|
| base | 4 | 100 % | base.log_sigma | +0.036 |
| dollar | 11 | 68 % | DX.rv15 | +0.037 |
| index_futures | 22 | 86 % | RTY.rv15 | +0.109 |
| own | 11 | 100 % | NQ.rv15 | +0.104 |
| rates | 21 | 73 % | 10Y.rv240 | +0.047 |
| equity_etfs | 44 | 81 % | QQQ.rv15 | +0.088 |
| volatility | 24 | 59 % | VXN.rv5 | +0.120 |

## Every feature

| Feature | Group | Coverage | Rank correlation with abs(z) |
|---|---|---|---|
| base.log_sigma | base | 100.0 % | +0.036 |
| base.release_ahead | base | 100.0 % | +0.008 |
| base.minute | base | 100.0 % | +0.008 |
| base.weekday | base | 100.0 % | +0.030 |
| DX.rv5 | dollar | 68.3 % | +0.033 |
| DX.rv15 | dollar | 68.4 % | +0.037 |
| DX.rv60 | dollar | 68.4 % | +0.035 |
| DX.rv240 | dollar | 68.4 % | +0.037 |
| DX.ret15 | dollar | 68.4 % | +0.009 |
| DX.ret60 | dollar | 68.4 % | +0.023 |
| DX.chg | dollar | 68.0 % | +0.021 |
| DX.vol60 | dollar | 68.6 % | +0.032 |
| DX.age | dollar | 75.2 % | -0.008 |
| DX.day_rv | dollar | 68.0 % | +0.036 |
| DX.rv5d | dollar | 71.9 % | +0.024 |
| ES.rv5 | index_futures | 99.6 % | +0.097 |
| ES.rv15 | index_futures | 99.6 % | +0.105 |
| ES.rv60 | index_futures | 99.6 % | +0.090 |
| ES.rv240 | index_futures | 99.6 % | +0.100 |
| ES.ret15 | index_futures | 99.6 % | -0.066 |
| ES.ret60 | index_futures | 99.6 % | -0.083 |
| ES.chg | index_futures | 100.0 % | -0.069 |
| ES.vol60 | index_futures | 100.0 % | +0.076 |
| ES.age | index_futures | 100.0 % | - |
| ES.day_rv | index_futures | 100.0 % | +0.071 |
| ES.rv5d | index_futures | 100.0 % | -0.024 |
| NQ.rv5 | own | 99.6 % | +0.098 |
| NQ.rv15 | own | 99.6 % | +0.104 |
| NQ.rv60 | own | 99.6 % | +0.088 |
| NQ.rv240 | own | 99.6 % | +0.100 |
| NQ.ret15 | own | 99.6 % | -0.066 |
| NQ.ret60 | own | 99.6 % | -0.081 |
| NQ.chg | own | 100.0 % | -0.070 |
| NQ.vol60 | own | 100.0 % | +0.072 |
| NQ.age | own | 100.0 % | - |
| NQ.day_rv | own | 100.0 % | +0.071 |
| NQ.rv5d | own | 100.0 % | -0.021 |
| 10Y.rv5 | rates | 72.8 % | +0.033 |
| 10Y.rv15 | rates | 72.9 % | +0.033 |
| 10Y.rv60 | rates | 72.9 % | +0.037 |
| 10Y.rv240 | rates | 72.9 % | +0.047 |
| 10Y.ret15 | rates | 72.9 % | -0.001 |
| 10Y.ret60 | rates | 72.9 % | +0.003 |
| 10Y.chg | rates | 72.5 % | -0.026 |
| 10Y.vol60 | rates | 73.2 % | +0.043 |
| 10Y.age | rates | 79.7 % | -0.009 |
| 10Y.day_rv | rates | 72.5 % | +0.029 |
| 10Y.rv5d | rates | 76.5 % | +0.018 |
| IWM.rv5 | equity_etfs | 54.2 % | +0.067 |
| IWM.rv15 | equity_etfs | 54.9 % | +0.076 |
| IWM.rv60 | equity_etfs | 57.6 % | +0.052 |
| IWM.rv240 | equity_etfs | 68.6 % | +0.052 |
| IWM.ret15 | equity_etfs | 54.9 % | -0.058 |
| IWM.ret60 | equity_etfs | 57.6 % | -0.061 |
| IWM.chg | equity_etfs | 83.0 % | -0.036 |
| IWM.vol60 | equity_etfs | 83.7 % | +0.024 |
| IWM.age | equity_etfs | 89.9 % | +0.004 |
| IWM.day_rv | equity_etfs | 83.0 % | +0.070 |
| IWM.rv5d | equity_etfs | 86.9 % | -0.011 |
| QQQ.rv5 | equity_etfs | 64.8 % | +0.079 |
| QQQ.rv15 | equity_etfs | 65.6 % | +0.088 |
| QQQ.rv60 | equity_etfs | 68.9 % | +0.063 |
| QQQ.rv240 | equity_etfs | 82.1 % | +0.062 |
| QQQ.ret15 | equity_etfs | 65.6 % | -0.068 |
| QQQ.ret60 | equity_etfs | 68.9 % | -0.074 |
| QQQ.chg | equity_etfs | 100.0 % | -0.065 |
| QQQ.vol60 | equity_etfs | 100.0 % | +0.035 |
| QQQ.age | equity_etfs | 100.0 % | +0.002 |
| QQQ.day_rv | equity_etfs | 100.0 % | +0.065 |
| QQQ.rv5d | equity_etfs | 100.0 % | -0.018 |
| RTY.rv5 | index_futures | 65.8 % | +0.100 |
| RTY.rv15 | index_futures | 65.8 % | +0.109 |
| RTY.rv60 | index_futures | 65.8 % | +0.104 |
| RTY.rv240 | index_futures | 65.8 % | +0.105 |
| RTY.ret15 | index_futures | 65.8 % | -0.055 |
| RTY.ret60 | index_futures | 65.8 % | -0.067 |
| RTY.chg | index_futures | 65.4 % | -0.054 |
| RTY.vol60 | index_futures | 66.0 % | +0.078 |
| RTY.age | index_futures | 72.5 % | - |
| RTY.day_rv | index_futures | 65.4 % | +0.085 |
| RTY.rv5d | index_futures | 69.3 % | -0.015 |
| SMH.rv5 | equity_etfs | 64.8 % | +0.058 |
| SMH.rv15 | equity_etfs | 65.6 % | +0.068 |
| SMH.rv60 | equity_etfs | 68.9 % | +0.056 |
| SMH.rv240 | equity_etfs | 82.1 % | +0.052 |
| SMH.ret15 | equity_etfs | 65.6 % | -0.060 |
| SMH.ret60 | equity_etfs | 68.9 % | -0.058 |
| SMH.chg | equity_etfs | 100.0 % | -0.048 |
| SMH.vol60 | equity_etfs | 100.0 % | +0.029 |
| SMH.age | equity_etfs | 100.0 % | +0.002 |
| SMH.day_rv | equity_etfs | 100.0 % | +0.056 |
| SMH.rv5d | equity_etfs | 100.0 % | -0.004 |
| SPY.rv5 | equity_etfs | 63.1 % | +0.075 |
| SPY.rv15 | equity_etfs | 63.9 % | +0.083 |
| SPY.rv60 | equity_etfs | 67.1 % | +0.065 |
| SPY.rv240 | equity_etfs | 79.9 % | +0.063 |
| SPY.ret15 | equity_etfs | 63.9 % | -0.064 |
| SPY.ret60 | equity_etfs | 67.1 % | -0.076 |
| SPY.chg | equity_etfs | 96.7 % | -0.061 |
| SPY.vol60 | equity_etfs | 97.4 % | +0.034 |
| SPY.age | equity_etfs | 100.0 % | +0.002 |
| SPY.day_rv | equity_etfs | 96.7 % | +0.065 |
| SPY.rv5d | equity_etfs | 100.0 % | -0.012 |
| TNX.rv5 | rates | 29.7 % | +0.025 |
| TNX.rv15 | rates | 30.4 % | +0.034 |
| TNX.rv60 | rates | 33.7 % | +0.035 |
| TNX.rv240 | rates | 37.0 % | +0.039 |
| TNX.ret15 | rates | 30.4 % | -0.011 |
| TNX.ret60 | rates | 33.7 % | +0.003 |
| TNX.chg | rates | 49.8 % | -0.013 |
| TNX.age | rates | 100.0 % | -0.003 |
| TNX.day_rv | rates | 100.0 % | +0.020 |
| TNX.rv5d | rates | 100.0 % | -0.005 |
| VIX.rv5 | volatility | 56.4 % | +0.099 |
| VIX.rv15 | volatility | 57.9 % | +0.104 |
| VIX.rv60 | volatility | 59.3 % | +0.077 |
| VIX.rv240 | volatility | 59.3 % | +0.080 |
| VIX.ret15 | volatility | 57.9 % | +0.058 |
| VIX.ret60 | volatility | 59.3 % | +0.063 |
| VIX.chg | volatility | 100.0 % | +0.033 |
| VIX.age | volatility | 100.0 % | -0.007 |
| VIX.day_rv | volatility | 100.0 % | +0.065 |
| VIX.rv5d | volatility | 100.0 % | -0.027 |
| VIX.level | volatility | 100.0 % | +0.055 |
| VIX.iv_rv | volatility | 100.0 % | +0.058 |
| VXN.rv5 | volatility | 28.6 % | +0.120 |
| VXN.rv15 | volatility | 29.3 % | +0.119 |
| VXN.rv60 | volatility | 31.5 % | +0.082 |
| VXN.rv240 | volatility | 31.5 % | +0.055 |
| VXN.ret15 | volatility | 29.3 % | +0.078 |
| VXN.ret60 | volatility | 31.5 % | +0.062 |
| VXN.chg | volatility | 31.5 % | +0.059 |
| VXN.age | volatility | 100.0 % | +0.010 |
| VXN.day_rv | volatility | 100.0 % | +0.037 |
| VXN.rv5d | volatility | 100.0 % | -0.020 |
| VXN.level | volatility | 100.0 % | +0.049 |
| VXN.iv_rv | volatility | 100.0 % | +0.059 |
