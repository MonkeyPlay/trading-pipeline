Appendix B Pre open field ownership
This mapping retains the full P1 output contract. The runtime may store richer evidence, but a display/export adapter owns the familiar 47 fields. P2's 40-field outcome export remains a separate adapter and must never overwrite this snapshot.
P1 fields	Owner and rule
1–3 Day, Weekday, Contract	Session identity and calendar; verified before any forecast
4–7 Previous RTH High, Low, Close, Overnight Open	Deterministic window calculations on a consistent contract
8–11 ON High, ON Low, Premarket High, Premarket Low	Deterministic extrema; separately specified windows and coverage
12 Price at 09:29	Only an actual eligible observation during that minute; never an earlier price renamed
13–14 Daily ATR and Two-Min ATR at 09:29	Versioned indicator configuration and frozen provenance; earlier profiles exported as unavailable under these exact time-specific names where not supported
15–19 Overnight, local, 5m, 15m and higher-timeframe structure	Frozen structure annotation with verified source timeframes and vocabularies
20–22 Price vs Long MA, Long MA Slope, Fast MA Alignment	Deterministic measurements plus declared classification convention
23–24 Premarket Pattern, Chop Score	Outcome-blind annotation under a frozen convention; null when indicators or coverage are unknown
25–30 Opening bias, first move, opening type, 15m direction, session type, close direction	LLM or named baseline prediction, target by target
31–33 Bullish, Bearish, Choppy Probability	Only the 15-minute direction distribution; percent display, fraction storage
34–35 First Level Tested and Price	Predicted reference identity, price resolved from frozen candidate set
36 Forecast Confidence	Evidence-based integer 1–5; separate from calibration
37–41 Analogue count, relation, dates, individual scores, mean similarity	Deterministic matcher and stored relations; mean is arithmetic
42–45 Upside and downside targets	Selected supported reference IDs resolved to prices and ordered by distance
46–47 Event Risk and Notes	Verified cutoff-safe event evidence and documented classification; unknown coverage is not Normal

The database version should store counterpart names such as cutoff_price and atr_2m_at_cutoff without mislabelling time. The Notion adapter should preserve unavailable time-specific properties and display the actual cutoff in provenance. Exact live Notion option names cannot be assumed from these files alone; schema verification is needed only if a Notion export feature is implemented.
