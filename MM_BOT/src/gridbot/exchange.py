"""Exchange-facing contracts for the paper engine.

This module intentionally contains no network client, no credentials and no live
order implementation. Stage 2 can only execute against a paper venue.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import NoReturn, Protocol

from .domain import DomainError, Side


ZERO = Decimal("0")


def _finite(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Aggressor(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderRole(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"


class OrderStatus(str, Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


OPEN_STATUSES = (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)
TERMINAL_STATUSES = (
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
)


class ExchangeError(RuntimeError):
    """Base error for paper/live venue contracts."""


class UnsupportedModeError(ExchangeError):
    """The requested environment has no implemented order path."""


class MarketDataError(ExchangeError):
    """Market data is missing, malformed, out of order or not usable."""


class StaleMarketDataError(MarketDataError):
    """The snapshot is older than the configured freshness bound."""


class LiveTradingUnavailable(ExchangeError):
    """Stage 2 has no live order endpoint by design."""


def live_order_port() -> NoReturn:
    """Never returns: there is no live order path in the TEST/paper stage."""
    raise LiveTradingUnavailable("live order endpoint is not implemented; stage 2 is paper-only")


@dataclass(frozen=True)
class MarketTick:
    """One normalized public market snapshot.

    ``aggressor`` is the taker side of the last trade, as reported by the venue.
    ``bid_quantity``/``ask_quantity`` are top-of-book sizes in the base asset and are
    used as a conservative queue-ahead input for maker fills.
    """

    symbol: str
    timestamp_ms: int
    bid: Decimal
    ask: Decimal
    bid_quantity: Decimal
    ask_quantity: Decimal
    last_price: Decimal
    last_quantity: Decimal
    aggressor: Aggressor | None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise MarketDataError("tick symbol is required")
        if not isinstance(self.timestamp_ms, int) or isinstance(self.timestamp_ms, bool) or self.timestamp_ms < 0:
            raise MarketDataError("tick timestamp must be a non-negative integer")
        values = (self.bid, self.ask, self.bid_quantity, self.ask_quantity, self.last_price, self.last_quantity)
        if any(not _finite(value) for value in values):
            raise MarketDataError("tick values must be finite Decimal values")
        if self.bid <= ZERO or self.ask <= ZERO or self.bid > self.ask:
            raise MarketDataError("tick book is invalid")
        if self.last_price <= ZERO or self.last_quantity < ZERO:
            raise MarketDataError("tick trade is invalid")
        if self.aggressor is not None and not isinstance(self.aggressor, Aggressor):
            raise MarketDataError("tick aggressor must be BUY, SELL or None")

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    def to_state(self) -> dict:
        return {
            "symbol": self.symbol,
            "timestamp_ms": self.timestamp_ms,
            "bid": str(self.bid),
            "ask": str(self.ask),
            "bid_quantity": str(self.bid_quantity),
            "ask_quantity": str(self.ask_quantity),
            "last_price": str(self.last_price),
            "last_quantity": str(self.last_quantity),
            "aggressor": None if self.aggressor is None else self.aggressor.value,
        }

    @classmethod
    def from_state(cls, state: dict) -> "MarketTick":
        aggressor = state.get("aggressor")
        return cls(
            symbol=state["symbol"],
            timestamp_ms=state["timestamp_ms"],
            bid=Decimal(state["bid"]),
            ask=Decimal(state["ask"]),
            bid_quantity=Decimal(state["bid_quantity"]),
            ask_quantity=Decimal(state["ask_quantity"]),
            last_price=Decimal(state["last_price"]),
            last_quantity=Decimal(state["last_quantity"]),
            aggressor=None if aggressor is None else Aggressor(aggressor),
        )


@dataclass(frozen=True)
class OrderRequest:
    """One deterministic order intent; the client id is the idempotency key."""

    client_order_id: str
    symbol: str
    position_side: Side
    role: OrderRole
    price: Decimal
    quantity: Decimal
    queue_ahead: Decimal
    created_ts: int
    post_only: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.client_order_id, str) or not self.client_order_id:
            raise DomainError("client_order_id is required")
        if not isinstance(self.symbol, str) or not self.symbol:
            raise DomainError("symbol is required")
        if not isinstance(self.position_side, Side) or not isinstance(self.role, OrderRole):
            raise DomainError("position side and role are required")
        if not _finite(self.price) or self.price <= ZERO:
            raise DomainError("order price must be a positive finite Decimal")
        if not _finite(self.quantity) or self.quantity <= ZERO:
            raise DomainError("order quantity must be a positive finite Decimal")
        if not _finite(self.queue_ahead) or self.queue_ahead < ZERO:
            raise DomainError("queue_ahead must be a non-negative finite Decimal")
        if not isinstance(self.created_ts, int) or isinstance(self.created_ts, bool) or self.created_ts < 0:
            raise DomainError("created_ts must be a non-negative integer")
        if not isinstance(self.post_only, bool):
            raise DomainError("post_only must be explicit")

    @property
    def order_side(self) -> OrderSide:
        if self.position_side is Side.LONG:
            return OrderSide.BUY if self.role is OrderRole.ENTRY else OrderSide.SELL
        return OrderSide.SELL if self.role is OrderRole.ENTRY else OrderSide.BUY


@dataclass(frozen=True)
class OrderSnapshot:
    client_order_id: str
    symbol: str
    position_side: Side
    role: OrderRole
    order_side: OrderSide
    price: Decimal
    quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    average_fill_price: Decimal
    fee_quote: Decimal
    status: OrderStatus
    created_ts: int
    activation_ts: int
    last_update_ts: int

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


@dataclass(frozen=True)
class Fill:
    client_order_id: str
    symbol: str
    position_side: Side
    role: OrderRole
    price: Decimal
    quantity: Decimal
    fee_quote: Decimal
    timestamp_ms: int
    taker: bool

    def to_state(self) -> dict:
        return {
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "position_side": self.position_side.value,
            "role": self.role.value,
            "price": str(self.price),
            "quantity": str(self.quantity),
            "fee_quote": str(self.fee_quote),
            "timestamp_ms": self.timestamp_ms,
            "taker": self.taker,
        }


@dataclass(frozen=True)
class PlacementResult:
    accepted: bool
    status: OrderStatus
    reason: str = ""
    duplicate: bool = False
    order: OrderSnapshot | None = None


@dataclass(frozen=True)
class CancelResult:
    applied: bool
    status: OrderStatus
    reason: str = ""


@dataclass(frozen=True)
class FillResult:
    accepted: bool
    reason: str = ""
    fill: Fill | None = None


class OrderPort(Protocol):
    """Minimal venue contract used by the engine; implemented by the paper venue."""

    def place(self, request: OrderRequest) -> PlacementResult: ...

    def cancel(self, client_order_id: str, now_ts: int) -> CancelResult: ...

    def order(self, client_order_id: str) -> OrderSnapshot | None: ...

    def open_orders(self) -> tuple[OrderSnapshot, ...]: ...

    def close_market(self, symbol: str, position_side: Side, quantity: Decimal, now_ts: int) -> FillResult: ...
