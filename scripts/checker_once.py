"""Run one serialized price check, at the interval saved in STKREF settings."""
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def enabled(config):
    checker = config.get('priceChecker', {})
    categories = checker.get('categories', {})
    return checker.get('enabled', True) and any(categories.get(k, True) for k in ('ETF', 'Stock', 'Fund'))


def seconds_to_wait(config, snapshot, at=None):
    interval = int(config.get('priceChecker', {}).get('intervalMinutes', 6)) * 60
    if interval <= 0:
        raise ValueError('Checker interval must be positive')
    value = snapshot.get('last_check_completed_at') or snapshot.get('generated_at')
    if not isinstance(value, str) or not value:
        return 0
    try:
        completed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if completed.tzinfo is None:
            return 0
    except (TypeError, ValueError):
        return 0
    at = at or dt.datetime.now(dt.timezone.utc)
    # A future provider/check timestamp must not suspend the queue indefinitely.
    return min(interval, max(0, interval - (at - completed).total_seconds()))


def command(*args):
    subprocess.run(args, cwd=ROOT, check=True)


def sync():
    command('git', 'fetch', 'origin', 'main')
    command('git', 'reset', '--hard', 'origin/main')


def read_state():
    config = json.loads((ROOT / 'data/stock-tickers.json').read_text(encoding='utf-8'))
    try:
        snapshot = json.loads((ROOT / 'market-data.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        snapshot = {}
    return config, snapshot


def wait_until_due(sync_state=sync, read=read_state, pause=time.sleep, clock=None, forced=False):
    while True:
        sync_state()
        config, data = read()
        if not enabled(config) and not forced:
            print('Checker disabled in latest settings; this queued program exits without retrieving prices.', flush=True)
            return False
        delay = seconds_to_wait(config, data, clock() if clock else None)
        if delay <= 0:
            return True
        print(f'One-check program waiting {delay:.1f}s for the saved interval.', flush=True)
        pause(min(30, delay))


def main():
    forced = (os.environ.get('FORCE_PRICE_CHECK', 'false').lower() == 'true'
              or os.environ.get('REFRESH_ATH', 'false').lower() == 'true')
    if not wait_until_due(forced=forced):
        return 0
    command('git', 'config', 'user.name', 'github-actions[bot]')
    command('git', 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    for attempt in range(3):
        # One check per program. Regenerate only if another repository writer
        # advances main during publication; never merge a stale snapshot.
        if attempt:
            if not wait_until_due(forced=forced):
                return 0
        command(sys.executable, 'scripts/github_market_update.py')
        command('git', 'add', '--', 'market-data.json', 'data/stock-monitor.json')
        changed = subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=ROOT).returncode
        if changed == 0:
            break
        if changed != 1:
            raise RuntimeError('Could not inspect snapshot changes')
        command('git', 'commit', '-m', 'Publish one completed STKREF price check')
        pushed = subprocess.run(['git', 'push', 'origin', 'HEAD:main'], cwd=ROOT).returncode
        if pushed == 0:
            break
        if attempt == 2:
            raise RuntimeError('Could not publish snapshot after three attempts')
        print('Source advanced during publication; retrying from latest data.', flush=True)
    # Completion includes direct delivery. A failed copy makes this job fail
    # visibly; the next queued program retries delivery after its own check.
    command(sys.executable, 'scripts/copy_market_data.py')
    print('One price check and direct copy completed; this program now exits.', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
