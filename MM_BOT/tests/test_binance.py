import hashlib
import hmac
import json
import unittest
import urllib.parse
from decimal import Decimal as D

from gridbot.binance import (
    AccountState,
    BinanceApiError,
    BinanceConfig,
    BinanceCredentials,
    BinanceEnvironment,
    BinanceRestClient,
    BinanceTradingAdapter,
    ClockDriftError,
    HttpResponse,
    MetadataError,
    NewOrderOutcome,
    OrderIntent,
    OutcomeUnknown,
    PositionMode,
    RateLimited,
    TradingDisabledError,
    TESTNET_BASE_URL,
)
from gridbot.domain import DomainError, Side
from gridbot.exchange import OrderRole, OrderStatus

SECRET = "super-secret-value"
KEY = "test-api-key-123"
SYMBOL = "ABCUSDT"


def response(payload, status: int = 200, headers: dict | None = None) -> HttpResponse:
    return HttpResponse(status, headers or {}, json.dumps(payload).encode("utf-8"))


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, params, headers, timeout_s):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "pairs": list(params),
                "headers": dict(headers),
                "timeout_s": timeout_s,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected transport call")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


ACCOUNT = {
    "totalWalletBalance": "1000",
    "availableBalance": "800",
    "totalUnrealizedProfit": "5",
    "canTrade": True,
    "multiAssetsMargin": False,
    "time": 1_000_500,
}
POSITIONS = [
    {
        "symbol": SYMBOL,
        "positionSide": "LONG",
        "positionAmt": "0.30",
        "entryPrice": "98",
        "markPrice": "99",
        "unRealizedProfit": "0.30",
    },
    {
        "symbol": SYMBOL,
        "positionSide": "SHORT",
        "positionAmt": "-0.20",
        "entryPrice": "102",
        "markPrice": "101",
        "unRealizedProfit": "0.20",
    },
]
OPEN_ORDER = {
    "clientOrderId": "gen-1:LONG:1:ENTRY",
    "orderId": 11,
    "symbol": SYMBOL,
    "side": "BUY",
    "positionSide": "LONG",
    "status": "PARTIALLY_FILLED",
    "price": "98",
    "origQty": "0.30",
    "executedQty": "0.10",
    "avgPrice": "98",
    "reduceOnly": False,
    "updateTime": 1_000_400,
}
EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": SYMBOL,
            "baseAsset": "ABC",
            "quoteAsset": "USDT",
            "contractType": "PERPETUAL",
            "status": "TRADING",
            "contractMultiplier": "1",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "1000"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
                {"filterType": "PERCENT_PRICE", "multiplierUp": "1.05", "multiplierDown": "0.95", "multiplierDecimal": "4"},
            ],
        }
    ]
}


class BinanceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock_value = 1_000_000
        self.credentials = BinanceCredentials(KEY, SECRET)

    def client(self, responses, *, config=None, credentials=True):
        transport = FakeHttp(responses)
        client = BinanceRestClient(
            self.credentials if credentials else None,
            config=config or BinanceConfig(enable_trading=True),
            transport=transport,
            clock=lambda: self.clock_value,
        )
        return client, transport

    def preflight_responses(self, dual=True):
        return [
            response({"serverTime": 1_000_500}),
            response(EXCHANGE_INFO),
            response({"dualSidePosition": dual}),
            response(ACCOUNT),
            response(POSITIONS),
            response([OPEN_ORDER]),
            response({
                "symbol": SYMBOL,
                "markPrice": "99.5",
                "lastFundingRate": "0.0001",
                "nextFundingTime": 1_010_000,
                "time": 1_000_500,
            }),
            response([{
                "symbol": SYMBOL,
                "brackets": [{
                    "bracketNotionalCap": "100000",
                    "initialLeverage": 20,
                    "maintMarginRatio": "0.01",
                    "cum": "0",
                }],
            }]),
        ]


