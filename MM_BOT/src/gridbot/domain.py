"""Pure Decimal domain types and calculations. No exchange or network access."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from enum import Enum
from typing import Callable, Sequence


ZERO = Decimal("0")
ONE = Decimal("1")


def _is_finite_decimal(value: object) -> bool:
    """Money/price values must be Decimal; floats are never silently accepted."""
    return isinstance(value, Decimal) and value.is_finite()


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class DomainError(ValueError):
    """Invalid or incomplete domain input; callers must fail closed."""


class _GridBuildError(DomainError):
    """Internal build failure with a stable, explicitly assigned rejection code."""

    def __init__(self, code: RejectionCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class RunMode(str, Enum):
    TEST = "TEST"
    LIVE = "LIVE"


class ParameterMode(str, Enum):
    AUTO = "AUTO"
    MANUAL = "MANUAL"


class RejectionCode(str, Enum):
    INVALID_INPUT = "INVALID_INPUT"
    MISSING_INPUT = "MISSING_INPUT"
    NO_VALID_LEVEL_COUNT = "NO_VALID_LEVEL_COUNT"
    EXCHANGE_FILTER = "EXCHANGE_FILTER"
    COST_HURDLE = "COST_HURDLE"
    RISK_LIMIT = "RISK_LIMIT"
    STALE_OR_INCOMPLETE_DATA = "STALE_OR_INCOMPLETE_DATA"


@dataclass(frozen=True)
class Rejection:
    code: RejectionCode
    message: str


@dataclass(frozen=True)
class Result:
    """Explicit success/failure wrapper; rejected inputs never yield a plan."""

    value: object | None = None
    rejection: Rejection | None = None

    @property
    def accepted(self) -> bool:
        return self.rejection is None and self.value is not None


@dataclass(frozen=True)
class ExchangeFilters:
    """Validated contract filters for a linear quote-settled instrument."""

    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal | None = None
    max_qty: Decimal | None = None
    max_notional: Decimal | None = None
    contract_multiplier: Decimal = ONE
    quote_asset: str = "USDT"
    base_asset: str = ""
    contract_type: str = "PERPETUAL"
    trading: bool = True

    def __post_init__(self) -> None:
        positive = (self.tick_size, self.step_size, self.min_qty, self.contract_multiplier)
        optional = tuple(x for x in (self.min_notional, self.max_qty, self.max_notional) if x is not None)
        if any(not isinstance(x, Decimal) or not x.is_finite() for x in (*positive, *optional)):
            raise DomainError("exchange filters must be finite Decimal values")
        if any(x <= ZERO for x in positive):
            raise DomainError("tick_size, step_size, min_qty and multiplier must be positive")
        if self.min_notional is not None and self.min_notional < ZERO:
            raise DomainError("min_notional cannot be negative")
        if self.max_qty is not None and self.max_qty <= ZERO:
            raise DomainError("max_qty must be positive")
        if self.max_notional is not None and self.max_notional <= ZERO:
            raise DomainError("max_notional must be positive")
        if self.min_notional is not None and self.max_notional is not None:
            if self.min_notional > self.max_notional:
                raise DomainError("min_notional exceeds max_notional")
        if self.max_qty is not None and self.max_qty < self.min_qty:
            raise DomainError("max_qty cannot be below min_qty")
        if not isinstance(self.quote_asset, str) or not self.quote_asset:
            raise DomainError("quote_asset is required for unit-explicit sizing")
        if not isinstance(self.base_asset, str) or not self.base_asset:
            raise DomainError("base_asset is required for unit-explicit sizing")
        if not isinstance(self.contract_type, str) or not self.contract_type:
            raise DomainError("contract_type is required")
        if not isinstance(self.trading, bool):
            raise DomainError("trading flag must be boolean")


@dataclass(frozen=True)
class QuoteVolumeRange:
    quote_asset: str
    minimum_quote_volume: Decimal
    maximum_quote_volume: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.quote_asset, str) or not self.quote_asset:
            raise DomainError("quote-volume range requires a quote asset")
        if not _is_finite_decimal(self.minimum_quote_volume) or not _is_finite_decimal(self.maximum_quote_volume):
            raise DomainError("quote-volume bounds must be finite Decimal values")
        if self.maximum_quote_volume <= ZERO:
            raise DomainError("maximum quote volume must be positive")
        if self.minimum_quote_volume < ZERO or self.maximum_quote_volume < self.minimum_quote_volume:
            raise DomainError("quote-volume bounds must be non-negative and ordered")


@dataclass(frozen=True)
class QuoteVolumeSnapshot:
    symbol: str
    quote_asset: str
    quote_volume_24h: Decimal
    snapshot_timestamp: int
    source: str

    def __post_init__(self) -> None:
        if not all(isinstance(item, str) and item for item in (self.symbol, self.quote_asset, self.source)):
            raise DomainError("symbol, quote asset and volume source are required")
        if not _is_finite_decimal(self.quote_volume_24h):
            raise DomainError("24-hour quote volume must be a finite Decimal value")
        if not _is_int(self.snapshot_timestamp) or self.snapshot_timestamp < 0:
            raise DomainError("quote-volume snapshot timestamp must be a non-negative integer")


def evaluate_quote_volume(*, snapshot: QuoteVolumeSnapshot, limits: QuoteVolumeRange,
                          now_timestamp: int, max_age_timestamp_units: int) -> Result:
    """Return an explicit candidate/rejection; bounds are inclusive and units are quote asset."""
    if not isinstance(snapshot, QuoteVolumeSnapshot) or not isinstance(limits, QuoteVolumeRange):
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "quote-volume snapshot and configured range are required"))
    if not _is_int(now_timestamp) or not _is_int(max_age_timestamp_units):
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "current and maximum-age timestamps must be integers"))
    if snapshot.quote_volume_24h < ZERO:
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "24-hour quote volume must be finite and non-negative"))
    if now_timestamp < snapshot.snapshot_timestamp or max_age_timestamp_units < 0 or now_timestamp-snapshot.snapshot_timestamp > max_age_timestamp_units:
        return Result(rejection=Rejection(RejectionCode.STALE_OR_INCOMPLETE_DATA, "24-hour quote-volume snapshot is stale or from the future"))
    if snapshot.quote_asset != limits.quote_asset:
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "quote-volume asset does not match configured range"))
    if not limits.minimum_quote_volume <= snapshot.quote_volume_24h <= limits.maximum_quote_volume:
        return Result(rejection=Rejection(RejectionCode.RISK_LIMIT, "24-hour quote volume is outside configured inclusive range"))
    return Result(value=snapshot)


@dataclass(frozen=True)
class CostAssumptions:
    """Explicit effective fee rates plus quote amounts applied to each modeled order cycle.

    ``entry_fee_rate``/``exit_fee_rate`` are the assumed effective rates for the order type
    actually used (maker or taker); there is no hidden default rate. ``execution_buffer_quote``,
    ``funding_cost_quote`` and ``required_net_profit_quote`` are absolute quote amounts per
    modeled order round-trip, not rates, and are never rescaled automatically by weight.
    ``funding_cost_quote`` is signed: positive values are costs, negative values rebates.
    The caller derives the funding amount from the real Binance funding schedule; the domain
    deliberately does not assume a fixed settlement interval.
    """

    entry_fee_rate: Decimal
    exit_fee_rate: Decimal
    execution_buffer_quote: Decimal
    funding_cost_quote: Decimal
    required_net_profit_quote: Decimal

    def __post_init__(self) -> None:
        values=(self.entry_fee_rate,self.exit_fee_rate,self.execution_buffer_quote,self.funding_cost_quote,self.required_net_profit_quote)
        if any(not isinstance(x,Decimal) or not x.is_finite() for x in values):
            raise DomainError("cost assumptions must be finite Decimal values")
        if self.entry_fee_rate < ZERO or self.exit_fee_rate < ZERO:
            raise DomainError("fee rates cannot be negative")
        if min(self.execution_buffer_quote, self.required_net_profit_quote) < ZERO:
            raise DomainError("execution buffer and required profit cannot be negative")


@dataclass(frozen=True)
class RiskLimits:
    """Required hard bounds for sizing a complete paired generation."""

    max_long_notional: Decimal
    max_short_notional: Decimal
    max_gross_notional: Decimal
    max_abs_net_notional: Decimal
    max_stress_loss_quote: Decimal
    max_initial_margin_quote: Decimal
    available_margin_quote: Decimal
    stress_loss_long_quote: Decimal
    stress_loss_short_quote: Decimal
    initial_margin_long_quote: Decimal
    initial_margin_short_quote: Decimal

    def __post_init__(self) -> None:
        values = (
            self.max_long_notional,
            self.max_short_notional,
            self.max_gross_notional,
            self.max_abs_net_notional,
            self.max_stress_loss_quote,
            self.max_initial_margin_quote,
            self.available_margin_quote,
            self.stress_loss_long_quote,
            self.stress_loss_short_quote,
            self.initial_margin_long_quote,
            self.initial_margin_short_quote,
        )
        if any(not isinstance(x,Decimal) or not x.is_finite() for x in values):
            raise DomainError("risk limits and modeled costs must be finite Decimal values")
        if any(x < ZERO for x in values):
            raise DomainError("risk limits and modeled costs cannot be negative")


@dataclass(frozen=True)
class AccountSnapshot:
    """Reconciled account state; deployable collateral is net of known obligations."""
    mode: RunMode
    snapshot_timestamp: int
    available_balance_quote: Decimal
    deployable_collateral_quote: Decimal
    reconciled: bool
    source: str
    unknown_external_positions: tuple[str, ...] = ()
    unknown_external_orders: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.mode,RunMode) or not _is_int(self.snapshot_timestamp) or self.snapshot_timestamp < 0:
            raise DomainError("account snapshot mode/timestamp is invalid")
        if any(not _is_finite_decimal(x) for x in (self.available_balance_quote,self.deployable_collateral_quote)):
            raise DomainError("account balances must be finite Decimal values")
        if min(self.available_balance_quote, self.deployable_collateral_quote) < ZERO:
            raise DomainError("account balances cannot be negative")
        if self.deployable_collateral_quote > self.available_balance_quote:
            raise DomainError("deployable collateral cannot exceed available balance")
        if not isinstance(self.reconciled, bool):
            raise DomainError("reconciliation flag must be explicit")
        if not isinstance(self.source, str) or not self.source:
            raise DomainError("account snapshot source is required")
        if not all(isinstance(item, str) and item for item in (*self.unknown_external_positions, *self.unknown_external_orders)):
            raise DomainError("unknown external identifiers must be non-empty strings")


@dataclass(frozen=True)
class AllocationPolicy:
    """Owner-approved strategy cap, protected reserve, and complete two-leg split."""
    max_strategy_allocation_quote: Decimal
    protected_reserve_quote: Decimal
    long_fraction: Decimal
    short_fraction: Decimal

    def __post_init__(self) -> None:
        values=(self.max_strategy_allocation_quote,self.protected_reserve_quote,self.long_fraction,self.short_fraction)
        if any(not isinstance(x,Decimal) or not x.is_finite() for x in values):
            raise DomainError("allocation policy values must be finite Decimal values")
        if self.max_strategy_allocation_quote <= ZERO or self.protected_reserve_quote < ZERO:
            raise DomainError("strategy allocation cap must be positive and reserve non-negative")
        if min(self.long_fraction, self.short_fraction) < ZERO:
            raise DomainError("side fractions cannot be negative")
        if self.long_fraction + self.short_fraction != ONE:
            raise DomainError("side fractions must sum exactly to one")


@dataclass(frozen=True)
class SideBudgets:
    strategy_allocation_quote: Decimal
    long_budget_quote: Decimal
    short_budget_quote: Decimal
    available_margin_quote: Decimal


@dataclass(frozen=True)
class ATRValue:
    value: Decimal
    period: int
    timeframe: str
    method: str
    warmup_bars: int
    last_bar_timestamp: int
    reference_price: Decimal
    reference_definition: str = "LAST_COMPLETED_CLOSE"

    def __post_init__(self) -> None:
        if any(not _is_finite_decimal(x) for x in (self.value,self.reference_price)):
            raise DomainError("ATR values must be finite Decimal values")
        if not _is_int(self.period) or not _is_int(self.warmup_bars) or not _is_int(self.last_bar_timestamp):
            raise DomainError("ATR period, warmup and bar timestamp must be integers")
        if self.value < ZERO or self.reference_price <= ZERO or self.period < 1 or self.warmup_bars < self.period or self.last_bar_timestamp < 0:
            raise DomainError("ATR inputs are outside valid bounds")
        if not isinstance(self.timeframe, str) or not self.timeframe or not isinstance(self.method, str) or not self.method or not isinstance(self.reference_definition, str) or not self.reference_definition:
            raise DomainError("ATR timeframe, method and reference are required")

    @property
    def relative(self) -> Decimal:
        if self.reference_price <= ZERO:
            raise DomainError("reference price must be positive")
        return self.value / self.reference_price


@dataclass(frozen=True)
class Candle:
    high: Decimal
    low: Decimal
    close: Decimal
    timestamp: int
    complete: bool = True

    def __post_init__(self) -> None:
        if any(not _is_finite_decimal(x) for x in (self.high,self.low,self.close)):
            raise DomainError("OHLC values must be finite Decimal values")
        if not _is_int(self.timestamp) or self.timestamp < 0:
            raise DomainError("candle timestamp must be a non-negative integer")
        if not isinstance(self.complete, bool):
            raise DomainError("candle completeness must be explicit")
        if self.high <= ZERO or self.low <= ZERO or self.close <= ZERO:
            raise DomainError("OHLC prices must be positive")
        if self.high < self.low or not self.low <= self.close <= self.high:
            raise DomainError("invalid candle OHLC ordering")


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if value < ZERO or step <= ZERO:
        raise DomainError("value must be non-negative and step positive")
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    if value < ZERO or step <= ZERO:
        raise DomainError("value must be non-negative and step positive")
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def true_ranges(candles: Sequence[Candle]) -> tuple[Decimal, ...]:
    """TR per completed candle in price units, one value per input candle.

    The first candle has no previous close inside the window, so its value is the anchor
    range ``high - low``; it is not a Wilder TR and ``wilder_atr`` excludes it from the seed.
    Every later value uses the canonical formula with the previous close, and gaps are kept.
    """
    if not candles:
        raise DomainError("candles are required")
    if any(not isinstance(candle, Candle) for candle in candles):
        raise DomainError("ATR input must contain Candle values")
    if any(not candle.complete for candle in candles):
        raise DomainError("incomplete candle in ATR input")
    if any(b.timestamp <= a.timestamp for a, b in zip(candles, candles[1:])):
        raise DomainError("candle timestamps must be strictly increasing")
    ranges: list[Decimal] = []
    previous_close: Decimal | None = None
    for candle in candles:
        if previous_close is None:
            tr = candle.high - candle.low
        else:
            tr = max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        ranges.append(tr)
        previous_close = candle.close
    return tuple(ranges)


def wilder_atr(
    candles: Sequence[Candle], *, period: int, timeframe: str, max_age_timestamp_units: int, now_timestamp: int
) -> ATRValue:
    """Wilder/RMA ATR seeded by mean of the first `period` completed TR observations.

    The first input candle is the anchor that supplies the previous close for the first true
    TR, so ``period + 1`` completed candles are required and the anchor range is not used in
    the seed. This matches ``ATR_p = mean(TR_1..TR_p)`` with every TR defined by a prior close.
    """
    if not _is_int(period) or not _is_int(max_age_timestamp_units) or not _is_int(now_timestamp):
        raise DomainError("period and timestamps must be integers")
    if not isinstance(timeframe, str) or not timeframe:
        raise DomainError("timeframe must be an explicit non-empty label")
    if period < 1 or max_age_timestamp_units < 0:
        raise DomainError("period must be positive and max_age_timestamp_units non-negative")
    if len(candles) < period + 1:
        raise DomainError("insufficient completed candles: period plus one prior close required")
    timeframe_ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}
    if timeframe not in timeframe_ms:
        raise DomainError("unsupported timeframe; use an explicitly modeled interval")
    interval = timeframe_ms[timeframe]
    if candles[0].timestamp % interval != 0:
        raise DomainError("candle timestamps are not aligned to the timeframe interval")
    ranges = true_ranges(candles)[1:]
    if any(b.timestamp - a.timestamp != interval for a, b in zip(candles, candles[1:])):
        raise DomainError("candle gap or unexpected timeframe interval")
    atr = sum(ranges[:period], ZERO) / Decimal(period)
    for tr in ranges[period:]:
        atr = ((atr * Decimal(period - 1)) + tr) / Decimal(period)
    last = candles[-1]
    candle_close_timestamp = last.timestamp + interval
    if now_timestamp < candle_close_timestamp or now_timestamp - candle_close_timestamp > max_age_timestamp_units:
        raise DomainError("last candle is incomplete, stale or timestamp is in the future")
    if last.close <= ZERO:
        raise DomainError("reference close must be positive")
    return ATRValue(
        value=atr,
        period=period,
        timeframe=timeframe,
        method="WILDER_RMA",
        warmup_bars=period + 1,
        last_bar_timestamp=last.timestamp,
        reference_price=last.close,
    )


def relative_volatility_index(fast: ATRValue, slow: ATRValue) -> Decimal:
    """Compare like-for-like normalized ATR series only; no cross-timeframe sqrt scaling."""
    if not isinstance(fast, ATRValue) or not isinstance(slow, ATRValue):
        raise DomainError("ATR inputs must be ATRValue instances")
    if fast.timeframe != slow.timeframe:
        raise DomainError("ATR timeframes must match")
    if fast.method != slow.method:
        raise DomainError("ATR methods must match")
    if fast.reference_definition != slow.reference_definition:
        raise DomainError("ATR reference price definitions must match")
    if fast.period < 1 or slow.period < 1 or fast.value < ZERO or slow.value < ZERO:
        raise DomainError("ATR period/value is invalid")
    if fast.last_bar_timestamp != slow.last_bar_timestamp:
        raise DomainError("ATR values must use the same completed-bar timestamp")
    denominator = slow.relative
    if denominator <= ZERO:
        raise DomainError("slow relative ATR is zero; AUTO is unavailable")
    return fast.relative / denominator


def normalized_weights(level_count: int, decay_lambda: Decimal) -> tuple[Decimal, ...]:
    """Normalized geometric weights. Decimal exp is used; lambda is not an empirical default."""
    if not _is_finite_decimal(decay_lambda):
        raise DomainError("lambda must be a finite Decimal")
    if not _is_int(level_count):
        raise DomainError("level_count must be an integer")
    if level_count < 1 or decay_lambda < ZERO:
        raise DomainError("level_count must be positive and lambda non-negative")
    raw = tuple((-decay_lambda * Decimal(i)).exp() for i in range(level_count))
    total = sum(raw, ZERO)
    if total <= ZERO:
        raise DomainError("weight normalization failed")
    weights = tuple(item / total for item in raw)
    if any(weight <= ZERO for weight in weights):
        raise DomainError("lambda is too large for positive Decimal weights at this precision")
    if sum(weights, ZERO) != ONE:
        # Keep the budget invariant exact despite finite Decimal context precision.
        weights = (*weights[:-1], ONE - sum(weights[:-1], ZERO))
    if weights[-1] <= ZERO:
        raise DomainError("weight normalization lost positive precision")
    return weights


def rounded_price(price: Decimal, tick_size: Decimal, side: Side) -> Decimal:
    """Round passively: Long BUY down; Short SELL up, preserving away-from-market side."""
    if not isinstance(side, Side):
        raise DomainError("side must be LONG or SHORT")
    if not isinstance(price, Decimal) or not isinstance(tick_size, Decimal) or not price.is_finite() or not tick_size.is_finite():
        raise DomainError("price and tick_size must be finite Decimal values")
    if price <= ZERO or tick_size <= ZERO:
        raise DomainError("price and tick_size must be positive")
    rounding = ROUND_FLOOR if side is Side.LONG else ROUND_CEILING
    return (price / tick_size).to_integral_value(rounding=rounding) * tick_size


def order_notional(quantity: Decimal, price: Decimal, multiplier: Decimal = ONE) -> Decimal:
    if any(not isinstance(x, Decimal) or not x.is_finite() for x in (quantity, price, multiplier)):
        raise DomainError("notional inputs must be finite Decimal values")
    if quantity < ZERO or price <= ZERO or multiplier <= ZERO:
        raise DomainError("invalid notional inputs")
    return quantity * price * multiplier


@dataclass(frozen=True)
class GridParameters:
    """Grid inputs; distances are explicit quote-price offsets from the anchor."""

    anchor: Decimal
    distances: tuple[Decimal, ...]
    weights: tuple[Decimal, ...]
    long_budget_quote: Decimal
    short_budget_quote: Decimal
    mode: RunMode
    quantity_mode: ParameterMode
    snapshot_timestamp: int
    effective_values: tuple[tuple[str, Decimal], ...] = ()

    def __post_init__(self) -> None:
        decimals=(self.anchor,self.long_budget_quote,self.short_budget_quote,*self.distances,*self.weights)
        if any(not isinstance(x,Decimal) or not x.is_finite() for x in decimals):
            raise DomainError("grid parameters must be finite Decimal values")
        if not isinstance(self.snapshot_timestamp, int) or self.snapshot_timestamp < 0:
            raise DomainError("grid snapshot timestamp must be a non-negative integer")
        if not isinstance(self.mode,RunMode) or not isinstance(self.quantity_mode,ParameterMode):
            raise DomainError("grid mode is invalid")
        if self.anchor <= ZERO:
            raise DomainError("anchor must be positive")
        if self.long_budget_quote <= ZERO or self.short_budget_quote <= ZERO:
            raise DomainError("both side budgets must be positive")
        if not self.distances or len(self.distances) != len(self.weights):
            raise DomainError("one positive distance and weight is required per level")
        if any(d <= ZERO for d in self.distances):
            raise DomainError("all level distances must be positive")
        if any(b <= a for a, b in zip(self.distances, self.distances[1:])):
            raise DomainError("level distances must be strictly increasing")
        if any(w <= ZERO for w in self.weights) or sum(self.weights, ZERO) != ONE:
            raise DomainError("weights must be positive and sum exactly to one")
        if self.quantity_mode is ParameterMode.AUTO and not self.effective_values:
            raise DomainError("AUTO parameters require recorded inputs/effective values")
        for entry in self.effective_values:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise DomainError("effective values must be (name, Decimal) pairs")
            name, value = entry
            if not isinstance(name, str) or not name or not _is_finite_decimal(value):
                raise DomainError("effective AUTO values require a name and a finite Decimal value")


@dataclass(frozen=True)
class OrderProposal:
    side: Side
    level: int
    price: Decimal
    quantity: Decimal
    notional_quote: Decimal
    target_notional_quote: Decimal
    target_exit_price: Decimal
    estimated_exit_net_profit_quote: Decimal


@dataclass(frozen=True)
class GridProposal:
    symbol: str
    mode: RunMode
    anchor: Decimal
    snapshot_timestamp: int
    long_orders: tuple[OrderProposal, ...]
    short_orders: tuple[OrderProposal, ...]
    long_notional_quote: Decimal
    short_notional_quote: Decimal
    gross_notional_quote: Decimal
    signed_net_notional_quote: Decimal
    stress_loss_quote: Decimal
    initial_margin_quote: Decimal
    config_version: str


@dataclass(frozen=True)
class RiskModel:
    """Explicit quote costs per generated quote-notional unit; no leverage inference."""

    stress_loss_fraction_long: Decimal
    stress_loss_fraction_short: Decimal
    initial_margin_fraction_long: Decimal
    initial_margin_fraction_short: Decimal

    def __post_init__(self) -> None:
        values = (
            self.stress_loss_fraction_long,
            self.stress_loss_fraction_short,
            self.initial_margin_fraction_long,
            self.initial_margin_fraction_short,
        )
        if any(not isinstance(v,Decimal) or not v.is_finite() for v in values):
            raise DomainError("risk model fractions must be finite Decimal values")
        if any(v < ZERO for v in values):
            raise DomainError("risk model fractions cannot be negative")
        if any(v <= ZERO for v in (self.initial_margin_fraction_long, self.initial_margin_fraction_short)):
            raise DomainError("initial margin fractions must be positive")
        if any(v > ONE for v in (self.initial_margin_fraction_long, self.initial_margin_fraction_short)):
            raise DomainError("initial margin fractions cannot exceed the quote notional")


def _proposal_for_side(
    side: Side,
    params: GridParameters,
    filters: ExchangeFilters,
    side_budget_quote: Decimal,
    costs: CostAssumptions,
) -> tuple[OrderProposal, ...]:
    proposals: list[OrderProposal] = []
    allocated = ZERO
    previous_price: Decimal | None = None
    for i, (distance, weight) in enumerate(zip(params.distances, params.weights), start=1):
        raw_price = params.anchor - distance if side is Side.LONG else params.anchor + distance
        if raw_price <= ZERO:
            raise _GridBuildError(RejectionCode.INVALID_INPUT, "grid produces a non-positive price")
        price = rounded_price(raw_price, filters.tick_size, side)
        if previous_price is not None:
            if side is Side.LONG and price >= previous_price:
                raise _GridBuildError(RejectionCode.INVALID_INPUT, "rounded Long levels are duplicate or not strictly descending")
            if side is Side.SHORT and price <= previous_price:
                raise _GridBuildError(RejectionCode.INVALID_INPUT, "rounded Short levels are duplicate or not strictly ascending")
        previous_price = price
        target = side_budget_quote * weight
        raw_quantity = target / (price * filters.contract_multiplier)
        quantity = _floor_to_step(raw_quantity, filters.step_size)
        if quantity < filters.min_qty:
            raise _GridBuildError(RejectionCode.EXCHANGE_FILTER, f"{side.value} level {i} falls below min_qty after rounding")
        if filters.max_qty is not None and quantity > filters.max_qty:
            raise _GridBuildError(RejectionCode.EXCHANGE_FILTER, f"{side.value} level {i} exceeds max_qty")
        notional = order_notional(quantity, price, filters.contract_multiplier)
        if filters.min_notional is not None and notional < filters.min_notional:
            raise _GridBuildError(RejectionCode.EXCHANGE_FILTER, f"{side.value} level {i} falls below min_notional after rounding")
        if filters.max_notional is not None and notional > filters.max_notional:
            raise _GridBuildError(RejectionCode.EXCHANGE_FILTER, f"{side.value} level {i} exceeds max_notional")
        if notional > target:
            raise _GridBuildError(RejectionCode.RISK_LIMIT, "rounded order exceeds its allocated quote budget")
        exit_result = cost_aware_exit_price(
            side=side, quantity=quantity, entry_price=price, multiplier=filters.contract_multiplier,
            entry_fee_rate=costs.entry_fee_rate, exit_fee_rate=costs.exit_fee_rate,
            funding_cost_quote=costs.funding_cost_quote, execution_buffer_quote=costs.execution_buffer_quote,
            required_net_profit_quote=costs.required_net_profit_quote, tick_size=filters.tick_size,
        )
        if not exit_result.accepted:
            raise _GridBuildError(
                RejectionCode.COST_HURDLE,
                f"{side.value} level {i} cannot meet cost-aware exit hurdle: {exit_result.rejection.message}",
            )
        exit_price, estimated_net = exit_result.value
        exit_notional = order_notional(quantity, exit_price, filters.contract_multiplier)
        if filters.min_notional is not None and exit_notional < filters.min_notional:
            raise _GridBuildError(RejectionCode.EXCHANGE_FILTER, f"{side.value} level {i} exit falls below min_notional")
        if filters.max_notional is not None and exit_notional > filters.max_notional:
            raise _GridBuildError(RejectionCode.EXCHANGE_FILTER, f"{side.value} level {i} exit exceeds max_notional")
        proposals.append(OrderProposal(side, i, price, quantity, notional, target, exit_price, estimated_net))
        allocated += notional
    if allocated > side_budget_quote:
        raise _GridBuildError(RejectionCode.RISK_LIMIT, f"rounded {side.value} grid exceeds side budget")
    return tuple(proposals)


def _check_risk(
    long_notional: Decimal,
    short_notional: Decimal,
    limits: RiskLimits,
    risk_model: RiskModel,
    effective_available_margin_quote: Decimal,
) -> tuple[Decimal, Decimal]:
    gross = long_notional + short_notional
    net = abs(long_notional - short_notional)
    long_stress = long_notional * risk_model.stress_loss_fraction_long
    short_stress = short_notional * risk_model.stress_loss_fraction_short
    stress = long_stress + short_stress
    long_margin = long_notional * risk_model.initial_margin_fraction_long
    short_margin = short_notional * risk_model.initial_margin_fraction_short
    margin = long_margin + short_margin
    if long_notional > limits.max_long_notional or short_notional > limits.max_short_notional:
        raise _GridBuildError(RejectionCode.RISK_LIMIT, "per-leg notional hard limit exceeded")
    if gross > limits.max_gross_notional:
        raise _GridBuildError(RejectionCode.RISK_LIMIT, "combined gross notional hard limit exceeded")
    if net > limits.max_abs_net_notional:
        raise _GridBuildError(RejectionCode.RISK_LIMIT, "absolute net notional hard limit exceeded")
    if stress > limits.max_stress_loss_quote:
        raise _GridBuildError(RejectionCode.RISK_LIMIT, "combined stress-loss hard limit exceeded")
    if margin > limits.max_initial_margin_quote or margin > limits.available_margin_quote or margin > effective_available_margin_quote:
        raise _GridBuildError(RejectionCode.RISK_LIMIT, "initial margin exceeds configured limit or fresh account available margin")
    if long_stress > limits.stress_loss_long_quote or short_stress > limits.stress_loss_short_quote:
        raise _GridBuildError(RejectionCode.RISK_LIMIT, "a leg exceeds its own stress-loss budget")
    if long_margin > limits.initial_margin_long_quote or short_margin > limits.initial_margin_short_quote:
        raise _GridBuildError(RejectionCode.RISK_LIMIT, "a leg exceeds its own initial-margin budget")
    return stress, margin


def check_exposure(
    *,
    long_notional: Decimal,
    short_notional: Decimal,
    limits: RiskLimits,
    risk_model: RiskModel,
    available_margin_quote: Decimal,
) -> Result:
    """Public, non-raising combined exposure check for live-lifecycle re-evaluation."""
    values = (long_notional, short_notional, available_margin_quote)
    if any(not _is_finite_decimal(value) or value < ZERO for value in values):
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "exposure inputs must be non-negative finite Decimals"))
    if not isinstance(limits, RiskLimits) or not isinstance(risk_model, RiskModel):
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "risk limits and model are required"))
    try:
        stress, margin = _check_risk(long_notional, short_notional, limits, risk_model, available_margin_quote)
    except _GridBuildError as exc:
        return Result(rejection=Rejection(exc.code, str(exc)))
    return Result(value=(stress, margin))


@dataclass(frozen=True)
class ResolvedParameter:
    value: Decimal
    mode: ParameterMode
    algorithm_version: str
    source_timestamp: int | None
    reason: str


def resolve_parameter(*, mode: ParameterMode, manual_value: Decimal | None, auto_candidate: Decimal | None,
                      minimum: Decimal, maximum: Decimal, algorithm_version: str, reason: str,
                      source_timestamp: int | None, now_timestamp: int, max_source_age_timestamp_units: int) -> Result:
    """Resolve only explicit bounded values; AUTO requires a fresh explained source."""
    if not _is_finite_decimal(minimum) or not _is_finite_decimal(maximum) or minimum > maximum:
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "parameter bounds must be finite Decimal values in order"))
    if not isinstance(algorithm_version, str) or not algorithm_version or not isinstance(reason, str) or not reason:
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "algorithm version and explanation are required"))
    if not _is_int(now_timestamp) or not _is_int(max_source_age_timestamp_units):
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "current and maximum-age timestamps must be integers"))
    if mode is ParameterMode.MANUAL:
        if manual_value is None:
            return Result(rejection=Rejection(RejectionCode.MISSING_INPUT, "manual value is required"))
        if not _is_finite_decimal(manual_value) or not minimum <= manual_value <= maximum:
            return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "manual value outside bounds"))
        return Result(value=ResolvedParameter(manual_value, mode, algorithm_version, source_timestamp, reason))
    if mode is not ParameterMode.AUTO:
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "unsupported parameter mode"))
    if auto_candidate is None or source_timestamp is None:
        return Result(rejection=Rejection(RejectionCode.MISSING_INPUT, "AUTO value and source timestamp are required"))
    if not _is_finite_decimal(auto_candidate) or not _is_int(source_timestamp):
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "AUTO value must be a finite Decimal and its source timestamp an integer"))
    if now_timestamp < source_timestamp or max_source_age_timestamp_units < 0 or now_timestamp-source_timestamp > max_source_age_timestamp_units:
        return Result(rejection=Rejection(RejectionCode.STALE_OR_INCOMPLETE_DATA, "AUTO input is stale or from the future"))
    if not minimum <= auto_candidate <= maximum:
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "AUTO value outside bounds"))
    return Result(value=ResolvedParameter(auto_candidate, mode, algorithm_version, source_timestamp, reason))


@dataclass(frozen=True)
class CloseQuantity:
    quantity: Decimal
    remaining_quantity: Decimal
    full_close: bool


def calculate_close_quantity(*, position_quantity: Decimal, requested_quantity: Decimal, step_size: Decimal, full_close: bool = False) -> Result:
    """Round reductions down and reject any amount that could reverse exposure."""
    values = (position_quantity, requested_quantity, step_size)
    if any(not _is_finite_decimal(x) or x <= ZERO for x in values):
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "position, requested quantity and step must be finite and positive"))
    if not isinstance(full_close, bool):
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "full_close must be an explicit boolean"))
    quantity = _floor_to_step(requested_quantity, step_size)
    if quantity <= ZERO:
        return Result(rejection=Rejection(RejectionCode.EXCHANGE_FILTER, "close quantity rounds to zero"))
    if quantity > position_quantity:
        return Result(rejection=Rejection(RejectionCode.RISK_LIMIT, "close quantity exceeds open position"))
    remaining = position_quantity-quantity
    is_full = remaining == ZERO
    if full_close and not is_full:
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "full close does not exactly flatten modeled position"))
    return Result(value=CloseQuantity(quantity, remaining, is_full))


def calculate_side_budgets(
    *, snapshot: AccountSnapshot, policy: AllocationPolicy, expected_mode: RunMode,
    now_timestamp: int, max_snapshot_age_timestamp_units: int,
) -> Result:
    if not isinstance(snapshot, AccountSnapshot) or not isinstance(policy, AllocationPolicy):
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "account snapshot and allocation policy are required"))
    if not isinstance(expected_mode, RunMode):
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "expected run mode must be TEST or LIVE"))
    if not _is_int(now_timestamp) or not _is_int(max_snapshot_age_timestamp_units):
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "current and maximum-age timestamps must be integers"))
    if snapshot.mode is not expected_mode:
        return Result(rejection=Rejection(RejectionCode.STALE_OR_INCOMPLETE_DATA, "account snapshot belongs to another environment"))
    if not snapshot.reconciled or snapshot.unknown_external_positions or snapshot.unknown_external_orders:
        return Result(rejection=Rejection(RejectionCode.STALE_OR_INCOMPLETE_DATA, "account is unreconciled or has unknown external state"))
    if now_timestamp < snapshot.snapshot_timestamp:
        return Result(rejection=Rejection(RejectionCode.STALE_OR_INCOMPLETE_DATA, "account snapshot is from the future"))
    if max_snapshot_age_timestamp_units < 0 or now_timestamp - snapshot.snapshot_timestamp > max_snapshot_age_timestamp_units:
        return Result(rejection=Rejection(RejectionCode.STALE_OR_INCOMPLETE_DATA, "account snapshot is stale"))
    available_after_reserve = max(ZERO, snapshot.deployable_collateral_quote - policy.protected_reserve_quote)
    allocation = min(policy.max_strategy_allocation_quote, available_after_reserve)
    long_budget = allocation * policy.long_fraction
    short_budget = allocation * policy.short_fraction
    if allocation <= ZERO or long_budget <= ZERO or short_budget <= ZERO:
        return Result(rejection=Rejection(RejectionCode.RISK_LIMIT, "no paired budget remains after reserve"))
    return Result(value=SideBudgets(allocation, long_budget, short_budget, available_after_reserve))


def build_paired_grid(
    *,
    symbol: str,
    params: GridParameters,
    filters: ExchangeFilters,
    limits: RiskLimits,
    risk_model: RiskModel,
    cost_assumptions: CostAssumptions,
    account_snapshot: AccountSnapshot,
    allocation_policy: AllocationPolicy,
    config_version: str,
    current_market_snapshot_timestamp: int,
    max_snapshot_age_timestamp_units: int,
    current_account_timestamp: int,
    max_account_age_timestamp_units: int,
    best_bid: Decimal,
    best_ask: Decimal,
) -> Result:
    """Build an all-or-nothing pure proposal for both sides; never calls the exchange.

    ``best_bid``/``best_ask`` are assumed to belong to ``params.snapshot_timestamp``; the
    caller attests that book snapshot, and its age is enforced against
    ``current_market_snapshot_timestamp``/``max_snapshot_age_timestamp_units``.
    """
    if not isinstance(params, GridParameters) or not isinstance(limits, RiskLimits) or not isinstance(risk_model, RiskModel) or not isinstance(cost_assumptions, CostAssumptions) or not isinstance(account_snapshot, AccountSnapshot) or not isinstance(allocation_policy, AllocationPolicy):
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "all grid, risk, cost and account inputs must be typed domain values"))
    if not isinstance(symbol, str) or not symbol or not isinstance(config_version, str) or not config_version:
        return Result(rejection=Rejection(RejectionCode.MISSING_INPUT, "symbol and config version are required"))
    if not isinstance(filters, ExchangeFilters) or not filters.trading or filters.contract_type != "PERPETUAL":
        return Result(rejection=Rejection(RejectionCode.EXCHANGE_FILTER, "instrument is not an enabled perpetual"))
    if symbol != f"{filters.base_asset}{filters.quote_asset}":
        return Result(rejection=Rejection(RejectionCode.EXCHANGE_FILTER, "symbol does not match the configured base/quote assets"))
    if not _is_finite_decimal(best_bid) or not _is_finite_decimal(best_ask):
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "best bid/ask must be finite Decimal values"))
    if best_bid <= ZERO or best_ask <= ZERO or best_bid > best_ask:
        return Result(rejection=Rejection(RejectionCode.STALE_OR_INCOMPLETE_DATA, "best bid/ask is missing or invalid"))
    if not _is_int(current_market_snapshot_timestamp) or not _is_int(max_snapshot_age_timestamp_units):
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "market snapshot timestamps must be integers"))
    if not _is_int(current_account_timestamp) or not _is_int(max_account_age_timestamp_units):
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "account snapshot timestamps must be integers"))
    account_budgets = calculate_side_budgets(
        snapshot=account_snapshot, policy=allocation_policy, expected_mode=params.mode,
        now_timestamp=current_account_timestamp,
        max_snapshot_age_timestamp_units=max_account_age_timestamp_units,
    )
    if not account_budgets.accepted:
        return Result(rejection=account_budgets.rejection)
    budgets = account_budgets.value
    if params.long_budget_quote > budgets.long_budget_quote or params.short_budget_quote > budgets.short_budget_quote:
        return Result(rejection=Rejection(RejectionCode.RISK_LIMIT, "grid side budget exceeds fresh account allocation"))
    if current_market_snapshot_timestamp < params.snapshot_timestamp:
        return Result(rejection=Rejection(RejectionCode.STALE_OR_INCOMPLETE_DATA, "snapshot timestamp is in the future"))
    if max_snapshot_age_timestamp_units < 0 or current_market_snapshot_timestamp - params.snapshot_timestamp > max_snapshot_age_timestamp_units:
        return Result(rejection=Rejection(RejectionCode.STALE_OR_INCOMPLETE_DATA, "market snapshot is stale"))
    try:
        long_orders = _proposal_for_side(Side.LONG, params, filters, params.long_budget_quote, cost_assumptions)
        short_orders = _proposal_for_side(Side.SHORT, params, filters, params.short_budget_quote, cost_assumptions)
        if any(order.price >= best_ask for order in long_orders):
            raise _GridBuildError(RejectionCode.INVALID_INPUT, "rounded Long entry is marketable at the current best ask")
        if any(order.price <= best_bid for order in short_orders):
            raise _GridBuildError(RejectionCode.INVALID_INPUT, "rounded Short entry is marketable at the current best bid")
        long_notional = sum((o.notional_quote for o in long_orders), ZERO)
        short_notional = sum((o.notional_quote for o in short_orders), ZERO)
        stress, margin = _check_risk(
            long_notional, short_notional, limits, risk_model,
            min(limits.available_margin_quote, budgets.available_margin_quote),
        )
        value = GridProposal(
            symbol=symbol,
            mode=params.mode,
            anchor=params.anchor,
            snapshot_timestamp=params.snapshot_timestamp,
            long_orders=long_orders,
            short_orders=short_orders,
            long_notional_quote=long_notional,
            short_notional_quote=short_notional,
            gross_notional_quote=long_notional + short_notional,
            signed_net_notional_quote=long_notional - short_notional,
            stress_loss_quote=stress,
            initial_margin_quote=margin,
            config_version=config_version,
        )
        return Result(value=value)
    except _GridBuildError as exc:
        return Result(rejection=Rejection(exc.code, str(exc)))
    except DomainError as exc:
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, str(exc)))


def expected_net_pnl(
    *,
    side: Side,
    quantity: Decimal,
    entry_price: Decimal,
    exit_price: Decimal,
    multiplier: Decimal,
    entry_fee_rate: Decimal,
    exit_fee_rate: Decimal,
    funding_cost_quote: Decimal,
    execution_buffer_quote: Decimal,
) -> Decimal:
    """Estimated linear-contract PnL; signed funding is a cost when positive, rebate when negative."""
    if not isinstance(side,Side):
        raise DomainError("side must be LONG or SHORT")
    values=(quantity,entry_price,exit_price,multiplier,entry_fee_rate,exit_fee_rate,funding_cost_quote,execution_buffer_quote)
    if any(not _is_finite_decimal(x) for x in values):
        raise DomainError("PnL inputs must be finite Decimal values")
    if quantity <= ZERO or entry_price <= ZERO or exit_price <= ZERO or multiplier <= ZERO:
        raise DomainError("quantity, prices and multiplier must be positive")
    if min(entry_fee_rate, exit_fee_rate, execution_buffer_quote) < ZERO:
        raise DomainError("fee rates and execution buffer cannot be negative")
    direction = ONE if side is Side.LONG else Decimal("-1")
    gross = direction * quantity * multiplier * (exit_price - entry_price)
    entry_fee = quantity * multiplier * entry_price * entry_fee_rate
    exit_fee = quantity * multiplier * exit_price * exit_fee_rate
    return gross - entry_fee - exit_fee - funding_cost_quote - execution_buffer_quote


@dataclass(frozen=True)
class EntryFill:
    quantity: Decimal
    price: Decimal
    fee_rate: Decimal

    def __post_init__(self) -> None:
        if not all(_is_finite_decimal(x) for x in (self.quantity, self.price, self.fee_rate)):
            raise DomainError("fill values must be finite Decimal values")
        if self.quantity <= ZERO or self.price <= ZERO or self.fee_rate < ZERO:
            raise DomainError("fill quantity/price must be positive and fee rate non-negative")


def expected_net_pnl_for_fills(*, side: Side, fills: Sequence[EntryFill], exit_price: Decimal,
                               exit_fee_rate: Decimal, multiplier: Decimal,
                               funding_cost_quote: Decimal, execution_buffer_quote: Decimal) -> Decimal:
    """Aggregate linear-contract PnL and the actual per-fill entry fee rates."""
    if not isinstance(side, Side) or not fills:
        raise DomainError("side and at least one entry fill are required")
    if any(not isinstance(fill, EntryFill) for fill in fills):
        raise DomainError("entry fills must be EntryFill values")
    if not all(_is_finite_decimal(x) for x in (exit_price, exit_fee_rate, multiplier, funding_cost_quote, execution_buffer_quote)):
        raise DomainError("exit PnL values must be finite Decimal values")
    if exit_price <= ZERO or multiplier <= ZERO or exit_fee_rate < ZERO or execution_buffer_quote < ZERO:
        raise DomainError("exit price/multiplier must be positive and fees/buffer non-negative")
    quantity = sum((fill.quantity for fill in fills), ZERO)
    entry_notional = sum((fill.quantity * fill.price * multiplier for fill in fills), ZERO)
    entry_fees = sum((fill.quantity * fill.price * multiplier * fill.fee_rate for fill in fills), ZERO)
    exit_notional = quantity * exit_price * multiplier
    gross = exit_notional - entry_notional if side is Side.LONG else entry_notional - exit_notional
    exit_fees = exit_notional * exit_fee_rate
    return gross - entry_fees - exit_fees - funding_cost_quote - execution_buffer_quote


def cost_aware_exit_price(
    *,
    side: Side,
    quantity: Decimal,
    entry_price: Decimal,
    multiplier: Decimal,
    entry_fee_rate: Decimal,
    exit_fee_rate: Decimal,
    funding_cost_quote: Decimal,
    execution_buffer_quote: Decimal,
    required_net_profit_quote: Decimal,
    tick_size: Decimal,
) -> Result:
    """Solve linear estimated exit hurdle and round passively away from entry."""
    try:
        if not isinstance(side,Side):
            raise DomainError("side must be LONG or SHORT")
        values=(quantity,entry_price,multiplier,entry_fee_rate,exit_fee_rate,funding_cost_quote,execution_buffer_quote,required_net_profit_quote,tick_size)
        if any(not isinstance(x,Decimal) or not x.is_finite() for x in values):
            raise DomainError("exit-price inputs must be finite Decimal values")
        if quantity <= ZERO or entry_price <= ZERO or multiplier <= ZERO or tick_size <= ZERO:
            raise DomainError("quantity, entry, multiplier and tick must be positive")
        if min(entry_fee_rate, exit_fee_rate, execution_buffer_quote, required_net_profit_quote) < ZERO:
            raise DomainError("fee rates, execution buffer and target must be non-negative")
        if exit_fee_rate >= ONE:
            raise DomainError("exit fee rate must be below one for a linear quote-settled exit")
        q = quantity * multiplier
        if side is Side.LONG:
            numerator = q * entry_price * (ONE + entry_fee_rate) + funding_cost_quote + execution_buffer_quote + required_net_profit_quote
            denominator = q * (ONE - exit_fee_rate)
            if denominator <= ZERO:
                raise DomainError("exit fee rate makes a finite Long target impossible")
            raw = numerator / denominator
            price = _ceil_to_step(raw, tick_size)
            if price <= entry_price:
                price = _ceil_to_step(entry_price + tick_size, tick_size)
        else:
            numerator = q * entry_price * (ONE - entry_fee_rate) - funding_cost_quote - execution_buffer_quote - required_net_profit_quote
            denominator = q * (ONE + exit_fee_rate)
            raw = numerator / denominator
            if raw <= ZERO:
                raise DomainError("required net target is impossible at a positive Short exit price")
            price = _floor_to_step(raw, tick_size)
            if price >= entry_price:
                price = _floor_to_step(entry_price - tick_size, tick_size)
            if price <= ZERO:
                raise DomainError("Short exit target rounds to a non-positive price")
        net = expected_net_pnl(
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            exit_price=price,
            multiplier=multiplier,
            entry_fee_rate=entry_fee_rate,
            exit_fee_rate=exit_fee_rate,
            funding_cost_quote=funding_cost_quote,
            execution_buffer_quote=execution_buffer_quote,
        )
        hurdle = validate_fee_edge(net, required_net_profit_quote)
        if not hurdle.accepted:
            return Result(rejection=hurdle.rejection)
        return Result(value=(price, net))
    except DomainError as exc:
        return Result(rejection=Rejection(RejectionCode.COST_HURDLE, str(exc)))


def select_level_count(
    *,
    min_levels: int,
    max_levels: int,
    max_allowed_levels: int,
    evaluate: Callable[[int], bool],
) -> Result:
    """Deterministically choose largest candidate passing the full supplied checks."""
    if not _is_int(min_levels) or not _is_int(max_levels) or not _is_int(max_allowed_levels):
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "level-count bounds must be integers"))
    if min_levels < 1 or max_levels < min_levels or max_allowed_levels < 1:
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "invalid level-count bounds"))
    upper = min(max_levels, max_allowed_levels)
    for count in range(upper, min_levels - 1, -1):
        try:
            if evaluate(count):
                return Result(value=count)
        except DomainError:
            continue
    return Result(rejection=Rejection(RejectionCode.NO_VALID_LEVEL_COUNT, "no level count passes full sizing and risk checks"))


def validate_fee_edge(net_profit_quote: Decimal, required_net_profit_quote: Decimal) -> Result:
    if not _is_finite_decimal(net_profit_quote) or not _is_finite_decimal(required_net_profit_quote):
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "fee-edge values must be finite Decimal amounts"))
    if required_net_profit_quote < ZERO:
        return Result(rejection=Rejection(RejectionCode.INVALID_INPUT, "required net profit cannot be negative"))
    if net_profit_quote < required_net_profit_quote:
        return Result(rejection=Rejection(RejectionCode.COST_HURDLE, "estimated net target is below required hurdle"))
    return Result(value=net_profit_quote)


def allocate_side_budget(total_budget: Decimal, long_fraction: Decimal, short_fraction: Decimal) -> tuple[Decimal, Decimal]:
    values = (total_budget, long_fraction, short_fraction)
    if any(not _is_finite_decimal(value) for value in values):
        raise DomainError("budget and side fractions must be finite Decimal values")
    if total_budget <= ZERO or long_fraction < ZERO or short_fraction < ZERO:
        raise DomainError("budget must be positive and fractions non-negative")
    if long_fraction + short_fraction != ONE:
        raise DomainError("side budget fractions must sum exactly to one")
    return total_budget * long_fraction, total_budget * short_fraction
