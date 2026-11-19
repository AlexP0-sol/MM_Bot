import unittest
from decimal import Decimal as D

from gridbot.exchange import Aggressor, OrderSide, OrderStatus
from gridbot.domain import Side
from gridbot.streams import (
    AccountUpdate,
    BackoffPolicy,
    ListenKeyExpired,
    ListenKeyState,
    MarketTickAssembler,
    OrderUpdate,
    StreamDataError,
    StreamHealth,
    StreamSession,
    StreamStatus,
    TradeEvent,
    UnsupportedStreamEvent,
    combine_book_and_trade,
    parse_private_event,
    parse_public_event,
    private_stream_url,
    public_stream_url,
)

SYMBOL = "ABCUSDT"


class PublicStreamTests(unittest.TestCase):
    def test_combined_agg_trade_is_unwrapped_and_parsed(self):
        payload = {
            "stream": "abcusdt@aggTrade",
            "data": {"e": "aggTrade", "s": SYMBOL, "p": "99.5", "q": "2", "T": 1_000, "m": True},
        }
        event = parse_public_event(payload)
        self.assertIsInstance(event, TradeEvent)
        self.assertEqual(event.price, D("99.5"))
        self.assertEqual(event.aggressor, Aggressor.SELL)

    def test_book_ticker_and_trade_combine_into_conservative_tick(self):
        book = parse_public_event(
            {
                "e": "bookTicker",
                "s": SYMBOL,
                "b": "99.9",
                "B": "5",
                "a": "100.1",
                "A": "6",
                "E": 2_000,
            }
        )
        trade = parse_public_event(
            {"e": "aggTrade", "s": SYMBOL, "p": "99.95", "q": "2", "T": 1_800, "m": True}
        )
        tick = combine_book_and_trade(book, trade)
        self.assertEqual(tick.timestamp_ms, 1_800)
        self.assertEqual(tick.bid, D("99.9"))
        self.assertEqual(tick.last_quantity, D("2"))
        self.assertEqual(tick.aggressor, Aggressor.SELL)

    def test_symbol_mismatch_is_rejected(self):
        book = parse_public_event(
            {"e": "bookTicker", "s": SYMBOL, "b": "1", "B": "1", "a": "2", "A": "1", "E": 1}
        )
        trade = parse_public_event(
            {"e": "aggTrade", "s": "OTHERUSDT", "p": "1", "q": "1", "T": 1, "m": False}
        )
        with self.assertRaises(StreamDataError):
            combine_book_and_trade(book, trade)

    def test_malformed_or_unknown_public_events_fail_closed(self):
        with self.assertRaises(StreamDataError):
            parse_public_event({"e": "aggTrade", "s": SYMBOL, "q": "1", "T": 1, "m": True})
        with self.assertRaises(UnsupportedStreamEvent):
            parse_public_event({"e": "depthUpdate", "s": SYMBOL})


