"""Shared configuration, provider retrieval and atomic market snapshot. Stdlib only."""
import concurrent.futures, datetime as dt, json, math, os, re, threading, urllib.request, urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = threading.RLock()
UA = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json,text/html'}
CATALOG = {}
DEFAULT_PRICE_CHECKER = {
    'enabled': True,
    'intervalMinutes': 8,
    'cycles': 30,
    'categories': {'ETF': True, 'Stock': True, 'Fund': True},
    'maxActiveCheckers': 3,
}
PRICE_CHECKER_INTERVALS = (5, 6, 8, 9, 10)
PRICE_CHECKER_CYCLE_MIN = 1
PRICE_CHECKER_TYPES = ('ETF', 'Stock', 'Fund')
for directory_file in (ROOT/'data/symbol-directory').glob('*.json'):
    CATALOG.update(json.loads(directory_file.read_text(encoding='utf-8')))

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00','Z')

def atomic(path, data):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, allow_nan=False); f.write('\n'); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)

def read_config():
    return json.loads((ROOT/'data/stock-tickers.json').read_text(encoding='utf-8'))

def symbol(value):
    if not isinstance(value,str): raise ValueError('Ticker must be text.')
    value=value.strip().upper()
    if not re.fullmatch(r'[A-Z0-9][A-Z0-9.\-]{0,31}',value) or value[-1] in '.-' or re.search(r'[.\-]{2}',value):
        raise ValueError('Invalid ticker format.')
    return value

def fetch(url):
    with urllib.request.urlopen(urllib.request.Request(url,headers=UA),timeout=12) as r:
        body=r.read()
        if body.startswith(b'\x1f\x8b'):
            import gzip
            body=gzip.decompress(body)
        return body.decode('utf-8')

def page(ticker):
    hosts=('sg.finance.yahoo.com','finance.yahoo.com') if ticker.endswith('.SI') else ('finance.yahoo.com','sg.finance.yahoo.com')
    for host in hosts:
        try:
            return fetch('https://'+host+'/quote/'+urllib.parse.quote(ticker)+'/').replace('\\"','"')
        except Exception as error:
            last=error
    raise ValueError('Yahoo Finance page unavailable for '+ticker+': '+str(last))

def lookup(ticker):
    ticker=symbol(ticker)
    if ticker not in CATALOG:
        source='Yahoo Finance quote webpage'
        try:
            data=page_quote(ticker)
        except Exception:
            data=chart_metadata(ticker)
            source='Yahoo Finance individual ticker chart'
        kind=data.get('quoteType') or data.get('instrumentType')
        name=data.get('longName') or data.get('shortName')
        market=data.get('fullExchangeName') or data.get('exchangeName') or data.get('exchange')
        type_map={'ETF':'ETF','EQUITY':'Stock','MUTUALFUND':'Fund','FUND':'Fund'}
        if kind not in type_map or not name or not market:
            raise ValueError('No verified Stock/ETF/Fund metadata on Yahoo Finance.')
        return {'ticker':ticker,'name':name,'region':market,'type':type_map[kind],'metadata_source':source}
    x=CATALOG[ticker]
    return {'ticker':ticker,'name':x['name'],'region':x['market'],'type':x['type'],'metadata_source':x['source']}

