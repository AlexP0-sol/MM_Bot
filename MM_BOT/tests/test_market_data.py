import json
import unittest
from decimal import Decimal as D
from urllib.parse import urlparse

from gridbot.exchange import Aggressor, MarketDataError
from gridbot.market_data import (
    BinancePublicMarketData,
    ReplayFeed,
    UrllibTransport,
)
from tests.support import SYMBOL, tick


class FakeTransport:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def get(self, url: str, timeout_s: float) -> bytes:
        self.urls.append(url)
        path = urlparse(url).path
        if path not in self.responses:
            raise OSError(f"unexpected path {path}")
        return json.dumps(self.responses[path]).encode("utf-8")


def book(**overrides) -> dict:
    values = {
        "symbol": SYMBOL,
        "bidPrice": "99.9",
        "bidQty": "5",
        "askPrice": "100.1",
        "askQty": "6",
        "time": 2_000,
    }
    values.update(overrides)
    return values


def trade(**overrides) -> dict:
    values = {"symbol": SYMBOL, "p": "99.95", "q": "2", "T": 1_800, "m": True}
    values.update(overrides)
    return values


class PublicMarketDataTests(unittest.TestCase):
    def adapter(self, responses: dict[str, object] | None = None):
        transport = FakeTransport(
            responses
            or {
                "/fapi/v1/ticker/bookTicker": book(),
                "/fapi/v1/aggTrades": [trade()],
            }
        )
        return BinancePublicMarketData(transport=transport), transport

    def test_latest_tick_normalizes_public_payloads(self):
        adapter, transport = self.adapter()
        result = adapter.latest_tick(SYMBOL)
        self.assertEqual(result.timestamp_ms, 1_800)
        self.assertEqual(result.bid, tick(0).bid)
        self.assertEqual(result.ask, tick(0).ask)
        self.assertEqual(result.mid, tick(0).mid)
        self.assertEqual(result.last_price, D("99.95"))
        self.assertEqual(result.last_quantity, D("2"))
        self.assertEqual(result.aggressor, Aggressor.SELL)
        paths = [urlparse(url).path for url in transport.urls]
        self.assertEqual(sorted(paths), ["/fapi/v1/aggTrades", "/fapi/v1/ticker/bookTicker"])
        self.assertFalse(any("order" in path or "account" in path for path in paths))

    def test_buy_aggressor_is_derived_from_the_maker_flag(self):
        adapter, _ = self.adapter(
            {
                "/fapi/v1/ticker/bookTicker": book(),
                "/fapi/v1/aggTrades": [trade(m=False)],
            }
        )
        self.assertEqual(adapter.latest_tick(SYMBOL).aggressor, Aggressor.BUY)

    def test_symbol_mismatch_and_missing_fields_fail_closed(self):
        adapter, _ = self.adapter(
            {
                "/fapi/v1/ticker/bookTicker": book(symbol="OTHERUSDT"),
                "/fapi/v1/aggTrades": [trade()],
            }
        )
        with self.assertRaises(MarketDataError):
            adapter.latest_tick(SYMBOL)

        adapter, _ = self.adapter(
            {
                "/fapi/v1/ticker/bookTicker": {key: value for key, value in book().items() if key != "time"},
                "/fapi/v1/aggTrades": [trade()],
            }
        )
        with self.assertRaises(MarketDataError):
            adapter.latest_tick(SYMBOL)

    def test_invalid_json_and_http_errors_are_wrapped(self):
        class Broken:
            def get(self, url: str, timeout_s: float) -> bytes:
                return b"<html>not json</html>"

        with self.assertRaises(MarketDataError):
            BinancePublicMarketData(transport=Broken()).book_ticker(SYMBOL)

        class Offline:
            def get(self, url: str, timeout_s: float) -> bytes:
                raise OSError("network down")

        with self.assertRaises(MarketDataError):
            BinancePublicMarketData(transport=Offline()).latest_agg_trade(SYMBOL)

    def test_klines_drop_the_unclosed_last_row(self):
        rows = [
            [0, "10", "12", "8", "9", "1", 59_999, "0", 0, "0", "0", "0"],
            [900_000, "9", "11", "8", "10", "1", 959_999, "0", 0, "0", "0", "0"],
            [1_800_000, "10", "12", "9", "11", "1", 1_859_999, "0", 0, "0", "0", "0"],
        ]
        adapter, _ = self.adapter({"/fapi/v1/klines": rows})
        candles = adapter.klines(SYMBOL, "15m", limit=3)
        self.assertEqual([candle.timestamp for candle in candles], [0, 900_000])
        self.assertEqual(candles[1].close, D("10"))
        self.assertTrue(all(candle.complete for candle in candles))

    def test_misaligned_or_unsupported_klines_fail_closed(self):
        rows = [
            [1, "10", "12", "8", "9", "1", 59_999, "0", 0, "0", "0", "0"],
            [900_000, "9", "11", "8", "10", "1", 959_999, "0", 0, "0", "0", "0"],
        ]
        adapter, _ = self.adapter({"/fapi/v1/klines": rows})
        with self.assertRaises(MarketDataError):
            adapter.klines(SYMBOL, "15m", limit=2)
        adapter, _ = self.adapter({"/fapi/v1/klines": rows})
        with self.assertRaises(MarketDataError):
            adapter.klines(SYMBOL, "30m", limit=2)

    def test_adapter_requires_https_and_positive_timeout(self):
        with self.assertRaises(MarketDataError):
            BinancePublicMarketData(transport=FakeTransport({}), base_url="http://example.com")
        with self.assertRaises(MarketDataError):
            BinancePublicMarketData(transport=FakeTransport({}), timeout_s=0)
        self.assertIsInstance(UrllibTransport(), UrllibTransport)

    def test_replay_feed_is_deterministic(self):
        feed = ReplayFeed((tick(1_000), tick(1_001)))
        self.assertEqual(feed.remaining(), 2)
        self.assertEqual(feed.next_tick().timestamp_ms, 1_000)
        self.assertEqual(feed.next_tick().timestamp_ms, 1_001)
        with self.assertRaises(MarketDataError):
            feed.next_tick()
        feed.reset()
        self.assertEqual(feed.next_tick().timestamp_ms, 1_000)


if __name__ == "__main__":
    unittest.main()
