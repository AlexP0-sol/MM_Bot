import json
import unittest
from dataclasses import replace
from decimal import Decimal as D

from gridbot.domain import Side
from gridbot.engine import EngineState, ExitPolicy, PaperGridEngine
from gridbot.exchange import (
    Aggressor,
    CancelResult,
    LiveTradingUnavailable,
    OrderRequest,
    OrderRole,
    OrderStatus,
    UnsupportedModeError,
    live_order_port,
)
from gridbot.paper import PaperExchange
from tests.support import (
    engine,
    filters,
    live_proposal,
    paper_config,
    make_risk_context,
    proposal,
    risk_limits,
    tick,
    SYMBOL,
)


class EngineTestBase(unittest.TestCase):
    def started(self, *, exit_policy=None, exchange=None):
        exchange = exchange or PaperExchange(paper_config())
        eng = engine(exchange=exchange, exit_policy=exit_policy)
        result = eng.start(proposal=proposal(), generation_id="gen-1", tick=tick(1_000), now_ts=1_000)
        self.assertTrue(result.accepted, result.reason)
        return eng, exchange

    def open_orders(self, exchange, *, role=None, side=None):
        return tuple(
            order
            for order in exchange.open_orders()
            if (role is None or order.role is role) and (side is None or order.position_side is side)
        )

    def tampered_proposal(self):
        plan = proposal()
        order = replace(
            plan.long_orders[1],
            price=D("100.2"),
            notional_quote=plan.long_orders[1].quantity * D("100.2"),
        )
        long_total = plan.long_orders[0].notional_quote + order.notional_quote
        return replace(
            plan,
            long_orders=(plan.long_orders[0], order),
            long_notional_quote=long_total,
            gross_notional_quote=long_total + plan.short_notional_quote,
            signed_net_notional_quote=long_total - plan.short_notional_quote,
        )


