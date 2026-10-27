"""Regular cash-market sessions for scheduled quote retrieval (not holiday-aware)."""
import datetime as dt
from zoneinfo import ZoneInfo


def market_open(ticker, at=None):
    at = at or dt.datetime.now(dt.timezone.utc)
    if at.tzinfo is None:
        raise ValueError('at must be timezone-aware')
    sg = ticker.upper().endswith('.SI')
    local = at.astimezone(ZoneInfo('Asia/Singapore' if sg else 'America/New_York'))
    if local.weekday() >= 5:
        return False
    clock = local.hour * 60 + local.minute
    if sg:
        return 9 * 60 <= clock < 12 * 60 or 13 * 60 <= clock < 17 * 60
    return 9 * 60 + 30 <= clock < 16 * 60