class CredentialsAndConfigTests(BinanceTestBase):
    def test_credentials_are_redacted(self):
        text = repr(self.credentials) + str(self.credentials)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(KEY, text)
        self.assertIn("***", text)

    def test_live_is_disabled_and_live_trading_is_impossible(self):
        with self.assertRaises(TradingDisabledError):
            BinanceConfig(environment=BinanceEnvironment.LIVE)
        with self.assertRaises(TradingDisabledError):
            BinanceConfig(environment=BinanceEnvironment.LIVE, allow_live=True, enable_trading=True)
        read_only_live = BinanceConfig(environment=BinanceEnvironment.LIVE, allow_live=True)
        self.assertFalse(read_only_live.enable_trading)

    def test_live_host_cannot_be_mislabeled_as_testnet(self):
        with self.assertRaises(TradingDisabledError):
            BinanceConfig(environment=BinanceEnvironment.TESTNET, base_url="https://fapi.binance.com")


    def test_trading_adapter_rejects_non_testnet_host(self):
        client, _ = self.client(
            [],
            config=BinanceConfig(base_url="https://testnet.binancefuture.com.evil.example", enable_trading=True),
        )
        with self.assertRaises(TradingDisabledError):
            BinanceTradingAdapter(client)

    def test_rest_order_mutations_require_explicit_testnet_trading(self):
        client, transport = self.client([], config=BinanceConfig())
        with self.assertRaises(TradingDisabledError):
            client.new_order([])
        with self.assertRaises(TradingDisabledError):
            client.cancel_order(SYMBOL, "cid")
        self.assertEqual(transport.calls, [])

    def test_trading_adapter_requires_explicit_enable(self):
        client, _ = self.client([], config=BinanceConfig())
        with self.assertRaises(TradingDisabledError):
            BinanceTradingAdapter(client)

    def test_no_withdraw_surface(self):
        client, _ = self.client([])
        self.assertFalse(any("withdraw" in name.lower() for name in dir(client)))


