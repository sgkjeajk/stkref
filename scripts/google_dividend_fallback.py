"""Read explicitly labelled dividend fields from the exact Google Finance quote.

No inferred dividend amounts, history, or all-time highs. Missing fields are
omitted so callers can retain their last verified values.
"""
import datetime as dt
import re
from html.parser import HTMLParser
from urllib.parse import quote
from google_price_fallback import google_symbol, quote_currency, parse_quote_time


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if not self.hidden:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.hidden:
            self.hidden -= 1
        if not self.hidden:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def parse_dividends(body, ticker):
    parser = VisibleText()
    parser.feed(body)
    lines = [re.sub(r'\s+', ' ', s).strip() for s in ''.join(parser.parts).splitlines()]
    lines = [s for s in lines if s]
    symbol = google_symbol(ticker)
    # The exact exchange-qualified symbol must occur before the primary quote.
    try:
        start = lines.index(symbol)
    except ValueError:
        raise ValueError('Exact Google Finance ticker not available')
    lines = lines[start:]
    for marker in ('Related stocks', 'News stories', 'People also search for'):
        if marker in lines:
            lines = lines[:lines.index(marker)]
    text = '\n'.join(lines)
    currency = quote_currency(ticker)
    # Validate the currency and original market timestamp near the primary quote,
    # never against another instrument in the related-stock or news sections.
    stamp = parse_quote_time(text[:1000].replace('UTC', 'GMT'), currency)
    age = dt.datetime.now(dt.timezone.utc) - stamp
    if age < dt.timedelta(minutes=-5) or age > dt.timedelta(days=7):
        raise ValueError('Google dividend quote timestamp invalid or stale')

    def value(labels, pattern):
        found = []
        for i, label in enumerate(lines[:-1]):
            if label in labels and re.fullmatch(pattern, lines[i + 1]):
                found.append(lines[i + 1])
        if len(set(found)) > 1:
            raise ValueError('Conflicting Google dividend values')
        return found[0] if found else None

    result = {}
    number = r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
    yield_text = value(('Dividend', 'Dividend yield'), number + r'%')
    if yield_text is not None:
        result['dividend_yield_pct'] = float(yield_text[:-1].replace(',', ''))
    sign = {'USD': '$', 'SGD': '$', 'EUR': '€'}[currency]
    amount = value(('Quarterly dividend', 'Monthly dividend', 'Semiannual dividend',
                    'Semi-annual dividend', 'Annual dividend', 'Dividend per share'),
                   re.escape(sign) + r'\s*' + number)
    if amount is not None:
        result['dividend_per_share'] = float(amount.lstrip(sign).replace(',', '').strip())
    date = value(('Ex-dividend date',), r'[A-Z][a-z]{2} \d{1,2}, \d{4}')
    if date is not None:
        result['ex_dividend_date'] = dt.datetime.strptime(date, '%b %d, %Y').strftime('%d %b %Y')
    if not result:
        raise ValueError('Google has no explicitly labelled dividend fields')
    if result.get('dividend_yield_pct', 0) > 100:
        raise ValueError('Google dividend yield outside supported range')
    result['dividend_source'] = 'Google Finance individual ticker webpage'
    result['dividend_quote_as_of'] = stamp.isoformat()
    return result


def retrieve(ticker, fetch):
    symbol = google_symbol(ticker)
    url = 'https://www.google.com/finance/quote/' + quote(symbol, safe=':') + '?hl=en'
    return parse_dividends(fetch(url), ticker)
