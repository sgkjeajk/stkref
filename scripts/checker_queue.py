"""Queue management for the STKREF price checker."""
import datetime as dt
import json
import os
import urllib.error
import urllib.request

API_VERSION = "2022-11-28"


def gh(method, url, token, body=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": API_VERSION,
    }
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = response.read().decode("utf-8")
            return json.loads(value) if value else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise SystemExit(f"GitHub API {method} failed: {error.code} {detail}")


def settings():
    with open("data/stock-tickers.json", encoding="utf-8") as stream:
        config = json.load(stream)
    checker = config.get("priceChecker", {})
    categories = checker.get("categories", {})
    enabled = checker.get("enabled", True) and any(
        categories.get(key, True) for key in ("ETF", "Stock", "Fund")
    )
    cycles = max(1, int(checker.get("cycles", 1)))
    threshold = int(checker.get("refillThreshold", 5))
    batch = int(checker.get("topUpJobs", 10))
    if threshold < 0 or batch < 1:
        raise ValueError("Refill threshold must be non-negative and top-up jobs positive")
    return enabled, cycles, threshold, batch


def active_runs(repo, workflow, token):
    base = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/runs"
    # Include pending admission states, and paginate so excess runs stay visible.
    runs = {}
    for status in ("queued", "in_progress", "waiting", "pending", "requested"):
        page = 1
        while True:
            batch = gh("GET", f"{base}?status={status}&per_page=100&page={page}", token).get("workflow_runs", [])
            for run in batch:
                runs[run["id"]] = run
            if len(batch) < 100:
                break
            page += 1
    return runs


def admitted(runs, current_id, limit):
    # Preserve the oldest slots. An excess newcomer must not reject every worker.
    ids = sorted(set(runs) | {int(current_id)})
    return int(current_id) in ids[:limit]


def dispatch(repo, workflow, cycles, token, request_id):
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches"
    gh("POST", url, token, {
        "ref": "main",
        "inputs": {
            "refresh_ath": False,
            "checker_cycles": str(cycles),
            "force_price_check": False,
            "automatic_topup": True,
            "request_id": request_id,
        },
    })


def main():
    mode = os.environ.get("QUEUE_MODE", "guard")
    repo = os.environ["REPO"]
    workflow = os.environ.get("WORKFLOW_FILE", "task.yml")
    token = os.environ["GH_TOKEN"]
    enabled, saved_cycles, threshold, batch = settings()
    limit = threshold + batch
    runs = active_runs(repo, workflow, token)
    count = len(runs)
    print(f"Active STKREF checker runs: {count}; refill at or below: {threshold}; add: {batch}; admission ceiling: {limit}")
    for run in sorted(runs.values(), key=lambda item: item.get("created_at", "")):
        print(f"- {run['id']} {run['status']} {run.get('event')} {run.get('html_url')}")

    if mode == "guard":
        force = os.environ.get("FORCE_PRICE_CHECK", "false").lower() == "true"
        history = os.environ.get("REFRESH_ATH", "false").lower() == "true"
        allowed = (enabled or force or history) and admitted(
            runs, int(os.environ["GITHUB_RUN_ID"]), limit)
        print(f"Checker admission: {allowed}")
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write("allowed=" + str(allowed).lower() + "\n")
        return

    if mode not in ("queue", "watchdog"):
        raise ValueError("Unknown queue mode: " + mode)
    if not enabled:
        print("Price checker is disabled or all categories are off; no run dispatched.")
        return

    # Called by the serialized controller only, after a worker has fully finished.
    # Reserve each successful dispatch locally: do not rely on API visibility
    # of a just-dispatched run to calculate the batch size.
    missing = batch if count <= threshold else 0
    for slot in range(missing):
        request_id = mode + "-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S%f")
        dispatch(repo, workflow, saved_cycles, token, request_id)
        print(f"Dispatched {saved_cycles}-cycle checker {slot + 1} of {missing}: {request_id}")
    if not missing:
        print("Active count is above the refill threshold; no jobs added.")


if __name__ == "__main__":
    main()