def validate(data):
    if not isinstance(data,dict) or data.get('version',1)!=1: raise ValueError('Unsupported configuration.')
    entries=data.get('tickers',data.get('instruments'))
    if not isinstance(entries,list): raise ValueError('Expected a ticker list.')
    interval=data.get('refreshIntervalMinutes',15)
    if type(interval)!=int or interval not in (5,10,15,30,60): raise ValueError('Invalid refresh interval.')
    raw_checker=data.get('priceChecker',{})
    if raw_checker is None: raw_checker={}
    if not isinstance(raw_checker,dict): raise ValueError('Invalid price checker settings.')
    checker={
        'enabled': raw_checker.get('enabled',DEFAULT_PRICE_CHECKER['enabled']),
        'intervalMinutes': raw_checker.get('intervalMinutes',DEFAULT_PRICE_CHECKER['intervalMinutes']),
        'cycles': raw_checker.get('cycles',DEFAULT_PRICE_CHECKER['cycles']),
        'categories': {**DEFAULT_PRICE_CHECKER['categories'], **raw_checker.get('categories',{})},
        'maxActiveCheckers': raw_checker.get('maxActiveCheckers',DEFAULT_PRICE_CHECKER['maxActiveCheckers'])
    }
    if type(checker['enabled'])!=bool: raise ValueError('Price checker enabled setting must be true or false.')
    if type(checker['intervalMinutes'])!=int or checker['intervalMinutes'] not in PRICE_CHECKER_INTERVALS:
        raise ValueError('Price checker interval must be 5, 6, 8, 9 or 10 minutes.')
    if type(checker['cycles'])!=int or checker['cycles'] < PRICE_CHECKER_CYCLE_MIN:
        raise ValueError('Price checker cycles must be a positive whole number.')
    if type(checker['maxActiveCheckers'])!=int or checker['maxActiveCheckers'] < 1:
        raise ValueError('Max active checker jobs must be a positive whole number.')
    if not isinstance(checker['categories'],dict): raise ValueError('Invalid price checker category settings.')
    checker['categories']={k: checker['categories'].get(k, True) for k in PRICE_CHECKER_TYPES}
    if any(type(v)!=bool for v in checker['categories'].values()):
        raise ValueError('Price checker category settings must be true or false.')
    seen=set(); result=[]
    for x in entries:
        if not isinstance(x,dict) or type(x.get('enabled'))!=bool: raise ValueError('Enabled must be true or false.')
        t=symbol(x.get('ticker'))
        if t in seen: raise ValueError('Duplicate ticker: '+t)
        seen.add(t)
        if all(isinstance(x.get(k),str) and x.get(k).strip() for k in ('name','region','type','metadata_source')) and x['type'] in ('ETF','Stock','Fund'):
            meta={'ticker':t,'name':x['name'].strip(),'region':x['region'].strip(),'type':x['type'],'metadata_source':x['metadata_source'].strip()}
        else:
            meta=lookup(t)
        result.append({**meta,'enabled':x['enabled']})
    result.sort(key=lambda x:({'ETF':0,'Stock':1,'Fund':2}.get(x['type'],3),x['ticker']))
    return {'version':1,'revision':data.get('revision',0),'refreshIntervalMinutes':interval,'priceChecker':checker,'instruments':result}

def save_config(data):
    validated=validate(data)
    with LOCK:
        old=read_config()
        if data.get('revision')!=old.get('revision',0): raise RuntimeError('Configuration changed in another window. Reload before saving.')
        validated['revision']=old.get('revision',0)+1; validated['saved_at']=now()
        atomic(ROOT/'data/stock-tickers.json',validated)
    return validated

def finite(v):
    return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)

def percentage(a,b):
    return (a/b-1)*100 if finite(a) and finite(b) and b>0 else None

def page_quote(ticker,text=None):
    text=page(ticker) if text is None else text
    matches=[]
    for match in re.finditer(r'\{"quoteResponse"',text):
        try:
            payload=json.JSONDecoder().raw_decode(text[match.start():])[0]
            matches.extend(q for q in payload['quoteResponse']['result'] if q.get('symbol','').upper()==ticker)
        except (ValueError,KeyError):
            continue
    if not matches: raise ValueError('No exact-symbol quote on webpage')
    data=matches[0]
    return data

def chart_metadata(ticker):
    errors=[]
    for host in ('query1.finance.yahoo.com','query2.finance.yahoo.com'):
        try:
            url='https://'+host+'/v8/finance/chart/'+urllib.parse.quote(ticker)+'?range=5d&interval=1d'
            payload=json.loads(fetch(url))
            items=payload.get('chart',{}).get('result') or []
            if len(items)!=1: raise ValueError('No unique chart result')
            meta=items[0].get('meta') or {}
            if meta.get('symbol','').upper()!=ticker.upper(): raise ValueError('Chart ticker mismatch')
            if not (meta.get('longName') or meta.get('shortName')): raise ValueError('Missing chart instrument name')
            return meta
        except Exception as error:
            errors.append(host+': '+str(error))
    raise ValueError('Yahoo Finance chart metadata unavailable: '+'; '.join(errors))