class SigningAndTimeTests(BinanceTestBase):
    def test_sync_time_and_hmac_signature(self):
        client, transport = self.client(
            [response({"serverTime": 1_000_500}), response([])]
        )
        offset = client.sync_time()
        self.assertEqual(offset, 500)
        self.assertEqual(client.timestamp(), 1_000_500)
        self.assertEqual(client.open_orders(), ())
        call = transport.calls[-1]
        self.assertEqual(call["method"], "GET")
        self.assertTrue(call["url"].startswith(TESTNET_BASE_URL))
        self.assertEqual(call["headers"]["X-MBX-APIKEY"], KEY)
        pairs = call["pairs"]
        self.assertEqual(pairs[-1][0], "signature")
        self.assertEqual(dict(pairs)["timestamp"], "1000500")
        self.assertEqual(dict(pairs)["recvWindow"], "5000")
        expected = hmac.new(SECRET.encode(), urllib.parse.urlencode(pairs[:-1]).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(pairs[-1][1], expected)

    def test_clock_drift_above_the_bound_blocks_signing(self):
        self.clock_value = 1_000_000
        client, _ = self.client([response({"serverTime": 1_100_000})])
        with self.assertRaises(ClockDriftError):
            client.sync_time()
        with self.assertRaises(ClockDriftError):
            client.timestamp()

    def test_signed_request_without_sync_is_refused(self):
        client, transport = self.client([])
        with self.assertRaises(ClockDriftError):
            client.open_orders()
        self.assertEqual(transport.calls, [])

    def test_rate_limit_and_weight_budget(self):
        client, transport = self.client(
            [
                response({"serverTime": 1_000_500}),
                response([], headers={"X-MBX-USED-WEIGHT-1M": "1200"}),
            ]
        )
        client.sync_time()
        client.open_orders()
        with self.assertRaises(RateLimited):
            client.open_orders()
        self.assertEqual(len(transport.calls), 2)

    def test_http_429_exposes_retry_after(self):
        client, _ = self.client(
            [
                response({"serverTime": 1_000_500}),
                response({"code": -1003, "msg": "Too many requests"}, status=429,
                         headers={"Retry-After": "1.5"}),
            ]
        )
        client.sync_time()
        with self.assertRaises(RateLimited) as raised:
            client.open_orders()
        self.assertEqual(raised.exception.retry_after_s, 1.5)


class MetadataAndAccountTests(BinanceTestBase):
    def test_exchange_info_maps_to_domain_filters(self):
        client, _ = self.client([response(EXCHANGE_INFO)])
        metadata = client.symbol_metadata(SYMBOL)
        filters = metadata.filters()
        self.assertEqual(filters.tick_size, D("0.1"))
        self.assertEqual(filters.step_size, D("0.01"))
        self.assertEqual(filters.min_qty, D("0.01"))
        self.assertEqual(filters.min_notional, D("5"))
        self.assertEqual(filters.contract_multiplier, D("1"))
        self.assertEqual((filters.base_asset, filters.quote_asset), ("ABC", "USDT"))
        self.assertTrue(filters.trading)

    def test_missing_symbol_is_reported(self):
        client, _ = self.client([response(EXCHANGE_INFO)])
        with self.assertRaises(MetadataError):
            client.symbol_metadata("NOPEUSDT")

    def test_account_state_maps_balances_positions_and_orders(self):
        client, _ = self.client(
            [
                response({"serverTime": 1_000_500}),
                response(ACCOUNT),
                response(POSITIONS),
                response([OPEN_ORDER]),
            ]
        )
        client.sync_time()
        state = client.account_state(SYMBOL)
        self.assertIsInstance(state, AccountState)
        self.assertEqual((state.wallet_balance, state.available_balance), (D("1000"), D("800")))
        self.assertEqual(len(state.positions), 2)
        long = next(position for position in state.positions if position.side is Side.LONG)
        short = next(position for position in state.positions if position.side is Side.SHORT)
        self.assertEqual(long.quantity, D("0.30"))
        self.assertEqual(short.quantity, D("0.20"))
        self.assertEqual(state.open_orders[0].remaining_quantity, D("0.20"))
        self.assertEqual(state.open_orders[0].status, OrderStatus.PARTIALLY_FILLED)

    def test_funding_and_mark_price_are_explicit(self):
        client, _ = self.client(
            [
                response(
                    {
                        "symbol": SYMBOL,
                        "markPrice": "99.5",
                        "lastFundingRate": "0.0001",
                        "nextFundingTime": 1_010_000,
                        "time": 1_000_500,
                    }
                )
            ]
        )
        info = client.funding_info(SYMBOL)
        self.assertEqual((info.mark_price, info.last_funding_rate), (D("99.5"), D("0.0001")))
        self.assertEqual(info.next_funding_time_ms, 1_010_000)


class TradingTests(BinanceTestBase):
    def ready_adapter(self, *, dual=True):
        responses = self.preflight_responses(dual=dual)
        client, transport = self.client(responses)
        adapter = BinanceTradingAdapter(client)
        state = adapter.preflight(SYMBOL)
        return adapter, transport, state

    def test_local_filter_price_band_and_exit_guards_run_before_network(self):
        adapter, transport, state = self.ready_adapter()
        self.assertEqual(state.mark_price, D("99.5"))
        self.assertEqual(state.leverage_brackets[0].max_leverage, D("20"))
        self.assertEqual(state.metadata.price_band_up, D("1.05"))
        calls = len(transport.calls)
        cases = [
            OrderIntent("tick", SYMBOL, Side.LONG, OrderRole.ENTRY, D("98.05"), D("0.10")),
            OrderIntent("step", SYMBOL, Side.LONG, OrderRole.ENTRY, D("98"), D("0.105")),
            OrderIntent("min-notional", SYMBOL, Side.LONG, OrderRole.ENTRY, D("98"), D("0.01")),
            OrderIntent("band", SYMBOL, Side.LONG, OrderRole.ENTRY, D("106"), D("0.10")),
            OrderIntent("over-exit", SYMBOL, Side.LONG, OrderRole.EXIT, D("99"), D("0.40")),
            OrderIntent("no-position", SYMBOL, Side.SHORT, OrderRole.EXIT, D("99"), D("0.01")),
        ]
        for intent in cases:
            outcome = adapter.place(intent)
            self.assertEqual(outcome.status, NewOrderOutcome.REJECTED, intent.client_order_id)
            self.assertTrue(outcome.reason)
        self.assertEqual(len(transport.calls), calls)

    def test_malformed_client_id_is_rejected_before_preflight(self):
        with self.assertRaises(DomainError):
            OrderIntent("bad id", SYMBOL, Side.LONG, OrderRole.ENTRY, D("98"), D("0.10"))
        with self.assertRaises(DomainError):
            OrderIntent("x" * 37, SYMBOL, Side.LONG, OrderRole.ENTRY, D("98"), D("0.10"))

    def test_account_must_be_trade_enabled_and_single_asset(self):
        for override in ({"canTrade": False}, {"multiAssetsMargin": True}):
            responses = self.preflight_responses()
            responses[3] = response({**ACCOUNT, **override})
            client, _ = self.client(responses)
            adapter = BinanceTradingAdapter(client)
            with self.assertRaises(TradingDisabledError):
                adapter.preflight(SYMBOL)
            self.assertIsNone(adapter.preflight_state)

    def test_stale_preflight_blocks_a_new_order(self):
        adapter, transport, _ = self.ready_adapter()
        self.clock_value += 40_000
        intent = OrderIntent("late", SYMBOL, Side.LONG, OrderRole.ENTRY, D("98"), D("0.10"))
        with self.assertRaises(TradingDisabledError):
            adapter.place(intent)
        self.assertEqual(len(transport.calls), 8)

    def test_preflight_requires_hedge_mode(self):
        adapter, _, state = self.ready_adapter(dual=True)
        self.assertEqual(state.position_mode, PositionMode.HEDGE)

        client, _ = self.client(self.preflight_responses(dual=False))
        one_way = BinanceTradingAdapter(client)
        with self.assertRaises(TradingDisabledError):
            one_way.preflight(SYMBOL)
        self.assertIsNone(one_way.preflight_state)

    def test_failed_repreflight_invalidates_the_previous_authorization(self):
        adapter, transport, _ = self.ready_adapter()
        self.assertIsNotNone(adapter.preflight_state)
        transport.responses.extend(self.preflight_responses(dual=False))
        with self.assertRaises(TradingDisabledError):
            adapter.preflight(SYMBOL)
        self.assertIsNone(adapter.preflight_state)
        intent = OrderIntent("after-failed-preflight", SYMBOL, Side.LONG, OrderRole.ENTRY, D("98"), D("0.10"))
        with self.assertRaises(TradingDisabledError):
            adapter.place(intent)

    def test_place_requires_preflight(self):
        client, _ = self.client([response({"serverTime": 1_000_500})])
        client.sync_time()
        adapter = BinanceTradingAdapter(client)
        intent = OrderIntent("cid", SYMBOL, Side.LONG, OrderRole.ENTRY, D("98"), D("0.10"))
        with self.assertRaises(TradingDisabledError):
            adapter.place(intent)

    def test_place_uses_gtx_limits_and_hedge_side(self):
        adapter, transport, _ = self.ready_adapter()
        transport.responses.append(response({**OPEN_ORDER, "status": "NEW", "executedQty": "0", "avgPrice": "0"}))
        intent = OrderIntent("gen-1:LONG:1:ENTRY", SYMBOL, Side.LONG, OrderRole.ENTRY, D("98"), D("0.10"))
        outcome = adapter.place(intent)
        self.assertEqual(outcome.status, NewOrderOutcome.ACK)
        call = transport.calls[-1]
        self.assertEqual(call["method"], "POST")
        params = dict(call["pairs"])
        self.assertEqual(params["side"], "BUY")
        self.assertEqual(params["positionSide"], "LONG")
        self.assertEqual(params["type"], "LIMIT")
        self.assertEqual(params["timeInForce"], "GTX")
        self.assertEqual(params["newClientOrderId"], "gen-1:LONG:1:ENTRY")
        self.assertNotIn("reduceOnly", params)

    def test_explicit_reject_is_reported_without_retry(self):
        adapter, transport, _ = self.ready_adapter()
        transport.responses.append(
            response({"code": -2022, "msg": "ReduceOnly Order is rejected."}, status=400)
        )
        intent = OrderIntent("cid", SYMBOL, Side.LONG, OrderRole.ENTRY, D("98"), D("0.10"))
        outcome = adapter.place(intent)
        self.assertEqual(outcome.status, NewOrderOutcome.REJECTED)
        self.assertIn("-2022", outcome.reason)
        self.assertEqual(len(transport.calls), 9)

    def test_unknown_create_outcome_is_resolved_without_retry(self):
        adapter, transport, _ = self.ready_adapter()
        transport.responses.append(TimeoutError("socket timed out"))
        transport.responses.append(response({**OPEN_ORDER, "status": "FILLED", "executedQty": "0.10", "avgPrice": "98"}))
        intent = OrderIntent("cid", SYMBOL, Side.LONG, OrderRole.ENTRY, D("98"), D("0.10"))
        first = adapter.place(intent)
        self.assertEqual(first.status, NewOrderOutcome.UNKNOWN)
        resolved = adapter.resolve_unknown(intent)
        self.assertEqual(resolved.status, NewOrderOutcome.ACK)
        self.assertEqual(resolved.order.status, OrderStatus.FILLED)
        self.assertEqual(transport.calls[-1]["method"], "GET")
        self.assertNotIn("POST", [call["method"] for call in transport.calls[9:]])

    def test_not_found_after_unknown_is_reported_and_not_retried(self):
        adapter, transport, _ = self.ready_adapter()
        transport.responses.append(OutcomeUnknown("timeout"))
        transport.responses.append(response({"code": -2013, "msg": "Order does not exist."}, status=400))
        intent = OrderIntent("cid", SYMBOL, Side.LONG, OrderRole.ENTRY, D("98"), D("0.10"))
        self.assertEqual(adapter.place(intent).status, NewOrderOutcome.UNKNOWN)
        outcome = adapter.resolve_unknown(intent)
        self.assertEqual(outcome.status, NewOrderOutcome.NOT_FOUND)
        self.assertEqual(len([call for call in transport.calls if call["method"] == "POST"]), 1)

    def test_reconcile_detects_unknown_orders_positions_and_missing_ids(self):
        client, transport = self.client(
            [
                response({"serverTime": 1_000_500}),
                response(ACCOUNT),
                response(POSITIONS),
                response([OPEN_ORDER, {**OPEN_ORDER, "clientOrderId": "external", "orderId": 99}]),
                response({**OPEN_ORDER, "orderId": 11}),
                response({"code": -2013, "msg": "Order does not exist."}, status=400),
            ]
        )
        client.sync_time()
        adapter = BinanceTradingAdapter(client)
        result = adapter.reconcile(
            SYMBOL,
            known_client_ids=["gen-1:LONG:1:ENTRY", "missing-id"],
            owned_positions=frozenset({(SYMBOL, Side.LONG)}),
        )
        self.assertFalse(result.ok)
        self.assertEqual([order.client_order_id for order in result.unknown_orders], ["external"])
        self.assertEqual([position.side for position in result.unknown_positions], [Side.SHORT])
        self.assertEqual(result.missing_known_orders, ("missing-id",))

    def test_reconcile_ok_when_everything_is_owned_and_known(self):
        client, _ = self.client(
            [
                response({"serverTime": 1_000_500}),
                response(ACCOUNT),
                response(POSITIONS),
                response([OPEN_ORDER]),
                response({**OPEN_ORDER, "orderId": 11}),
            ]
        )
        client.sync_time()
        adapter = BinanceTradingAdapter(client)
        result = adapter.reconcile(
            SYMBOL,
            known_client_ids=["gen-1:LONG:1:ENTRY"],
            owned_positions=frozenset({(SYMBOL, Side.LONG), (SYMBOL, Side.SHORT)}),
        )
        self.assertTrue(result.ok)

    def test_cancel_unknown_outcome_is_not_retried(self):
        adapter, transport, _ = self.ready_adapter()
        transport.responses.append(TimeoutError("cancel timeout"))
        outcome = adapter.cancel(SYMBOL, "gen-1:LONG:1:ENTRY")
        self.assertEqual(outcome.status, NewOrderOutcome.UNKNOWN)
        self.assertEqual(len([call for call in transport.calls if call["method"] == "DELETE"]), 1)


if __name__ == "__main__":
    unittest.main()