class EngineLifecycleTests(EngineTestBase):
    def test_start_places_both_sides_with_deterministic_ids_and_refuses_live(self):
        eng, exchange = self.started()
        self.assertEqual(eng.state, EngineState.ACTIVE)
        ids = {order.client_order_id for order in exchange.open_orders()}
        self.assertEqual(
            ids,
            {
                "gen-1:LONG:1:ENTRY",
                "gen-1:LONG:2:ENTRY",
                "gen-1:SHORT:1:ENTRY",
                "gen-1:SHORT:2:ENTRY",
            },
        )
        second = eng.start(proposal=proposal(), generation_id="gen-2", tick=tick(1_100), now_ts=1_100)
        self.assertFalse(second.accepted)
        self.assertIn("ACTIVE", second.reason)

        fresh = engine()
        with self.assertRaises(UnsupportedModeError):
            fresh.start(proposal=live_proposal(), generation_id="gen-live", tick=tick(1_000), now_ts=1_000)
        with self.assertRaises(LiveTradingUnavailable):
            live_order_port()

    def test_rejected_entry_cancels_accepted_orders_and_requires_reconciliation(self):
        tampered = self.tampered_proposal()
        exchange = PaperExchange(paper_config())
        eng = engine(exchange=exchange)
        result = eng.start(proposal=tampered, generation_id="gen-bad", tick=tick(1_000), now_ts=1_000)
        self.assertFalse(result.accepted)
        self.assertIn("post-only", result.reason)
        self.assertEqual(eng.state, EngineState.RECONCILIATION_REQUIRED)
        self.assertEqual(exchange.open_orders(), ())
        self.assertEqual(exchange.order("gen-bad:LONG:1:ENTRY").status, OrderStatus.CANCELED)

    def test_entry_fill_cancels_remaining_levels_and_places_one_exit(self):
        eng, exchange = self.started()
        fills = eng.on_tick(tick(1_100, bid="97.9", ask="98.1", last="98", quantity="10"), now_ts=1_100)
        self.assertEqual(len(fills), 1)
        self.assertEqual(eng.position(Side.LONG), (D("0.30"), D("98")))
        self.assertEqual(exchange.order("gen-1:LONG:2:ENTRY").status, OrderStatus.CANCELED)
        exits = self.open_orders(exchange, role=OrderRole.EXIT)
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].position_side, Side.LONG)
        self.assertEqual(exits[0].quantity, D("0.30"))
        self.assertGreater(exits[0].price, D("98"))
        self.assertEqual(len(self.open_orders(exchange, role=OrderRole.ENTRY, side=Side.SHORT)), 2)

    def test_gap_through_levels_uses_vwap_for_a_single_exit(self):
        eng, exchange = self.started()
        fills = eng.on_tick(tick(1_100, bid="94.9", ask="95.1", last="95", quantity="100"), now_ts=1_100)
        self.assertEqual(len(fills), 2)
        self.assertEqual(eng.position(Side.LONG), (D("0.50"), D("97.2")))
        exits = self.open_orders(exchange, role=OrderRole.EXIT)
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].quantity, D("0.50"))
        self.assertEqual(exits[0].price, D("97.5"))
        self.assertFalse(self.open_orders(exchange, role=OrderRole.ENTRY, side=Side.LONG))

    def test_partial_exit_keeps_the_remaining_target_and_does_not_reverse(self):
        eng, exchange = self.started()
        eng.on_tick(tick(1_100, bid="97.9", ask="98.1", last="98", quantity="10"), now_ts=1_100)
        fills = eng.on_tick(tick(1_200, bid="98.4", ask="98.6", last="98.5", quantity="0.1",
                                aggressor=Aggressor.BUY), now_ts=1_200)
        self.assertEqual(len(fills), 1)
        self.assertEqual(eng.position(Side.LONG)[0], D("0.20"))
        exits = self.open_orders(exchange, role=OrderRole.EXIT)
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].remaining_quantity, D("0.20"))
        eng.on_tick(tick(1_300, bid="98.4", ask="98.6", last="98.5", quantity="1",
                         aggressor=Aggressor.BUY), now_ts=1_300)
        self.assertEqual(eng.position(Side.LONG)[0], D("0"))
        self.assertFalse(self.open_orders(exchange, role=OrderRole.EXIT))

    def test_drain_cancels_entries_keeps_exit_and_stops_when_flat(self):
        eng, exchange = self.started()
        eng.on_tick(tick(1_100, bid="97.9", ask="98.1", last="98", quantity="10"), now_ts=1_100)
        self.assertTrue(eng.drain(1_200))
        self.assertEqual(eng.state, EngineState.DRAINING)
        self.assertFalse(self.open_orders(exchange, role=OrderRole.ENTRY))
        self.assertEqual(len(self.open_orders(exchange, role=OrderRole.EXIT)), 1)
        eng.on_tick(tick(1_300, bid="98.4", ask="98.6", last="98.5", quantity="1",
                         aggressor=Aggressor.BUY), now_ts=1_300)
        self.assertEqual(eng.state, EngineState.STOPPED)

    def test_simultaneous_positions_close_market_and_stop(self):
        eng, exchange = self.started(exit_policy=ExitPolicy(D("0"), D("0.01"), D("20")))
        eng.on_tick(tick(1_100, bid="94.9", ask="95.1", last="95", quantity="100"), now_ts=1_100)
        eng.on_tick(tick(1_200, bid="103.9", ask="104.1", last="104", quantity="100",
                         aggressor=Aggressor.BUY), now_ts=1_200)
        self.assertEqual(eng.position(Side.LONG)[0], D("0.50"))
        self.assertEqual(eng.position(Side.SHORT)[0], D("0.48"))
        self.assertEqual(len(self.open_orders(exchange, role=OrderRole.EXIT)), 2)
        self.assertTrue(eng.close_market_and_stop(1_300))
        self.assertEqual(eng.state, EngineState.STOPPED)
        self.assertEqual(eng.position(Side.LONG)[0], D("0"))
        self.assertEqual(eng.position(Side.SHORT)[0], D("0"))
        self.assertEqual(exchange.open_orders(), ())
        self.assertTrue(any(fill.taker for fill in exchange.fills))

    def test_close_market_without_a_book_is_not_reported_as_stopped(self):
        eng, exchange = self.started()
        eng.on_tick(tick(1_100, bid="97.9", ask="98.1", last="98", quantity="10"), now_ts=1_100)
        exchange._ticks.clear()
        self.assertFalse(eng.close_market_and_stop(1_200))
        self.assertEqual(eng.state, EngineState.RECONCILIATION_REQUIRED)
        self.assertNotEqual(eng.position(Side.LONG)[0], D("0"))

    def test_stale_tick_pauses_and_a_fresh_tick_resumes(self):
        eng, exchange = self.started()
        result = eng.on_tick(tick(1_100, bid="97.9", ask="98.1", last="98", quantity="10"), now_ts=6_101)
        self.assertEqual(result, ())
        self.assertEqual(eng.state, EngineState.PAUSED)
        self.assertEqual(eng.pause_reason, "STALE_FEED")
        fills = eng.on_tick(tick(1_200, bid="97.9", ask="98.1", last="98", quantity="10"), now_ts=1_200)
        self.assertEqual(len(fills), 1)
        self.assertEqual(eng.state, EngineState.ACTIVE)

    def test_close_market_refuses_a_stale_snapshot(self):
        eng, exchange = self.started()
        eng.on_tick(tick(1_100, bid="97.9", ask="98.1", last="98", quantity="10"), now_ts=1_100)
        eng.on_tick(tick(1_200, bid="97.9", ask="98.1", last="98", quantity="1"), now_ts=6_201)
        self.assertEqual(eng.state, EngineState.PAUSED)
        self.assertFalse(eng.close_market_and_stop(6_300))
        self.assertEqual(eng.state, EngineState.RECONCILIATION_REQUIRED)
        self.assertNotEqual(eng.position(Side.LONG)[0], D("0"))

    def test_exit_hurdle_failure_pauses_without_market_fallback(self):
        eng, exchange = self.started(exit_policy=ExitPolicy(D("0"), D("0.01"), D("1000")))
        eng.on_tick(tick(1_100, bid="101.9", ask="102.1", last="102", quantity="10",
                         aggressor=Aggressor.BUY), now_ts=1_100)
        self.assertEqual(eng.state, EngineState.PAUSED)
        self.assertTrue(eng.pause_reason.startswith("EXIT_HURDLE"))
        self.assertEqual(eng.position(Side.SHORT)[0], D("0.29"))
        self.assertEqual(exchange.open_orders(), ())
        self.assertFalse(any(fill.taker for fill in exchange.fills))

    def test_one_side_flat_does_not_rearm_that_side(self):
        eng, exchange = self.started()
        eng.on_tick(tick(1_100, bid="97.9", ask="98.1", last="98", quantity="10"), now_ts=1_100)
        eng.on_tick(tick(1_200, bid="98.4", ask="98.6", last="98.5", quantity="1",
                         aggressor=Aggressor.BUY), now_ts=1_200)
        self.assertEqual(eng.position(Side.LONG)[0], D("0"))
        eng.on_tick(tick(1_300, bid="89.9", ask="90.1", last="90", quantity="100"), now_ts=1_300)
        self.assertEqual(eng.position(Side.LONG)[0], D("0"))
        self.assertFalse(self.open_orders(exchange, role=OrderRole.ENTRY, side=Side.LONG))
        self.assertFalse(self.open_orders(exchange, role=OrderRole.EXIT, side=Side.LONG))
        self.assertEqual(len(self.open_orders(exchange, role=OrderRole.ENTRY, side=Side.SHORT)), 2)


