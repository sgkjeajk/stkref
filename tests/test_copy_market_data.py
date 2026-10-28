import base64
import json
import unittest
from urllib.error import HTTPError
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from copy_market_data import copy_snapshot, validate


def snapshot(hour):
    return {'generated_at': f'2026-10-28T{hour:02d}:00:00Z',
            'last_check_completed_at': f'2026-10-28T{hour:02d}:00:00Z',
            'instruments': [{'ticker': 'D05.SI', 'current_price': 73.96}]}


class Destination:
    def __init__(self, current, conflicts=0):
        self.current = current
        self.conflicts = conflicts
        self.calls = []
        self.entries = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == '/git/ref/heads/main':
            return {'object': {'sha': 'head'}}
        if path.startswith('/contents/'):
            data = self.current if 'market-data.json' in path else {'source_dividend_generated_at': 'keep-me'}
            return {'content': base64.b64encode(json.dumps(data).encode()).decode()}
        if path == '/git/commits/head':
            return {'tree': {'sha': 'base-tree'}}
        if method == 'POST' and path == '/git/trees':
            self.entries = body['tree']
            return {'sha': 'new-tree'}
        if method == 'POST' and path == '/git/commits':
            return {'sha': 'new-commit'}
        if method == 'PATCH':
            if self.conflicts:
                self.conflicts -= 1
                self.current = snapshot(14)
                raise HTTPError('https://api.github.com', 422, 'Head advanced', {}, None)
            return {}
        raise AssertionError((method, path))


class CopyTests(unittest.TestCase):
    def test_atomic_copy_preserves_dividend_status(self):
        remote = Destination(snapshot(12))
        copy_snapshot(snapshot(13), remote)
        self.assertEqual([x['path'] for x in remote.entries], ['market-data.json', 'refresh-sync-status.json'])
        status = json.loads(remote.entries[1]['content'])
        self.assertEqual(status['source_dividend_generated_at'], 'keep-me')
        patch = [body for method, _, body in remote.calls if method == 'PATCH']
        self.assertEqual(patch, [{'sha': 'new-commit', 'force': False}])

    def test_never_overwrites_newer_or_equal_snapshot(self):
        for hour in (13, 14):
            remote = Destination(snapshot(hour))
            copy_snapshot(snapshot(13), remote)
            self.assertFalse(any(method != 'GET' for method, _, _ in remote.calls))

    def test_competing_newer_commit_wins_on_retry(self):
        remote = Destination(snapshot(12), conflicts=1)
        message = copy_snapshot(snapshot(13), remote, pause=lambda _: None)
        self.assertIn('newer', message)
        self.assertEqual(sum(method == 'PATCH' for method, _, _ in remote.calls), 1)

    def test_invalid_snapshot_is_rejected_before_network(self):
        remote = Destination(snapshot(12))
        data = snapshot(13)
        data['instruments'] = []
        with self.assertRaises(ValueError):
            copy_snapshot(data, remote)
        self.assertEqual(remote.calls, [])

    def test_auth_failure_is_not_retried(self):
        calls = []
        def denied(*args):
            calls.append(args)
            raise HTTPError('https://api.github.com', 403, 'Forbidden', {}, None)
        with self.assertRaises(HTTPError):
            copy_snapshot(snapshot(13), denied, pause=lambda _: None)
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
