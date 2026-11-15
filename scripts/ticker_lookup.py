"""Server-side exact or uniquely suffixed Yahoo Finance ticker metadata lookup."""
import json
import os
import urllib.parse
import urllib.request

from market_service import ROOT, atomic, lookup as exact_lookup, now, symbol

TYPE_MAP = {
    "EQUITY": "Stock",
    "STOCK": "Stock",
    "ETF": "ETF",
    "MUTUALFUND": "Fund",
    "FUND": "Fund",
    "MUTUAL FUND": "Fund",
}

def search_quotes(ticker):
    query = urllib.parse.urlencode({"q": ticker, "quotesCount": 20, "newsCount": 0})
    request = urllib.request.Request(
        "https://query1.finance.yahoo.com/v1/finance/search?" + query,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload.get("quotes", [])

def candidate_metadata(quote):
    kind = str(quote.get("quoteType") or quote.get("typeDisp") or "").strip().upper()
    name = quote.get("longname") or quote.get("shortname")
    market = quote.get("exchDisp") or quote.get("exchange")
    category = TYPE_MAP.get(kind)
    if not category or not name or not market:
        return None
    return {
        "ticker": symbol(quote.get("symbol")),
        "name": str(name).strip(),
        "region": str(market).strip(),
        "type": category,
        "metadata_source": "Yahoo Finance symbol search",
    }

def lookup_ticker(value):
    ticker = symbol(value)
    try:
        return exact_lookup(ticker)
    except Exception as exact_error:
        quotes = search_quotes(ticker)
        exact = [
            quote for quote in quotes
            if str(quote.get("symbol", "")).strip().upper() == ticker
        ]
        matches = exact or [
            quote for quote in quotes
            if str(quote.get("symbol", "")).strip().upper().startswith(ticker + ".")
        ]
        found = {}
        for quote in matches:
            item = candidate_metadata(quote)
            if item:
                found[item["ticker"]] = item
        if len(found) == 1:
            return next(iter(found.values()))
        if len(found) > 1:
            options = ", ".join(sorted(found))
            raise ValueError("Ticker is ambiguous. Enter its full Yahoo Finance symbol: " + options)
        raise ValueError("Yahoo Finance could not verify this ticker or a unique market suffix. Direct lookup: " + str(exact_error))

if __name__ == "__main__":
    raw_ticker = os.environ.get("LOOKUP_TICKER", "").strip()
    ticker = symbol(raw_ticker)
    request_id = os.environ.get("REQUEST_ID", "")
    try:
        result = {"request_id": request_id, "generated_at": now(), "instrument": lookup_ticker(ticker)}
    except Exception as error:
        result = {"request_id": request_id, "generated_at": now(), "error": str(error)}
    path = ROOT / "data" / "lookups" / (ticker + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic(path, result)
    print("Completed metadata lookup for", ticker)
