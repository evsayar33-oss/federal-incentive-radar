# Capacity and schedule protection

Repository Guardian runs daily and after changes to its own code. PRs run offline tests only. The trusted audit uses the repository-scoped GITHUB_TOKEN; no new paid service or account-wide token is required.

- Reads all artifact pages, cache usage, repository size and all branch trees. Read failures mean INCOMPLETE, never zero usage.
- Writes a small latest report to `ops-health:repository-health.json`. This branch is for operational metadata only. It is not a research checkpoint or a trading input.
- Operational warning budgets are 700 MiB Git repository, 400 MiB artifacts and 8 GiB cache. These are early-warning levels, NOT the account's actual billing limits. Daily growth estimates warn within 14 days of a budget.
- Re-enables ONLY `disabled_inactivity`, respecting manual disablement. Writes a tiny heartbeat to the default branch only when its latest commit is at least 30 days old. Same-provider failures can still stop this job: independent external monitoring is required.
- Before artifact expiry (7-day window), preserves the original ZIP and provenance in release assets. SHA-256 is verified by downloading the uploaded copy before a receipt is written. Processing is bounded (20 new archives/run, 512 MiB/file, 5-minute admission budget). Any backlog is reported. No original artifact is deleted early. GitHub's existing retention expires it normally. Consumers keep their existing access window.
- Release archives use ID buckets (at most 500 ZIPs and 500 receipts/release). Receipts include source artifact metadata and archive hash. Download the ZIP from the listed tag to recover the original artifact files.
- RAM/disk sampling runs every 10 seconds in Linux jobs that check out this repository. It writes constant-size state to runner temporary storage and reports in the job summary. It measures runner-wide available resources, not exact process peak RSS. A runner killed by OOM may not finish its final report. Telemetry failure does not cancel research or orders.
- No Git history rewrite, cache purge, data truncation, trading-rule change, or paid quota increase is performed.

## Remaining external boundaries

Account billing usage/plan/budgets require account access. Public standard runners being free does not imply unlimited storage. Releases, Actions, API rate limits and external data providers can fail. Expiring artifacts larger than the archive worker limit need a dedicated archive job. Continuous data growth eventually requires storage migration; warnings are not a guarantee of unlimited capacity. The daily report and external observer must be checked for freshness, not merely for a green previous run.

## Large source recovery

Files at least 25 MiB in any branch receive a release backup keyed by the exact Git blob SHA. The worker streams the raw blob to disk, verifies its Git identity and byte count, uploads the original bytes, downloads them again, and verifies SHA256 before writing a provenance receipt. Original files and branches remain untouched. The receipt records the original path; recovery downloads the `.bin` asset and restores that path after checking its digest.

At most four distinct blobs, each at most 100 MiB, are processed per run with a ten-minute admission budget. Unprotected large files remain alerts. Verified files become informational below 50 MiB; a 50 MiB operational warning or a 14-day growth projection still raises an alert even when a backup exists. These are early operational budgets, not promises of unlimited GitHub capacity. No history is truncated and no paid tier is enabled.