def quote_webpage(ticker):
    data=page_quote(ticker)
    def raw(field):
        value=data.get(field)
        value=value.get('raw') if isinstance(value,dict) else value
        if not finite(value): raise ValueError('Webpage quote field unavailable: '+field)
        return value
    cur,prev,stamp=raw('regularMarketPrice'),raw('regularMarketPreviousClose'),raw('regularMarketTime')
    if cur<=0 or prev<=0: raise ValueError('Invalid webpage quote')
    currency=data.get('currency')
    return {'current_price':cur,'prev_close':prev,'current_pct_change':percentage(cur,prev),'price_as_of':dt.datetime.fromtimestamp(stamp,dt.timezone.utc).isoformat(),'source':'Yahoo Finance quote webpage','price_retrieved_at':now(),**({'currency':currency} if currency else {})}


def previous_session_close(ticker, quote_time, currency):
    """Use a completed daily close; chartPreviousClose is the range baseline."""
    from zoneinfo import ZoneInfo
    errors=[]
    for host in ('query1.finance.yahoo.com','query2.finance.yahoo.com'):
        try:
            url='https://'+host+'/v8/finance/chart/'+urllib.parse.quote(ticker)+'?range=3mo&interval=1d'
            items=json.loads(fetch(url)).get('chart',{}).get('result') or []
            if len(items)!=1: raise ValueError('No unique daily chart')
            item=items[0]; meta=item.get('meta') or {}
            if meta.get('symbol','').upper()!=ticker.upper() or meta.get('currency')!=currency:
                raise ValueError('Daily close ticker or currency mismatch')
            zone=ZoneInfo(meta['exchangeTimezoneName'])
            quote_date=quote_time.astimezone(zone).date()
            stamps=item.get('timestamp') or []
            quotes=item.get('indicators',{}).get('quote') or []
            closes=quotes[0].get('close',[]) if quotes else []
            if len(stamps)!=len(closes): raise ValueError('Daily timestamps and closes differ')
            points=[(dt.datetime.fromtimestamp(ts,dt.timezone.utc).astimezone(zone).date(),close)
                    for ts,close in zip(stamps,closes) if finite(ts) and finite(close) and close>0]
            points=[(day,close) for day,close in points if day<quote_date]
            if not points: raise ValueError('No completed close before quote date')
            day,close=max(points,key=lambda x:x[0])
            if (quote_date-day).days>10: raise ValueError('Previous session close is outdated')
            return {'prev_close':close,'prev_close_as_of':day.isoformat(),
                    'prev_close_source':'Yahoo Finance verified daily close ('+host+')'}
        except Exception as error: errors.append(str(error))
    raise ValueError('Previous session close unavailable: '+'; '.join(errors))


def quote_chart(ticker):
    """Yahoo per-ticker chart metadata fallback; regular session only."""
    errors=[]
    for host in ('query1.finance.yahoo.com','query2.finance.yahoo.com'):
        try:
            url='https://'+host+'/v8/finance/chart/'+urllib.parse.quote(ticker)+'?range=5d&interval=1m'
            payload=json.loads(fetch(url))
            items=payload.get('chart',{}).get('result') or []
            if len(items)!=1: raise ValueError('No unique chart result')
            meta=items[0].get('meta') or {}
            if meta.get('symbol','').upper()!=ticker.upper(): raise ValueError('Chart ticker mismatch')
            currency=meta.get('currency')
            if not isinstance(currency,str) or not currency: raise ValueError('Missing quote currency')
            cur=meta.get('regularMarketPrice')
            prev=meta.get('previousClose')
            stamp=meta.get('regularMarketTime')
            if not finite(cur) or cur<=0: raise ValueError('Missing regular price')
            if not finite(stamp) or stamp<=0: raise ValueError('Missing original market quote timestamp')
            quote_time=dt.datetime.fromtimestamp(stamp,dt.timezone.utc)
            if quote_time>dt.datetime.now(dt.timezone.utc)+dt.timedelta(minutes=5): raise ValueError('Future quote timestamp')
            close_data={}
            if not finite(prev) or prev<=0:
                close_data=previous_session_close(ticker,quote_time,currency)
                prev=close_data['prev_close']
            return {'current_price':cur,'prev_close':prev,'current_pct_change':percentage(cur,prev),
                    'price_as_of':quote_time.isoformat(),'source':'Yahoo Finance individual ticker chart',
                    'price_retrieved_at':now(),'currency':currency,**close_data}
        except Exception as error: errors.append(host+': '+str(error))
    raise ValueError('Yahoo individual ticker chart unavailable: '+'; '.join(errors))

