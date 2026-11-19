"""Read-only public Binance USDⓈ-M market data adapter for TEST/paper runs.

Only unauthenticated public endpoints are used. The HTTP transport is injected so
tests never touch the network and no credentials are possible in this module.
Stage 3 will add websocket streams; stage 2 uses conservative polling snapshots.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol, Sequence

from .domain import Candle
from .exchange import Aggressor, MarketDataError, MarketTick


INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}


def _decimal(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MarketDataError(f"field {field} is not a decimal") from exc
    if not parsed.is_finite():
        raise MarketDataError(f"field {field} is not finite")
    return parsed


class HttpTransport(Protocol):
    def get(self, url: str, timeout_s: float) -> bytes: ...


class UrllibTransport:
    """Minimal stdlib transport; used only when no test transport is injected."""

    def get(self, url: str, timeout_s: float) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": "mm-gridbot/0.2 (paper stage)"})
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.read()


class ReplayFeed:
    """Deterministic scripted feed for tests and scenario replays."""

    def __init__(self, ticks: Sequence[MarketTick]) -> None:
        if not ticks:
            raise MarketDataError("replay feed requires at least one tick")
        self._ticks = tuple(ticks)
        self._index = 0

    def remaining(self) -> int:
        return len(self._ticks) - self._index

    def next_tick(self) -> MarketTick:
        if self._index >= len(self._ticks):
            raise MarketDataError("replay feed is exhausted")
        tick = self._ticks[self._index]
        self._index += 1
        return tick

    def reset(self) -> None:
        self._index = 0


class BinancePublicMarketData:
    """Unauthenticated REST snapshots with strict normalization."""

    def __init__(
        self,
        transport: HttpTransport | None = None,
        *,
        base_url: str = "https://fapi.binance.com",
        timeout_s: float = 10.0,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.startswith("https://"):
            raise MarketDataError("public market data requires an https base URL")
        if not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
            raise MarketDataError("timeout must be positive")
        self._transport = transport if transport is not None else UrllibTransport()
        self._base_url = base_url.rstrip("/")
        self._timeout_s = float(timeout_s)

    def _get_json(self, path: str, params: dict[str, str]) -> object:
        query = urllib.parse.urlencode(params)
        url = f"{self._base_url}{path}?{query}"
        try:
            payload = self._transport.get(url, self._timeout_s)
        except OSError as exc:
            raise MarketDataError(f"public market request failed: {exc}") from exc
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MarketDataError("public market response is not valid JSON") from exc

    def book_ticker(self, symbol: str) -> dict:
        payload = self._get_json("/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        if not isinstance(payload, dict):
            raise MarketDataError("book ticker payload must be an object")
        return payload

    def latest_agg_trade(self, symbol: str) -> dict:
        payload = self._get_json("/fapi/v1/aggTrades", {"symbol": symbol, "limit": "1"})
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            raise MarketDataError("agg trade payload must contain one trade")
        return payload[0]

    def latest_tick(self, symbol: str) -> MarketTick:
        """Combine the freshest book and last public trade into one conservative tick.

        The tick timestamp is the older of the two component timestamps: a fresh trade
        cannot make a stale book look fresh. The aggressor is derived from Binance's
        ``m`` flag (buyer-is-maker means the seller was the taker).
        """
        book = self.book_ticker(symbol)
        trade = self.latest_agg_trade(symbol)
        return build_market_tick(symbol, book, trade)

    def klines(self, symbol: str, interval: str, limit: int = 200) -> tuple[Candle, ...]:
        """Closed klines only; the in-progress final row is dropped."""
        if interval not in INTERVAL_MS:
            raise MarketDataError("unsupported kline interval")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 2 <= limit <= 1500:
            raise MarketDataError("kline limit must be between 2 and 1500")
        payload = self._get_json(
            "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": str(limit)}
        )
        return build_candles(payload, interval)


def build_market_tick(symbol: str, book: dict, trade: dict) -> MarketTick:
    """Combine a bookTicker payload and an aggTrade payload into one conservative tick.

    Shared with the stage-3 adapter so public data normalization has a single source.
    """
    if not isinstance(book, dict) or not isinstance(trade, dict):
        raise MarketDataError("book and trade payloads must be objects")
    if book.get("symbol") != symbol or trade.get("symbol") != symbol:
        raise MarketDataError("symbol mismatch in public market payload")
    book_time = book.get("time")
    if not isinstance(book_time, int) or isinstance(book_time, bool):
        raise MarketDataError("book ticker has no integer timestamp")
    bid = _decimal(book.get("bidPrice"), "bidPrice")
    ask = _decimal(book.get("askPrice"), "askPrice")
    bid_quantity = _decimal(book.get("bidQty"), "bidQty")
    ask_quantity = _decimal(book.get("askQty"), "askQty")
    trade_time = trade.get("T")
    if not isinstance(trade_time, int) or isinstance(trade_time, bool):
        raise MarketDataError("agg trade has no integer timestamp")
    maker_flag = trade.get("m")
    if not isinstance(maker_flag, bool):
        raise MarketDataError("agg trade has no maker flag")
    return MarketTick(
        symbol=symbol,
        timestamp_ms=min(book_time, trade_time),
        bid=bid,
        ask=ask,
        bid_quantity=bid_quantity,
        ask_quantity=ask_quantity,
        last_price=_decimal(trade.get("p"), "trade price"),
        last_quantity=_decimal(trade.get("q"), "trade quantity"),
        aggressor=Aggressor.SELL if maker_flag else Aggressor.BUY,
    )


def build_candles(rows: object, interval: str) -> tuple[Candle, ...]:
    """Map raw kline rows to completed Candles; the in-progress last row is dropped."""
    if interval not in INTERVAL_MS:
        raise MarketDataError("unsupported kline interval")
    if not isinstance(rows, list) or len(rows) < 2:
        raise MarketDataError("kline payload is too short")
    candles: list[Candle] = []
    for row in rows[:-1]:
        if not isinstance(row, list) or len(row) < 8:
            raise MarketDataError("malformed kline row")
        timestamp = row[0]
        if not isinstance(timestamp, int) or isinstance(timestamp, bool):
            raise MarketDataError("kline timestamp must be an integer")
        if timestamp % INTERVAL_MS[interval] != 0:
            raise MarketDataError("kline timestamp is not aligned to the interval")
        candles.append(
            Candle(
                high=_decimal(row[2], "high"),
                low=_decimal(row[3], "low"),
                close=_decimal(row[4], "close"),
                timestamp=timestamp,
                complete=True,
            )
        )
    return tuple(candles)
