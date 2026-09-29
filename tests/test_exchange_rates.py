import unittest
from unittest.mock import patch

from src.exchange_rates import fetch_exchange_rate


class Response:
    def __enter__(self): return self
    def __exit__(self, *args): return None
    def read(self): return b'{"date":"2026-09-14","rates":{"USD":0.15}}'


class ExchangeRateTests(unittest.TestCase):
    @patch("src.exchange_rates.urllib.request.urlopen", return_value=Response())
    def test_reads_daily_reference_rate(self, _open):
        result = fetch_exchange_rate("CNY", "USD")
        self.assertEqual(result["rate"], 0.15)
        self.assertEqual(result["date"], "2026-09-14")


if __name__ == "__main__":
    unittest.main()
