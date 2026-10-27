# stkref
Refresh stock-monitor

## Copied refresh algorithm
Reference source: [`sgkjeajk/digital-tools`](https://github.com/sgkjeajk/digital-tools), branch `main`. Source files retrieved on 2026-10-27.

- `scripts/github_market_update.py`: refresh entry point; category filtering, market-open checks, recent-price checks, disabled-checker handling, and snapshot output.
- `scripts/market_service.py`: configuration validation, quote retrieval and fallback handling, and snapshot writing.
- `scripts/market_hours.py`: Singapore and US regular-session checks.
- `scripts/google_price_fallback.py`: fallback quote retrieval.
- `reference/stock-monitor.yml`: source GitHub Actions orchestration, including refresh cycles, wait interval, concurrency handling, and commits. It is under `reference/`, so GitHub Actions will not run it automatically.

This is a code reference, not a standalone deployed checker. The scripts expect configuration and output files such as `data/stock-tickers.json`, `market-data.json`, and `data/stock-monitor.json`. The workflow also calls production scripts and tests that are not included in this reference copy.
