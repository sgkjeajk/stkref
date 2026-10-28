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
BRANCH = 'main'


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


def github(token):
    def request(method, path, body=None):
        url = 'https://api.github.com/repos/' + DESTINATION + path
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


def copy_snapshot(data, request, pause=time.sleep):
    validate(data)
    for attempt in range(3):
        try:
            head = request('GET', '/git/ref/heads/' + BRANCH)['object']['sha']
            current = remote_json(request, 'market-data.json', head)
            validate(current)
            if stamp(current) >= stamp(data):
                return 'Destination already has an equal or newer check; nothing overwritten.'
            previous = remote_json(request, 'refresh-sync-status.json', head, missing={})
            # Keep the existing dividend copy information unchanged.
            status = {**previous,
                      'source_market_generated_at': data['generated_at'],
                      'copied_at': dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')}
            commit = request('GET', '/git/commits/' + head)
            entries = [{'path': path, 'mode': '100644', 'type': 'blob',
                        'content': json.dumps(value, indent=2) + '\n'}
                       for path, value in [('market-data.json', data),
                                           ('refresh-sync-status.json', status)]]
            tree = request('POST', '/git/trees', {'base_tree': commit['tree']['sha'], 'tree': entries})
            new_commit = request('POST', '/git/commits', {
                'message': 'Copy completed STKREF market check',
                'tree': tree['sha'], 'parents': [head]})
            # Non-forced update prevents overwriting other jobs' commits.
            request('PATCH', '/git/refs/heads/' + BRANCH,
                    {'sha': new_commit['sha'], 'force': False})
            return 'Copied market snapshot and copy timestamp in one destination commit.'
        except urllib.error.HTTPError as error:
            if error.code not in (409, 422, 429, 500, 502, 503, 504) or attempt == 2:
                raise
            pause(2 ** attempt)
    raise RuntimeError('Copy retries exhausted')


def main():
    token = os.environ.get('EVERYDAYCOMPASS_TOKEN', '').strip()
    if not token:
        print('::warning::Direct market copy unavailable: EVERYDAYCOMPASS_TOKEN is missing. '
              'Source refresh is saved; scheduled destination sync remains the fallback.')
        return 2
    root = Path(__file__).resolve().parents[1]
    try:
        data = json.loads((root / 'market-data.json').read_text(encoding='utf-8'))
        print(copy_snapshot(data, github(token)))
        return 0
    except urllib.error.HTTPError as error:
        # Do not print response bodies or request headers containing credentials.
        print('Rejected endpoint: ' + error.url.split('?')[0])
        print('Required permissions: ' + error.headers.get('X-Accepted-GitHub-Permissions', 'not supplied'))
        print('::warning::Direct market copy failed: GitHub HTTP ' + str(error.code) +
              '. Source refresh is saved; scheduled destination sync remains the fallback.')
    except Exception as error:
        print('::warning::Direct market copy failed (' + type(error).__name__ +
              '). Source refresh is saved; scheduled destination sync remains the fallback.')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
