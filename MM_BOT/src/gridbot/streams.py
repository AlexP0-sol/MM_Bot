"""Public/private stream event normalization, health and reconnect policy.

The concrete websocket transport is intentionally not implemented here: stage 3 can
only validate event parsing, staleness and reconnect behavior offline. A runtime
socket adapter must feed these parsers and health tracker; it is part of the manual
Testnet opt-in, never of the automated suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Callable, Protocol

from .binance import MetadataError, map_order_status
from .domain import Side
from .exchange import Aggressor, MarketTick, OrderSide, OrderStatus
from .websocket import WebSocketError


class StreamDataError(RuntimeError):
    """A stream payload is malformed or internally inconsistent."""


class UnsupportedStreamEvent(StreamDataError):
    """A well-formed event type that this stage does not model."""


def unwrap(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise StreamDataError("stream payload must be an object")
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def _string(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise StreamDataError(f"stream field {field} is missing")
    return value


def _decimal(payload: dict, field: str) -> Decimal:
    try:
        value = Decimal(str(payload.get(field)))
    except (InvalidOperation, ValueError) as exc:
        raise StreamDataError(f"stream field {field} is not a decimal") from exc
    if not value.is_finite():
        raise StreamDataError(f"stream field {field} is not finite")
    return value


def _integer(payload: dict, field: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise StreamDataError(f"stream field {field} must be an integer")
    return value


@dataclass(frozen=True)
class TradeEvent:
    symbol: str
    price: Decimal
    quantity: Decimal
    aggressor: Aggressor
    timestamp_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise StreamDataError("trade symbol is required")
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in (self.price, self.quantity)):
            raise StreamDataError("trade price/quantity must be finite Decimals")
        if self.price <= Decimal("0") or self.quantity < Decimal("0"):
            raise StreamDataError("trade price/quantity is invalid")
        if not isinstance(self.timestamp_ms, int) or isinstance(self.timestamp_ms, bool) or self.timestamp_ms < 0:
            raise StreamDataError("trade timestamp must be a non-negative integer")
        if not isinstance(self.aggressor, Aggressor):
            raise StreamDataError("trade aggressor is invalid")


@dataclass(frozen=True)
class BookEvent:
    symbol: str
    bid: Decimal
    ask: Decimal
    bid_quantity: Decimal
    ask_quantity: Decimal
    timestamp_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise StreamDataError("book symbol is required")
        values = (self.bid, self.ask, self.bid_quantity, self.ask_quantity)
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
            raise StreamDataError("book values must be finite Decimals")
        if self.bid <= Decimal("0") or self.ask <= Decimal("0") or self.bid > self.ask:
            raise StreamDataError("book event is invalid")
        if self.bid_quantity < Decimal("0") or self.ask_quantity < Decimal("0"):
            raise StreamDataError("book quantities cannot be negative")
        if not isinstance(self.timestamp_ms, int) or isinstance(self.timestamp_ms, bool) or self.timestamp_ms < 0:
            raise StreamDataError("book timestamp must be a non-negative integer")


@dataclass(frozen=True)
class OrderUpdate:
    client_order_id: str
    order_id: int
    symbol: str
    side: OrderSide
    position_side: Side | None
    status: OrderStatus
    price: Decimal
    quantity: Decimal
    executed_quantity: Decimal
    average_price: Decimal
    reject_reason: str
    timestamp_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.client_order_id, str) or not self.client_order_id:
            raise StreamDataError("order update client id is required")
        if not isinstance(self.symbol, str) or not self.symbol:
            raise StreamDataError("order update symbol is required")
        if not isinstance(self.order_id, int) or isinstance(self.order_id, bool) or self.order_id < 0:
            raise StreamDataError("order id must be a non-negative integer")
        if not isinstance(self.side, OrderSide) or not isinstance(self.status, OrderStatus):
            raise StreamDataError("order side/status is invalid")
        for value, name in ((self.price, "price"), (self.quantity, "quantity"),
                            (self.executed_quantity, "executed quantity"), (self.average_price, "average price")):
            if not isinstance(value, Decimal) or not value.is_finite() or value < Decimal("0"):
                raise StreamDataError(f"order {name} must be a non-negative finite Decimal")
        if self.price <= Decimal("0") or self.quantity <= Decimal("0"):
            raise StreamDataError("order price and quantity must be positive")
        if self.executed_quantity > self.quantity:
            raise StreamDataError("order executed quantity exceeds quantity")
        if not isinstance(self.timestamp_ms, int) or isinstance(self.timestamp_ms, bool) or self.timestamp_ms < 0:
            raise StreamDataError("order update timestamp must be a non-negative integer")

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.executed_quantity


@dataclass(frozen=True)
class AccountPositionUpdate:
    symbol: str
    side: Side
    quantity: Decimal
    entry_price: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise StreamDataError("position symbol is required")
        if not isinstance(self.side, Side):
            raise StreamDataError("position side is invalid")
        if not isinstance(self.quantity, Decimal) or not self.quantity.is_finite() or self.quantity < Decimal("0"):
            raise StreamDataError("position quantity must be a non-negative finite Decimal")
        if not isinstance(self.entry_price, Decimal) or not self.entry_price.is_finite() or self.entry_price < Decimal("0"):
            raise StreamDataError("position entry price must be a non-negative finite Decimal")


@dataclass(frozen=True)
class AccountUpdate:
    reason: str
    positions: tuple[AccountPositionUpdate, ...]
    timestamp_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str):
            raise StreamDataError("account update reason must be a string")
        if any(not isinstance(position, AccountPositionUpdate) for position in self.positions):
            raise StreamDataError("account update positions are malformed")
        if not isinstance(self.timestamp_ms, int) or isinstance(self.timestamp_ms, bool) or self.timestamp_ms < 0:
            raise StreamDataError("account update timestamp must be a non-negative integer")


@dataclass(frozen=True)
class ListenKeyExpired:
    timestamp_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.timestamp_ms, int) or isinstance(self.timestamp_ms, bool) or self.timestamp_ms < 0:
            raise StreamDataError("listen-key expiry timestamp must be a non-negative integer")


@dataclass
class ListenKeyState:
    """Tracks Binance listen-key expiry and keepalive timing without sleeping.

    Binance USDⓈ-M listen keys expire after 60 minutes; sending a keepalive at least
    every 30 minutes is the documented safe operating rule. Both values remain explicit
    configuration so a changed exchange rule can be applied without code surgery.
    """

    key: str
    created_ms: int
    last_keepalive_ms: int
    validity_ms: int = 3_600_000
    keepalive_interval_ms: int = 1_800_000

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise StreamDataError("listen key is required")
        for value, name in ((self.created_ms, "created_ms"), (self.last_keepalive_ms, "last_keepalive_ms"),
                            (self.validity_ms, "validity_ms"), (self.keepalive_interval_ms, "keepalive_interval_ms")):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise StreamDataError(f"{name} must be a non-negative integer")
        if self.validity_ms <= 0 or self.keepalive_interval_ms <= 0:
            raise StreamDataError("listen-key validity and keepalive interval must be positive")
        if self.last_keepalive_ms < self.created_ms:
            raise StreamDataError("listen-key keepalive cannot precede creation")
        if self.last_keepalive_ms - self.created_ms >= self.validity_ms:
            raise StreamDataError("listen key is already expired")

    @property
    def expires_at_ms(self) -> int:
        return self.created_ms + self.validity_ms

    def _check_now(self, now_ms: int) -> int:
        if not isinstance(now_ms, int) or isinstance(now_ms, bool):
            raise StreamDataError("now_ms must be an integer")
        if now_ms < self.last_keepalive_ms:
            raise StreamDataError("listen-key clock moved backwards")
        return now_ms

    def expired(self, now_ms: int) -> bool:
        return self._check_now(now_ms) >= self.expires_at_ms

    def needs_keepalive(self, now_ms: int) -> bool:
        now = self._check_now(now_ms)
        return now < self.expires_at_ms and now - self.last_keepalive_ms >= self.keepalive_interval_ms

    def on_keepalive(self, now_ms: int) -> None:
        now = self._check_now(now_ms)
        if now >= self.expires_at_ms:
            raise StreamDataError("cannot keepalive an expired listen key")
        self.last_keepalive_ms = now

    def remaining_ms(self, now_ms: int) -> int:
        now = self._check_now(now_ms)
        return max(0, self.expires_at_ms - now)


def parse_public_event(payload: object) -> TradeEvent | BookEvent:
    data = unwrap(payload)
    event_type = data.get("e")
    if event_type == "aggTrade":
        maker_flag = data.get("m")
        if not isinstance(maker_flag, bool):
            raise StreamDataError("aggTrade maker flag is missing")
        return TradeEvent(
            symbol=_string(data, "s"),
            price=_decimal(data, "p"),
            quantity=_decimal(data, "q"),
            aggressor=Aggressor.SELL if maker_flag else Aggressor.BUY,
            timestamp_ms=_integer(data, "T"),
        )
    if event_type == "bookTicker":
        return BookEvent(
            symbol=_string(data, "s"),
            bid=_decimal(data, "b"),
            ask=_decimal(data, "a"),
            bid_quantity=_decimal(data, "B"),
            ask_quantity=_decimal(data, "A"),
            timestamp_ms=_integer(data, "E"),
        )
    raise UnsupportedStreamEvent(f"unsupported public event {event_type!r}")


def combine_book_and_trade(book: BookEvent, trade: TradeEvent) -> MarketTick:
    if book.symbol != trade.symbol:
        raise StreamDataError("book/trade symbol mismatch")
    return MarketTick(
        symbol=book.symbol,
        timestamp_ms=min(book.timestamp_ms, trade.timestamp_ms),
        bid=book.bid,
        ask=book.ask,
        bid_quantity=book.bid_quantity,
        ask_quantity=book.ask_quantity,
        last_price=trade.price,
        last_quantity=trade.quantity,
        aggressor=trade.aggressor,
    )


def parse_private_event(payload: object) -> OrderUpdate | AccountUpdate | ListenKeyExpired:
    data = unwrap(payload)
    event_type = data.get("e")
    if event_type == "listenKeyExpired":
        return ListenKeyExpired(timestamp_ms=_integer(data, "E"))
    if event_type == "ORDER_TRADE_UPDATE":
        order = data.get("o")
        if not isinstance(order, dict):
            raise StreamDataError("ORDER_TRADE_UPDATE has no order object")
        status_value = order.get("X")
        if not isinstance(status_value, str):
            raise StreamDataError("order update status is missing")
        try:
            status = map_order_status(status_value)
        except MetadataError as exc:
            raise StreamDataError(str(exc)) from exc
        side_value = order.get("S")
        if side_value not in ("BUY", "SELL"):
            raise StreamDataError("order update side is invalid")
        position_side_value = order.get("ps")
        if position_side_value in ("LONG", "SHORT"):
            position_side = Side(position_side_value)
        elif position_side_value in (None, "BOTH"):
            position_side = None
        else:
            raise StreamDataError("order update positionSide is invalid")
        return OrderUpdate(
            client_order_id=_string(order, "c"),
            order_id=_integer(order, "i"),
            symbol=_string(order, "s"),
            side=OrderSide(side_value),
            position_side=position_side,
            status=status,
            price=_decimal(order, "p"),
            quantity=_decimal(order, "q"),
            executed_quantity=_decimal(order, "z"),
            average_price=_decimal(order, "ap"),
            reject_reason=str(order.get("R", "")),
            timestamp_ms=_integer(order, "T"),
        )
    if event_type == "ACCOUNT_UPDATE":
        update = data.get("a")
        if not isinstance(update, dict):
            raise StreamDataError("ACCOUNT_UPDATE has no account object")
        positions_payload = update.get("P")
        if not isinstance(positions_payload, list):
            raise StreamDataError("ACCOUNT_UPDATE positions are missing")
        positions: list[AccountPositionUpdate] = []
        for item in positions_payload:
            if not isinstance(item, dict):
                raise StreamDataError("account position update is malformed")
            amount = _decimal(item, "pa")
            position_side_value = item.get("ps")
            if position_side_value in ("LONG", "SHORT"):
                side = Side(position_side_value)
            elif amount > Decimal("0"):
                side = Side.LONG
            elif amount < Decimal("0"):
                side = Side.SHORT
            else:
                continue
            positions.append(
                AccountPositionUpdate(
                    symbol=_string(item, "s"),
                    side=side,
                    quantity=abs(amount),
                    entry_price=_decimal(item, "ep"),
                )
            )
        return AccountUpdate(
            reason=str(update.get("m", "")),
            positions=tuple(positions),
            timestamp_ms=_integer(data, "E"),
        )
    raise UnsupportedStreamEvent(f"unsupported private event {event_type!r}")


class StreamStatus(str, Enum):
    HEALTHY = "HEALTHY"
    STALE = "STALE"
    DISCONNECTED = "DISCONNECTED"
    KEY_EXPIRED = "KEY_EXPIRED"


@dataclass
class StreamHealth:
    """Tracks whether a stream is fresh, connected and has a valid listen key."""

    max_age_ms: int
    last_message_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.max_age_ms, int) or isinstance(self.max_age_ms, bool) or self.max_age_ms <= 0:
            raise StreamDataError("max_age_ms must be a positive integer")
        if not isinstance(self.last_message_ms, int) or isinstance(self.last_message_ms, bool):
            raise StreamDataError("last_message_ms must be an integer")
        self.connected = True
        self.key_expired = False

    def _check_now(self, now_ms: int) -> int:
        if not isinstance(now_ms, int) or isinstance(now_ms, bool):
            raise StreamDataError("now_ms must be an integer")
        if now_ms < self.last_message_ms:
            raise StreamDataError("stream clock moved backwards")
        return now_ms

    def on_message(self, now_ms: int) -> None:
        self.last_message_ms = self._check_now(now_ms)

    def on_disconnect(self, now_ms: int) -> None:
        self._check_now(now_ms)
        self.connected = False

    def on_connect(self, now_ms: int) -> None:
        self.last_message_ms = self._check_now(now_ms)
        self.connected = True

    def on_listen_key_expired(self, now_ms: int) -> None:
        self.last_message_ms = self._check_now(now_ms)
        self.key_expired = True

    def on_listen_key_created(self, now_ms: int) -> None:
        self.last_message_ms = self._check_now(now_ms)
        self.key_expired = False

    def status(self, now_ms: int) -> StreamStatus:
        now = self._check_now(now_ms)
        if self.key_expired:
            return StreamStatus.KEY_EXPIRED
        if not self.connected:
            return StreamStatus.DISCONNECTED
        if now - self.last_message_ms > self.max_age_ms:
            return StreamStatus.STALE
        return StreamStatus.HEALTHY

    def trading_ready(self, now_ms: int) -> bool:
        return self.status(now_ms) is StreamStatus.HEALTHY


@dataclass(frozen=True)
class BackoffPolicy:
    base_ms: int = 500
    multiplier: int = 2
    max_ms: int = 30_000
    max_attempts: int = 6

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in (self.base_ms, self.multiplier, self.max_ms, self.max_attempts)
        ):
            raise StreamDataError("backoff values must be positive integers")
        if self.multiplier < 2:
            raise StreamDataError("backoff multiplier must be at least two")
        if self.base_ms > self.max_ms:
            raise StreamDataError("backoff base cannot exceed the cap")

    def exhausted(self, attempt: int) -> bool:
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
            raise StreamDataError("attempt must be a non-negative integer")
        return attempt >= self.max_attempts

    def delay_ms(self, attempt: int) -> int:
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
            raise StreamDataError("attempt must be a non-negative integer")
        if attempt >= self.max_attempts:
            raise StreamDataError("backoff attempts are exhausted")
        return min(self.base_ms * (self.multiplier ** attempt), self.max_ms)


class MarketTickAssembler:
    """Combines bookTicker and aggTrade events into one conservative tick per symbol."""

    def __init__(self, symbol: str) -> None:
        if not isinstance(symbol, str) or not symbol:
            raise StreamDataError("assembler symbol is required")
        self.symbol = symbol
        self._book: BookEvent | None = None
        self._trade: TradeEvent | None = None
        self._last_emitted_ms = -1

    def on_event(self, event: object) -> MarketTick | None:
        if isinstance(event, BookEvent):
            if event.symbol != self.symbol:
                return None
            if self._book is not None and event.timestamp_ms < self._book.timestamp_ms:
                raise StreamDataError("bookTicker event went backwards")
            self._book = event
        elif isinstance(event, TradeEvent):
            if event.symbol != self.symbol:
                return None
            if self._trade is not None and event.timestamp_ms < self._trade.timestamp_ms:
                raise StreamDataError("aggTrade event went backwards")
            self._trade = event
        else:
            raise StreamDataError("unsupported market event")
        if self._book is None or self._trade is None:
            return None
        tick = combine_book_and_trade(self._book, self._trade)
        if tick.timestamp_ms <= self._last_emitted_ms:
            return None
        self._last_emitted_ms = tick.timestamp_ms
        return tick


class StreamTransport(Protocol):
    def connect(self) -> None: ...

    def recv_text(self) -> str: ...

    def close(self) -> None: ...


class StreamSession:
    """Reconnect/health loop around a transport; no exchange calls and no sleeping."""

    def __init__(
        self,
        *,
        transport: StreamTransport,
        health: StreamHealth,
        backoff: BackoffPolicy,
        clock: Callable[[], int],
        sleep: Callable[[float], None],
    ) -> None:
        if not isinstance(health, StreamHealth) or not isinstance(backoff, BackoffPolicy):
            raise StreamDataError("stream health and backoff are required")
        if not callable(clock) or not callable(sleep):
            raise StreamDataError("stream clock and sleep must be callable")
        self.transport = transport
        self.health = health
        self.backoff = backoff
        self.clock = clock
        self.sleep = sleep

    def run(self, handle: Callable[[str], None], *, max_messages: int | None = None) -> int:
        if not callable(handle):
            raise StreamDataError("message handler is required")
        if max_messages is not None and (not isinstance(max_messages, int) or max_messages <= 0):
            raise StreamDataError("max_messages must be a positive integer or None")
        delivered = 0
        attempt = 0
        try:
            while max_messages is None or delivered < max_messages:
                try:
                    self.transport.connect()
                    self.health.on_connect(self.clock())
                    while max_messages is None or delivered < max_messages:
                        text = self.transport.recv_text()
                        self.health.on_message(self.clock())
                        handle(text)
                        attempt = 0
                        delivered += 1
                except (WebSocketError, OSError, StreamDataError):
                    self.health.on_disconnect(self.clock())
                    if self.backoff.exhausted(attempt + 1):
                        raise StreamDataError("stream reconnect attempts are exhausted")
                    self.sleep(self.backoff.delay_ms(attempt) / 1000.0)
                    attempt += 1
        finally:
            self.transport.close()
        return delivered


def public_stream_url(symbol: str, *, testnet: bool = False) -> str:
    if not isinstance(symbol, str) or not symbol:
        raise StreamDataError("symbol is required for a public stream")
    base = "wss://stream.binancefuture.com" if testnet else "wss://fstream.binance.com"
    lower = symbol.lower()
    return f"{base}/stream?streams={lower}@bookTicker/{lower}@aggTrade"


def private_stream_url(listen_key: str, *, testnet: bool = False) -> str:
    if not isinstance(listen_key, str) or not listen_key:
        raise StreamDataError("listen key is required for a private stream")
    base = "wss://stream.binancefuture.com" if testnet else "wss://fstream.binance.com"
    return f"{base}/ws/{listen_key}"
