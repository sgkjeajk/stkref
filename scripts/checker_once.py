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
    if snapshot.get('_copy_pending'):
        return 0  # A failed delivery advances the next queued program immediately.
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


def command(*args, **kwargs):
    subprocess.run(args, cwd=ROOT, check=True, **kwargs)


def sync():
    command('git', 'fetch', 'origin', 'main')
    command('git', 'reset', '--hard', 'origin/main')


def read_state():
    config = json.loads((ROOT / 'data/stock-tickers.json').read_text(encoding='utf-8'))
    try:
        snapshot = json.loads((ROOT / 'market-data.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        snapshot = {}
    try:
        delivery = json.loads((ROOT / 'data/checker-delivery.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        delivery = {}
    stamp = snapshot.get('last_check_completed_at') or snapshot.get('generated_at')
    snapshot['_copy_pending'] = (delivery.get('status') == 'pending'
                                 and delivery.get('source_market_generated_at') == stamp)
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


def record_delivery(status):
    data = json.loads((ROOT / 'market-data.json').read_text(encoding='utf-8'))
    stamp = data.get('last_check_completed_at') or data['generated_at']
    state = {'source_market_generated_at': stamp, 'status': status}
    path = ROOT / 'data/checker-delivery.json'
    path.write_text(json.dumps(state, indent=2) + '\n', encoding='utf-8')
    return stamp


def mark_delivered(expected):
    # Clear the pending marker only after BOTH destination files were checked
    # and copied successfully. Keep the marker on any delivery failure.
    for attempt in range(3):
        sync()
        _, data = read_state()
        stamp = data.get('last_check_completed_at') or data.get('generated_at')
        if stamp != expected:
            raise RuntimeError('Source snapshot changed before delivery acknowledgement')
        record_delivery('completed')
        command('git', 'add', '--', 'data/checker-delivery.json')
        changed = subprocess.run(['git','diff','--cached','--quiet'],cwd=ROOT).returncode
        if changed == 0:
            return
        if changed != 1:
            raise RuntimeError('Could not inspect delivery acknowledgement')
        command('git','commit','-m','Acknowledge completed direct market and dividend copy')
        if subprocess.run(['git','push','origin','HEAD:main'],cwd=ROOT).returncode == 0:
            return
    raise RuntimeError('Could not acknowledge direct delivery after three attempts')


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
        _, previous = read_state()
        retry_delivery = previous.get('_copy_pending', False)
        env = dict(os.environ)
        if retry_delivery:
            env['FORCE_PRICE_CHECK'] = 'true'
            print('Prior direct copy failed; new checker runs immediately without the normal interval.', flush=True)
        command(sys.executable, 'scripts/github_market_update.py', env=env)
        completed_stamp = record_delivery('pending')
        command('git', 'add', '--', 'market-data.json', 'data/stock-monitor.json', 'data/checker-delivery.json')
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
    mark_delivered(completed_stamp)
    print('One price check and direct copy completed; this program now exits.', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
