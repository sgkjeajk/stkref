import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import market_service


class ValidateConfigTests(unittest.TestCase):
    def test_preserves_complete_saved_metadata_without_lookup(self):
        original_lookup = market_service.lookup
        try:
            def fail_lookup(ticker):
                raise AssertionError("lookup should not run for complete saved metadata")

            market_service.lookup = fail_lookup
            result = market_service.validate(
                {
                    "version": 1,
                    "refreshIntervalMinutes": 15,
                    "instruments": [
                        {
                            "ticker": "SHEL",
                            "enabled": True,
                            "name": "Shell plc",
                            "region": "NYSE",
                            "type": "Stock",
                            "metadata_source": "Yahoo Finance quote webpage",
                        }
                    ],
                }
            )
        finally:
            market_service.lookup = original_lookup

        self.assertEqual(result["instruments"][0]["ticker"], "SHEL")
        self.assertEqual(result["instruments"][0]["name"], "Shell plc")
        self.assertEqual(result["instruments"][0]["type"], "Stock")
        self.assertEqual(result["priceChecker"]["cycles"], 30)
        self.assertEqual(result["priceChecker"]["intervalMinutes"], 8)
        self.assertTrue(result["priceChecker"]["categories"]["Fund"])

    def test_ticker_only_entry_still_uses_lookup(self):
        calls = []
        original_lookup = market_service.lookup
        try:
            def fake_lookup(ticker):
                calls.append(ticker)
                return {
                    "ticker": ticker,
                    "name": "Apple Inc.",
                    "region": "United States",
                    "type": "Stock",
                    "metadata_source": "Test directory",
                }

            market_service.lookup = fake_lookup
            result = market_service.validate(
                {
                    "version": 1,
                    "tickers": [{"ticker": "aapl", "enabled": False}],
                }
            )
        finally:
            market_service.lookup = original_lookup

        self.assertEqual(calls, ["AAPL"])
        self.assertEqual(result["instruments"][0]["ticker"], "AAPL")
        self.assertFalse(result["instruments"][0]["enabled"])

    def test_accepts_custom_price_checker_settings(self):
        result = market_service.validate(
            {
                "version": 1,
                "refreshIntervalMinutes": 15,
                "priceChecker": {
                    "enabled": True,
                    "intervalMinutes": 10,
                    "cycles": 12,
                    "categories": {"ETF": True, "Stock": True, "Fund": False},
                },
                "instruments": [
                    {
                        "ticker": "0P00009VED.SI",
                        "enabled": True,
                        "name": "Franklin Income A(Mdis)SGD-H1",
                        "region": "Singapore",
                        "type": "Fund",
                        "metadata_source": "Yahoo Finance fund page",
                    }
                ],
            }
        )

        self.assertEqual(result["priceChecker"]["intervalMinutes"], 10)
        self.assertEqual(result["priceChecker"]["cycles"], 12)
        self.assertFalse(result["priceChecker"]["categories"]["Fund"])

    def test_accepts_15_minute_price_checker_interval(self):
        result = market_service.validate(
            {"version": 1, "instruments": [], "priceChecker": {"enabled": True, "intervalMinutes": 15, "cycles": 1}}
        )
        self.assertEqual(result["priceChecker"]["intervalMinutes"], 15)
        self.assertEqual(result["priceChecker"]["cycles"], 1)

    def test_accepts_more_than_400_tickers_and_unbounded_checker_settings(self):
        entries = [
            {
                "ticker": f"TEST{i:03d}",
                "enabled": True,
                "name": f"Test Instrument {i}",
                "region": "Test Market",
                "type": "Stock",
                "metadata_source": "Test fixture",
            }
            for i in range(401)
        ]
        result = market_service.validate({"version": 1, "instruments": entries, "priceChecker": {"cycles": 31, "maxActiveCheckers": 11}})
        self.assertEqual(len(result["instruments"]), 401)
        self.assertEqual(result["priceChecker"]["cycles"], 31)
        self.assertEqual(result["priceChecker"]["maxActiveCheckers"], 11)

class QuoteChartTests(unittest.TestCase):
    def test_rejects_chart_baseline_without_verified_daily_close(self):
        original_fetch = market_service.fetch
        original_now = market_service.dt.datetime

        class FixedDateTime(market_service.dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 11, 15, 20, tzinfo=tz)

        try:
            def fake_fetch(url):
                self.assertIn("0P0000Z32L.SI", url)
                return """
                {
                  "chart": {
                    "result": [{
                      "meta": {
                        "currency": "SGD",
                        "symbol": "0P0000Z32L.SI",
                        "regularMarketPrice": 7.5065,
                        "chartPreviousClose": 7.5107,
                        "regularMarketTime": 1791536400
                      }
                    }]
                  }
                }
                """

            market_service.fetch = fake_fetch
            market_service.dt.datetime = FixedDateTime
            with self.assertRaisesRegex(ValueError, "Previous session close unavailable"):
                market_service.quote_chart("0P0000Z32L.SI")
        finally:
            market_service.fetch = original_fetch
            market_service.dt.datetime = original_now



if __name__ == "__main__":
    unittest.main()
