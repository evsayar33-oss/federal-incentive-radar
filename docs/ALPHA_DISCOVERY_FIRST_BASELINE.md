# Alpha Discovery — first real-data baseline (13 November 2026)

Run: [GitHub Actions #43496593033](https://github.com/evsayar33-oss/federal-incentive-radar/actions/runs/43496593033)

Source: public `data` branch snapshot commit `9ea77104b6ba3bcf41539ec88e58c193ac9fd99d`. This is a read-only research extraction, **not a modification of H083**.

## Observed data inventory

- 3,483,076 distinct date–ticker closing-price rows
- 2,368 tickers after cleaning
- SPY history 2019-10-24 to **2026-11-10**
- 3,887 event/ticker feed rows
- 3,178 event rows rejected because the historical action date could not prove causal availability
- 619 corporate RSS rows rejected from causal backtest because a publication timestamp from retrospective Google News collection is **not** proof of earliest observation
- 90 directly linked federal events with usable as-observed timestamps
- 0 prospectively first-observed private beneficiaries in this published snapshot
- **0 matured 20-session event observations** in all five fixed hypotheses

**Interpretation:** No positive or negative 20-session prospective return can yet be estimated. Zero valid observations is **not zero profit**, not a negative result, and not evidence that any strategy beats SPY. The current data snapshot and timing make profitability estimates impossible.

## Follow-up from this baseline

- Freeze the five hypotheses and their costs as of **2026-11-13 19:54:31 UTC**. Events already known before that time are exploratory, never retroactively called prospective validation.
- Record new first-seen events in a separate independent prospective queue as the data feed updates.
- Wait for the **actual 20 SPY trading sessions**, not calendar days, to complete. Measure hypothetical next-close entry and 20-session exit with net asset and benchmark costs.
- Independently cluster prospective returns by month; collect at least 60 matured positions across 12 distinct future months; correct the family of five tests for multiplicity.
- Do not promote any winner to validated alpha without independently confirmed point-in-time market data, prospective records and realistic execution costs.

The weekly read-only Action in this PR re-runs analysis against each new published data snapshot after merge. It creates artifacts, but does not change the H083 workflow, production trading models, historical evidence registry or data branch.

## Caveat

This is a **baseline from an earlier commit** in this research PR. Subsequent commits refined the forward-preregistration status and reporting schema but did not alter the recorded historical data inventory or turn missing outcomes into returns. The final code must pass CI before the research PR is merged.
