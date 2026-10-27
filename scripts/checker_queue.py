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
    limit = max(1, min(10, int(checker.get("maxActiveCheckers", 5))))
    return enabled, cycles, limit


def active_runs(repo, workflow, token):
    base = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/runs"
    runs = []
    for status in ("queued", "in_progress"):
        runs.extend(gh("GET", f"{base}?status={status}&per_page=100", token).get("workflow_runs", []))
    return {run["id"]: run for run in runs}


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
    enabled, saved_cycles, limit = settings()
    runs = active_runs(repo, workflow, token)
    count = len(runs)
    print(f"Active STKREF checker runs: {count}; configured limit: {limit}")
    for run in sorted(runs.values(), key=lambda item: item.get("created_at", "")):
        print(f"- {run['id']} {run['status']} {run.get('event')} {run.get('html_url')}")

    if mode == "guard":
        force = os.environ.get("FORCE_PRICE_CHECK", "false").lower() == "true"
        history = os.environ.get("REFRESH_ATH", "false").lower() == "true"
        if (not enabled and not force and not history) or count > limit:
            print("Checker disabled or active-run limit exceeded; skip this refresh.")
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write("allowed=false\n")
        else:
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write("allowed=true\n")
        return

    if not enabled:
        print("Price checker is disabled or all categories are off; no run dispatched.")
        return

    if count >= limit:
        print("The configured active-run limit is already reached; no run dispatched.")
        return

    cycles = saved_cycles
    if mode == "queue":
        refresh_ath = os.environ.get("REFRESH_ATH", "false").lower() == "true"
        if refresh_ath:
            print("ATH refresh requested; queue top-up is not needed.")
            return
        try:
            cycles = max(1, int(os.environ.get("CHECKER_CYCLES", "") or saved_cycles))
        except ValueError:
            cycles = saved_cycles
        if cycles <= 1:
            print("Cycle count is 1; no repeated-cycle top-up is needed.")
            return
    request_id = mode + "-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S%f")
    dispatch(repo, workflow, cycles, token, request_id)
    print(f"Dispatched one {cycles}-cycle checker run: {request_id}")


if __name__ == "__main__":
    main()
