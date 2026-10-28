# STKREF single-check program queue

The source of truth is `data/stock-tickers.json` in `sgkjeajk/stkref`.

- `priceChecker.intervalMinutes`: minimum time from the previous completed check to the next retrieval (currently 6 minutes).
- `priceChecker.refillThreshold`: count queued and running single-check programs; top up when the count is at or below this value (currently 5).
- `priceChecker.topUpJobs`: number of new programs to queue in a top-up (currently 10), not a target pool size.
- Each program performs exactly one check, publishes its snapshot and copies directly to EverydayCompass, then exits.
- The old `cycles` field and `checker_cycles` workflow input are compatibility fields only; new programs always perform one check.
- The old `maxActiveCheckers` field no longer determines concurrent retrieval. Only one price-checker workflow may execute at a time.

The Task workflow uses workflow-level `concurrency`, `cancel-in-progress: false` and `queue: max`. Other single-check programs remain pending rather than performing parallel repeated loops. Every new head program rereads the latest saved interval, settings and snapshot while waiting. It checks the due time again after publication conflicts. GitHub startup delays can make a check later than its due time.

The completion controller reads current settings after completed Task workflows, including failures, and has an independent five-minute recovery schedule. It serializes replenishment. At 0 remaining it adds 10; at 5 it adds 10 (total 15); at 6 it adds none. It counts queued, pending, waiting, requested and in-progress single-check runs, with pagination and deduplication. Obsolete parallel-loop runs are excluded from this count. Their old guard and provider entry point exit without retrieving prices so they cannot interfere during migration.

## Backup examined before implementation

`myai118/digital-tools`, branch `backup/stock-monitor-before-disable-refresh-2026-10-27`, commit `852a71f8bf4acb59b9632046a22ec3af0bd82d2d`:

- `.github/workflows/stock-monitor.yml` serialized workflow execution, but repeated checks inside one workflow and dispatched one successor at a low count.
- `data/stock-tickers.json` saved a six-minute interval and ten cycles.
- `scripts/github_market_update.py` nevertheless used a hard-coded ten-minute freshness/heartbeat interval.

The backup establishes that retrieval was serial. This implementation follows the clarified user requirement for ten separate queued single-check programs rather than restoring the backup's repeated-loop behavior.

GitHub's larger queue documentation: https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency#example-queueing-multiple-pending-runs
