# Independent Alpha Discovery V1 — frozen research protocol

This track is separate from H083. **H083's 24-month forward register, workflows, approvals, and strategy thresholds are untouched.** Research here cannot authorize trades, mark a strategy as alpha-validated or change the evidence registry.

## Five fixed questions (not five proven strategies)

| ID | Exposure hypothesis | Eligibility |
| --- | --- | --- |
| FEDERAL_DIRECT_20D | Direct issuer receiving a federal award | event family = federal_award |
| POLICY_REGULATORY_20D | Directly linked listed issuer affected by regulation | event family = policy_regulatory |
| FEDERAL_PRESTRENGTH_20D | Award recipient with prior relative 20-session price strength vs SPY | previous 20 closes available before entry |
| FEDERAL_SECTOR_LEAD_20D | Award recipient whose mapped sector ETF led SPY before the event | sector ETF and benchmark daily prices observed |
| DIRECT_PRIVATE_RECIPIENT_20D | Direct named beneficiary of a private strategic investment | prospectively timestamped economic_relationships only; historical Google RSS backfills excluded |

These are hypothesis *families*, not a scanned parameter grid. Each has exactly the same fixed 20-session holding horizon and no tuning against holdout outcomes.

## Research and evidence policy

1. Ingest the immutable-in-process snapshot of the repo's **data branch**; artifact records the source commit SHA. That branch itself is replaceable, so results are reproducible only if the snapshot is retained.
2. Require explicit UTC evidence timestamps and source quality `exact_utc`/`first_seen_utc` for federal signals. Historical corporate RSS publication timestamps are **not** used as proof of first observation. For private direct recipients, require separately stored prospective `economic_relationships.observed_at_utc`.
3. Use SPY's observed trading calendar; enter a hypothetical position at the **first full trading-session close strictly after** the event's New York date. Exit at the close 20 SPY sessions later. Never interpolate a missing stock/benchmark bar.
4. Use 25 bps roundtrip hypothetical asset costs and 5 bps benchmark costs, chosen before inspecting outcomes. This is a simplified implementation, **not true venue-dependent execution or spread data**. Symbol shares, orders and stop levels are not generated.
5. Per hypothesis, eliminate overlapping entry/holding windows on the same ticker. This reduces but does not eliminate cross-ticker event-correlation.
6. **Development**: exits before 2025-01-01. **Embargo**: 2025-01-01 through 2025-01-31. **Holdout**: entries from 2025-02-01. This chronology is **not a true out-of-sample holdout for newly conceived hypotheses** if researchers have seen 2025–2026 prices, and the present-day ticker dataset suffers survivorship bias.
7. Require 40 independent-per-ticker-window observations in development, 40 in holdout, and 12 distinct holdout months. Use entry-month-cluster bootstrap lower 5% percentile and month-sign-flip one-sided permutation p-values. Apply Benjamini–Hochberg q≤0.05 **across all five predeclared families**, including those with insufficient data.
8. Even when these checks pass, label only `CANDIDATE_REQUIRES_PROSPECTIVE`, never `VALIDATED_ALPHA`. A genuinely newly recorded and frozen future cohort is needed for actual forward confirmation. No paper trade profit is assumed until its exit day has occurred.
9. Publish **every failed and passing hypothesis** and the entire eligible simulated observation ledger, not just the best performer.

## Critical limitations

- Present `prices.parquet` is a current-ticker universe, not a historical constituents/membership archive; survivorship/corporate-action provider adjustments can bias results.
- Calendar-close fills are idealizations. Real execution may be impossible at historical closes and costs can exceed 25 bps, especially in small caps.
- `first_seen_utc` may describe when the Radar ingested a source, not the actual original public release. That is conservative on timing, but not proof of causality.
- Overlapping US policy announcements, clustered industries, and repeated beneficiaries can violate classical independence; month clustering is only an approximation.
- A successful GitHub workflow or historically positive return is **not** forward alpha evidence.
- H083 remains isolated in `h083_forward.yml`, `h083_forward_*` and its validated paper-plan gate.

## Output locations

When the independent workflow runs, it emits:

- `hypothesis_scorecard.csv`: five full families and sample sufficiency / sign / multiple-testing outcome
- `observations.csv`: hypothetical close-to-close event-level returns with timing and costs
- `diagnostics.json`: feed acceptance/rejection counts, data provenance and limitations
- `data_snapshot.txt`: exact source `data` branch commit SHA

Artifacts expire according to GitHub Actions retention. The workflow does not push to `data` and therefore cannot corrupt active H083 research.

**Decision rule:** No evidence -> no profitable-strategy claim. Positive retrospective results -> prospective investigation only. True profitable alpha requires genuinely prospective, independently time-stamped observations and audited net outperforming benchmark through multiple regimes.
