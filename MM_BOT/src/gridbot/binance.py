"""Binance USDⓈ-M adapter: signed REST client, metadata, account state and reconciliation.

Stage 3 is Testnet-opt-in only. The adapter refuses LIVE trading outright, refuses
trading without an explicit ``enable_trading`` flag and refuses every trade before a
successful hedge-mode preflight. There is no withdraw method and no withdrawal scope.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable, Mapping, Sequence

from .domain import Candle, DomainError, ExchangeFilters, Side
from .exchange import MarketTick, OrderRole, OrderSide, OrderStatus
from .market_data import INTERVAL_MS, build_candles, build_market_tick


TESTNET_BASE_URL = "https://testnet.binancefuture.com"
LIVE_BASE_URL = "https://fapi.binance.com"


ZERO = Decimal("0")
ONE = Decimal("1")
_CLIENT_ID_PATTERN = re.compile(r"^[\.A-Za-z0-9_:\-/]{1,36}$")


class BinanceError(RuntimeError):
    """Base error for the Binance adapter."""


class BinanceNetworkError(BinanceError):
    """Transport failed for a request whose outcome is known to be no-op."""


class BinanceApiError(BinanceError):
    def __init__(self, code: int | None, message: str, status: int) -> None:
        super().__init__(f"binance api error {code}: {message} (http {status})")
        self.code = code
        self.message = message
        self.status = status


class RateLimited(BinanceError):
    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class OutcomeUnknown(BinanceError):
    """The request may or may not have reached the matching engine."""


class TradingDisabledError(BinanceError):
    """Trading is not enabled for this environment/config/preflight."""


class ClockDriftError(BinanceError):
    """Local clock differs from the venue clock beyond the configured bound."""


class MetadataError(BinanceError):
    """Exchange metadata is missing or malformed."""


class BinanceEnvironment(str, Enum):
    TESTNET = "TESTNET"
    LIVE = "LIVE"


class PositionMode(str, Enum):
    HEDGE = "HEDGE"
    ONE_WAY = "ONE_WAY"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport:
    def request(
        self,
        method: str,
        url: str,
        params: Sequence[tuple[str, str]],
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> HttpResponse:
        raise NotImplementedError


class UrllibTransport(HttpTransport):
    """Minimal stdlib HTTP transport used only when no transport is injected."""

    def request(
        self,
        method: str,
        url: str,
        params: Sequence[tuple[str, str]],
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> HttpResponse:
        query = urllib.parse.urlencode(list(params))
        full_url = f"{url}?{query}" if query else url
        request = urllib.request.Request(full_url, method=method, headers=dict(headers))
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                return HttpResponse(response.status, dict(response.headers.items()), response.read())
        except urllib.error.HTTPError as exc:
            return HttpResponse(exc.code, dict(exc.headers.items()), exc.read())


@dataclass(frozen=True)
class BinanceCredentials:
    """API credentials. The secret never appears in repr/str or error messages."""

    api_key: str
    api_secret: str

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str) or not self.api_key:
            raise DomainError("api_key is required")
        if not isinstance(self.api_secret, str) or not self.api_secret:
            raise DomainError("api_secret is required")

    def __repr__(self) -> str:
        return "BinanceCredentials(api_key='***', api_secret='***')"

    __str__ = __repr__


@dataclass(frozen=True)
class BinanceConfig:
    environment: BinanceEnvironment = BinanceEnvironment.TESTNET
    base_url: str | None = None
    recv_window_ms: int = 5_000
    timeout_s: float = 10.0
    max_weight_1m: int = 1_200
    max_clock_drift_ms: int = 1_000
    max_preflight_age_ms: int = 30_000
    max_mark_price_age_ms: int = 60_000
    enable_trading: bool = False
    allow_live: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.environment, BinanceEnvironment):
            raise DomainError("environment must be TESTNET or LIVE")
        if not isinstance(self.recv_window_ms, int) or not 1 <= self.recv_window_ms <= 60_000:
            raise DomainError("recv_window_ms must be between 1 and 60000")
        if not isinstance(self.timeout_s, (int, float)) or self.timeout_s <= 0:
            raise DomainError("timeout must be positive")
        if not isinstance(self.max_weight_1m, int) or self.max_weight_1m <= 0:
            raise DomainError("max_weight_1m must be positive")
        if not isinstance(self.max_clock_drift_ms, int) or self.max_clock_drift_ms < 0:
            raise DomainError("max_clock_drift_ms must be non-negative")
        if not isinstance(self.max_preflight_age_ms, int) or self.max_preflight_age_ms <= 0:
            raise DomainError("max_preflight_age_ms must be positive")
        if not isinstance(self.max_mark_price_age_ms, int) or self.max_mark_price_age_ms <= 0:
            raise DomainError("max_mark_price_age_ms must be positive")
        if self.base_url is not None and (not isinstance(self.base_url, str) or not self.base_url.startswith("https://")):
            raise DomainError("base_url must be https")
        if self.base_url is not None and self.environment is BinanceEnvironment.TESTNET:
            if urllib.parse.urlsplit(self.base_url).hostname == "fapi.binance.com":
                raise TradingDisabledError("a LIVE host cannot be configured as TESTNET")
        if self.environment is BinanceEnvironment.LIVE:
            if not self.allow_live:
                raise TradingDisabledError("LIVE is disabled; a local explicit allow_live decision is required")
            if self.enable_trading:
                raise TradingDisabledError("LIVE trading is not enabled in stage 3")

    @property
    def resolved_base_url(self) -> str:
        if self.base_url is not None:
            return self.base_url.rstrip("/")
        return TESTNET_BASE_URL if self.environment is BinanceEnvironment.TESTNET else LIVE_BASE_URL


def _header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _decimal(value: object, field: str, error: type[Exception] = MetadataError) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise error(f"field {field} is not a decimal") from exc
    if not parsed.is_finite():
        raise error(f"field {field} is not finite")
    return parsed


def _integer(value: object, field: str, error: type[Exception] = MetadataError) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise error(f"field {field} must be an integer")
    return value


def _bool(value: object, field: str, error: type[Exception] = MetadataError) -> bool:
    if not isinstance(value, bool):
        raise error(f"field {field} must be a boolean")
    return value


@dataclass(frozen=True)
class SymbolMetadata:
    symbol: str
    base_asset: str
    quote_asset: str
    contract_type: str
    status: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal | None
    min_notional: Decimal | None
    max_notional: Decimal | None
    multiplier: Decimal
    price_band_up: Decimal | None = None
    price_band_down: Decimal | None = None

    def __post_init__(self) -> None:
        for value, name in ((self.symbol, "symbol"), (self.base_asset, "base_asset"),
                            (self.quote_asset, "quote_asset"), (self.contract_type, "contract_type"),
                            (self.status, "status")):
            if not isinstance(value, str) or not value:
                raise MetadataError(f"{name} is required")
        for value, name in ((self.tick_size, "tick_size"), (self.step_size, "step_size"),
                            (self.min_qty, "min_qty"), (self.multiplier, "multiplier")):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
                raise MetadataError(f"{name} must be a positive finite Decimal")
        if self.max_qty is not None and (not isinstance(self.max_qty, Decimal) or not self.max_qty.is_finite() or self.max_qty < self.min_qty):
            raise MetadataError("max_qty must be finite and at least min_qty")
        if self.min_notional is not None and (not isinstance(self.min_notional, Decimal) or not self.min_notional.is_finite() or self.min_notional < ZERO):
            raise MetadataError("min_notional must be a non-negative finite Decimal")
        if self.max_notional is not None and (not isinstance(self.max_notional, Decimal) or not self.max_notional.is_finite() or self.max_notional <= ZERO):
            raise MetadataError("max_notional must be a positive finite Decimal")
        if self.min_notional is not None and self.max_notional is not None and self.min_notional > self.max_notional:
            raise MetadataError("min_notional exceeds max_notional")
        for value, name in ((self.price_band_up, "price_band_up"), (self.price_band_down, "price_band_down")):
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO):
                raise MetadataError(f"{name} must be a positive finite Decimal")
        if self.price_band_up is not None and self.price_band_up < ONE:
            raise MetadataError("price_band_up must be at least one")
        if self.price_band_down is not None and self.price_band_down > ONE:
            raise MetadataError("price_band_down must be at most one")

    def order_violation(self, *, price: Decimal, quantity: Decimal) -> str | None:
        """Return a concrete local rejection reason, or None if the order matches filters."""
        if not isinstance(price, Decimal) or not price.is_finite() or price <= ZERO:
            return "price must be a positive finite Decimal"
        if not isinstance(quantity, Decimal) or not quantity.is_finite() or quantity <= ZERO:
            return "quantity must be a positive finite Decimal"
        if price % self.tick_size != ZERO:
            return f"price is not a multiple of tickSize {self.tick_size}"
        if quantity % self.step_size != ZERO:
            return f"quantity is not a multiple of stepSize {self.step_size}"
        if quantity < self.min_qty:
            return f"quantity is below minQty {self.min_qty}"
        if self.max_qty is not None and quantity > self.max_qty:
            return f"quantity exceeds maxQty {self.max_qty}"
        notional = quantity * price * self.multiplier
        if self.min_notional is not None and notional < self.min_notional:
            return f"notional {notional} is below minNotional {self.min_notional}"
        if self.max_notional is not None and notional > self.max_notional:
            return f"notional {notional} exceeds maxNotional {self.max_notional}"
        return None

    def price_band_violation(self, *, price: Decimal, mark_price: Decimal) -> str | None:
        if (self.price_band_up is None and self.price_band_down is None) or mark_price <= ZERO:
            return None
        upper = mark_price * self.price_band_up if self.price_band_up is not None else None
        lower = mark_price * self.price_band_down if self.price_band_down is not None else None
        if upper is not None and price > upper:
            return f"price exceeds the PERCENT_PRICE upper band {upper}"
        if lower is not None and price < lower:
            return f"price is below the PERCENT_PRICE lower band {lower}"
        return None

    def filters(self) -> ExchangeFilters:
        return ExchangeFilters(
            tick_size=self.tick_size,
            step_size=self.step_size,
            min_qty=self.min_qty,
            min_notional=self.min_notional,
            max_qty=self.max_qty,
            max_notional=self.max_notional,
            contract_multiplier=self.multiplier,
            quote_asset=self.quote_asset,
            base_asset=self.base_asset,
            contract_type=self.contract_type,
            trading=self.status == "TRADING",
        )

    @classmethod
    def from_exchange_info(cls, payload: dict) -> "SymbolMetadata":
        if not isinstance(payload, dict):
            raise MetadataError("symbol metadata must be an object")
        symbol = payload.get("symbol")
        base_asset = payload.get("baseAsset")
        quote_asset = payload.get("quoteAsset")
        contract_type = payload.get("contractType")
        status = payload.get("status")
        if not all(isinstance(value, str) and value for value in (symbol, base_asset, quote_asset, contract_type, status)):
            raise MetadataError("symbol metadata is missing identity fields")
        filters = payload.get("filters")
        if not isinstance(filters, list):
            raise MetadataError("symbol filters must be a list")
        by_type = {item.get("filterType"): item for item in filters if isinstance(item, dict)}
        price_filter = by_type.get("PRICE_FILTER")
        lot_size = by_type.get("LOT_SIZE")
        if price_filter is None or lot_size is None:
            raise MetadataError("PRICE_FILTER and LOT_SIZE are required")
        min_notional_filter = by_type.get("MIN_NOTIONAL")
        min_notional = None
        if min_notional_filter is not None:
            raw_notional = min_notional_filter.get("notional", min_notional_filter.get("minNotional"))
            min_notional = _decimal(raw_notional, "minNotional")
        band_filter = by_type.get("PERCENT_PRICE")
        price_band_up = None
        price_band_down = None
        if band_filter is not None:
            price_band_up = _decimal(band_filter.get("multiplierUp"), "multiplierUp")
            price_band_down = _decimal(band_filter.get("multiplierDown"), "multiplierDown")
        multiplier = _decimal(payload.get("contractMultiplier", "1"), "contractMultiplier")
        return cls(
            symbol=symbol,
            base_asset=base_asset,
            quote_asset=quote_asset,
            contract_type=contract_type,
            status=status,
            tick_size=_decimal(price_filter.get("tickSize"), "tickSize"),
            step_size=_decimal(lot_size.get("stepSize"), "stepSize"),
            min_qty=_decimal(lot_size.get("minQty"), "minQty"),
            max_qty=_decimal(lot_size.get("maxQty"), "maxQty"),
            min_notional=min_notional,
            max_notional=None,
            multiplier=multiplier,
            price_band_up=price_band_up,
            price_band_down=price_band_down,
        )


ORDER_STATUS_MAP = {
    "NEW": OrderStatus.NEW,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
    "EXPIRED_IN_MATCH": OrderStatus.EXPIRED,
    "PENDING_CANCEL": OrderStatus.NEW,
}


@dataclass(frozen=True)
class ExchangeOrder:
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
    reduce_only: bool
    update_time: int

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.executed_quantity

    @property
    def is_open(self) -> bool:
        return self.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)

    @classmethod
    def from_payload(cls, payload: dict) -> "ExchangeOrder":
        if not isinstance(payload, dict):
            raise MetadataError("order payload must be an object")
        client_order_id = payload.get("clientOrderId")
        symbol = payload.get("symbol")
        order_id = payload.get("orderId")
        side = payload.get("side")
        status = payload.get("status")
        if not isinstance(client_order_id, str) or not client_order_id:
            raise MetadataError("order client id is missing")
        if not isinstance(symbol, str) or not symbol:
            raise MetadataError("order symbol is missing")
        if side not in ("BUY", "SELL"):
            raise MetadataError("order side is invalid")
        if status not in ORDER_STATUS_MAP:
            raise MetadataError(f"unknown order status {status!r}")
        position_side_value = payload.get("positionSide")
        if position_side_value in ("LONG", "SHORT"):
            position_side = Side(position_side_value)
        elif position_side_value in (None, "BOTH"):
            position_side = None
        else:
            raise MetadataError("order positionSide is invalid")
        price = _decimal(payload.get("price", "0"), "price")
        quantity = _decimal(payload.get("origQty", "0"), "origQty")
        executed = _decimal(payload.get("executedQty", "0"), "executedQty")
        average = _decimal(payload.get("avgPrice", "0"), "avgPrice")
        if min(price, quantity, executed, average) < ZERO:
            raise MetadataError("order price/quantity cannot be negative")
        if executed > quantity:
            raise MetadataError("order executed quantity exceeds quantity")
        return cls(
            client_order_id=client_order_id,
            order_id=_integer(order_id, "orderId", MetadataError),
            symbol=symbol,
            side=OrderSide(side),
            position_side=position_side,
            status=ORDER_STATUS_MAP[status],
            price=price,
            quantity=quantity,
            executed_quantity=executed,
            average_price=average,
            reduce_only=bool(payload.get("reduceOnly", False)),
            update_time=_integer(payload.get("updateTime", payload.get("time", 0)), "updateTime", MetadataError),
        )


@dataclass(frozen=True)
class PositionState:
    symbol: str
    side: Side
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True)
class AccountState:
    server_time_ms: int
    wallet_balance: Decimal
    available_balance: Decimal
    unrealized_pnl: Decimal
    can_trade: bool
    multi_assets_margin: bool
    positions: tuple[PositionState, ...]
    open_orders: tuple[ExchangeOrder, ...]


@dataclass(frozen=True)
class FundingInfo:
    symbol: str
    mark_price: Decimal
    last_funding_rate: Decimal
    next_funding_time_ms: int
    timestamp_ms: int


@dataclass(frozen=True)
class LeverageBracket:
    notional_cap: Decimal
    max_leverage: Decimal
    maintenance_margin_ratio: Decimal
    cumulative_maintenance_amount: Decimal

    def __post_init__(self) -> None:
        values = (self.notional_cap, self.max_leverage, self.maintenance_margin_ratio, self.cumulative_maintenance_amount)
        if any(not isinstance(value, Decimal) or not value.is_finite() or value < ZERO for value in values):
            raise MetadataError("leverage bracket values must be non-negative finite Decimals")
        if self.max_leverage <= ZERO or self.maintenance_margin_ratio >= ONE:
            raise MetadataError("leverage bracket values are outside safe bounds")


@dataclass(frozen=True)
class PreflightState:
    symbol: str
    metadata: SymbolMetadata
    account: AccountState
    position_mode: PositionMode
    mark_price: Decimal
    leverage_brackets: tuple[LeverageBracket, ...]
    synced_at_ms: int


class NewOrderOutcome(str, Enum):
    ACK = "ACK"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    NOT_FOUND = "NOT_FOUND"


@dataclass(frozen=True)
class OrderIntent:
    client_order_id: str
    symbol: str
    position_side: Side
    role: OrderRole
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.client_order_id, str) or not self.client_order_id:
            raise DomainError("client_order_id is required")
        if not isinstance(self.symbol, str) or not self.symbol or self.symbol != self.symbol.upper() or not self.symbol.isalnum():
            raise DomainError("symbol must be an uppercase alphanumeric Binance symbol")
        if len(self.client_order_id) > 36 or not _CLIENT_ID_PATTERN.fullmatch(self.client_order_id):
            raise DomainError("client_order_id must match Binance's 1..36 character client-id charset")
        if not isinstance(self.position_side, Side) or not isinstance(self.role, OrderRole):
            raise DomainError("position side and role are required")
        for value in (self.price, self.quantity):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
                raise DomainError("order price and quantity must be positive finite Decimals")

    @property
    def side(self) -> OrderSide:
        if self.position_side is Side.LONG:
            return OrderSide.BUY if self.role is OrderRole.ENTRY else OrderSide.SELL
        return OrderSide.SELL if self.role is OrderRole.ENTRY else OrderSide.BUY


@dataclass(frozen=True)
class PlacementOutcome:
    status: NewOrderOutcome
    order: ExchangeOrder | None = None
    reason: str = ""


@dataclass(frozen=True)
class ReconciliationResult:
    ok: bool
    account: AccountState
    unknown_orders: tuple[ExchangeOrder, ...]
    unknown_positions: tuple[PositionState, ...]
    missing_known_orders: tuple[str, ...]


def map_order_status(value: str) -> OrderStatus:
    if value not in ORDER_STATUS_MAP:
        raise MetadataError(f"unknown order status {value!r}")
    return ORDER_STATUS_MAP[value]


def _format_decimal(value: Decimal) -> str:
    return format(value, "f")


class BinanceRestClient:
    """Signed/public REST access. No retry is performed for order mutations."""

    def __init__(
        self,
        credentials: BinanceCredentials | None = None,
        *,
        config: BinanceConfig | None = None,
        transport: HttpTransport | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.credentials = credentials
        self.config = config or BinanceConfig()
        if self.config.enable_trading and credentials is None:
            raise TradingDisabledError("trading requires credentials")
        self._transport = transport if transport is not None else UrllibTransport()
        self._clock = clock if clock is not None else (lambda: time.time_ns() // 1_000_000)
        self._offset_ms = 0
        self._synced = False
        self._used_weight = 0
        self._weight_reset_at = self._clock() + 60_000

    # -- time / limits ---------------------------------------------------

    def _local_ms(self) -> int:
        value = self._clock()
        if not isinstance(value, int):
            raise BinanceError("clock must return integer milliseconds")
        return value

    def server_time(self) -> int:
        payload = self._request("GET", "/fapi/v1/time")
        if not isinstance(payload, dict):
            raise BinanceApiError(None, "time payload is malformed", 200)
        return _integer(payload.get("serverTime"), "serverTime", BinanceError)

    def sync_time(self) -> int:
        server = self.server_time()
        offset = server - self._local_ms()
        if abs(offset) > self.config.max_clock_drift_ms:
            raise ClockDriftError(f"local clock differs from venue by {offset} ms")
        self._offset_ms = offset
        self._synced = True
        return offset

    def timestamp(self) -> int:
        if not self._synced:
            raise ClockDriftError("server time has not been synchronized")
        return self._local_ms() + self._offset_ms

    @property
    def used_weight(self) -> int:
        return self._used_weight

    def _reset_weight_if_needed(self) -> None:
        now = self._local_ms()
        if now >= self._weight_reset_at:
            self._used_weight = 0
            self._weight_reset_at = now + 60_000

    def _track_weight(self, headers: Mapping[str, str]) -> None:
        raw = _header(headers, "X-MBX-USED-WEIGHT-1M")
        if raw is None:
            return
        try:
            self._used_weight = max(self._used_weight, int(raw))
        except ValueError:
            raise BinanceError("invalid used-weight header") from None

    # -- request layer ---------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        params: Sequence[tuple[str, str]] = (),
        *,
        signed: bool = False,
        ambiguous_order: bool = False,
    ) -> Any:
        if signed and method in ("POST", "DELETE") and path == "/fapi/v1/order":
            self._require_order_mutation_enabled()
        self._reset_weight_if_needed()
        if self._used_weight >= self.config.max_weight_1m:
            raise RateLimited("used-weight budget exhausted; refuse the request")
        pairs = list(params)
        headers: dict[str, str] = {}
        if signed:
            if self.credentials is None:
                raise TradingDisabledError("signed request requires credentials")
            if not self._synced:
                raise ClockDriftError("server time has not been synchronized")
            pairs.append(("timestamp", str(self.timestamp())))
            pairs.append(("recvWindow", str(self.config.recv_window_ms)))
            query = urllib.parse.urlencode(pairs)
            signature = hmac.new(
                self.credentials.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            pairs.append(("signature", signature))
            headers["X-MBX-APIKEY"] = self.credentials.api_key
            if method != "GET":
                headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            response = self._transport.request(
                method, f"{self.config.resolved_base_url}{path}", pairs, headers, self.config.timeout_s
            )
        except OSError as exc:
            if ambiguous_order:
                raise OutcomeUnknown(f"transport failure with unknown order outcome ({type(exc).__name__})") from exc
            raise BinanceNetworkError(f"transport failure ({type(exc).__name__})") from exc
        self._track_weight(response.headers)

        if response.status in (418, 429):
            retry_raw = _header(response.headers, "Retry-After")
            try:
                retry_after = float(retry_raw) if retry_raw not in (None, "") else None
            except ValueError:
                retry_after = None
            raise RateLimited(f"rate limited (http {response.status})", retry_after)
        if response.status >= 500:
            if ambiguous_order:
                raise OutcomeUnknown(f"http {response.status} with unknown order outcome")
            raise BinanceApiError(None, "server error", response.status)

        payload = self._decode(response.body)
        if payload is None:
            raise BinanceApiError(None, "response body is not valid json", response.status)
        if response.status >= 400:
            if not isinstance(payload, dict):
                raise BinanceApiError(None, "request rejected without a json body", response.status)
            code = payload.get("code")
            message = payload.get("msg", "request rejected")
            raise BinanceApiError(
                code if isinstance(code, int) else None, str(message), response.status
            )
        return payload

    @staticmethod
    def _decode(body: bytes) -> Any:
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _get(self, path: str, params: Sequence[tuple[str, str]] = ()) -> Any:
        return self._request("GET", path, params)

    def _signed(
        self,
        method: str,
        path: str,
        params: Sequence[tuple[str, str]] = (),
        *,
        ambiguous_order: bool = False,
    ) -> Any:
        return self._request(method, path, params, signed=True, ambiguous_order=ambiguous_order)

    # -- public market data ---------------------------------------------

    def exchange_info(self) -> tuple[SymbolMetadata, ...]:
        payload = self._get("/fapi/v1/exchangeInfo")
        if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
            raise MetadataError("exchangeInfo payload is malformed")
        return tuple(SymbolMetadata.from_exchange_info(item) for item in payload["symbols"])

    def symbol_metadata(self, symbol: str) -> SymbolMetadata:
        for metadata in self.exchange_info():
            if metadata.symbol == symbol:
                return metadata
        raise MetadataError(f"symbol {symbol} is not listed")

    def book_ticker(self, symbol: str) -> dict:
        payload = self._get("/fapi/v1/ticker/bookTicker", [("symbol", symbol)])
        if not isinstance(payload, dict):
            raise MetadataError("bookTicker payload is malformed")
        return payload

    def agg_trade(self, symbol: str) -> dict:
        payload = self._get("/fapi/v1/aggTrades", [("symbol", symbol), ("limit", "1")])
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            raise MetadataError("aggTrades payload is malformed")
        return payload[0]

    def latest_tick(self, symbol: str) -> MarketTick:
        return build_market_tick(symbol, self.book_ticker(symbol), self.agg_trade(symbol))

    def klines(self, symbol: str, interval: str, limit: int = 200) -> tuple[Candle, ...]:
        if interval not in INTERVAL_MS:
            raise MetadataError("unsupported kline interval")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 2 <= limit <= 1500:
            raise MetadataError("kline limit must be between 2 and 1500")
        payload = self._get(
            "/fapi/v1/klines", [("symbol", symbol), ("interval", interval), ("limit", str(limit))]
        )
        return build_candles(payload, interval)

    def funding_info(self, symbol: str) -> FundingInfo:
        payload = self._get("/fapi/v1/premiumIndex", [("symbol", symbol)])
        if not isinstance(payload, dict) or payload.get("symbol") != symbol:
            raise MetadataError("premiumIndex payload is malformed")
        return FundingInfo(
            symbol=symbol,
            mark_price=_decimal(payload.get("markPrice"), "markPrice"),
            last_funding_rate=_decimal(payload.get("lastFundingRate"), "lastFundingRate"),
            next_funding_time_ms=_integer(payload.get("nextFundingTime"), "nextFundingTime", MetadataError),
            timestamp_ms=_integer(payload.get("time"), "time", MetadataError),
        )

    # -- signed account data --------------------------------------------

    def position_mode(self) -> PositionMode:
        payload = self._signed("GET", "/fapi/v1/positionSide/dual")
        dual = _bool(payload.get("dualSidePosition") if isinstance(payload, dict) else None,
                     "dualSidePosition", MetadataError)
        return PositionMode.HEDGE if dual else PositionMode.ONE_WAY

    def open_orders(self, symbol: str | None = None) -> tuple[ExchangeOrder, ...]:
        params: list[tuple[str, str]] = []
        if symbol is not None:
            params.append(("symbol", symbol))
        payload = self._signed("GET", "/fapi/v1/openOrders", params)
        if not isinstance(payload, list):
            raise BinanceApiError(None, "openOrders payload is malformed", 200)
        return tuple(ExchangeOrder.from_payload(item) for item in payload)

    def query_order(self, symbol: str, client_order_id: str) -> ExchangeOrder | None:
        try:
            payload = self._signed(
                "GET", "/fapi/v1/order",
                [("symbol", symbol), ("origClientOrderId", client_order_id)],
            )
        except BinanceApiError as exc:
            if exc.code == -2013:
                return None
            raise
        return ExchangeOrder.from_payload(payload)

    def account_state(self, symbol: str | None = None) -> AccountState:
        account = self._signed("GET", "/fapi/v3/account")
        position_payload = self._signed("GET", "/fapi/v3/positionRisk")
        if not isinstance(account, dict) or not isinstance(position_payload, list):
            raise BinanceApiError(None, "account payload is malformed", 200)
        positions: list[PositionState] = []
        for item in position_payload:
            if not isinstance(item, dict):
                raise MetadataError("position payload is malformed")
            amount = _decimal(item.get("positionAmt"), "positionAmt")
            if amount == ZERO:
                continue
            side_value = item.get("positionSide")
            if side_value == "LONG":
                side = Side.LONG
            elif side_value == "SHORT":
                side = Side.SHORT
            elif side_value in (None, "BOTH"):
                side = Side.LONG if amount > ZERO else Side.SHORT
            else:
                raise MetadataError("positionSide is invalid")
            if symbol is not None and item.get("symbol") != symbol:
                continue
            positions.append(
                PositionState(
                    symbol=str(item.get("symbol")),
                    side=side,
                    quantity=abs(amount),
                    entry_price=_decimal(item.get("entryPrice"), "entryPrice"),
                    mark_price=_decimal(item.get("markPrice"), "markPrice"),
                    unrealized_pnl=_decimal(item.get("unRealizedProfit"), "unRealizedProfit"),
                )
            )
        return AccountState(
            server_time_ms=_integer(
                account.get("time", account.get("updateTime", self.timestamp())),
                "time",
                MetadataError,
            ),
            wallet_balance=_decimal(account.get("totalWalletBalance"), "totalWalletBalance"),
            available_balance=_decimal(account.get("availableBalance"), "availableBalance"),
            unrealized_pnl=_decimal(account.get("totalUnrealizedProfit"), "totalUnrealizedProfit"),
            can_trade=_bool(account.get("canTrade"), "canTrade", MetadataError),
            multi_assets_margin=_bool(account.get("multiAssetsMargin"), "multiAssetsMargin", MetadataError),
            positions=tuple(positions),
            open_orders=self.open_orders(symbol),
        )

    def leverage_brackets(self, symbol: str) -> tuple[LeverageBracket, ...]:
        payload = self._signed("GET", "/fapi/v1/leverageBracket", [("symbol", symbol)])
        if not isinstance(payload, list):
            raise MetadataError("leverageBracket payload is malformed")
        for item in payload:
            if not isinstance(item, dict) or item.get("symbol") != symbol:
                continue
            brackets = item.get("brackets")
            if not isinstance(brackets, list) or not brackets:
                raise MetadataError("leverageBracket has no brackets")
            parsed: list[LeverageBracket] = []
            for bracket in brackets:
                if not isinstance(bracket, dict):
                    raise MetadataError("leverage bracket is malformed")
                parsed.append(
                    LeverageBracket(
                        notional_cap=_decimal(bracket.get("bracketNotionalCap"), "bracketNotionalCap"),
                        max_leverage=_decimal(bracket.get("initialLeverage"), "initialLeverage"),
                        maintenance_margin_ratio=_decimal(bracket.get("maintMarginRatio"), "maintMarginRatio"),
                        cumulative_maintenance_amount=_decimal(bracket.get("cum"), "cum"),
                    )
                )
            return tuple(parsed)
        raise MetadataError(f"symbol {symbol} has no leverage bracket")

    def preflight(self, symbol: str) -> PreflightState:
        self.sync_time()
        metadata = self.symbol_metadata(symbol)
        if metadata.contract_type != "PERPETUAL" or metadata.status != "TRADING":
            raise TradingDisabledError("symbol is not an enabled perpetual")
        if metadata.symbol != f"{metadata.base_asset}{metadata.quote_asset}":
            raise MetadataError("symbol does not match its base/quote assets")
        mode = self.position_mode()
        account = self.account_state(symbol)
        if not account.can_trade:
            raise TradingDisabledError("account canTrade is false")
        if account.multi_assets_margin:
            raise TradingDisabledError("multi-assets margin mode is not modeled by this adapter")
        funding = self.funding_info(symbol)
        now = self.timestamp()
        if funding.symbol != symbol or funding.mark_price <= ZERO:
            raise MetadataError("mark price is missing for preflight")
        if funding.timestamp_ms > now or now - funding.timestamp_ms > self.config.max_mark_price_age_ms:
            raise MetadataError("mark price snapshot is stale or from the future")
        brackets = self.leverage_brackets(symbol)
        return PreflightState(
            symbol=symbol,
            metadata=metadata,
            account=account,
            position_mode=mode,
            mark_price=funding.mark_price,
            leverage_brackets=brackets,
            synced_at_ms=now,
        )

    # -- listen key (user data stream) ----------------------------------

    def create_listen_key(self) -> str:
        payload = self._signed("POST", "/fapi/v1/listenKey")
        key = payload.get("listenKey") if isinstance(payload, dict) else None
        if not isinstance(key, str) or not key:
            raise BinanceApiError(None, "listenKey payload is malformed", 200)
        return key

    def keepalive_listen_key(self) -> None:
        self._signed("PUT", "/fapi/v1/listenKey")

    def close_listen_key(self) -> None:
        self._signed("DELETE", "/fapi/v1/listenKey")

    # -- order mutations (Testnet only, explicit opt-in) ----------------

    def _require_order_mutation_enabled(self) -> None:
        if not self.config.enable_trading:
            raise TradingDisabledError("order mutation requires enable_trading=True")
        if self.config.environment is BinanceEnvironment.LIVE:
            raise TradingDisabledError("LIVE order mutation is not enabled in stage 3")
        if self.credentials is None:
            raise TradingDisabledError("order mutation requires credentials")
        if urllib.parse.urlsplit(self.config.resolved_base_url).hostname != "testnet.binancefuture.com":
            raise TradingDisabledError("order mutation is restricted to the official Testnet host")

    def new_order(self, params: Sequence[tuple[str, str]]) -> ExchangeOrder:
        self._require_order_mutation_enabled()
        payload = self._signed("POST", "/fapi/v1/order", params, ambiguous_order=True)
        return ExchangeOrder.from_payload(payload)

    def cancel_order(self, symbol: str, client_order_id: str) -> ExchangeOrder:
        self._require_order_mutation_enabled()
        payload = self._signed(
            "DELETE", "/fapi/v1/order",
            [("symbol", symbol), ("origClientOrderId", client_order_id)],
            ambiguous_order=True,
        )
        return ExchangeOrder.from_payload(payload)


class BinanceTradingAdapter:
    """Hedge-mode Testnet trading guard around the REST client."""

    def __init__(self, client: BinanceRestClient) -> None:
        if not isinstance(client, BinanceRestClient):
            raise DomainError("a BinanceRestClient is required")
        if not client.config.enable_trading:
            raise TradingDisabledError("trading is disabled in the client config")
        if client.config.environment is BinanceEnvironment.LIVE:
            raise TradingDisabledError("LIVE trading is not enabled in stage 3")
        if urllib.parse.urlsplit(client.config.resolved_base_url).hostname != "testnet.binancefuture.com":
            raise TradingDisabledError("trading is restricted to the official Testnet host")
        if client.credentials is None:
            raise TradingDisabledError("trading requires credentials")
        self.client = client
        self._preflight: PreflightState | None = None

    @property
    def preflight_state(self) -> PreflightState | None:
        return self._preflight

    def preflight(self, symbol: str) -> PreflightState:
        self._preflight = None
        state = self.client.preflight(symbol)
        if state.position_mode is not PositionMode.HEDGE:
            raise TradingDisabledError("hedge mode is required for paired grids")
        self._preflight = state
        return state

    def place(self, intent: OrderIntent) -> PlacementOutcome:
        if not isinstance(intent, OrderIntent):
            raise DomainError("an OrderIntent is required")
        if self._preflight is None or self._preflight.symbol != intent.symbol:
            raise TradingDisabledError("a successful preflight for this symbol is required")
        now = self.client.timestamp()
        age = now - self._preflight.synced_at_ms
        if age < 0 or age > self.client.config.max_preflight_age_ms:
            raise TradingDisabledError("preflight snapshot is stale or from the future")
        violation = self._preflight.metadata.order_violation(
            price=intent.price, quantity=intent.quantity
        )
        if violation is None:
            violation = self._preflight.metadata.price_band_violation(
                price=intent.price, mark_price=self._preflight.mark_price
            )
        if violation is not None:
            return PlacementOutcome(NewOrderOutcome.REJECTED, None, violation)
        if intent.role is OrderRole.EXIT:
            position = next(
                (
                    item
                    for item in self._preflight.account.positions
                    if item.symbol == intent.symbol and item.side is intent.position_side
                ),
                None,
            )
            if position is None:
                return PlacementOutcome(
                    NewOrderOutcome.REJECTED,
                    None,
                    "no known bot-owned position for this exit; refresh preflight",
                )
            if intent.quantity > position.quantity:
                return PlacementOutcome(
                    NewOrderOutcome.REJECTED,
                    None,
                    "exit quantity exceeds the preflight position; refusing to reverse exposure",
                )
        params = [
            ("symbol", intent.symbol),
            ("side", intent.side.value),
            ("positionSide", intent.position_side.value),
            ("type", "LIMIT"),
            ("timeInForce", "GTX"),
            ("quantity", _format_decimal(intent.quantity)),
            ("price", _format_decimal(intent.price)),
            ("newClientOrderId", intent.client_order_id),
        ]
        try:
            order = self.client.new_order(params)
        except BinanceApiError as exc:
            return PlacementOutcome(NewOrderOutcome.REJECTED, None, str(exc))
        except (OutcomeUnknown, RateLimited) as exc:
            return PlacementOutcome(NewOrderOutcome.UNKNOWN, None, str(exc))
        return PlacementOutcome(NewOrderOutcome.ACK, order)

    def resolve_unknown(self, intent: OrderIntent) -> PlacementOutcome:
        order = self.client.query_order(intent.symbol, intent.client_order_id)
        if order is None:
            return PlacementOutcome(
                NewOrderOutcome.NOT_FOUND, None, "client order id was not found; do not re-create blindly"
            )
        return PlacementOutcome(NewOrderOutcome.ACK, order)

    def cancel(self, symbol: str, client_order_id: str) -> PlacementOutcome:
        try:
            order = self.client.cancel_order(symbol, client_order_id)
        except BinanceApiError as exc:
            return PlacementOutcome(NewOrderOutcome.REJECTED, None, str(exc))
        except (OutcomeUnknown, RateLimited) as exc:
            return PlacementOutcome(NewOrderOutcome.UNKNOWN, None, str(exc))
        return PlacementOutcome(NewOrderOutcome.ACK, order)

    def reconcile(
        self,
        symbol: str,
        *,
        known_client_ids: Sequence[str],
        owned_positions: frozenset[tuple[str, Side]] = frozenset(),
    ) -> ReconciliationResult:
        account = self.client.account_state(symbol)
        known = set(known_client_ids)
        unknown_orders = tuple(
            order for order in account.open_orders if order.client_order_id not in known
        )
        missing: list[str] = []
        for client_order_id in sorted(known):
            if self.client.query_order(symbol, client_order_id) is None:
                missing.append(client_order_id)
        unknown_positions = tuple(
            position
            for position in account.positions
            if (position.symbol, position.side) not in owned_positions
        )
        return ReconciliationResult(
            ok=not unknown_orders and not unknown_positions and not missing,
            account=account,
            unknown_orders=unknown_orders,
            unknown_positions=unknown_positions,
            missing_known_orders=tuple(sorted(missing)),
        )
