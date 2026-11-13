# Economic intelligence and research risk — staged rollout

This PR introduces **research-only primitives** and a fail-closed H083 coverage audit. It does not place orders, promote evidence, alter existing historical tables, change the UI, or automatically merge into main.

## P0 — H083 coverage audit

The watch status now includes `exclusion_reasons`, `data_coverage_ok`, and `decision`. When fewer than 80% of eligible symbols have usable features, `ok=false`, `reason=INSUFFICIENT_PRICE_COVERAGE`, and no candidates are published. The exclusion table remains available for diagnostics. An empty eligible universe is not treated as a price-provider failure, but still has no signals.

The observed November 13 snapshot had 140 eligible, 0 priced, 0 candidates. This change **detects** rather than claims to repair the upstream price issue. Check provider availability, as-of alignment, and exclusion reasons before altering feature thresholds. Never loosen liquidity thresholds to manufacture candidates.

The H083 forward runner also skips outside the exact 17 ET window. A green Actions check can mean a safe skip. Review the H083 log and persisted forward evidence separately; do not count skipped runs as valid forward months.

## P1 — directed economic relationships

`radar.economic_intelligence.extract_relationship` identifies only explicit directed investment and announced acquisition headlines, preserving observation time and source URL. `match_relationship` independently resolves both parties, refuses fuzzy ticker mappings, and sets `validated_alpha=false`. This is a deliberately narrow parser, not a production-grade SEC agreement reader. It does not replace the existing `corp_invest` pipeline and cannot label indirect sector beneficiaries as proven.

Next: add official-company and SEC primary-source confirmation, stable legal entity IDs, event deduplication, signed agreements versus plans versus completed transactions, and controlled replay with observation timestamps. Then integrate as a **separate**, versioned dataset, not by overwriting the existing `invest_events` table.

## P2 — expectancy and risk

`radar.research_risk.expectancy` reports descriptive, cost-adjusted R multiples. It makes no statistical alpha claim. `risk_gate` rejects unvalidated alpha, invalid inputs, insufficient cash, concentration, portfolio exposure, stop risk and insufficient reward/risk. It is a standalone research primitive; not wired to a broker or to the existing trade pipeline.

Next: benchmark-relative out-of-sample tests, costs in basis points and USD, spread/slippage scenarios, bootstrap confidence intervals, correlated holdings and gap risk. No automatic execution.

## P3/P4 — forward registry and reporting

Retain H083's frozen prospective decisions and its long-duration evidence gate. After validating the new relationship schema and risk metrics, add read-only dashboard/Telegram explanations showing observation time, source, beneficiary confidence, net expectancy and reasons for BLOCK. Existing UI and Telegram payloads remain unchanged in this PR.

## Review and acceptance

Run `PYTHONPATH=src pytest -q tests` in CI. Verify no production strategy thresholds, broker credentials, workflows, or `data` branch artifacts were changed. Review the coverage-fail-closed behavior before merging. This is the **first implementation stage**, not a claim that all P0–P4 work or live profitability is complete.