class PrivateStreamTests(unittest.TestCase):
    def order_payload(self, **overrides):
        values = {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1_100,
            "o": {
                "s": SYMBOL,
                "c": "gen-1:LONG:1:ENTRY",
                "S": "BUY",
                "ps": "LONG",
                "X": "PARTIALLY_FILLED",
                "p": "98",
                "q": "0.30",
                "z": "0.10",
                "ap": "98",
                "R": "",
                "i": 11,
                "T": 1_000,
            },
        }
        values.update(overrides)
        return values

    def test_partial_fill_order_update_is_normalized(self):
        event = parse_private_event(self.order_payload())
        self.assertIsInstance(event, OrderUpdate)
        self.assertEqual(event.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(event.position_side, Side.LONG)
        self.assertEqual(event.side, OrderSide.BUY)
        self.assertEqual(event.remaining_quantity, D("0.20"))

    def test_rejected_order_update_keeps_the_reason(self):
        payload = self.order_payload()
        payload["o"].update({"X": "REJECTED", "z": "0", "R": "post-only would cross"})
        event = parse_private_event(payload)
        self.assertEqual(event.status, OrderStatus.REJECTED)
        self.assertEqual(event.reject_reason, "post-only would cross")

    def test_account_update_maps_long_and_short_positions(self):
        payload = {
            "e": "ACCOUNT_UPDATE",
            "E": 1_200,
            "a": {
                "m": "ORDER",
                "P": [
                    {"s": SYMBOL, "pa": "0.30", "ep": "98", "ps": "LONG"},
                    {"s": SYMBOL, "pa": "-0.20", "ep": "102", "ps": "SHORT"},
                    {"s": SYMBOL, "pa": "0", "ep": "0"},
                ],
            },
        }
        event = parse_private_event(payload)
        self.assertIsInstance(event, AccountUpdate)
        self.assertEqual(event.reason, "ORDER")
        self.assertEqual(
            [(item.side, item.quantity) for item in event.positions],
            [(Side.LONG, D("0.30")), (Side.SHORT, D("0.20"))],
        )

    def test_listen_key_expiry_and_unsupported_events(self):
        self.assertIsInstance(parse_private_event({"e": "listenKeyExpired", "E": 1_300}), ListenKeyExpired)
        with self.assertRaises(UnsupportedStreamEvent):
            parse_private_event({"e": "MARGIN_CALL", "E": 1_300})

    def test_malformed_order_update_fails_closed(self):
        payload = self.order_payload()
        del payload["o"]["z"]
        with self.assertRaises(StreamDataError):
            parse_private_event(payload)


class ListenKeyAndAssemblerTests(unittest.TestCase):
    def test_listen_key_expiry_and_keepalive_timing(self):
        key = ListenKeyState("key", 0, 0, validity_ms=60_000, keepalive_interval_ms=30_000)
        self.assertEqual(key.expires_at_ms, 60_000)
        self.assertFalse(key.needs_keepalive(29_999))
        self.assertTrue(key.needs_keepalive(30_000))
        self.assertFalse(key.expired(59_999))
        self.assertTrue(key.expired(60_000))
        with self.assertRaises(StreamDataError):
            key.on_keepalive(60_000)
        key.on_keepalive(30_000)
        self.assertEqual(key.remaining_ms(30_000), 30_000)
        with self.assertRaises(StreamDataError):
            key.on_keepalive(29_999)

    def test_assembler_requires_both_sides_and_rejects_backwards_events(self):
        assembler = MarketTickAssembler(SYMBOL)
        book = parse_public_event(
            {"e": "bookTicker", "s": SYMBOL, "b": "99.9", "B": "1", "a": "100.1", "A": "1", "E": 1_000}
        )
        trade = parse_public_event(
            {"e": "aggTrade", "s": SYMBOL, "p": "100", "q": "1", "T": 1_100, "m": False}
        )
        self.assertIsNone(assembler.on_event(book))
        tick = assembler.on_event(trade)
        self.assertEqual(tick.timestamp_ms, 1_000)
        self.assertIsNone(assembler.on_event(trade))
        with self.assertRaises(StreamDataError):
            assembler.on_event(parse_public_event(
                {"e": "bookTicker", "s": SYMBOL, "b": "99.8", "B": "1", "a": "100", "A": "1", "E": 999}
            ))

    def test_stream_urls_are_explicit(self):
        self.assertEqual(
            public_stream_url("ABCUSDT"),
            "wss://fstream.binance.com/stream?streams=abcusdt@bookTicker/abcusdt@aggTrade",
        )
        self.assertTrue(public_stream_url("ABCUSDT", testnet=True).startswith("wss://stream.binancefuture.com/"))
        self.assertEqual(private_stream_url("listen", testnet=True), "wss://stream.binancefuture.com/ws/listen")
        with self.assertRaises(StreamDataError):
            public_stream_url("")


class FakeStreamTransport:
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.current = []
        self.connects = 0
        self.closed = 0

    def connect(self):
        self.connects += 1
        self.current = list(self.scripts.pop(0))

    def recv_text(self):
        item = self.current.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.closed += 1


class StreamSessionTests(unittest.TestCase):
    def test_reconnects_after_a_transport_error_and_uses_backoff(self):
        transport = FakeStreamTransport([
            ["one", OSError("drop")],
            ["two"],
        ])
        health = StreamHealth(max_age_ms=1_000, last_message_ms=0)
        sleeps = []
        clock_value = [0]
        session = StreamSession(
            transport=transport,
            health=health,
            backoff=BackoffPolicy(base_ms=100, multiplier=2, max_ms=1_000, max_attempts=3),
            clock=lambda: clock_value[0],
            sleep=lambda seconds: sleeps.append(seconds),
        )
        received = []
        self.assertEqual(session.run(received.append, max_messages=2), 2)
        self.assertEqual(received, ["one", "two"])
        self.assertEqual(transport.connects, 2)
        self.assertEqual(transport.closed, 1)
        self.assertEqual(sleeps, [0.1])
        self.assertTrue(health.trading_ready(0))

    def test_exhausted_reconnect_attempts_fail_closed(self):
        transport = FakeStreamTransport([[OSError("down")], [OSError("down")]])
        health = StreamHealth(max_age_ms=1_000, last_message_ms=0)
        session = StreamSession(
            transport=transport,
            health=health,
            backoff=BackoffPolicy(base_ms=100, multiplier=2, max_ms=1_000, max_attempts=2),
            clock=lambda: 0,
            sleep=lambda _seconds: None,
        )
        with self.assertRaises(StreamDataError):
            session.run(lambda _text: None, max_messages=1)
        self.assertEqual(transport.connects, 2)
        self.assertEqual(transport.closed, 1)
        self.assertFalse(health.trading_ready(0))

class StreamHealthAndBackoffTests(unittest.TestCase):
    def test_health_lifecycle(self):
        health = StreamHealth(max_age_ms=1_000, last_message_ms=0)
        self.assertEqual(health.status(500), StreamStatus.HEALTHY)
        self.assertTrue(health.trading_ready(500))
        self.assertEqual(health.status(1_001), StreamStatus.STALE)
        self.assertFalse(health.trading_ready(1_001))
        health.on_disconnect(1_100)
        self.assertEqual(health.status(1_100), StreamStatus.DISCONNECTED)
        health.on_connect(1_200)
        self.assertEqual(health.status(1_200), StreamStatus.HEALTHY)
        health.on_listen_key_expired(1_300)
        self.assertEqual(health.status(1_300), StreamStatus.KEY_EXPIRED)
        self.assertFalse(health.trading_ready(1_300))
        health.on_listen_key_created(1_400)
        self.assertEqual(health.status(1_400), StreamStatus.HEALTHY)

    def test_backwards_clock_is_rejected(self):
        health = StreamHealth(max_age_ms=1_000, last_message_ms=1_000)
        with self.assertRaises(StreamDataError):
            health.on_message(900)

    def test_backoff_is_deterministic_and_bounded(self):
        policy = BackoffPolicy(base_ms=500, multiplier=2, max_ms=30_000, max_attempts=6)
        self.assertEqual(
            [policy.delay_ms(attempt) for attempt in range(6)],
            [500, 1_000, 2_000, 4_000, 8_000, 16_000],
        )
        self.assertTrue(policy.exhausted(6))
        with self.assertRaises(StreamDataError):
            policy.delay_ms(6)
        capped = BackoffPolicy(base_ms=1_000, multiplier=3, max_ms=5_000, max_attempts=5)
        self.assertEqual(capped.delay_ms(3), 5_000)


if __name__ == "__main__":
    unittest.main()