def quote(ticker):
    """Try the individual Yahoo quote webpage, then the same ticker's chart metadata."""
    try:
        return quote_webpage(ticker)
    except Exception as webpage_error:
        try: return quote_chart(ticker)
        except Exception as chart_error:
            try: return quote_google(ticker)
            except Exception as google_error:
                raise ValueError('Yahoo webpage failed: '+str(webpage_error)+'; Yahoo chart failed: '+str(chart_error)+'; Google Finance failed: '+str(google_error))

def quote_google(ticker):
    """Last-resort independent current-price source, never used for ATH."""
    import subprocess,sys
    script=str(ROOT/'scripts/google_price_fallback.py')
    process=subprocess.run([sys.executable,script,ticker],capture_output=True,text=True,timeout=45)
    if process.returncode:
        raise ValueError('Google ticker quote failed: '+(process.stdout or process.stderr)[-400:])
    data=json.loads(process.stdout.strip())
    if data.get('source') not in ('Google Finance individual ticker webpage','Google Finance individual ticker (rendered)') or not finite(data.get('current_price')) or data['current_price']<=0:
        raise ValueError('Unverified Google quote')
    if not isinstance(data.get('currency'),str) or not data.get('currency'):raise ValueError('Google quote currency missing')
    quote_time=dt.datetime.fromisoformat(data['price_as_of'])
    if quote_time.tzinfo is None:raise ValueError('Missing Google quote timezone')
    age=dt.datetime.now(dt.timezone.utc)-quote_time
    if age<dt.timedelta(minutes=-5) or age>dt.timedelta(days=7):raise ValueError('Stale Google quote')
    if not finite(data.get('prev_close')) or data['prev_close']<=0:
        data['prev_close']=None
        data['current_pct_change']=None
    return data

def historical_high(ticker):
    from html.parser import HTMLParser
    class Rows(HTMLParser):
        def __init__(self): super().__init__();self.rows=[];self.row=None;self.cell=None
        def handle_starttag(self,tag,attrs):
            if tag=='tr': self.row=[]
            if tag=='td' and self.row is not None: self.cell=[]
        def handle_data(self,data):
            if self.cell is not None: self.cell.append(data)
        def handle_endtag(self,tag):
            if tag=='td' and self.cell is not None:
                self.row.append(''.join(self.cell).strip());self.cell=None
            if tag=='tr' and self.row is not None:
                self.rows.append(self.row);self.row=None
    end=int(dt.datetime.now(dt.timezone.utc).timestamp())
    hosts=('sg.finance.yahoo.com','finance.yahoo.com','uk.finance.yahoo.com','ca.finance.yahoo.com','au.finance.yahoo.com') if ticker.endswith('.SI') else ('finance.yahoo.com','sg.finance.yahoo.com')
    for host in hosts:
        try:
            text=fetch('https://'+host+'/quote/'+urllib.parse.quote(ticker)+'/history/?period1=946684800&period2='+str(end)).replace('\\"','"')
            metadata=page_quote(ticker,text)
            parser=Rows();parser.feed(text)
            points=[]
            for row in parser.rows:
                if len(row)!=7: continue
                try:
                    date=dt.datetime.strptime(row[0],'%b %d, %Y').date()
                    high=float(row[2].replace(',',''))
                    if date>=dt.date(2000,1,1) and finite(high) and high>0: points.append((date,high))
                except ValueError: continue
            if not points: raise ValueError('No historical daily highs on webpage')
            inception=metadata.get('firstTradeDateMilliseconds')
            if isinstance(inception,dict): inception=inception.get('raw')
            start=max(dt.date(2000,1,1),dt.datetime.fromtimestamp(inception/1000,dt.timezone.utc).date()) if finite(inception) else dt.date(2000,1,1)
            oldest,newest=min(d for d,h in points),max(d for d,h in points)
            if (oldest-start).days>10: raise ValueError('Historical table does not reach inception or January 2000')
            if (dt.datetime.now(dt.timezone.utc).date()-newest).days>10: raise ValueError('Historical table is outdated')
            if len(points)<max(1,(newest-oldest).days*0.55): raise ValueError('Historical daily table appears incomplete')
            return {'ath':max(h for d,h in points),'ath_retrieved_at':now(),'ath_error':None,'ath_source':'Yahoo historical webpage daily highs','ath_history_start':oldest.isoformat(),'ath_history_end':newest.isoformat(),'ath_history_rows':len(points)}
        except Exception as error: last=error
    raise last


