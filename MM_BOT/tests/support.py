"""Shared deterministic fixtures for stage-2 tests."""

from dataclasses import replace
from decimal import Decimal as D

from gridbot.domain import (
    AccountSnapshot,
    AllocationPolicy,
    CostAssumptions,
    ExchangeFilters,
    GridProposal,
    GridParameters,
    ParameterMode,
    RiskLimits,
    RiskModel,
    RunMode,
    Side,
    build_paired_grid,
)
from gridbot.engine import ExitPolicy, PaperGridEngine, PaperRiskContext
from gridbot.exchange import Aggressor, MarketTick
from gridbot.paper import PaperConfig, PaperExchange, PaperFees

SYMBOL = "ABCUSDT"


def filters(**overrides) -> ExchangeFilters:
    values = dict(
        tick_size=D("0.1"),
        step_size=D("0.01"),
        min_qty=D("0.01"),
        min_notional=D("1"),
        max_qty=D("1000"),
        max_notional=D("100000"),
        contract_multiplier=D("1"),
        quote_asset="USDT",
        base_asset="ABC",
    )
    values.update(overrides)
    return ExchangeFilters(**values)


def paper_config(**overrides) -> PaperConfig:
    values = dict(
        fees=PaperFees(D("0.0002"), D("0.0005")),
        initial_balance_quote=D("1000"),
        margin_fraction=D("0.5"),
        multiplier=D("1"),
        latency_ms=0,
        tick_size=D("0.1"),
        market_close_slippage_ticks=0,
        min_notional=D("1"),
    )
    values.update(overrides)
    return PaperConfig(**values)


def risk_limits() -> RiskLimits:
    return RiskLimits(
        max_long_notional=D("100"),
        max_short_notional=D("100"),
        max_gross_notional=D("200"),
        max_abs_net_notional=D("100"),
        max_stress_loss_quote=D("100"),
        max_initial_margin_quote=D("200"),
        available_margin_quote=D("200"),
        stress_loss_long_quote=D("50"),
        stress_loss_short_quote=D("50"),
        initial_margin_long_quote=D("100"),
        initial_margin_short_quote=D("100"),
    )


def make_risk_context(**overrides) -> PaperRiskContext:
    values = dict(
        limits=risk_limits(),
        risk_model=RiskModel(D("0.1"), D("0.1"), D("0.5"), D("0.5")),
        available_margin_quote=D("200"),
    )
    values.update(overrides)
    return PaperRiskContext(**values)


def proposal(*, mode: RunMode = RunMode.TEST) -> GridProposal:
    params = GridParameters(
        anchor=D("100"),
        distances=(D("2"), D("4")),
        weights=(D("0.6"), D("0.4")),
        long_budget_quote=D("50"),
        short_budget_quote=D("50"),
        mode=mode,
        quantity_mode=ParameterMode.MANUAL,
        snapshot_timestamp=1_000,
    )
    limits = risk_limits()
    costs = CostAssumptions(
        entry_fee_rate=D("0.0002"),
        exit_fee_rate=D("0.0002"),
        execution_buffer_quote=D("0.01"),
        funding_cost_quote=D("0"),
        required_net_profit_quote=D("0.1"),
    )
    result = build_paired_grid(
        symbol=SYMBOL,
        params=params,
        filters=filters(),
        limits=limits,
        risk_model=RiskModel(D("0.1"), D("0.1"), D("0.5"), D("0.5")),
        cost_assumptions=costs,
        account_snapshot=AccountSnapshot(
            mode=mode,
            snapshot_timestamp=1_000,
            available_balance_quote=D("100"),
            deployable_collateral_quote=D("100"),
            reconciled=True,
            source="paper",
        ),
        allocation_policy=AllocationPolicy(D("100"), D("0"), D("0.5"), D("0.5")),
        config_version="cfg-1",
        current_market_snapshot_timestamp=1_000,
        max_snapshot_age_timestamp_units=0,
        current_account_timestamp=1_000,
        max_account_age_timestamp_units=0,
        best_bid=D("99.9"),
        best_ask=D("100.1"),
    )
    if not result.accepted:
        raise AssertionError(f"fixture proposal rejected: {result.rejection}")
    return result.value


def tick(
    timestamp_ms: int,
    *,
    bid: str = "99.9",
    ask: str = "100.1",
    last: str = "100",
    quantity: str = "1",
    aggressor: Aggressor | None = Aggressor.SELL,
    symbol: str = SYMBOL,
    bid_quantity: str = "5",
    ask_quantity: str = "5",
) -> MarketTick:
    return MarketTick(
        symbol=symbol,
        timestamp_ms=timestamp_ms,
        bid=D(bid),
        ask=D(ask),
        bid_quantity=D(bid_quantity),
        ask_quantity=D(ask_quantity),
        last_price=D(last),
        last_quantity=D(quantity),
        aggressor=aggressor,
    )


def engine(
    *,
    exchange: PaperExchange | None = None,
    exit_policy: ExitPolicy | None = None,
    risk_context: PaperRiskContext | None = None,
    max_tick_age_ms: int = 5_000,
    queue: str = "0",
) -> PaperGridEngine:
    return PaperGridEngine(
        exchange=exchange or PaperExchange(paper_config()),
        filters=filters(),
        exit_policy=exit_policy or ExitPolicy(D("0"), D("0.01"), D("0.1")),
        risk_context=risk_context if risk_context is not None else make_risk_context(),
        max_tick_age_ms=max_tick_age_ms,
        queue_ahead_quantity=D(queue),
    )


def live_proposal() -> GridProposal:
    return replace(proposal(), mode=RunMode.LIVE)


__all__ = [
    "SYMBOL",
    "make_risk_context",
    "risk_limits",
    "engine",
    "filters",
    "live_proposal",
    "paper_config",
    "proposal",
    "tick",
    "Side",
]
