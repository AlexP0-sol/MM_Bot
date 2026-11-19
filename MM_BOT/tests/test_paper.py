import unittest
from decimal import Decimal as D

from gridbot.domain import DomainError, Side
from gridbot.exchange import Aggressor, MarketDataError, OrderRequest, OrderRole, OrderStatus
from gridbot.paper import PaperConfig, PaperExchange, PaperFees
from tests.support import SYMBOL, paper_config, tick


def request(
    client_order_id: str,
    *,
    side: Side = Side.LONG,
    role: OrderRole = OrderRole.ENTRY,
    price: str = "98",
    quantity: str = "1",
    queue: str = "0",
    created_ts: int = 0,
) -> OrderRequest:
    return OrderRequest(
        client_order_id=client_order_id,
        symbol=SYMBOL,
        position_side=side,
        role=role,
        price=D(price),
        quantity=D(quantity),
        queue_ahead=D(queue),
        created_ts=created_ts,
    )


class PaperExchangeTests(unittest.TestCase):
    def exchange(self, **overrides) -> PaperExchange:
        exchange = PaperExchange(paper_config(**overrides))
        exchange.update_market(tick(1_000))
        return exchange

    def test_post_only_rejects_crossing_order_and_keeps_it_visible(self):
        exchange = self.exchange()
        result = exchange.place(request("cross", price="100.1"))
        self.assertFalse(result.accepted)
        self.assertEqual(result.status, OrderStatus.REJECTED)
        self.assertIn("post-only", result.reason)
        self.assertEqual(exchange.open_orders(), ())
        stored = exchange.order("cross")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, OrderStatus.REJECTED)
        self.assertTrue(exchange.place(request("passive", price="100.0")).accepted)

    def test_misaligned_tick_or_step_is_rejected_locally(self):
        exchange = self.exchange()
        price = exchange.place(request("bad-price", price="98.05"))
        self.assertFalse(price.accepted)
        self.assertIn("tick/step", price.reason)
        quantity = exchange.place(request("bad-step", price="98", quantity="0.105"))
        self.assertFalse(quantity.accepted)
        self.assertIn("tick/step", quantity.reason)
        below_notional = exchange.place(request("below-notional", price="90", quantity="0.01"))
        self.assertFalse(below_notional.accepted)
        self.assertIn("minNotional", below_notional.reason)
        larger_min = PaperExchange(paper_config(min_qty=D("0.02")))
        larger_min.update_market(tick(1_000))
        tiny = larger_min.place(request("tiny", price="98", quantity="0.01"))
        self.assertFalse(tiny.accepted)
        self.assertIn("minQty", tiny.reason)

    def test_touch_does_not_fill_until_explicit_queue_is_consumed(self):
        exchange = self.exchange(latency_ms=100)
        accepted = exchange.place(request("q", queue="2", created_ts=1_000))
        self.assertTrue(accepted.accepted)
        self.assertEqual(exchange.on_tick(tick(1_050, last="98", quantity="5")), ())
        self.assertEqual(exchange.order("q").status, OrderStatus.NEW)
        fills = exchange.on_tick(tick(1_100, last="98", quantity="2"))
        self.assertEqual(len(fills), 0)
        self.assertEqual(exchange.order("q").status, OrderStatus.NEW)
        fills = exchange.on_tick(tick(1_200, last="98", quantity="1"))
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].quantity, D("1"))
        self.assertEqual(exchange.order("q").status, OrderStatus.FILLED)

    def test_partial_then_full_fill_charges_maker_fees(self):
        exchange = self.exchange()
        exchange.place(request("p", quantity="1"))
        first = exchange.on_tick(tick(1_100, last="98", quantity="0.4"))
        self.assertEqual(len(first), 1)
        self.assertEqual(exchange.order("p").status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(exchange.account.balance_quote, D("999.99216"))
        second = exchange.on_tick(tick(1_200, last="98", quantity="0.6"))
        self.assertEqual(len(second), 1)
        self.assertEqual(exchange.order("p").status, OrderStatus.FILLED)
        self.assertEqual(exchange.account.balance_quote, D("999.9804"))
        self.assertEqual(exchange.account.position_quantity(SYMBOL, Side.LONG), D("1"))
        self.assertEqual(exchange.account.position_vwap(SYMBOL, Side.LONG), D("98"))

    def test_trade_through_limit_bypasses_the_queue(self):
        exchange = self.exchange(latency_ms=100)
        exchange.place(request("sweep", queue="5", created_ts=1_000))
        fills = exchange.on_tick(tick(1_100, last="97", quantity="1"))
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].quantity, D("1"))

    def test_aggressor_and_price_must_both_match(self):
        exchange = self.exchange()
        exchange.place(request("buy", price="98"))
        self.assertEqual(exchange.on_tick(tick(1_100, last="97", aggressor=Aggressor.BUY)), ())
        self.assertEqual(exchange.on_tick(tick(1_200, last="99", aggressor=Aggressor.SELL)), ())
        self.assertEqual(exchange.order("buy").status, OrderStatus.NEW)

    def test_cancel_prevents_later_fill_and_completed_fill_blocks_cancel(self):
        exchange = self.exchange()
        exchange.place(request("cancel-me"))
        self.assertTrue(exchange.cancel("cancel-me", 1_050).applied)
        self.assertEqual(exchange.on_tick(tick(1_100, last="98", quantity="5")), ())
        self.assertEqual(exchange.order("cancel-me").status, OrderStatus.CANCELED)

        exchange.place(request("filled"))
        self.assertEqual(len(exchange.on_tick(tick(1_200, last="98", quantity="5"))), 1)
        result = exchange.cancel("filled", 1_250)
        self.assertFalse(result.applied)
        self.assertEqual(result.status, OrderStatus.FILLED)

    def test_duplicate_client_id_is_idempotent_and_fills_once(self):
        exchange = self.exchange()
        first = exchange.place(request("dup", quantity="1"))
        second = exchange.place(request("dup", quantity="1"))
        self.assertTrue(first.accepted)
        self.assertTrue(second.duplicate)
        self.assertEqual(len(exchange.orders()), 1)
        self.assertEqual(len(exchange.on_tick(tick(1_100, last="98", quantity="5"))), 1)
        self.assertEqual(len(exchange.fills), 1)
        self.assertEqual(exchange.account.position_quantity(SYMBOL, Side.LONG), D("1"))

    def test_exit_cannot_exceed_or_reverse_the_position(self):
        exchange = self.exchange()
        result = exchange.place(request("exit", side=Side.LONG, role=OrderRole.EXIT, price="102"))
        self.assertFalse(result.accepted)
        self.assertIn("exceeds", result.reason)

    def test_market_close_uses_bid_ask_with_explicit_slippage_and_taker_fee(self):
        exchange = self.exchange(market_close_slippage_ticks=1)
        exchange.place(request("entry", quantity="1"))
        exchange.on_tick(tick(1_100, bid="98", ask="98.1", last="98", quantity="5"))
        close = exchange.close_market(SYMBOL, Side.LONG, D("1"), 1_200)
        self.assertTrue(close.accepted)
        self.assertEqual(close.fill.price, D("97.9"))
        self.assertTrue(close.fill.taker)
        self.assertEqual(exchange.account.position_quantity(SYMBOL, Side.LONG), D("0"))
        self.assertEqual(exchange.account.realized_pnl_quote, D("-0.1"))
        self.assertEqual(exchange.account.balance_quote, D("999.83145"))

    def test_funding_sign_is_position_side_aware(self):
        exchange = self.exchange()
        exchange.place(request("long", side=Side.LONG, quantity="1", price="100"))
        exchange.place(request("short", side=Side.SHORT, quantity="1", price="100"))
        exchange.on_tick(tick(1_100, last="100", quantity="5", aggressor=Aggressor.SELL))
        exchange.on_tick(tick(1_200, last="100", quantity="5", aggressor=Aggressor.BUY))
        self.assertEqual(exchange.account.position_quantity(SYMBOL, Side.LONG), D("1"))
        self.assertEqual(exchange.account.position_quantity(SYMBOL, Side.SHORT), D("1"))
        total = exchange.apply_funding(D("0.001"), 1_300)
        self.assertEqual(total, D("0"))
        self.assertEqual(exchange.account.funding_paid_quote, D("0"))
        exchange.close_market(SYMBOL, Side.SHORT, D("1"), 1_400)
        total = exchange.apply_funding(D("0.001"), 1_500)
        self.assertGreater(total, D("0"))
        self.assertEqual(total, D("0.1"))

    def test_margin_is_checked_against_fresh_paper_equity(self):
        exchange = PaperExchange(paper_config(initial_balance_quote=D("10"), margin_fraction=D("0.9")))
        exchange.update_market(tick(1_000))
        rejected = exchange.place(request("big", price="100", quantity="1"))
        self.assertFalse(rejected.accepted)
        self.assertIn("margin", rejected.reason)

        funded = PaperExchange(paper_config(initial_balance_quote=D("60"), margin_fraction=D("0.5")))
        funded.update_market(tick(1_000))
        self.assertTrue(funded.place(request("first", price="100", quantity="1")).accepted)
        funded.on_tick(tick(1_100, last="100", quantity="5"))
        second = funded.place(request("second", price="100", quantity="0.3"))
        self.assertFalse(second.accepted)
        self.assertIn("margin", second.reason)

    def test_open_entry_orders_reserve_margin_before_fill(self):
        exchange = PaperExchange(paper_config(initial_balance_quote=D("1000"), margin_fraction=D("0.5")))
        exchange.update_market(tick(1_000))
        self.assertTrue(exchange.place(request("first", price="100", quantity="10")).accepted)
        self.assertTrue(exchange.place(request("second", price="100", quantity="10")).accepted)
        self.assertEqual(exchange.reserved_margin(), D("1000"))
        third = exchange.place(request("third", price="100", quantity="10"))
        self.assertFalse(third.accepted)
        self.assertIn("margin", third.reason)

    def test_one_trade_volume_is_shared_across_matching_orders(self):
        exchange = self.exchange()
        self.assertTrue(exchange.place(request("high", price="99.5", quantity="0.5")).accepted)
        self.assertTrue(exchange.place(request("low", price="99", quantity="0.5")).accepted)
        fills = exchange.on_tick(tick(1_100, last="98.9", quantity="0.6"))
        self.assertEqual(sum((fill.quantity for fill in fills), D("0")), D("0.6"))
        self.assertEqual(exchange.order("high").status, OrderStatus.FILLED)
        self.assertEqual(exchange.order("low").filled_quantity, D("0.1"))
        self.assertEqual(exchange.order("low").remaining_quantity, D("0.4"))

    def test_market_close_rejects_quantity_above_position(self):
        exchange = self.exchange()
        exchange.place(request("entry", quantity="1"))
        exchange.on_tick(tick(1_100, last="98", quantity="5"))
        close = exchange.close_market(SYMBOL, Side.LONG, D("2"), 1_200)
        self.assertFalse(close.accepted)
        self.assertIn("exceeds", close.reason)
        self.assertEqual(exchange.account.position_quantity(SYMBOL, Side.LONG), D("1"))

    def test_mark_to_market_loss_reduces_available_margin(self):
        exchange = PaperExchange(paper_config(initial_balance_quote=D("100"), margin_fraction=D("0.5")))
        exchange.update_market(tick(1_000))
        self.assertTrue(exchange.place(request("loss", price="100", quantity="1")).accepted)
        exchange.on_tick(tick(1_100, last="100", quantity="5"))
        exchange.on_tick(tick(1_200, bid="60", ask="60.1", last="60", quantity="1"))
        self.assertEqual(exchange.account.equity({SYMBOL: D("60.05")}, D("1")), D("60.03"))
        rejected = exchange.place(request("more", price="60", quantity="1.1"))
        self.assertFalse(rejected.accepted)
        self.assertIn("margin", rejected.reason)

    def test_out_of_order_or_duplicate_ticks_are_rejected(self):
        exchange = self.exchange()
        exchange.on_tick(tick(1_100))
        with self.assertRaises(MarketDataError):
            exchange.on_tick(tick(1_100))
        with self.assertRaises(MarketDataError):
            exchange.update_market(tick(1_050))

    def test_state_roundtrip_preserves_orders_fills_and_positions(self):
        exchange = self.exchange()
        exchange.place(request("one", quantity="1", queue="1"))
        exchange.on_tick(tick(1_100, last="98", quantity="5"))
        state = exchange.to_state()
        restored = PaperExchange.from_state(paper_config(), state)
        self.assertEqual(restored.to_state(), state)
        self.assertEqual(restored.account.position_quantity(SYMBOL, Side.LONG), D("1"))
        self.assertEqual(len(restored.fills), 1)
        self.assertEqual(restored.order("one").status, OrderStatus.FILLED)

    def test_configuration_rejects_invalid_paper_inputs(self):
        with self.assertRaises(DomainError):
            PaperFees(D("1"), D("0.0005"))
        with self.assertRaises(DomainError):
            PaperConfig(PaperFees(D("0"), D("0")), D("0"), D("0.5"), D("1"), 0, D("0.1"), 0)


if __name__ == "__main__":
    unittest.main()