def historical_high_chart(ticker):
    """Second Yahoo individual-ticker ATH route; full daily highs, never a MAX line chart."""
    errors=[]
    end=int(dt.datetime.now(dt.timezone.utc).timestamp())+86400
    for host in ('query1.finance.yahoo.com','query2.finance.yahoo.com'):
        try:
            url='https://'+host+'/v8/finance/chart/'+urllib.parse.quote(ticker)+'?period1=946684800&period2='+str(end)+'&interval=1d&events=history'
            payload=json.loads(fetch(url))
            items=payload.get('chart',{}).get('result') or []
            if len(items)!=1: raise ValueError('Missing unique historical chart')
            item=items[0]
            meta=item.get('meta') or {}
            if meta.get('symbol','').upper()!=ticker.upper(): raise ValueError('Historical ticker mismatch')
            if not isinstance(meta.get('currency'),str) or not meta.get('currency'): raise ValueError('Historical currency missing')
            stamps=item.get('timestamp') or []
            quotes=item.get('indicators',{}).get('quote') or []
            highs=quotes[0].get('high') if quotes else []
            if not stamps or len(stamps)!=len(highs): raise ValueError('Historical timestamps/highs missing or unequal')
            points=[(dt.datetime.fromtimestamp(ts,dt.timezone.utc).date(),high)
                    for ts,high in zip(stamps,highs)
                    if finite(ts) and finite(high) and high>0]
            if not points: raise ValueError('No valid historical daily highs')
            inception=meta.get('firstTradeDate')
            start=max(dt.date(2000,1,1),dt.datetime.fromtimestamp(inception,dt.timezone.utc).date()) if finite(inception) else dt.date(2000,1,1)
            oldest,newest=min(d for d,h in points),max(d for d,h in points)
            if (oldest-start).days>10: raise ValueError('Historical chart does not reach inception or January 2000')
            if (dt.datetime.now(dt.timezone.utc).date()-newest).days>10: raise ValueError('Historical chart is outdated')
            if len(points)<max(1,(newest-oldest).days*0.55): raise ValueError('Historical chart daily highs incomplete')
            return {'ath':max(h for d,h in points),'ath_retrieved_at':now(),
                    'ath_error':None,'ath_source':'Yahoo individual ticker daily chart ('+host+')',
                    'ath_history_start':oldest.isoformat(),'ath_history_end':newest.isoformat(),
                    'ath_history_rows':len(points)}
        except Exception as error: errors.append(host+': '+str(error))
    raise ValueError('Yahoo individual ticker ATH chart failed: '+'; '.join(errors))

def historical_high_verified(ticker):
    """Original historical webpage first; verified individual-ticker chart if it fails."""
    try: return historical_high(ticker)
    except Exception as page_error:
        try: return historical_high_chart(ticker)
        except Exception as chart_error:
            raise ValueError('Yahoo historical webpage failed: '+str(page_error)+'; '+str(chart_error))