class EngineSafetyTests(EngineTestBase):
    def test_exit_target_includes_the_actual_entry_fee(self):
        eng, exchange = self.started()
        eng.on_tick(tick(1_100, bid="97.9", ask="98.1", last="98", quantity="10"), now_ts=1_100)
        exits = self.open_orders(exchange, role=OrderRole.EXIT)
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].price, D("98.5"))
        self.assertGreater(exits[0].price, D("98.4"))

    def test_filter_change_pauses_when_a_live_exit_is_no_longer_valid(self):
        eng, exchange = self.started()
        eng.on_tick(tick(1_100, bid="97.9", ask="98.1", last="98", quantity="10"), now_ts=1_100)
        canceled = eng.update_filters(filters(tick_size=D("1")), 1_200)
        self.assertEqual(canceled, ())
        self.assertEqual(eng.state, EngineState.PAUSED)
        self.assertEqual(eng.pause_reason, "EXIT_FILTER_VIOLATION")
        self.assertEqual(len(self.open_orders(exchange, role=OrderRole.EXIT)), 1)
        self.assertNotEqual(eng.position(Side.LONG)[0], D("0"))

    def test_start_rejects_mismatched_notional_totals(self):
        plan = replace(proposal(), long_notional_quote=D("0"))
        eng = engine()
        result = eng.start(proposal=plan, generation_id="gen-mismatch", tick=tick(1_000), now_ts=1_000)
        self.assertFalse(result.accepted)
        self.assertIn("notional totals", result.reason)
        self.assertEqual(eng.state, EngineState.IDLE)

    def test_mark_to_market_breach_cancels_the_other_legs_entries(self):
        tight = replace(risk_limits(), max_gross_notional=D("120"))
        eng = engine(exchange=PaperExchange(paper_config()), risk_context=make_risk_context(limits=tight))
        started = eng.start(proposal=proposal(), generation_id="gen-risk", tick=tick(1_000), now_ts=1_000)
        self.assertTrue(started.accepted, started.reason)
        eng.on_tick(tick(1_100, bid="94.9", ask="95.1", last="95", quantity="100"), now_ts=1_100)
        self.assertEqual(eng.position(Side.LONG)[0], D("0.5"))
        self.assertEqual(len(self.open_orders(eng.exchange, role=OrderRole.ENTRY, side=Side.SHORT)), 2)
        eng.on_tick(tick(1_200, bid="199.9", ask="200.1", last="200", quantity="1",
                         aggressor=Aggressor.SELL), now_ts=1_200)
        self.assertEqual(eng.state, EngineState.PAUSED)
        self.assertTrue(eng.pause_reason.startswith("COMBINED_RISK"))
        self.assertFalse(self.open_orders(eng.exchange, role=OrderRole.ENTRY))
        self.assertEqual(eng.position(Side.LONG)[0], D("0.5"))

    def test_close_market_does_not_stop_if_cancel_is_unconfirmed(self):
        class StuckCancelExchange(PaperExchange):
            def cancel(self, client_order_id, now_ts):
                order = self._orders.get(client_order_id)
                if order is not None and order.request.role is OrderRole.EXIT and order.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
                    return CancelResult(False, order.status, "simulated cancel failure")
                return super().cancel(client_order_id, now_ts)

        exchange = StuckCancelExchange(paper_config())
        eng, exchange = self.started(exchange=exchange)
        eng.on_tick(tick(1_100, bid="97.9", ask="98.1", last="98", quantity="10"), now_ts=1_100)
        self.assertFalse(eng.close_market_and_stop(1_200))
        self.assertEqual(eng.state, EngineState.RECONCILIATION_REQUIRED)
        self.assertNotEqual(eng.position(Side.LONG)[0], D("0"))


