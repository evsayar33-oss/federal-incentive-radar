# H083 independent OHLCV fallback — Twelve Data (research-only)

This is an **optional** independent data provider layered after the existing
Yahoo batch and single-symbol retry, before the existing Stooq fallback.
Its absence or refusal never changes the existing paper/trade decisions.

## One-time activation

1. Register for a Twelve Data Basic account at https://twelvedata.com/register
   and obtain your personal API key from https://twelvedata.com/account/api-keys.
2. In GitHub repository `evsayar33-oss/federal-incentive-radar`, open
   **Settings > Secrets and variables > Actions > New repository secret**.
3. Add exactly **`TWELVE_DATA_API_KEY`** as the secret name. Paste the actual
   provider key as its value. Never commit the key or send it in Telegram.
4. After merging the PR and configuring the secret, let the regular scheduled
   after-close Radar run publish `data/event_watch_status.json`. Inspect
   `price_download.independent_provider`,
   `independent_attempted`, `independent_recovered`,
   `independent_rejected`, `providers`,
   `data_coverage_pct` and `data_coverage_ok`. The H083 forward job may
   also use it for research screening, but **not** for new paper fills.

Without the secret: `independent_provider=DISABLED_NO_API_KEY`, no Twelve
Data requests are made, and prior Yahoo → Stooq behavior remains operational.

## Resource and provenance constraints

- Twelve Data Basic is advertised at **8 API credits/minute, 800/day**
  (subject to provider changes and eligibility restrictions).
- The HTTP host is paced at **8.5 seconds/request**, and the independent
  provider has a hard **5-symbol attempt cap per scan**. No automatic
  rate-limit retry loops. The upstream Stooq fallback keeps the original
  shared 24-symbol recovery ceiling.
- Only USD daily OHLCV from the US/Eastern exchange timezone, with matching
  symbol identity, is accepted; the API key is sent in an HTTP Authorization
  header, never in URL query parameters or evidence logs.
- `adjust=all` requests splits and dividend adjustments to facilitate
  comparison with Yahoo's `auto_adjust=True`; if the free tier rejects
  this setting, the provider fails closed and the next fallback is used.
- An entire replacement series must satisfy the **last completed benchmark
  close**, latest positive volume, every required benchmark session for the
  200-close feature window, and a close overlap agreement of ≤2% where the
  primary series exists. No missing-session fabrication, merging individual
  bars, interpolation, backdated signals or adjustment-convention guessing.
- When the primary is absent, a full independently recovered series is
  labeled `TWELVE_DATA_INDEPENDENT_NO_PRIMARY_OVERLAP`.
- **ALL Twelve Data and Stooq sourced symbols are barred from generating
  forward-paper fill approvals or source-only claims of validated alpha.**
  They may improve non-trading WATCH coverage only.
- Historical results, immutable observation dates, strategy thresholds, 80%
  coverage gate, existing research state and production rules are unchanged.

## Operational limitations

This provider requires a **user-created API key**, and therefore cannot be
declared active or empirically confirmed until the secret is installed and a
real data run has completed. A request for a delisted OTC ticker, unsupported
symbol or missing official close may be legitimately rejected by all vendors.
No backup can guarantee 100% historical OHLCV coverage.

The free Basic license is for eligible personal/internal non-commercial use;
review Twelve Data's terms and market data redistribution restrictions before
using it in a public/commercial deployment:
https://twelvedata.com/terms
https://twelvedata.com/pricing

Provider API documentation:
https://twelvedata.com/docs