def previous_day_change(ticker,price_as_of):
    """Previous two completed sessions before the quote's market date."""
    from html import unescape
    from zoneinfo import ZoneInfo
    zone=ZoneInfo('Asia/Singapore' if ticker.endswith('.SI') else 'Europe/Berlin' if ticker.endswith('.MU') else 'America/New_York')
    market_date=dt.datetime.fromisoformat(price_as_of).astimezone(zone).date()
    end=int(dt.datetime.now(dt.timezone.utc).timestamp())
    errors=[]

    def validated(rows,source):
        # Never infer a closing price from the live quote or a different symbol.
        closes=sorted({d:v for d,v in rows if d<market_date and finite(v) and v>0}.items(),reverse=True)
        if len(closes)<2:
            raise ValueError('Fewer than two verified completed sessions')
        (recent,latest),(earlier,previous)=closes[:2]
        return {'prev_day_pct_change':percentage(latest,previous),
                'prev_day_change_as_of':recent.isoformat(),
                'prev_day_change_retrieved_at':now(),
                'prev_day_change_source':source,
                'prev_day_change_error':None}

    hosts=('sg.finance.yahoo.com','finance.yahoo.com','uk.finance.yahoo.com') if ticker.endswith('.SI') else ('finance.yahoo.com','sg.finance.yahoo.com')
    for host in hosts:
        try:
            text=fetch('https://'+host+'/quote/'+urllib.parse.quote(ticker)+'/history/?period1='+str(end-60*86400)+'&period2='+str(end))
            rows=[]
            for tr in re.findall(r'<tr\b[^>]*>(.*?)</tr>',text,re.S):
                cells=[unescape(re.sub(r'<[^>]+>','',v)).strip() for v in re.findall(r'<td\b[^>]*>(.*?)</td>',tr,re.S)]
                if len(cells)!=7: continue
                try:
                    rows.append((dt.datetime.strptime(cells[0],'%b %d, %Y').date(),float(cells[4].replace(',',''))))
                except ValueError: continue
            return validated(rows,'Yahoo Finance historical webpage')
        except Exception as error:
            errors.append(str(error))

    # Yahoo chart is a separate historical-data route, not the quote's previous close.
    # It is unofficial and may itself be unavailable or rate-limited.
    for host in ('query2.finance.yahoo.com','query1.finance.yahoo.com'):
        try:
            url='https://'+host+'/v8/finance/chart/'+urllib.parse.quote(ticker)+'?range=3mo&interval=1d'
            payload=json.loads(fetch(url))
            results=payload.get('chart',{}).get('result') or []
            if len(results)!=1 or results[0].get('meta',{}).get('symbol','').upper()!=ticker.upper():
                raise ValueError('Historical chart symbol mismatch')
            chart=results[0]
            stamps=chart.get('timestamp') or []
            quotes=chart.get('indicators',{}).get('quote') or []
            closes=quotes[0].get('close',[]) if quotes else []
            if len(stamps)!=len(closes):
                raise ValueError('Historical chart timestamps and closes differ')
            rows=[(dt.datetime.fromtimestamp(ts,dt.timezone.utc).astimezone(zone).date(),close)
                  for ts,close in zip(stamps,closes) if isinstance(ts,(int,float))]
            return validated(rows,'Yahoo Finance daily chart ('+host+')')
        except Exception as error:
            errors.append(str(error))
    if ticker.endswith('.MU'):
        try:
            # A one-time verified exchange snapshot is evidence for its exact
            # dates only. It is never treated as a newly fetched market history.
            saved=json.loads((ROOT/'data/verified-munich-closes.json').read_text(encoding='utf-8'))
            if (ticker!='GQC0.MU' or saved.get('ticker')!=ticker or
                saved.get('isin')!='LU0690374961' or saved.get('currency')!='EUR' or
                saved.get('source_url')!='https://www.boerse-muenchen.de/fonds/LU0690374961/'):
                raise ValueError('Saved exchange history identity mismatch')
            rows=[(dt.date.fromisoformat(p['date']),p['close']) for p in saved['closes']]
            verified=validated(rows,'Börse München dated closes (one-time verified)')
            expected=market_date-dt.timedelta(days=1)
            while expected.weekday()>=5:
                expected-=dt.timedelta(days=1)
            # Conservative on holidays: fail rather than silently reuse a date.
            if verified['prev_day_change_as_of']!=expected.isoformat():
                raise ValueError('Saved exchange closes do not cover the required previous session')
            verified['prev_day_change_retrieved_at']=saved['verified_at']
            verified['prev_day_change_source_url']=saved['source_url']
            return verified
        except Exception as error:
            errors.append(str(error))
    raise ValueError('Historical closes unavailable: '+'; '.join(errors[-4:]))