class EngineRecoveryTests(EngineTestBase):
    def test_restart_restores_state_reconciles_and_continues(self):
        eng, exchange = self.started()
        eng.on_tick(tick(1_100, bid="97.9", ask="98.1", last="98", quantity="10"), now_ts=1_100)
        exchange_state = exchange.to_state()
        engine_state = eng.to_state()
        self.assertIsInstance(json.dumps(exchange_state), str)
        self.assertIsInstance(json.dumps(engine_state), str)
        self.assertEqual(json.loads(json.dumps(engine_state))["state"], "ACTIVE")

        restored_exchange = PaperExchange.from_state(paper_config(), exchange_state)
        restored = PaperGridEngine.from_state(
            exchange=restored_exchange,
            filters=filters(),
            exit_policy=ExitPolicy(D("0"), D("0.01"), D("0.1")),
            risk_context=make_risk_context(),
            max_tick_age_ms=5_000,
            queue_ahead_quantity=D("0"),
            state=engine_state,
        )
        self.assertEqual(restored.to_state(), engine_state)
        blocked = restored.on_tick(
            tick(1_150, bid="98.4", ask="98.6", last="98.5", quantity="1", aggressor=Aggressor.BUY),
            now_ts=1_150,
        )
        self.assertEqual(blocked, ())
        self.assertFalse(restored.drain(1_160))
        self.assertTrue(restored.reconcile())
        fills = restored.on_tick(
            tick(1_200, bid="98.4", ask="98.6", last="98.5", quantity="1", aggressor=Aggressor.BUY),
            now_ts=1_200,
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(restored.position(Side.LONG)[0], D("0"))
        self.assertTrue(restored.drain(1_300))
        self.assertEqual(restored.state, EngineState.STOPPED)

    def test_new_generation_after_stopped_uses_fresh_per_generation_ids(self):
        eng, exchange = self.started()
        self.assertTrue(eng.drain(1_100))
        self.assertEqual(eng.state, EngineState.STOPPED)
        self.assertEqual(exchange.open_orders(), ())
        restarted = eng.start(proposal=proposal(), generation_id="gen-2", tick=tick(1_200), now_ts=1_200)
        self.assertTrue(restarted.accepted)
        ids = {order.client_order_id for order in exchange.open_orders()}
        self.assertEqual(len(ids), 4)
        self.assertTrue(all(client_id.startswith("gen-2:") for client_id in ids))
        self.assertEqual(
            eng.to_state()["entry_ids"]["LONG"],
            ["gen-2:LONG:1:ENTRY", "gen-2:LONG:2:ENTRY"],
        )

    def test_unexplained_position_breaks_reconciliation(self):
        eng, exchange = self.started()
        eng.on_tick(tick(1_100, bid="97.9", ask="98.1", last="98", quantity="10"), now_ts=1_100)
        position = exchange.account.positions[(SYMBOL, Side.LONG)]
        position.quantity += D("1")
        self.assertFalse(eng.reconcile())
        self.assertEqual(eng.state, EngineState.RECONCILIATION_REQUIRED)

    def test_start_rejects_a_stale_proposal_snapshot(self):
        eng = engine()
        result = eng.start(
            proposal=proposal(), generation_id="gen-late", tick=tick(10_000), now_ts=10_000
        )
        self.assertFalse(result.accepted)
        self.assertIn("proposal snapshot", result.reason)

    def test_unknown_external_order_breaks_reconciliation(self):
        eng, exchange = self.started()
        eng.on_tick(tick(1_100, bid="97.9", ask="98.1", last="98", quantity="10"), now_ts=1_100)
        engine_state = eng.to_state()
        restored_exchange = PaperExchange.from_state(paper_config(), exchange.to_state())
        extra = restored_exchange.place(
            OrderRequest(
                client_order_id="external", symbol=SYMBOL, position_side=Side.LONG,
                role=OrderRole.ENTRY, price=D("90"), quantity=D("0.02"), queue_ahead=D("0"),
                created_ts=1_200,
            )
        )
        self.assertTrue(extra.accepted)
        restored = PaperGridEngine.from_state(
            exchange=restored_exchange,
            filters=filters(),
            exit_policy=ExitPolicy(D("0"), D("0.01"), D("0.1")),
            risk_context=make_risk_context(),
            max_tick_age_ms=5_000,
            queue_ahead_quantity=D("0"),
            state=engine_state,
        )
        self.assertFalse(restored.reconcile())
        self.assertEqual(restored.state, EngineState.RECONCILIATION_REQUIRED)

    def test_filters_update_cancels_only_entries_that_now_violate_bounds(self):
        eng, exchange = self.started()
        canceled = eng.update_filters(filters(min_notional=D("25")), 1_100)
        self.assertEqual(
            set(canceled),
            {"gen-1:LONG:2:ENTRY", "gen-1:SHORT:2:ENTRY"},
        )
        self.assertEqual(
            {order.client_order_id for order in exchange.open_orders()},
            {"gen-1:LONG:1:ENTRY", "gen-1:SHORT:1:ENTRY"},
        )
        self.assertEqual(eng.state, EngineState.ACTIVE)

    def test_no_fill_drain_cancels_everything_and_stops(self):
        eng, exchange = self.started()
        self.assertEqual(eng.on_tick(tick(1_100, last="100", quantity="10",
                                          aggressor=Aggressor.BUY), now_ts=1_100), ())
        self.assertTrue(eng.drain(1_200))
        self.assertEqual(eng.state, EngineState.STOPPED)
        self.assertEqual(exchange.open_orders(), ())

    def test_trend_without_pullback_keeps_the_short_exit_resting(self):
        eng, exchange = self.started()
        fills = eng.on_tick(tick(1_100, bid="101.9", ask="102.1", last="102", quantity="10",
                                 aggressor=Aggressor.BUY), now_ts=1_100)
        self.assertEqual(len(fills), 1)
        self.assertEqual(eng.position(Side.SHORT)[0], D("0.29"))
        self.assertEqual(exchange.order("gen-1:SHORT:2:ENTRY").status, OrderStatus.CANCELED)
        self.assertFalse(self.open_orders(exchange, role=OrderRole.ENTRY, side=Side.SHORT))
        self.assertEqual(len(self.open_orders(exchange, role=OrderRole.ENTRY, side=Side.LONG)), 2)
        for step, price in enumerate(("103", "104", "105"), start=1):
            fills = eng.on_tick(
                tick(1_100 + step * 100, bid=f"{float(price) - 0.1:.1f}", ask=f"{float(price) + 0.1:.1f}",
                     last=price, quantity="10", aggressor=Aggressor.BUY),
                now_ts=1_100 + step * 100,
            )
            self.assertEqual(fills, ())
        exits = self.open_orders(exchange, role=OrderRole.EXIT)
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].position_side, Side.SHORT)
        self.assertLess(exits[0].price, D("102"))
        self.assertEqual(eng.position(Side.SHORT)[0], D("0.29"))

    def test_rejected_entry_can_be_explicitly_recovered_after_reconciliation(self):
        tampered = self.tampered_proposal()
        exchange = PaperExchange(paper_config())
        eng = engine(exchange=exchange)
        self.assertFalse(
            eng.start(proposal=tampered, generation_id="gen-bad", tick=tick(1_000), now_ts=1_000).accepted
        )
        self.assertEqual(eng.state, EngineState.RECONCILIATION_REQUIRED)
        self.assertTrue(eng.resume_after_reconciliation(1_100))
        self.assertEqual(eng.state, EngineState.STOPPED)
        restarted = eng.start(proposal=proposal(), generation_id="gen-2", tick=tick(1_200), now_ts=1_200)
        self.assertTrue(restarted.accepted, restarted.reason)

    def test_start_requires_a_flat_venue(self):
        eng, exchange = self.started()
        other = engine(exchange=exchange)
        result = other.start(proposal=proposal(), generation_id="gen-2", tick=tick(1_100), now_ts=1_100)
        self.assertFalse(result.accepted)
        self.assertIn("not flat", result.reason)


if __name__ == "__main__":
    unittest.main()
