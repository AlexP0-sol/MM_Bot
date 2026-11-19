"""Deterministic paper venue: account, conservative maker fills, fees and funding.

The model never treats a price touch as a guaranteed fill. A resting buy fills only
after an observed sell-aggressor trade at or below its limit has consumed the explicit
queue-ahead quantity; a resting sell requires the symmetric buy-aggressor trade. Fills
are executed at the order's own limit price, never at a better price, and each fill is
capped by the observed trade quantity.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .domain import DomainError, Side
from .exchange import (
    Aggressor,
    CancelResult,
    Fill,
    FillResult,
    MarketDataError,
    MarketTick,
    OrderRequest,
    OrderRole,
    OrderSide,
    OrderSnapshot,
    OrderStatus,
    PlacementResult,
    OPEN_STATUSES,
)


ZERO = Decimal("0")


def _finite(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


@dataclass(frozen=True)
class PaperFees:
    maker_rate: Decimal
    taker_rate: Decimal

    def __post_init__(self) -> None:
        if not _finite(self.maker_rate) or not _finite(self.taker_rate):
            raise DomainError("paper fee rates must be finite Decimal values")
        if self.maker_rate < ZERO or self.taker_rate < ZERO:
            raise DomainError("paper fee rates cannot be negative")
        if self.maker_rate >= Decimal("1") or self.taker_rate >= Decimal("1"):
            raise DomainError("paper fee rates must be below one")


@dataclass(frozen=True)
class PaperConfig:
    fees: PaperFees
    initial_balance_quote: Decimal
    margin_fraction: Decimal
    multiplier: Decimal
    latency_ms: int
    tick_size: Decimal
    market_close_slippage_ticks: int
    step_size: Decimal = Decimal("0.01")
    min_qty: Decimal = Decimal("0.01")
    min_notional: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.fees, PaperFees):
            raise DomainError("paper fees are required")
        if not _finite(self.initial_balance_quote) or self.initial_balance_quote <= ZERO:
            raise DomainError("initial paper balance must be a positive finite Decimal")
        if not _finite(self.margin_fraction) or not (ZERO < self.margin_fraction <= Decimal("1")):
            raise DomainError("margin fraction must be in (0, 1]")
        if not _finite(self.multiplier) or self.multiplier <= ZERO:
            raise DomainError("contract multiplier must be positive")
        if not isinstance(self.latency_ms, int) or isinstance(self.latency_ms, bool) or self.latency_ms < 0:
            raise DomainError("latency_ms must be a non-negative integer")
        if not _finite(self.tick_size) or self.tick_size <= ZERO:
            raise DomainError("tick size must be positive")
        if not _finite(self.step_size) or self.step_size <= ZERO:
            raise DomainError("step size must be positive")
        if not _finite(self.min_qty) or self.min_qty <= ZERO:
            raise DomainError("minimum quantity must be positive")
        if self.min_notional is not None and (not _finite(self.min_notional) or self.min_notional < ZERO):
            raise DomainError("minimum notional must be a non-negative finite Decimal")
        if (
            not isinstance(self.market_close_slippage_ticks, int)
            or isinstance(self.market_close_slippage_ticks, bool)
            or self.market_close_slippage_ticks < 0
        ):
            raise DomainError("market-close slippage ticks must be a non-negative integer")


@dataclass
class PaperPosition:
    quantity: Decimal
    entry_vwap: Decimal
    entry_fee_quote: Decimal = ZERO

    def to_state(self) -> dict:
        return {
            "quantity": str(self.quantity),
            "entry_vwap": str(self.entry_vwap),
            "entry_fee_quote": str(self.entry_fee_quote),
        }


class PaperAccount:
    """Cash, separately tracked Long/Short positions, realized PnL and cost ledger."""

    def __init__(self, balance_quote: Decimal) -> None:
        self.balance_quote = balance_quote
        self.positions: dict[tuple[str, Side], PaperPosition] = {}
        self.realized_pnl_quote = ZERO
        self.fees_paid_quote = ZERO
        self.funding_paid_quote = ZERO
        self.entry_fees_paid_quote = ZERO
        self.exit_fees_paid_quote = ZERO

    def position_quantity(self, symbol: str, side: Side) -> Decimal:
        position = self.positions.get((symbol, side))
        return ZERO if position is None else position.quantity

    def position_vwap(self, symbol: str, side: Side) -> Decimal:
        position = self.positions.get((symbol, side))
        return ZERO if position is None else position.entry_vwap

    def position_entry_fee_quote(self, symbol: str, side: Side) -> Decimal:
        position = self.positions.get((symbol, side))
        return ZERO if position is None else position.entry_fee_quote

    def _valuations(self, valuations: dict[str, Decimal]) -> dict[str, Decimal]:
        for (symbol, _side), position in self.positions.items():
            if position.quantity > ZERO and symbol not in valuations:
                raise MarketDataError(f"no valuation price for open position {symbol}")
        return valuations

    def unrealized_pnl(self, valuations: dict[str, Decimal], multiplier: Decimal) -> Decimal:
        prices = self._valuations(valuations)
        total = ZERO
        for (symbol, side), position in self.positions.items():
            if position.quantity <= ZERO:
                continue
            mark = prices[symbol]
            if side is Side.LONG:
                total += (mark - position.entry_vwap) * position.quantity * multiplier
            else:
                total += (position.entry_vwap - mark) * position.quantity * multiplier
        return total

    def used_margin(self, valuations: dict[str, Decimal], margin_fraction: Decimal, multiplier: Decimal) -> Decimal:
        prices = self._valuations(valuations)
        total = ZERO
        for (symbol, _side), position in self.positions.items():
            if position.quantity > ZERO:
                total += prices[symbol] * position.quantity * multiplier * margin_fraction
        return total

    def equity(self, valuations: dict[str, Decimal], multiplier: Decimal) -> Decimal:
        return self.balance_quote + self.unrealized_pnl(valuations, multiplier)

    def available_margin(
        self, valuations: dict[str, Decimal], margin_fraction: Decimal, multiplier: Decimal
    ) -> Decimal:
        return self.equity(valuations, multiplier) - self.used_margin(valuations, margin_fraction, multiplier)

    def apply_fill(self, fill: Fill, multiplier: Decimal) -> Decimal:
        key = (fill.symbol, fill.position_side)
        position = self.positions.get(key)
        self.fees_paid_quote += fill.fee_quote
        if fill.role is OrderRole.ENTRY:
            self.entry_fees_paid_quote += fill.fee_quote
            self.balance_quote -= fill.fee_quote
            notional = fill.quantity * fill.price * multiplier
            if position is None:
                self.positions[key] = PaperPosition(fill.quantity, fill.price, fill.fee_quote)
            else:
                total_quantity = position.quantity + fill.quantity
                position.entry_vwap = (position.entry_vwap * position.quantity + notional) / total_quantity
                position.quantity = total_quantity
                position.entry_fee_quote += fill.fee_quote
            return ZERO

        if position is None or fill.quantity > position.quantity:
            raise MarketDataError("exit fill exceeds the modeled position")
        self.exit_fees_paid_quote += fill.fee_quote
        if fill.position_side is Side.LONG:
            realized = (fill.price - position.entry_vwap) * fill.quantity * multiplier
        else:
            realized = (position.entry_vwap - fill.price) * fill.quantity * multiplier
        position.quantity -= fill.quantity
        if position.quantity == ZERO:
            del self.positions[key]
        self.realized_pnl_quote += realized
        self.balance_quote += realized - fill.fee_quote
        return realized

    def apply_funding(self, rate: Decimal, valuations: dict[str, Decimal], multiplier: Decimal) -> Decimal:
        prices = self._valuations(valuations)
        total_cost = ZERO
        for (symbol, side), position in self.positions.items():
            if position.quantity <= ZERO:
                continue
            notional = prices[symbol] * position.quantity * multiplier
            cost = notional * rate if side is Side.LONG else -notional * rate
            total_cost += cost
        if total_cost != ZERO:
            self.balance_quote -= total_cost
            self.funding_paid_quote += total_cost
        return total_cost

    def to_state(self) -> dict:
        return {
            "balance_quote": str(self.balance_quote),
            "realized_pnl_quote": str(self.realized_pnl_quote),
            "fees_paid_quote": str(self.fees_paid_quote),
            "funding_paid_quote": str(self.funding_paid_quote),
            "entry_fees_paid_quote": str(self.entry_fees_paid_quote),
            "exit_fees_paid_quote": str(self.exit_fees_paid_quote),
            "positions": [
                {"symbol": symbol, "side": side.value, "quantity": str(position.quantity),
                 "entry_vwap": str(position.entry_vwap), "entry_fee_quote": str(position.entry_fee_quote)}
                for (symbol, side), position in sorted(self.positions.items(), key=lambda item: (item[0][0], item[0][1].value))
            ],
        }

    @classmethod
    def from_state(cls, state: dict) -> "PaperAccount":
        account = cls(Decimal(state["balance_quote"]))
        account.realized_pnl_quote = Decimal(state["realized_pnl_quote"])
        account.fees_paid_quote = Decimal(state["fees_paid_quote"])
        account.funding_paid_quote = Decimal(state["funding_paid_quote"])
        account.entry_fees_paid_quote = Decimal(state["entry_fees_paid_quote"])
        account.exit_fees_paid_quote = Decimal(state["exit_fees_paid_quote"])
        for item in state["positions"]:
            account.positions[(item["symbol"], Side(item["side"]))] = PaperPosition(
                Decimal(item["quantity"]), Decimal(item["entry_vwap"]),
                Decimal(item.get("entry_fee_quote", "0")),
            )
        return account


@dataclass
class _PaperOrder:
    request: OrderRequest
    status: OrderStatus
    activation_ts: int
    queue_remaining: Decimal
    filled_quantity: Decimal = ZERO
    average_fill_price: Decimal = ZERO
    fee_quote: Decimal = ZERO
    last_update_ts: int = 0

    @property
    def remaining_quantity(self) -> Decimal:
        return self.request.quantity - self.filled_quantity

    def snapshot(self) -> OrderSnapshot:
        return OrderSnapshot(
            client_order_id=self.request.client_order_id,
            symbol=self.request.symbol,
            position_side=self.request.position_side,
            role=self.request.role,
            order_side=self.request.order_side,
            price=self.request.price,
            quantity=self.request.quantity,
            filled_quantity=self.filled_quantity,
            remaining_quantity=self.remaining_quantity,
            average_fill_price=self.average_fill_price,
            fee_quote=self.fee_quote,
            status=self.status,
            created_ts=self.request.created_ts,
            activation_ts=self.activation_ts,
            last_update_ts=self.last_update_ts,
        )

    def to_state(self) -> dict:
        request = self.request
        return {
            "client_order_id": request.client_order_id,
            "symbol": request.symbol,
            "position_side": request.position_side.value,
            "role": request.role.value,
            "price": str(request.price),
            "quantity": str(request.quantity),
            "queue_ahead": str(request.queue_ahead),
            "created_ts": request.created_ts,
            "post_only": request.post_only,
            "status": self.status.value,
            "activation_ts": self.activation_ts,
            "queue_remaining": str(self.queue_remaining),
            "filled_quantity": str(self.filled_quantity),
            "average_fill_price": str(self.average_fill_price),
            "fee_quote": str(self.fee_quote),
            "last_update_ts": self.last_update_ts,
        }

    @classmethod
    def from_state(cls, state: dict) -> "_PaperOrder":
        request = OrderRequest(
            client_order_id=state["client_order_id"],
            symbol=state["symbol"],
            position_side=Side(state["position_side"]),
            role=OrderRole(state["role"]),
            price=Decimal(state["price"]),
            quantity=Decimal(state["quantity"]),
            queue_ahead=Decimal(state["queue_ahead"]),
            created_ts=state["created_ts"],
            post_only=state["post_only"],
        )
        return cls(
            request=request,
            status=OrderStatus(state["status"]),
            activation_ts=state["activation_ts"],
            queue_remaining=Decimal(state["queue_remaining"]),
            filled_quantity=Decimal(state["filled_quantity"]),
            average_fill_price=Decimal(state["average_fill_price"]),
            fee_quote=Decimal(state["fee_quote"]),
            last_update_ts=state["last_update_ts"],
        )


class PaperExchange:
    """Deterministic in-memory paper venue. It has no network and no credentials."""

    def __init__(self, config: PaperConfig) -> None:
        if not isinstance(config, PaperConfig):
            raise DomainError("paper config is required")
        self.config = config
        self.account = PaperAccount(config.initial_balance_quote)
        self._orders: dict[str, _PaperOrder] = {}
        self._fills: list[Fill] = []
        self._ticks: dict[str, MarketTick] = {}
        self._funding_events: list[dict] = []

    # -- market data -----------------------------------------------------

    @property
    def fills(self) -> tuple[Fill, ...]:
        return tuple(self._fills)

    def last_tick(self, symbol: str) -> MarketTick | None:
        return self._ticks.get(symbol)

    def _valuations(self) -> dict[str, Decimal]:
        return {symbol: tick.mid for symbol, tick in self._ticks.items()}

    def update_market(self, tick: MarketTick) -> None:
        if not isinstance(tick, MarketTick):
            raise MarketDataError("a typed MarketTick is required")
        previous = self._ticks.get(tick.symbol)
        if previous is not None and tick.timestamp_ms <= previous.timestamp_ms:
            raise MarketDataError("market tick is out of order or duplicated")
        self._ticks[tick.symbol] = tick

    def on_tick(self, tick: MarketTick) -> tuple[Fill, ...]:
        """Advance the market, then simulate maker fills for this single snapshot."""
        self.update_market(tick)
        new_fills: list[Fill] = []
        orders = sorted(
            (order for order in self._orders.values()
             if order.status in OPEN_STATUSES and order.request.symbol == tick.symbol),
            key=lambda order: (order.activation_ts, order.request.client_order_id),
        )
        remaining_volume = tick.last_quantity
        for order in orders:
            if remaining_volume <= ZERO:
                break
            fill, used_volume = self._simulate(order, tick, remaining_volume)
            remaining_volume -= used_volume
            if fill is not None:
                new_fills.append(fill)
        return tuple(new_fills)

    def _simulate(self, order: _PaperOrder, tick: MarketTick, available_volume: Decimal) -> tuple[Fill | None, Decimal]:
        """Simulate one order against a share of the observed trade volume.

        A single public trade cannot fill every resting level with its full size: the
        trade volume is consumed in deterministic order priority, including queue-ahead.
        """
        request = order.request
        if tick.timestamp_ms < order.activation_ts or tick.last_quantity <= ZERO or available_volume <= ZERO:
            return None, ZERO
        if request.order_side is OrderSide.BUY:
            if tick.aggressor is not Aggressor.SELL or tick.last_price > request.price:
                return None, ZERO
        else:
            if tick.aggressor is not Aggressor.BUY or tick.last_price < request.price:
                return None, ZERO

        # A trade strictly through the limit means the queue no longer protects the order.
        if (request.order_side is OrderSide.BUY and tick.last_price < request.price) or (
            request.order_side is OrderSide.SELL and tick.last_price > request.price
        ):
            order.queue_remaining = ZERO
        volume = available_volume
        used_volume = ZERO
        if order.queue_remaining > ZERO:
            consumed = min(order.queue_remaining, volume)
            order.queue_remaining -= consumed
            volume -= consumed
            used_volume += consumed
        quantity = min(order.remaining_quantity, volume)
        if quantity <= ZERO:
            return None, used_volume

        position_quantity = self.account.position_quantity(request.symbol, request.position_side)
        if request.role is OrderRole.EXIT:
            quantity = min(quantity, position_quantity)
            if quantity <= ZERO:
                order.status = OrderStatus.CANCELED
                order.last_update_ts = tick.timestamp_ms
                return None, used_volume

        notional = quantity * request.price * self.config.multiplier
        fee = notional * self.config.fees.maker_rate
        fill = Fill(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            position_side=request.position_side,
            role=request.role,
            price=request.price,
            quantity=quantity,
            fee_quote=fee,
            timestamp_ms=tick.timestamp_ms,
            taker=False,
        )
        self.account.apply_fill(fill, self.config.multiplier)
        total_quantity = order.filled_quantity + quantity
        order.average_fill_price = (
            order.average_fill_price * order.filled_quantity + request.price * quantity
        ) / total_quantity
        order.filled_quantity = total_quantity
        order.fee_quote += fee
        order.last_update_ts = tick.timestamp_ms
        order.status = (
            OrderStatus.FILLED if order.remaining_quantity == ZERO else OrderStatus.PARTIALLY_FILLED
        )
        self._fills.append(fill)
        return fill, used_volume + quantity

    # -- orders ----------------------------------------------------------

    def order(self, client_order_id: str) -> OrderSnapshot | None:
        order = self._orders.get(client_order_id)
        return None if order is None else order.snapshot()

    def orders(self) -> tuple[OrderSnapshot, ...]:
        return tuple(order.snapshot() for order in self._orders.values())

    def open_orders(self, symbol: str | None = None) -> tuple[OrderSnapshot, ...]:
        return tuple(
            order.snapshot()
            for order in self._orders.values()
            if order.status in OPEN_STATUSES and (symbol is None or order.request.symbol == symbol)
        )

    def position(self, symbol: str, side: Side) -> tuple[Decimal, Decimal]:
        return self.account.position_quantity(symbol, side), self.account.position_vwap(symbol, side)

    def position_entry_fee_rate(self, symbol: str, side: Side) -> Decimal:
        quantity, vwap = self.position(symbol, side)
        notional = quantity * vwap * self.config.multiplier
        if notional <= ZERO:
            return ZERO
        return self.account.position_entry_fee_quote(symbol, side) / notional

    def reserved_margin(self) -> Decimal:
        """Margin held by open entry intents, mirroring exchange open-order reserve."""
        total = ZERO
        for order in self._orders.values():
            if order.status in OPEN_STATUSES and order.request.role is OrderRole.ENTRY:
                total += (
                    order.remaining_quantity * order.request.price * self.config.multiplier
                    * self.config.margin_fraction
                )
        return total

    def place(self, request: OrderRequest) -> PlacementResult:
        if not isinstance(request, OrderRequest):
            raise DomainError("an OrderRequest is required")
        existing = self._orders.get(request.client_order_id)
        if existing is not None:
            snapshot = existing.snapshot()
            return PlacementResult(
                accepted=existing.status in OPEN_STATUSES,
                status=existing.status,
                reason="duplicate client order id; existing intent returned",
                duplicate=True,
                order=snapshot,
            )
        tick = self._ticks.get(request.symbol)
        if tick is None:
            return PlacementResult(False, OrderStatus.REJECTED, "no market snapshot for symbol")
        if request.price % self.config.tick_size != ZERO or request.quantity % self.config.step_size != ZERO:
            order = _PaperOrder(
                request=request, status=OrderStatus.REJECTED,
                activation_ts=request.created_ts + self.config.latency_ms,
                queue_remaining=request.queue_ahead, last_update_ts=request.created_ts,
            )
            self._orders[request.client_order_id] = order
            return PlacementResult(False, OrderStatus.REJECTED, "price or quantity violates tick/step filters", order=order.snapshot())
        if request.quantity < self.config.min_qty:
            order = _PaperOrder(
                request=request, status=OrderStatus.REJECTED,
                activation_ts=request.created_ts + self.config.latency_ms,
                queue_remaining=request.queue_ahead, last_update_ts=request.created_ts,
            )
            self._orders[request.client_order_id] = order
            return PlacementResult(False, OrderStatus.REJECTED, "quantity is below the paper minQty", order=order.snapshot())
        if self.config.min_notional is not None:
            notional = request.price * request.quantity * self.config.multiplier
            if notional < self.config.min_notional:
                order = _PaperOrder(
                    request=request, status=OrderStatus.REJECTED,
                    activation_ts=request.created_ts + self.config.latency_ms,
                    queue_remaining=request.queue_ahead, last_update_ts=request.created_ts,
                )
                self._orders[request.client_order_id] = order
                return PlacementResult(False, OrderStatus.REJECTED, "notional is below the paper minNotional", order=order.snapshot())
        if request.post_only:
            crosses = (
                request.order_side is OrderSide.BUY and request.price >= tick.ask
            ) or (request.order_side is OrderSide.SELL and request.price <= tick.bid)
            if crosses:
                order = _PaperOrder(
                    request=request,
                    status=OrderStatus.REJECTED,
                    activation_ts=request.created_ts + self.config.latency_ms,
                    queue_remaining=request.queue_ahead,
                    last_update_ts=request.created_ts,
                )
                self._orders[request.client_order_id] = order
                return PlacementResult(False, OrderStatus.REJECTED, "post-only order would cross the book", order=order.snapshot())

        if request.role is OrderRole.EXIT:
            available = self.account.position_quantity(request.symbol, request.position_side)
            if request.quantity > available:
                order = _PaperOrder(
                    request=request,
                    status=OrderStatus.REJECTED,
                    activation_ts=request.created_ts + self.config.latency_ms,
                    queue_remaining=request.queue_ahead,
                    last_update_ts=request.created_ts,
                )
                self._orders[request.client_order_id] = order
                return PlacementResult(False, OrderStatus.REJECTED, "exit exceeds modeled position", order=order.snapshot())
        else:
            required = request.quantity * request.price * self.config.multiplier * self.config.margin_fraction
            try:
                available_margin = self.account.available_margin(
                    self._valuations(), self.config.margin_fraction, self.config.multiplier
                ) - self.reserved_margin()
            except MarketDataError as exc:
                return PlacementResult(False, OrderStatus.REJECTED, f"cannot value account: {exc}")
            if available_margin < required:
                order = _PaperOrder(
                    request=request,
                    status=OrderStatus.REJECTED,
                    activation_ts=request.created_ts + self.config.latency_ms,
                    queue_remaining=request.queue_ahead,
                    last_update_ts=request.created_ts,
                )
                self._orders[request.client_order_id] = order
                return PlacementResult(False, OrderStatus.REJECTED, "insufficient paper margin", order=order.snapshot())

        order = _PaperOrder(
            request=request,
            status=OrderStatus.NEW,
            activation_ts=request.created_ts + self.config.latency_ms,
            queue_remaining=request.queue_ahead,
            last_update_ts=request.created_ts,
        )
        self._orders[request.client_order_id] = order
        return PlacementResult(True, OrderStatus.NEW, order=order.snapshot())

    def cancel(self, client_order_id: str, now_ts: int) -> CancelResult:
        order = self._orders.get(client_order_id)
        if order is None:
            return CancelResult(False, OrderStatus.REJECTED, "unknown client order id")
        if order.status not in OPEN_STATUSES:
            return CancelResult(False, order.status, "order is already terminal")
        order.status = OrderStatus.CANCELED
        order.last_update_ts = now_ts
        return CancelResult(True, OrderStatus.CANCELED)

    def close_market(self, symbol: str, position_side: Side, quantity: Decimal, now_ts: int) -> FillResult:
        tick = self._ticks.get(symbol)
        if tick is None:
            return FillResult(False, "no market snapshot for symbol")
        available = self.account.position_quantity(symbol, position_side)
        if not _finite(quantity) or quantity <= ZERO:
            return FillResult(False, "close quantity must be positive")
        if quantity > available:
            return FillResult(False, "close quantity exceeds modeled position")
        if quantity <= ZERO:
            return FillResult(False, "no modeled position to close")
        if position_side is Side.LONG:
            price = tick.bid - self.config.tick_size * self.config.market_close_slippage_ticks
        else:
            price = tick.ask + self.config.tick_size * self.config.market_close_slippage_ticks
        if price <= ZERO:
            return FillResult(False, "slippage model produced a non-positive price")
        notional = quantity * price * self.config.multiplier
        fill = Fill(
            client_order_id=f"market-close:{len(self._fills) + 1}:{symbol}:{position_side.value}",
            symbol=symbol,
            position_side=position_side,
            role=OrderRole.EXIT,
            price=price,
            quantity=quantity,
            fee_quote=notional * self.config.fees.taker_rate,
            timestamp_ms=now_ts,
            taker=True,
        )
        self.account.apply_fill(fill, self.config.multiplier)
        self._fills.append(fill)
        return FillResult(True, fill=fill)

    def apply_funding(self, rate: Decimal, now_ts: int) -> Decimal:
        if not _finite(rate):
            raise DomainError("funding rate must be a finite Decimal")
        if not isinstance(now_ts, int) or isinstance(now_ts, bool) or now_ts < 0:
            raise DomainError("funding timestamp must be a non-negative integer")
        cost = self.account.apply_funding(rate, self._valuations(), self.config.multiplier)
        self._funding_events.append(
            {"rate": str(rate), "timestamp_ms": now_ts, "cost_quote": str(cost)}
        )
        return cost

    @property
    def funding_events(self) -> tuple[dict, ...]:
        return tuple(self._funding_events)

    # -- restart ---------------------------------------------------------

    def to_state(self) -> dict:
        return {
            "account": self.account.to_state(),
            "orders": [order.to_state() for order in self._orders.values()],
            "fills": [fill.to_state() for fill in self._fills],
            "ticks": [tick.to_state() for tick in self._ticks.values()],
            "funding_events": list(self._funding_events),
        }

    @classmethod
    def from_state(cls, config: PaperConfig, state: dict) -> "PaperExchange":
        exchange = cls(config)
        exchange.account = PaperAccount.from_state(state["account"])
        exchange._orders = {item["client_order_id"]: _PaperOrder.from_state(item) for item in state["orders"]}
        exchange._ticks = {item["symbol"]: MarketTick.from_state(item) for item in state["ticks"]}
        exchange._funding_events = list(state.get("funding_events", []))
        exchange._fills = [
            Fill(
                client_order_id=item["client_order_id"],
                symbol=item["symbol"],
                position_side=Side(item["position_side"]),
                role=OrderRole(item["role"]),
                price=Decimal(item["price"]),
                quantity=Decimal(item["quantity"]),
                fee_quote=Decimal(item["fee_quote"]),
                timestamp_ms=item["timestamp_ms"],
                taker=item["taker"],
            )
            for item in state["fills"]
        ]
        return exchange
