# STKREF single-check program queue

The source of truth is `data/stock-tickers.json` in `sgkjeajk/stkref`.

- `priceChecker.intervalMinutes`: minimum time from the previous completed check to the next retrieval (currently 6 minutes).
- `priceChecker.refillThreshold`: count queued and running single-check programs; top up when the count is at or below this value (currently 5).
- `priceChecker.topUpJobs`: number of new programs to queue in a top-up (currently 10), not a target pool size.
- Each program performs exactly one check, publishes its snapshot and copies directly to EverydayCompass, then exits.
- The old `cycles` field and `checker_cycles` workflow input are compatibility fields only; new programs always perform one check.
- The old `maxActiveCheckers` field no longer determines concurrent retrieval. Only one price-checker workflow may execute at a time.

The Task workflow uses workflow-level `concurrency`, `cancel-in-progress: false` and `queue: max`. Other single-check programs remain pending rather than performing parallel repeated loops. Every new head program rereads the latest saved interval, settings and snapshot while waiting. It checks the due time again after publication conflicts. GitHub startup delays can make a check later than its due time.

Every price checker workflow executes `scripts/checker_queue.py` itself at startup and in an `if: always()` finish step. Each invocation refreshes settings from main, counts queued/running programs and adds the saved batch when the count is at or below the saved threshold. Full-batch replenishment therefore runs within the serialized price-checker workflow, without a downstream `workflow_run` dependency.

The separate five-minute/manual/push watchdog is a bootstrap only: when the entire queue is empty, it starts one seed price checker, whose own startup performs the normal full-batch top-up. At any nonzero count the bootstrap adds nothing, preventing a racing watchdog from adding a duplicate full batch. GitHub startup and cron delays are still possible; those schedules are not the primary continuity mechanism.

An incident on 29 Oct 2026 showed the previous batch finishing at 07:02 UTC with no remaining programs and no subsequent downstream top-up event. The successful direct copy at 07:02 UTC rules out the destination copy as the cause of that gap. This design moves the missing operation into each checker rather than relying on event chaining.


## Backup examined before implementation

`myai118/digital-tools`, branch `backup/stock-monitor-before-disable-refresh-2026-10-27`, commit `852a71f8bf4acb59b9632046a22ec3af0bd82d2d`:

- `.github/workflows/stock-monitor.yml` serialized workflow execution, but repeated checks inside one workflow and dispatched one successor at a low count.
- `data/stock-tickers.json` saved a six-minute interval and ten cycles.
- `scripts/github_market_update.py` nevertheless used a hard-coded ten-minute freshness/heartbeat interval.

The backup establishes that retrieval was serial. This implementation follows the clarified user requirement for ten separate queued single-check programs rather than restoring the backup's repeated-loop behavior.

GitHub's larger queue documentation: https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency#example-queueing-multiple-pending-runs

## Immediate successor after delivery failure

Before source publication, the program writes a pending delivery record in `data/checker-delivery.json` alongside the price snapshot in the same commit. Only after both destination JSON files have been checked/copied successfully does it acknowledge completed delivery. A failed copy leaves the pending marker and fails the workflow. The next waiting single-check program sees the pending marker for the same source snapshot, bypasses the normal interval, and forces a new retrieval instead of skipping fresh prices. It then attempts delivery of both files. No extra parallel price-checking process is launched; the next queued program takes over as soon as GitHub starts it. A completed delivery restores the saved interval. Disabling the checker still stops automatic retrieval.
