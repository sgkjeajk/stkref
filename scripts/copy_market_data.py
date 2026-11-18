"""Copy a completed checker snapshot directly, without waiting for a sync job."""
import base64
import datetime as dt
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

DESTINATION = 'myai118/EverydayCompass'
TICKER_DESTINATION = 'myai118/digital-tools'
TICKER_PATH = 'data/stock-tickers.json'
BRANCH = 'main'
ROOT = Path(__file__).resolve().parents[1]


def stamp(data):
    value = data.get('last_check_completed_at') or data.get('generated_at')
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Snapshot timestamp must include a timezone')
    return parsed


def validate(data):
    stamp(data)
    if not isinstance(data.get('instruments'), list) or not data['instruments']:
        raise ValueError('Refusing to publish an empty or invalid market snapshot')


def validate_tickers(data):
    if not isinstance(data, dict):
        raise ValueError('Refusing to publish an invalid ticker configuration')
    revision = data.get('revision')
    if not isinstance(revision, int) or revision < 0:
        raise ValueError('Ticker configuration revision must be a non-negative integer')
    if not isinstance(data.get('instruments'), list) or not data['instruments']:
        raise ValueError('Refusing to publish an empty ticker configuration')


def github(token, destination=DESTINATION):
    def request(method, path, body=None):
        url = 'https://api.github.com/repos/' + destination + path
        headers = {'Authorization': 'Bearer ' + token,
                   'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28'}
        data = None if body is None else json.dumps(body).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    return request


def remote_json(request, path, revision, missing=None):
    try:
        result = request('GET', '/contents/' + path + '?ref=' + revision)
    except urllib.error.HTTPError as error:
        if error.code == 404 and missing is not None:
            return missing
        raise
    return json.loads(base64.b64decode(result['content']))


def copy_ticker_config(tickers, token, pause=time.sleep):
    """Copy the source ticker configuration to the live monitor repository."""
    validate_tickers(tickers)
    request = github(token, TICKER_DESTINATION)
    for attempt in range(3):
        try:
            try:
                current = remote_json(request, TICKER_PATH, BRANCH)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    current = {}
                else:
                    raise
            current_revision = current.get('revision', -1)
            if isinstance(current_revision, int) and current_revision >= tickers['revision']:
                return 'Live monitor ticker configuration is already current.'
            head = request('GET', '/git/ref/heads/' + BRANCH)['object']['sha']
            raw = request('GET', '/contents/' + TICKER_PATH + '?ref=' + head)
            content = json.dumps(tickers, indent=2) + '\n'
            request('PUT', '/contents/' + TICKER_PATH, {
                'message': 'Sync STKREF ticker configuration',
                'branch': BRANCH,
                'sha': raw['sha'],
                'content': base64.b64encode(content.encode('utf-8')).decode('ascii')
            })
            return 'Copied ticker configuration to ' + TICKER_DESTINATION + '.'
        except urllib.error.HTTPError as error:
            if error.code not in (409, 422, 429, 500, 502, 503, 504) or attempt == 2:
                raise
            pause(2 ** attempt)
    raise RuntimeError('Ticker configuration copy retries exhausted')


def copy_snapshot(data, request, pause=time.sleep, dividends=None, tickers=None):
    validate(data)
    if tickers is not None:
        validate_tickers(tickers)
    for attempt in range(3):
        try:
            head = request('GET', '/git/ref/heads/' + BRANCH)['object']['sha']
            current = remote_json(request, 'market-data.json', head)
            validate(current)
            market_newer = stamp(current) < stamp(data)
            dividend_newer = False
            ticker_newer = False
            if tickers is not None:
                current_tickers = remote_json(request, 'stock-tickers.json', head, missing={})
                current_revision = current_tickers.get('revision', -1)
                ticker_newer = not isinstance(current_revision, int) or current_revision < tickers['revision']
            if dividends is not None:
                current_dividends = remote_json(request, 'dividend-data.json', head, missing={})
                dividend_newer = (not current_dividends.get('generated_at')
                                  or stamp(current_dividends) < stamp(dividends))
            if not market_newer and not dividend_newer and not ticker_newer:
                return 'Destination already has equal or newer market, dividend and ticker data; nothing overwritten.'
            previous = remote_json(request, 'refresh-sync-status.json', head, missing={})
            status = {**previous,
                      'copied_at': dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')}
            files = [('refresh-sync-status.json', status)]
            if market_newer:
                status['source_market_generated_at'] = data['generated_at']
                files.insert(0, ('market-data.json', data))
            if dividend_newer:
                status['source_dividend_generated_at'] = dividends['generated_at']
                files.append(('dividend-data.json', dividends))
            if ticker_newer:
                status['source_ticker_revision'] = tickers['revision']
                files.append(('stock-tickers.json', tickers))
            commit = request('GET', '/git/commits/' + head)
            entries = [{'path': path, 'mode': '100644', 'type': 'blob',
                        'content': json.dumps(value, indent=2) + '\n'}
                       for path, value in files]
            tree = request('POST', '/git/trees', {'base_tree': commit['tree']['sha'], 'tree': entries})
            new_commit = request('POST', '/git/commits', {
                'message': 'Copy completed STKREF market, dividend and ticker data',
                'tree': tree['sha'], 'parents': [head]})
            # Non-forced update prevents overwriting other jobs' commits.
            request('PATCH', '/git/refs/heads/' + BRANCH,
                    {'sha': new_commit['sha'], 'force': False})
            return 'Checked market and dividend snapshots; copied newer data and timestamps in one destination commit.'
        except urllib.error.HTTPError as error:
            if error.code not in (409, 422, 429, 500, 502, 503, 504) or attempt == 2:
                raise
            pause(2 ** attempt)
    raise RuntimeError('Copy retries exhausted')


def main():
    token = os.environ.get('EVERYDAYCOMPASS_TOKEN', '').strip()
    if not token:
        print('::warning::Direct market copy unavailable: EVERYDAYCOMPASS_TOKEN is missing. '
              'Source refresh is saved; the next queued checker retries delivery.')
        return 2
    root = ROOT
    try:
        data = json.loads((root / 'market-data.json').read_text(encoding='utf-8'))
        ticker_path = root / 'data' / 'stock-tickers.json'
        tickers = json.loads(ticker_path.read_text(encoding='utf-8'))
        dividend_path = root / 'dividend-data.json'
        dividends = json.loads(dividend_path.read_text(encoding='utf-8'))
        stamp(dividends)  # Both source files are required before completion.
        validate_tickers(tickers)
        ticker_message = copy_ticker_config(tickers, token)
        market_message = copy_snapshot(data, github(token), dividends=dividends, tickers=tickers)
        print(ticker_message + ' ' + market_message))
        return 0
    except urllib.error.HTTPError as error:
        # Do not print response bodies or request headers containing credentials.
        print('Rejected endpoint: ' + error.url.split('?')[0])
        print('Required permissions: ' + error.headers.get('X-Accepted-GitHub-Permissions', 'not supplied'))
        print('::warning::Direct market copy failed: GitHub HTTP ' + str(error.code) +
              '. Source refresh is saved; the next queued checker retries delivery.')
    except Exception as error:
        print('::warning::Direct market copy failed (' + type(error).__name__ +
              '). Source refresh is saved; the next queued checker retries delivery.')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
