# Radar Integrity V2 — unified implementation and acceptance contract

**Scope:** one follow-up PR to #39. No changes to main or production data until merged. No broker integration. The H083 prospective paper-validation criteria remain unchanged.

## Data path

1. Existing federal, congressional, SEC and private-investment collectors remain in place.
2. The corporate RSS pipeline accepts **exact matches or explicit user overrides**, not fuzzy matches, for listed tickers. Its existing statistical evidence table is left intact; this change may reduce newly accepted events.
3. Newly first-observed corporate headlines with explicitly directed economic relationships are copied to `economic_relationships`. Names, independent ticker matches, role, announcement state, first observation UTC, publication UTC, original news URL and a **PRIMARY_SOURCE_REQUIRED** status are persisted. Earlier historical headlines are not retrospectively assigned a newly invented source-first-seen time. An investor is not conflated with the investment recipient.
   A second, strictly read-only SEC metadata matching pass writes `economic_primary_review` when issuer ticker and filing date plausibly correspond to the same announcement. This is a **review candidate**, not transaction confirmation; the source filing must be inspected for both legal parties and terms.
4. `decision_audit` records observation-time context for new relationships and H083 calculations, chained with SHA-256. A repeated identical decision is idempotent; a changed payload with the same identity raises an error.
5. The after-close H083 watch and forward loop use Yahoo auto-adjusted daily OHLCV, with up to **24 bounded Stooq whole-series fallback** requests. **No stitching of adjusted/unadjusted bars, no future filling, no market-day interpolation.** Provider provenance, counts and errors are exposed. A total provider outage remains a fail-closed condition, not an excuse to invent prices.
6. The watch engine separates **valid price-feature coverage** from **strategy liquidity**. Liquidity failure is not misreported as a Yahoo outage. Less than 80% usable-price-feature coverage blocks new candidates. The 80% rule is a conservative operational threshold, **not an optimized alpha parameter**.
   Secondary OHLCV is permitted for **WATCH-only** diagnosis. If a candidate or pending fill would require Stooq's unverified price adjustment conventions, the H083 runner **refuses to freeze or resolve that paper fill** rather than mixing source conventions.
7. `h083_research_scorecard` summarizes **completed prospective** monthly net portfolio returns, cost-stressed returns, SPY excess, 3-month-block bootstrap lower estimate, drawdown and drift. These are portfolio percentage returns, **not R-multiples**, because H083 monthly rotations do not have trade-specific stops.
8. SPY 200-day trend and recent-vs-historical return volatility provide `RISK_ON`, `RISK_OFF`, `HIGH_STRESS` or `UNKNOWN` diagnostics. Non-risk-on, insufficient data, performance drift or missing forward validation can only **remove** information-only paper plans, never authorize them. The original forward evidence gate, frozen cohorts and completed month accounting are unchanged. The frozen forward cohorts and evidence gate are unchanged.
9. The shared H083 Streamlit page exposes the economics, source validity, costs, SPY-relative risk, coverage and decision audit. The optional Telegram extension sends research-only, unverified relationship notices and deduplicated quality/stress alerts. No action can submit orders.

## Critical provenance distinctions

| Claim | Required evidence | V2 treatment |
| --- | --- | --- |
| Headline announces a transaction | RSS headline + URL + timestamps | `HEADLINE_ONLY_UNVERIFIED` |
| Entity X invests in Y | Explicit directed text, independently resolved names | Named investor vs direct recipient |
| Binding contract signed/completed | Primary document with signed parties/date/terms | **Not automatically certified**; manual primary-source review required |
| Listed issuer benefits economically | Evidence of transaction and link from recipient/supplier to listed issuer | No automatic indirect-beneficiary claim |
| Forward market outperformance | 24 consecutive complete months, 60 positions, net costs, SPY excess, BH-adjusted significance and positive block-bootstrap lower bound | Existing H083 gate unchanged |
| Broker trade authorized | Separate broker-specific control, user authority and execution validation | **Never authorized** |

**Primary-source review remains a human/issuer-filing evidence task.** Automatic SEC cross-linking of unstructured disclosures could falsely certify a headline; this PR does not pretend that a coincident SEC mention proves the legal agreement.

## Operational failure and notification handling

- A green GitHub Actions job can be a scheduled **SKIPPED** attempt, not a forward-test observation. `h083_run_audit` and `h083_last_attempt` explicitly distinguish `SKIPPED`, `COMPLETED_RESEARCH`, and `FAILED`. A skip never increments 24-month validation or creates fictitious fills.
- On an invalid audit chain or low coverage, watch candidates are cleared and status reports why. `event_watch_exclusions` explains per-ticker reasons.
- Alerts are opt-in via the existing Telegram environment configuration; unverified headlines are prominently labeled **NOT BUY/SELL SIGNALS**. Successful deliveries are deduplicated; research health warnings are capped once per reason per UTC day.
- The data branch currently publishes **replaceable one-commit snapshots**. The local hash chain checks consistency *within the retained snapshot*; it is NOT immutable storage, a legally defensible archive or a third-party timestamp. An independent WORM/S3 Object Lock or timestamp service would be needed for stronger tamper resistance.

## Verification

Automated offline regressions:

- Synthetic OHLCV: bounded fallback, source attribution and corrupted/duplicate bar rejection.
- No conflation of illiquid stocks with missing OHLCV coverage.
- Explicit directed investment, supplier, partnership and agreement roles; ambiguous entity names and future publications rejected.
- Idempotent decision journal, hash-chain mutation detection and legitimate H083 clock skips.
- Closed-month-only net portfolio expectancy, SPY excess, regime as-of isolation, and persistent `broker_authorized=False`.
- Existing historical evidence and H083 24-month, 60-position frozen forward validation regression suite.

CI command: `PYTHONPATH=src pytest -q tests` (Python 3.12 via existing GitHub Actions).

**Offline tests do not validate live Stooq response stability, current Yahoo coverage, actual SEC contract language or expected future returns.** Once merged, watch the next eligible NYSE after-close run and verify `event_watch_status.json`, `h083_last_attempt.json`, `h083_research_scorecard`, `economic_relationships`, `decision_audit` and Telegram outputs. Avoid promoting WATCH to trade merely because an expected job succeeds.

## Post-merge rollback

To revert this PR, use GitHub's PR Revert flow on main. The new research tables on the separate data branch need not be deleted; before reuse of prior engine logic, archive the data snapshot and check the last known `h083_forward_status`. Never silently seed a backtest with new prospective ledger records.