def dividends(ticker,current_price):
    """Retrieve dividend events during the scheduled dividend refresh."""
    period1=int((dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=400)).timestamp())
    period2=int(dt.datetime.now(dt.timezone.utc).timestamp())
    for host in ('query1.finance.yahoo.com','query2.finance.yahoo.com'):
        try:
            url='https://'+host+'/v8/finance/chart/'+urllib.parse.quote(ticker)+'?period1='+str(period1)+'&period2='+str(period2)+'&interval=1d&events=div'
            payload=json.loads(fetch(url)); results=payload.get('chart',{}).get('result') or []
            if len(results)!=1: raise ValueError('No unique dividend chart result')
            events=(results[0].get('events') or {}).get('dividends') or {}
            rows=sorted((int(v['date']),float(v['amount'])) for v in events.values() if finite(v.get('amount')) and float(v['amount'])>=0)
            if not rows:
                return {'dividend_per_share':None,'dividend_yield_pct':None,'dividend_as_of':None,'dividend_retrieved_at':now(),'dividend_error':None}
            cutoff=int((dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=365)).timestamp())
            annual=sum(amount for stamp,amount in rows if stamp>=cutoff)
            stamp,amount=rows[-1]
            return {'dividend_per_share':amount,'dividend_yield_pct':annual/current_price*100 if finite(current_price) and current_price>0 else None,'dividend_as_of':dt.datetime.fromtimestamp(stamp,dt.timezone.utc).isoformat(),'dividend_retrieved_at':now(),'dividend_error':None}
        except Exception:
            continue
    return {'dividend_error':'Dividend sources unavailable; saved dividend values retained.','dividend_retrieved_at':now()}

def retrieve(x,old,history=False,include_dividend=False):
    result={**old,**x}
    try:
        result.update(quote(x['ticker']))
        if include_dividend: result.update(dividends(x['ticker'],result.get('current_price')))
        result['refresh_error']=None
        try: result.update(previous_day_change(x['ticker'],result['price_as_of']))
        except Exception as error:
            # Preserve the last verified value and its original as-of timestamp.
            if not finite(result.get('prev_day_pct_change')):
                result['prev_day_pct_change']=None
            result['prev_day_change_error']='Previous trading-day change unavailable; saved value (if any) is stale: '+str(error)
        # Price-only runs preserve historical data and its original timestamp.
        if finite(result.get('ath')):
            if result['current_price']>result['ath']:
                result['ath']=result['current_price']
                result['ath_source']='Observed new high from current-price scrape'
            result['drawdown_pct']=percentage(result['current_price'],result['ath'])
        else:
            result['drawdown_pct']=None
            result['ath_error']='No saved ATH available; historical retrieval is disabled.'
    except Exception as error:
        result['refresh_error']='Webpage scraping failed; retained saved price and timestamp. '+str(error)
    if history:
        try:
            result.update(historical_high_verified(x['ticker']))
            if finite(result.get('current_price')):
                result['ath']=max(result['ath'],result['current_price'])
            result['drawdown_pct']=percentage(result.get('current_price'),result['ath'])
        except Exception as error:
            result['ath_error']=('Historical scrape failed; saved ATH and timestamp retained. ' if finite(result.get('ath')) else 'ATH unavailable; historical coverage could not be verified. ')+str(error)
    return result

def refresh(history=False, eligible_tickers=None, include_dividend=False):
    with LOCK: cfg=validate(read_config())
    path=ROOT/'market-data.json'
    old=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    prior={x['ticker']:x for x in old.get('instruments',[])}
    active=[x for x in cfg['instruments'] if x['enabled']]
    categories=cfg.get('priceChecker',{}).get('categories',DEFAULT_PRICE_CHECKER['categories'])
    selected=[x for x in active if (history or categories.get(x['type'],True)) and (eligible_tickers is None or x['ticker'] in eligible_tickers)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        updated={x['ticker']:row for x,row in zip(selected,pool.map(lambda x:retrieve(x,prior.get(x['ticker'],{}),history,include_dividend),selected))}
    # Preserve closed-market prices and their original retrieval timestamps.
    rows=[updated.get(x['ticker'],{**prior.get(x['ticker'],{}),**x}) for x in active]
    result={'generated_at':now(),'ath_since':'2000-01-01','config_revision':cfg.get('revision',0),'instruments':rows,'source':'Yahoo Finance; per-row timestamps and errors apply'}
    with LOCK:
        if read_config().get('revision',0)!=cfg.get('revision',0): raise RuntimeError('Configuration changed during refresh; retry.')
        atomic(path,result)
    return result
