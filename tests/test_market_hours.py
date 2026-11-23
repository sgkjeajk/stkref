import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from market_hours import market_open, quote_window_open


class MarketWindowTests(unittest.TestCase):
    def test_us_pre_market_is_eligible_but_not_regular_market(self):
        at = dt.datetime(2026, 11, 23, 13, 0, tzinfo=dt.timezone.utc)
        self.assertFalse(market_open('AAPL', at))
        self.assertTrue(quote_window_open('AAPL', at))

    def test_us_post_market_is_eligible_but_not_regular_market(self):
        at = dt.datetime(2026, 11, 23, 22, 0, tzinfo=dt.timezone.utc)
        self.assertFalse(market_open('AAPL', at))
        self.assertTrue(quote_window_open('AAPL', at))

    def test_sgx_remains_regular_session_only(self):
        at = dt.datetime(2026, 11, 23, 0, 0, tzinfo=dt.timezone.utc)
        self.assertFalse(quote_window_open('D05.SI', at))
