from decimal import Decimal as D
import unittest

from gridbot.domain import (
    ATRValue,
    AccountSnapshot,
    calculate_close_quantity,
    AllocationPolicy,
    Candle,
    CostAssumptions,
    DomainError,
    ExchangeFilters,
    EntryFill,
    GridParameters,
    QuoteVolumeRange,
    QuoteVolumeSnapshot,
    ParameterMode,
    RejectionCode,
    RiskLimits,
    RiskModel,
    RunMode,
    Side,
    allocate_side_budget,
    build_paired_grid,
    calculate_side_budgets,
    cost_aware_exit_price,
    expected_net_pnl,
    expected_net_pnl_for_fills,
    evaluate_quote_volume,
    normalized_weights,
    order_notional,
    relative_volatility_index,
    resolve_parameter,
    rounded_price,
    select_level_count,
    true_ranges,
    validate_fee_edge,
    wilder_atr,
)


class DecimalDomainTests(unittest.TestCase):
    def filters(self, **overrides):
        values = dict(tick_size=D("0.1"), step_size=D("0.01"), min_qty=D("0.01"), min_notional=D("1"),
                      max_qty=D("1000"), max_notional=D("100000"), contract_multiplier=D("1"),
                      quote_asset="USDT", base_asset="ABC")
        values.update(overrides)
        return ExchangeFilters(**values)

    def limits(self, **overrides):
        values = dict(max_long_notional=D("100"), max_short_notional=D("100"), max_gross_notional=D("200"),
                      max_abs_net_notional=D("100"), max_stress_loss_quote=D("100"),
                      max_initial_margin_quote=D("200"), available_margin_quote=D("200"),
                      stress_loss_long_quote=D("50"), stress_loss_short_quote=D("50"),
                      initial_margin_long_quote=D("100"), initial_margin_short_quote=D("100"))
        values.update(overrides)
        return RiskLimits(**values)

    def params(self, **overrides):
        values = dict(anchor=D("100"), distances=(D("2"), D("4")),
                      weights=(D("0.6"), D("0.4")), long_budget_quote=D("50"), short_budget_quote=D("50"), mode=RunMode.TEST,
                      quantity_mode=ParameterMode.MANUAL, snapshot_timestamp=10_000)
        values.update(overrides)
        return GridParameters(**values)

    def risk_model(self, **overrides):
        values = dict(stress_loss_fraction_long=D("0.1"), stress_loss_fraction_short=D("0.1"),
                      initial_margin_fraction_long=D("0.5"), initial_margin_fraction_short=D("0.5"))
        values.update(overrides)
        return RiskModel(**values)

    def account(self, **overrides):
        values = dict(mode=RunMode.TEST, snapshot_timestamp=10_000, available_balance_quote=D("100"),
                      deployable_collateral_quote=D("100"), reconciled=True, source="paper-ledger")
        values.update(overrides)
        return AccountSnapshot(**values)

    def allocation(self, **overrides):
        values = dict(max_strategy_allocation_quote=D("100"), protected_reserve_quote=D("0"),
                      long_fraction=D("0.5"), short_fraction=D("0.5"))
        values.update(overrides)
        return AllocationPolicy(**values)

    def build(self, **kwargs):
        costs = CostAssumptions(entry_fee_rate=D("0.001"), exit_fee_rate=D("0.001"),
                                execution_buffer_quote=D("0.01"), funding_cost_quote=D("0"),
                                required_net_profit_quote=D("0.1"))
        values = dict(symbol="ABCUSDT", params=self.params(), filters=self.filters(), limits=self.limits(),
                      risk_model=self.risk_model(), cost_assumptions=costs,
                      account_snapshot=self.account(), allocation_policy=self.allocation(),
                      config_version="config-1", current_market_snapshot_timestamp=10_000,
                      max_snapshot_age_timestamp_units=0, current_account_timestamp=10_000,
                      max_account_age_timestamp_units=0, best_bid=D("99.9"), best_ask=D("100.1"))
        values.update(kwargs)
        return build_paired_grid(**values)

    def test_auto_manual_resolution_is_bounded_and_fresh(self):
        common=dict(minimum=D("1"),maximum=D("5"),algorithm_version="v1",reason="bounded candidate",
                    source_timestamp=100,now_timestamp=100,max_source_age_timestamp_units=0)
        manual=resolve_parameter(mode=ParameterMode.MANUAL,manual_value=D("3"),auto_candidate=None,**common)
        self.assertTrue(manual.accepted)
        self.assertEqual(manual.value.value,D("3"))
        self.assertFalse(resolve_parameter(mode=ParameterMode.MANUAL,manual_value=D("6"),auto_candidate=None,**common).accepted)
        self.assertFalse(resolve_parameter(mode=ParameterMode.AUTO,manual_value=None,auto_candidate=D("3"),**{**common,"source_timestamp":99}).accepted)
        self.assertFalse(resolve_parameter(mode=ParameterMode.AUTO,manual_value=None,auto_candidate=None,**common).accepted)

    def test_close_quantity_rounds_down_and_never_reverses(self):
        part=calculate_close_quantity(position_quantity=D("1"),requested_quantity=D("0.456"),step_size=D("0.01"))
        self.assertEqual((part.value.quantity,part.value.remaining_quantity),(D("0.45"),D("0.55")))
        full=calculate_close_quantity(position_quantity=D("1"),requested_quantity=D("1"),step_size=D("0.01"),full_close=True)
        self.assertTrue(full.value.full_close)
        self.assertFalse(calculate_close_quantity(position_quantity=D("1"),requested_quantity=D("1.01"),step_size=D("0.01")).accepted)
        self.assertFalse(calculate_close_quantity(position_quantity=D("1"),requested_quantity=D("0.995"),step_size=D("0.01"),full_close=True).accepted)

    def test_rounded_duplicate_grid_levels_reject(self):
        result=self.build(params=self.params(distances=(D("1.01"),D("1.04"))))
        self.assertFalse(result.accepted)
        self.assertEqual(result.rejection.code, RejectionCode.INVALID_INPUT)
        self.assertIn("duplicate",result.rejection.message)

    def test_non_finite_values_reject_at_domain_boundaries(self):
        with self.assertRaises(DomainError): normalized_weights(3,D("NaN"))
        with self.assertRaises(DomainError): self.filters(tick_size=D("Infinity"))
        with self.assertRaises(DomainError): order_notional(D("NaN"),D("1"))
        with self.assertRaises(DomainError): Candle(D("10"),D("8"),D("9"),1.5)
        with self.assertRaises(DomainError): self.filters(base_asset="")
        with self.assertRaises(DomainError): self.filters(min_qty=D("2"), max_qty=D("1"))
        with self.assertRaises(DomainError): QuoteVolumeRange("USDT", D("0"), D("0"))
        with self.assertRaises(DomainError): self.risk_model(initial_margin_fraction_long=D("2"))
        with self.assertRaises(DomainError): ATRValue(D("1"), 14, "15m", 5, 15, 100, D("100"))

    def test_non_decimal_money_is_rejected_not_coerced(self):
        with self.assertRaises(DomainError): allocate_side_budget(100.0,0.5,0.5)
        with self.assertRaises(DomainError): QuoteVolumeRange("USDT",100.0,1000.0)
        with self.assertRaises(DomainError): QuoteVolumeSnapshot("ABCUSDT","USDT",100.0,100,"ticker")
        self.assertFalse(validate_fee_edge(0.9,1.0).accepted)
        common=dict(minimum=D("1"),maximum=D("5"),algorithm_version="v1",reason="bounded candidate",
                    source_timestamp=100,now_timestamp=100,max_source_age_timestamp_units=0)
        self.assertFalse(resolve_parameter(mode=ParameterMode.MANUAL,manual_value=3.0,auto_candidate=None,**common).accepted)
        self.assertFalse(self.build(best_bid=99.9).accepted)

    def test_symbol_and_effective_auto_values_must_be_explicit(self):
        mismatch=self.build(symbol="XYZUSDT")
        self.assertEqual(mismatch.rejection.code,RejectionCode.EXCHANGE_FILTER)
        with self.assertRaises(DomainError):
            self.params(quantity_mode=ParameterMode.AUTO,effective_values=(("lambda",0.75),))

    def test_exit_fee_rate_must_allow_a_finite_linear_target(self):
        result=cost_aware_exit_price(side=Side.LONG,quantity=D("1"),entry_price=D("100"),multiplier=D("1"),
                                     entry_fee_rate=D("0.001"),exit_fee_rate=D("1"),funding_cost_quote=D("0"),
                                     execution_buffer_quote=D("0"),required_net_profit_quote=D("1"),tick_size=D("0.1"))
        self.assertFalse(result.accepted)
        self.assertEqual(result.rejection.code,RejectionCode.COST_HURDLE)


    def test_quote_volume_gate_is_unit_explicit_fresh_and_inclusive(self):
        limits=QuoteVolumeRange("USDT",D("100"),D("1000"))
        snapshot=QuoteVolumeSnapshot("ABCUSDT","USDT",D("100"),100,"ticker")
        self.assertTrue(evaluate_quote_volume(snapshot=snapshot,limits=limits,now_timestamp=100,max_age_timestamp_units=0).accepted)
        self.assertFalse(evaluate_quote_volume(snapshot=QuoteVolumeSnapshot("ABCUSDT","BTC",D("100"),100,"ticker"),
                                               limits=limits,now_timestamp=100,max_age_timestamp_units=0).accepted)
        self.assertFalse(evaluate_quote_volume(snapshot=QuoteVolumeSnapshot("ABCUSDT","USDT",D("99"),100,"ticker"),
                                               limits=limits,now_timestamp=100,max_age_timestamp_units=0).accepted)
        self.assertFalse(evaluate_quote_volume(snapshot=snapshot,limits=limits,now_timestamp=101,max_age_timestamp_units=0).accepted)

    def test_fill_level_pnl_uses_each_entry_fee_and_signed_funding(self):
        fills=(EntryFill(D("1"),D("100"),D("0.001")),EntryFill(D("1"),D("99"),D("0.002")))
        pnl=expected_net_pnl_for_fills(side=Side.LONG,fills=fills,exit_price=D("101"),exit_fee_rate=D("0.001"),
                                       multiplier=D("1"),funding_cost_quote=D("-0.1"),execution_buffer_quote=D("0.2"))
        self.assertEqual(pnl,D("2.400"))

    def test_true_range_uses_previous_close_gap(self):
        candles = [Candle(D("10"), D("8"), D("9"), 1), Candle(D("12"), D("11"), D("11.5"), 2),
                   Candle(D("11"), D("9"), D("10"), 3)]
        self.assertEqual(true_ranges(candles), (D("2"), D("3"), D("2.5")))

    def test_wilder_atr_seed_recursion_and_stale_guard(self):
        candles = [Candle(D("10"), D("8"), D("9"), 0), Candle(D("12"), D("11"), D("11.5"), 900_000),
                   Candle(D("11"), D("9"), D("10"), 1_800_000), Candle(D("10.5"), D("9.5"), D("10"), 2_700_000)]
        atr = wilder_atr(candles, period=2, timeframe="15m", max_age_timestamp_units=0, now_timestamp=3_600_000)
        self.assertEqual(atr.value, D("1.875"))
        self.assertEqual(atr.relative, D("0.1875"))
        self.assertEqual(atr.warmup_bars, 3)
        self.assertEqual(wilder_atr(candles, period=3, timeframe="15m", max_age_timestamp_units=0,
                                    now_timestamp=3_600_000).value, D("6.5")/D("3"))
        with self.assertRaises(DomainError):
            wilder_atr(candles, period=4, timeframe="15m", max_age_timestamp_units=0, now_timestamp=3_600_000)
        with self.assertRaises(DomainError):
            wilder_atr(candles, period=2, timeframe="15m", max_age_timestamp_units=0, now_timestamp=3_600_001)
        misaligned = [Candle(c.high, c.low, c.close, c.timestamp + 1) for c in candles]
        with self.assertRaises(DomainError):
            wilder_atr(misaligned, period=2, timeframe="15m", max_age_timestamp_units=0, now_timestamp=3_600_001)

    def test_atr_index_requires_aligned_timeframe_and_bar(self):
        fast = ATRValue(D("2"), 14, "15m", "WILDER_RMA", 14, 100, D("100"))
        slow = ATRValue(D("1"), 96, "15m", "WILDER_RMA", 96, 100, D("100"))
        self.assertEqual(relative_volatility_index(fast, slow), D("2"))
        with self.assertRaises(DomainError):
            relative_volatility_index(fast, ATRValue(D("1"), 96, "5m", "WILDER_RMA", 96, 100, D("100")))

    def test_geometric_weights_sum_exactly_and_are_bounded(self):
        for n in range(1, 20):
            weights = normalized_weights(n, D("0.75"))
            self.assertEqual(sum(weights, D("0")), D("1"))
            self.assertTrue(all(D("0") < w <= D("1") for w in weights))
            self.assertEqual(tuple(sorted(weights, reverse=True)), weights)
        self.assertEqual(normalized_weights(3, D("0")),
                         (D("0.3333333333333333333333333333"), D("0.3333333333333333333333333333"),
                          D("0.3333333333333333333333333334")))

    def test_paired_test_grid_and_downward_quantity_rounding(self):
        result = self.build()
        self.assertTrue(result.accepted, result.rejection)
        plan = result.value
        self.assertEqual(plan.mode, RunMode.TEST)
        self.assertEqual(len(plan.long_orders), 2)
        self.assertEqual(len(plan.short_orders), 2)
        for order in (*plan.long_orders, *plan.short_orders):
            self.assertEqual(order.quantity % D("0.01"), D("0"))
            self.assertLessEqual(order.notional_quote, order.target_notional_quote)
            self.assertGreaterEqual(order.estimated_exit_net_profit_quote, D("0.1"))
            self.assertGreater(order.target_exit_price, order.price) if order.side is Side.LONG else self.assertLess(order.target_exit_price, order.price)
        self.assertEqual(plan.gross_notional_quote, plan.long_notional_quote + plan.short_notional_quote)
        self.assertEqual(plan.signed_net_notional_quote, plan.long_notional_quote - plan.short_notional_quote)

    def test_directional_price_rounding_is_passive(self):
        self.assertEqual(self.build().value.long_orders[0].price, D("98"))
        self.assertEqual(self.build().value.short_orders[0].price, D("102"))

    def test_price_rounding_requires_a_side_and_never_reverses_direction(self):
        self.assertEqual(rounded_price(D("100.09"), D("0.1"), Side.LONG), D("100.0"))
        self.assertEqual(rounded_price(D("100.01"), D("0.1"), Side.SHORT), D("100.1"))
        for side in ("LONG", "SHORT"):
            with self.assertRaises(DomainError):
                rounded_price(D("100.09"), D("0.1"), side)

    def test_min_notional_rejects_instead_of_increasing_quantity(self):
        result = self.build(filters=self.filters(min_notional=D("30")), params=self.params(long_budget_quote=D("20"), short_budget_quote=D("20")))
        self.assertFalse(result.accepted)
        self.assertEqual(result.rejection.code, RejectionCode.EXCHANGE_FILTER)

    def test_stale_or_future_market_snapshot_fails_closed(self):
        stale = self.build(max_snapshot_age_timestamp_units=1, current_market_snapshot_timestamp=10_002)
        future = self.build(current_market_snapshot_timestamp=9_999)
        self.assertEqual(stale.rejection.code, RejectionCode.STALE_OR_INCOMPLETE_DATA)
        self.assertEqual(future.rejection.code, RejectionCode.STALE_OR_INCOMPLETE_DATA)

    def test_non_marketable_constraints_and_invalid_book_reject(self):
        self.assertFalse(self.build(best_ask=D("97.9")).accepted)
        self.assertFalse(self.build(best_bid=D("101"), best_ask=D("100")).accepted)

    def test_combined_gross_and_stress_guards_include_both_legs(self):
        result = self.build(limits=self.limits(max_gross_notional=D("70")))
        self.assertFalse(result.accepted)
        self.assertEqual(result.rejection.code, RejectionCode.RISK_LIMIT)
        self.assertFalse(self.build(limits=self.limits(max_stress_loss_quote=D("2"))).accepted)

    def test_initial_margin_cannot_exceed_fresh_account_collateral(self):
        result=self.build(account_snapshot=self.account(available_balance_quote=D("10"),deployable_collateral_quote=D("10")))
        self.assertFalse(result.accepted)
        self.assertEqual(result.rejection.code,RejectionCode.RISK_LIMIT)

    def test_per_leg_stress_budget_is_enforced_independently(self):
        result = self.build(limits=self.limits(stress_loss_long_quote=D("1")))
        self.assertFalse(result.accepted)
        self.assertIn("leg", result.rejection.message)

    def test_auto_parameters_require_explainability(self):
        with self.assertRaises(DomainError):
            self.params(quantity_mode=ParameterMode.AUTO, effective_values=())

    def test_grid_snapshot_timestamp_is_required_and_typed(self):
        for timestamp in (None, -1, "10000"):
            with self.assertRaises(DomainError):
                self.params(snapshot_timestamp=timestamp)

    def test_pnl_sign_and_fees_are_side_correct(self):
        common = dict(quantity=D("2"), entry_price=D("100"), exit_price=D("101"), multiplier=D("1"),
                      entry_fee_rate=D("0.001"), exit_fee_rate=D("0.001"), funding_cost_quote=D("0.1"),
                      execution_buffer_quote=D("0.2"))
        self.assertEqual(expected_net_pnl(side=Side.LONG, **common), D("1.298"))
        self.assertEqual(expected_net_pnl(side=Side.SHORT, **common), D("-2.702"))

    def test_cost_aware_exit_clears_hurdle_after_rounding(self):
        common = dict(quantity=D("1"), entry_price=D("100"), multiplier=D("1"), entry_fee_rate=D("0.001"),
                      exit_fee_rate=D("0.001"), funding_cost_quote=D("0.2"), execution_buffer_quote=D("0.1"),
                      required_net_profit_quote=D("1"), tick_size=D("0.1"))
        for side in (Side.LONG, Side.SHORT):
            result = cost_aware_exit_price(side=side, **common)
            self.assertTrue(result.accepted, result.rejection)
            price, net = result.value
            self.assertGreaterEqual(net, D("1"))
            self.assertGreater(price, D("100")) if side is Side.LONG else self.assertLess(price, D("100"))

    def test_paired_proposal_rejects_unreachable_cost_hurdle(self):
        costs=CostAssumptions(entry_fee_rate=D("0.01"),exit_fee_rate=D("0.01"),execution_buffer_quote=D("1"),
                              funding_cost_quote=D("0"),required_net_profit_quote=D("1000"))
        result=self.build(cost_assumptions=costs)
        self.assertFalse(result.accepted)
        self.assertEqual(result.rejection.code,RejectionCode.COST_HURDLE)

    def test_unreachable_short_hurdle_rejects(self):
        result = cost_aware_exit_price(side=Side.SHORT, quantity=D("1"), entry_price=D("1"), multiplier=D("1"),
                                       entry_fee_rate=D("0"), exit_fee_rate=D("0"), funding_cost_quote=D("2"),
                                       execution_buffer_quote=D("0"), required_net_profit_quote=D("0"), tick_size=D("0.01"))
        self.assertFalse(result.accepted)
        self.assertEqual(result.rejection.code, RejectionCode.COST_HURDLE)

    def test_level_count_selects_largest_full_check_pass(self):
        result = select_level_count(min_levels=1, max_levels=6, max_allowed_levels=5,
                                    evaluate=lambda n: n in {2, 4})
        self.assertEqual(result.value, 4)
        none = select_level_count(min_levels=2, max_levels=4, max_allowed_levels=4, evaluate=lambda n: False)
        self.assertEqual(none.rejection.code, RejectionCode.NO_VALID_LEVEL_COUNT)

    def test_account_budget_is_capped_and_reserve_protected(self):
        result = calculate_side_budgets(
            snapshot=self.account(available_balance_quote=D("80"), deployable_collateral_quote=D("80")),
            policy=self.allocation(max_strategy_allocation_quote=D("60"), protected_reserve_quote=D("10")),
            expected_mode=RunMode.TEST, now_timestamp=10_000, max_snapshot_age_timestamp_units=0,
        )
        self.assertTrue(result.accepted, result.rejection)
        self.assertEqual(result.value.strategy_allocation_quote, D("60"))
        self.assertEqual((result.value.long_budget_quote, result.value.short_budget_quote), (D("30"), D("30")))

    def test_account_budget_fails_closed_on_stale_mode_or_unknown_external_state(self):
        snapshots = (self.account(snapshot_timestamp=9_999), self.account(mode=RunMode.LIVE),
                     self.account(unknown_external_orders=("unowned",)),
                     self.account(unknown_external_positions=("unowned",)))
        for snapshot in snapshots:
            result = calculate_side_budgets(snapshot=snapshot, policy=self.allocation(), expected_mode=RunMode.TEST,
                                            now_timestamp=10_000, max_snapshot_age_timestamp_units=0)
            self.assertFalse(result.accepted)
            self.assertEqual(result.rejection.code, RejectionCode.STALE_OR_INCOMPLETE_DATA)

    def test_paired_budget_cannot_exceed_account_allocation_cap(self):
        result = self.build(allocation_policy=self.allocation(max_strategy_allocation_quote=D("99")))
        self.assertFalse(result.accepted)
        self.assertEqual(result.rejection.code, RejectionCode.RISK_LIMIT)

    def test_each_side_budget_is_checked_against_its_own_allocation(self):
        params = self.params(long_budget_quote=D("21"), short_budget_quote=D("20"))
        policy = self.allocation(max_strategy_allocation_quote=D("100"), long_fraction=D("0.2"), short_fraction=D("0.8"))
        result = self.build(params=params, allocation_policy=policy)
        self.assertFalse(result.accepted)
        self.assertEqual(result.rejection.code, RejectionCode.RISK_LIMIT)

    def test_side_budget_requires_complete_allocation(self):
        self.assertEqual(allocate_side_budget(D("100"), D("0.5"), D("0.5")), (D("50"), D("50")))
        with self.assertRaises(DomainError):
            allocate_side_budget(D("100"), D("0.6"), D("0.5"))

    def test_domain_module_is_pure_and_has_no_network_or_clock_imports(self):
        import ast
        import inspect
        import gridbot.domain as domain_module

        tree = ast.parse(inspect.getsource(domain_module))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        forbidden = {"socket", "ssl", "http", "urllib", "requests", "httpx", "aiohttp",
                     "websockets", "binance", "time", "datetime", "random", "os", "asyncio"}
        self.assertFalse(roots & forbidden)

    def test_fee_edge_rejects_below_required_hurdle(self):
        self.assertEqual(validate_fee_edge(D("0.9"), D("1")).rejection.code, RejectionCode.COST_HURDLE)
        self.assertTrue(validate_fee_edge(D("1"), D("1")).accepted)


if __name__ == "__main__":
    unittest.main()
