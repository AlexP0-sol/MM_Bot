"""Paired-grid paper lifecycle: intents, exits, drain, close and restart.

Stage 2 deliberately implements only the TEST/paper path. ``start`` refuses a LIVE
proposal, and the exchange module exposes no live order endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .domain import (
    DomainError,
    ExchangeFilters,
    GridProposal,
    RiskLimits,
    RiskModel,
    RunMode,
    Side,
    check_exposure,
    cost_aware_exit_price,
)
from .exchange import (
    Fill,
    MarketDataError,
    MarketTick,
    OrderRequest,
    OrderRole,
    UnsupportedModeError,
    OPEN_STATUSES,
)
from .paper import PaperExchange


ZERO = Decimal("0")


def _finite(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


class EngineState(str, Enum):
    IDLE = "IDLE"
    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    STOPPED = "STOPPED"
    PAUSED = "PAUSED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


@dataclass(frozen=True)
class ExitPolicy:
    """Explicit remaining-cost hurdle for the aggregated exit of one position."""

    funding_cost_quote: Decimal
    execution_buffer_quote: Decimal
    required_net_profit_quote: Decimal

    def __post_init__(self) -> None:
        values = (self.funding_cost_quote, self.execution_buffer_quote, self.required_net_profit_quote)
        if any(not _finite(value) for value in values):
            raise DomainError("exit policy values must be finite Decimal amounts")
        if min(self.execution_buffer_quote, self.required_net_profit_quote) < ZERO:
            raise DomainError("exit buffer and required profit cannot be negative")


@dataclass(frozen=True)
class PaperRiskContext:
    """Account-level hard limits used to re-check the generation after every fill."""

    limits: RiskLimits
    risk_model: RiskModel
    available_margin_quote: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.limits, RiskLimits) or not isinstance(self.risk_model, RiskModel):
            raise DomainError("risk limits and model are required")
        if not _finite(self.available_margin_quote) or self.available_margin_quote < ZERO:
            raise DomainError("available margin must be a non-negative finite Decimal")


@dataclass(frozen=True)
class StartResult:
    accepted: bool
    reason: str = ""
    client_order_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EngineSnapshotState:
    """JSON-safe engine state without the immutable configuration."""

    state: EngineState
    resume_state: EngineState | None
    pause_reason: str
    generation_id: str | None
    symbol: str | None
    config_version: str | None
    entry_ids: dict[str, tuple[str, ...]]
    exit_ids: dict[str, str]
    exit_attempts: dict[str, int]
    known_order_ids: tuple[str, ...]


class PaperGridEngine:
    """Deterministic paired-grid lifecycle on top of :class:`PaperExchange`."""

    def __init__(
        self,
        *,
        exchange: PaperExchange,
        filters: ExchangeFilters,
        exit_policy: ExitPolicy,
        risk_context: PaperRiskContext,
        max_tick_age_ms: int,
        queue_ahead_quantity: Decimal,
    ) -> None:
        if not isinstance(exchange, PaperExchange):
            raise DomainError("stage 2 engine requires a PaperExchange")
        if not isinstance(filters, ExchangeFilters):
            raise DomainError("exchange filters are required")
        if not isinstance(exit_policy, ExitPolicy):
            raise DomainError("exit policy is required")
        if not isinstance(risk_context, PaperRiskContext):
            raise DomainError("risk context is required")
        if not isinstance(max_tick_age_ms, int) or isinstance(max_tick_age_ms, bool) or max_tick_age_ms <= 0:
            raise DomainError("max_tick_age_ms must be a positive integer")
        if not _finite(queue_ahead_quantity) or queue_ahead_quantity < ZERO:
            raise DomainError("queue_ahead_quantity must be a non-negative finite Decimal")
        self.exchange = exchange
        self.filters = filters
        self.exit_policy = exit_policy
        self.risk_context = risk_context
        self.max_tick_age_ms = max_tick_age_ms
        self.queue_ahead_quantity = queue_ahead_quantity

        self.state = EngineState.IDLE
        self.resume_state: EngineState | None = None
        self.pause_reason = ""
        self.generation_id: str | None = None
        self.symbol: str | None = None
        self.config_version: str | None = None
        self._entry_ids: dict[Side, list[str]] = {Side.LONG: [], Side.SHORT: []}
        self._exit_ids: dict[Side, str | None] = {Side.LONG: None, Side.SHORT: None}
        self._exit_attempts: dict[Side, int] = {Side.LONG: 0, Side.SHORT: 0}
        self._known_order_ids: list[str] = []
        self._journal: list[dict] = []
        self._reconciled = True

    # -- introspection ---------------------------------------------------

    @property
    def journal(self) -> tuple[dict, ...]:
        return tuple(self._journal)

    def position(self, side: Side) -> tuple[Decimal, Decimal]:
        if self.symbol is None:
            return ZERO, ZERO
        return self.exchange.position(self.symbol, side)

    def open_order_ids(self) -> tuple[str, ...]:
        return tuple(snapshot.client_order_id for snapshot in self.exchange.open_orders())

    def _record(self, now_ts: int, event: str, detail: dict | None = None) -> None:
        self._journal.append({"ts": now_ts, "event": event, "detail": detail or {}})

    def _pause(self, reason: str, now_ts: int) -> None:
        self.state = EngineState.PAUSED
        self.pause_reason = reason
        self._record(now_ts, "PAUSED", {"reason": reason})

    def _flat(self) -> bool:
        return self.position(Side.LONG)[0] == ZERO and self.position(Side.SHORT)[0] == ZERO

    def _cancel(self, client_order_id: str, now_ts: int) -> bool:
        result = self.exchange.cancel(client_order_id, now_ts)
        if result.applied:
            self._record(now_ts, "CANCELED", {"client_order_id": client_order_id})
        return result.applied

    def _cancel_entries(self, side: Side, now_ts: int) -> None:
        for client_order_id in self._entry_ids[side]:
            snapshot = self.exchange.order(client_order_id)
            if snapshot is not None and snapshot.status in OPEN_STATUSES:
                self._cancel(client_order_id, now_ts)
        self._entry_ids[side] = []

    def _cancel_all_entries(self, now_ts: int) -> None:
        for side in (Side.LONG, Side.SHORT):
            self._cancel_entries(side, now_ts)

    # -- start -----------------------------------------------------------

    def start(
        self,
        *,
        proposal: GridProposal,
        generation_id: str,
        tick: MarketTick,
        now_ts: int,
    ) -> StartResult:
        if not isinstance(proposal, GridProposal):
            raise DomainError("a GridProposal is required")
        if proposal.mode is not RunMode.TEST:
            raise UnsupportedModeError("stage 2 supports TEST proposals only")
        if not isinstance(generation_id, str) or not generation_id:
            raise DomainError("generation_id is required")
        if not isinstance(proposal.snapshot_timestamp, int) or isinstance(proposal.snapshot_timestamp, bool) or proposal.snapshot_timestamp < 0:
            return StartResult(False, "proposal snapshot timestamp is invalid")
        if not isinstance(tick, MarketTick):
            raise DomainError("a typed market tick is required")
        if not isinstance(now_ts, int) or isinstance(now_ts, bool) or now_ts < 0:
            raise DomainError("now_ts must be a non-negative integer")
        if not self._reconciled:
            return StartResult(False, "reconciliation is required before a new generation")
        if self.state not in (EngineState.IDLE, EngineState.STOPPED):
            return StartResult(False, f"engine is {self.state.value}")
        if not proposal.long_orders or not proposal.short_orders:
            return StartResult(False, "a paired proposal must contain both sides")
        if tick.symbol != proposal.symbol or self.exchange.open_orders(proposal.symbol):
            return StartResult(False, "venue is not flat for a new generation")
        if self.exchange.position(proposal.symbol, Side.LONG)[0] != ZERO or self.exchange.position(
            proposal.symbol, Side.SHORT
        )[0] != ZERO:
            return StartResult(False, "venue still has a modeled position")
        if tick.timestamp_ms > now_ts or now_ts - tick.timestamp_ms > self.max_tick_age_ms:
            return StartResult(False, "market snapshot is stale or from the future")
        if proposal.snapshot_timestamp > now_ts or now_ts - proposal.snapshot_timestamp > self.max_tick_age_ms:
            return StartResult(False, "proposal snapshot is stale or from the future")
        if tick.timestamp_ms < proposal.snapshot_timestamp:
            return StartResult(False, "market snapshot predates the proposal")
        if proposal.symbol != f"{self.filters.base_asset}{self.filters.quote_asset}":
            return StartResult(False, "proposal symbol does not match configured filters")
        for side, orders in ((Side.LONG, proposal.long_orders), (Side.SHORT, proposal.short_orders)):
            for order in orders:
                if order.side is not side:
                    return StartResult(False, "proposal side/order mismatch")
        if any(snapshot.status in OPEN_STATUSES for snapshot in self.exchange.orders()) or self.exchange.account.positions:
            return StartResult(False, "venue is not flat for a new generation")
        computed_long = sum(
            (order.price * order.quantity * self.exchange.config.multiplier for order in proposal.long_orders), ZERO
        )
        computed_short = sum(
            (order.price * order.quantity * self.exchange.config.multiplier for order in proposal.short_orders), ZERO
        )
        if computed_long != proposal.long_notional_quote or computed_short != proposal.short_notional_quote:
            return StartResult(False, "proposal notional totals do not match its orders")
        risk = check_exposure(
            long_notional=proposal.long_notional_quote,
            short_notional=proposal.short_notional_quote,
            limits=self.risk_context.limits,
            risk_model=self.risk_context.risk_model,
            available_margin_quote=self.risk_context.available_margin_quote,
        )
        if not risk.accepted:
            return StartResult(False, f"proposal fails the current risk context: {risk.rejection.message}")

        try:
            self.exchange.on_tick(tick)
        except MarketDataError as exc:
            return StartResult(False, f"market data rejected: {exc}")

        self._entry_ids = {Side.LONG: [], Side.SHORT: []}
        self._exit_ids = {Side.LONG: None, Side.SHORT: None}
        self._exit_attempts = {Side.LONG: 0, Side.SHORT: 0}
        accepted: list[str] = []
        attempted: list[str] = []
        requests: list[OrderRequest] = []
        for side, orders in ((Side.LONG, proposal.long_orders), (Side.SHORT, proposal.short_orders)):
            for order in orders:
                requests.append(
                    OrderRequest(
                        client_order_id=f"{generation_id}:{side.value}:{order.level}:ENTRY",
                        symbol=proposal.symbol,
                        position_side=side,
                        role=OrderRole.ENTRY,
                        price=order.price,
                        quantity=order.quantity,
                        queue_ahead=self.queue_ahead_quantity,
                        created_ts=now_ts,
                        post_only=True,
                    )
                )

        for request in requests:
            attempted.append(request.client_order_id)
            self._known_order_ids.append(request.client_order_id)
            result = self.exchange.place(request)
            if result.accepted:
                accepted.append(request.client_order_id)
                self._entry_ids[request.position_side].append(request.client_order_id)
            else:
                for client_order_id in accepted:
                    self._cancel(client_order_id, now_ts)
                self.state = EngineState.RECONCILIATION_REQUIRED
                self.pause_reason = "ENTRY_REJECTED"
                self._record(
                    now_ts,
                    "ENTRY_REJECTED",
                    {"client_order_id": request.client_order_id, "reason": result.reason},
                )
                return StartResult(False, result.reason or "entry rejected", tuple(attempted))

        self.state = EngineState.ACTIVE
        self.resume_state = None
        self.pause_reason = ""
        self.generation_id = generation_id
        self.symbol = proposal.symbol
        self.config_version = proposal.config_version
        self._record(
            now_ts,
            "STARTED",
            {"generation_id": generation_id, "symbol": proposal.symbol, "orders": len(accepted)},
        )
        return StartResult(True, "", tuple(attempted))

    # -- market/fill handling -------------------------------------------

    def on_tick(self, tick: MarketTick, *, now_ts: int) -> tuple[Fill, ...]:
        if not isinstance(tick, MarketTick):
            raise DomainError("a typed market tick is required")
        if not self._reconciled:
            return ()
        if self.state in (EngineState.IDLE, EngineState.STOPPED, EngineState.RECONCILIATION_REQUIRED):
            return ()
        if self.symbol is None or tick.symbol != self.symbol:
            return ()
        if tick.timestamp_ms > now_ts or now_ts - tick.timestamp_ms > self.max_tick_age_ms:
            if self.state is not EngineState.PAUSED:
                self.resume_state = self.state
                self.state = EngineState.PAUSED
                self._pause("STALE_FEED", now_ts)
            return ()
        if self.state is EngineState.PAUSED:
            if self.pause_reason != "STALE_FEED" or self.resume_state is None:
                return ()
            self.state = self.resume_state
            self.resume_state = None
            self.pause_reason = ""
            self._record(now_ts, "RESUMED", {})

        try:
            fills = self.exchange.on_tick(tick)
        except MarketDataError as exc:
            self.state = EngineState.RECONCILIATION_REQUIRED
            self.pause_reason = f"MARKET_DATA:{exc}"
            self._record(now_ts, "RECONCILIATION_REQUIRED", {"reason": str(exc)})
            return ()

        dirty: set[Side] = set()
        for fill in fills:
            self._record(
                now_ts,
                "FILL",
                {
                    "client_order_id": fill.client_order_id,
                    "side": fill.position_side.value,
                    "role": fill.role.value,
                    "price": str(fill.price),
                    "quantity": str(fill.quantity),
                    "fee_quote": str(fill.fee_quote),
                },
            )
            dirty.add(fill.position_side)
        for side in (Side.LONG, Side.SHORT):
            if side in dirty:
                self._sync_side(side, now_ts)
        self._enforce_combined_risk(now_ts, tick.mid)
        if self.state is EngineState.DRAINING and self._flat() and not self.exchange.open_orders(self.symbol):
            self.state = EngineState.STOPPED
            self._record(now_ts, "STOPPED", {"reason": "flat and reconciled"})
        return fills

    def _sync_side(self, side: Side, now_ts: int) -> bool:
        self._cancel_entries(side, now_ts)
        quantity, vwap = self.position(side)
        exit_id = self._exit_ids[side]
        if quantity <= ZERO:
            if exit_id is not None:
                snapshot = self.exchange.order(exit_id)
                if snapshot is not None and snapshot.status in OPEN_STATUSES:
                    self._cancel(exit_id, now_ts)
                self._exit_ids[side] = None
            return True
        if exit_id is not None:
            snapshot = self.exchange.order(exit_id)
            if snapshot is not None and snapshot.status in OPEN_STATUSES:
                if snapshot.remaining_quantity == quantity:
                    return True
                self._cancel(exit_id, now_ts)
            self._exit_ids[side] = None

        resolved = cost_aware_exit_price(
            side=side,
            quantity=quantity,
            entry_price=vwap,
            multiplier=self.exchange.config.multiplier,
            entry_fee_rate=self.exchange.position_entry_fee_rate(self.symbol or "", side),
            exit_fee_rate=self.exchange.config.fees.maker_rate,
            funding_cost_quote=self.exit_policy.funding_cost_quote,
            execution_buffer_quote=self.exit_policy.execution_buffer_quote,
            required_net_profit_quote=self.exit_policy.required_net_profit_quote,
            tick_size=self.filters.tick_size,
        )
        if not resolved.accepted:
            self._cancel_all_entries(now_ts)
            self._pause(f"EXIT_HURDLE:{resolved.rejection.code.value}", now_ts)
            return False
        price, _net = resolved.value
        self._exit_attempts[side] += 1
        client_order_id = f"{self.generation_id}:{side.value}:TP:{self._exit_attempts[side]}"
        result = self.exchange.place(
            OrderRequest(
                client_order_id=client_order_id,
                symbol=self.symbol or "",
                position_side=side,
                role=OrderRole.EXIT,
                price=price,
                quantity=quantity,
                queue_ahead=self.queue_ahead_quantity,
                created_ts=now_ts,
                post_only=True,
            )
        )
        self._known_order_ids.append(client_order_id)
        if not result.accepted:
            self._cancel_all_entries(now_ts)
            self._pause(f"EXIT_REJECTED:{result.reason}", now_ts)
            return False
        self._exit_ids[side] = client_order_id
        self._record(
            now_ts,
            "EXIT_PLACED",
            {"client_order_id": client_order_id, "price": str(price), "quantity": str(quantity)},
        )
        return True

    def _enforce_combined_risk(self, now_ts: int, mark_price: Decimal) -> bool:
        if self.state not in (EngineState.ACTIVE, EngineState.DRAINING):
            return True
        if self.symbol is None:
            return True
        long_notional = ZERO
        short_notional = ZERO
        long_quantity, long_vwap = self.position(Side.LONG)
        short_quantity, short_vwap = self.position(Side.SHORT)
        long_notional += long_quantity * (mark_price if long_quantity > ZERO else long_vwap)
        short_notional += short_quantity * (mark_price if short_quantity > ZERO else short_vwap)
        for snapshot in self.exchange.open_orders(self.symbol):
            if snapshot.role is not OrderRole.ENTRY:
                continue
            notional = snapshot.price * snapshot.remaining_quantity * self.exchange.config.multiplier
            if snapshot.position_side is Side.LONG:
                long_notional += notional
            else:
                short_notional += notional
        result = check_exposure(
            long_notional=long_notional,
            short_notional=short_notional,
            limits=self.risk_context.limits,
            risk_model=self.risk_context.risk_model,
            available_margin_quote=self.risk_context.available_margin_quote,
        )
        if result.accepted:
            return True
        self._cancel_all_entries(now_ts)
        self._pause(f"COMBINED_RISK:{result.rejection.code.value}", now_ts)
        return False

    # -- controls --------------------------------------------------------

    def drain(self, now_ts: int) -> bool:
        if not self._reconciled:
            return False
        if self.state in (EngineState.IDLE, EngineState.STOPPED, EngineState.RECONCILIATION_REQUIRED):
            return False
        self._cancel_all_entries(now_ts)
        self.state = EngineState.DRAINING
        self.resume_state = None
        self.pause_reason = ""
        self._record(now_ts, "DRAINING", {})
        if self._flat() and not self.exchange.open_orders(self.symbol):
            self.state = EngineState.STOPPED
            self._record(now_ts, "STOPPED", {"reason": "flat and reconciled"})
        return True

    def close_market_and_stop(self, now_ts: int) -> bool:
        if not self._reconciled:
            return False
        if self.state in (EngineState.IDLE, EngineState.STOPPED):
            return False
        tick = self.exchange.last_tick(self.symbol or "")
        if tick is None or tick.timestamp_ms > now_ts or now_ts - tick.timestamp_ms > self.max_tick_age_ms:
            self.state = EngineState.RECONCILIATION_REQUIRED
            self.pause_reason = "CLOSE_STALE_FEED"
            self._record(now_ts, "CLOSE_UNCONFIRMED", {"reason": "stale or missing market snapshot"})
            return False
        for snapshot in self.exchange.open_orders(self.symbol):
            if not self._cancel(snapshot.client_order_id, now_ts) and snapshot.status in OPEN_STATUSES:
                self.state = EngineState.RECONCILIATION_REQUIRED
                self.pause_reason = f"CLOSE_CANCEL_UNCONFIRMED:{snapshot.client_order_id}"
                self._record(
                    now_ts,
                    "CLOSE_UNCONFIRMED",
                    {"client_order_id": snapshot.client_order_id, "reason": "cancel not confirmed"},
                )
                return False
        if self.exchange.open_orders(self.symbol):
            self.state = EngineState.RECONCILIATION_REQUIRED
            self.pause_reason = "CLOSE_ORDERS_REMAIN"
            self._record(now_ts, "CLOSE_UNCONFIRMED", {"reason": "open orders remain after cancel"})
            return False
        for side in (Side.LONG, Side.SHORT):
            self._exit_ids[side] = None
            remaining = self.position(side)[0]
            if remaining <= ZERO:
                continue
            result = self.exchange.close_market(self.symbol or "", side, remaining, now_ts)
            if not result.accepted or result.fill is None:
                self.state = EngineState.RECONCILIATION_REQUIRED
                self.pause_reason = f"CLOSE_UNCONFIRMED:{result.reason}"
                self._record(now_ts, "CLOSE_UNCONFIRMED", {"side": side.value, "reason": result.reason})
                return False
            self._record(
                now_ts,
                "MARKET_CLOSE_FILL",
                {"side": side.value, "price": str(result.fill.price), "quantity": str(result.fill.quantity)},
            )
        if self._flat() and not self.exchange.open_orders(self.symbol):
            self.state = EngineState.STOPPED
            self._record(now_ts, "STOPPED", {"reason": "market close confirmed flat"})
            return True
        self.state = EngineState.RECONCILIATION_REQUIRED
        self.pause_reason = "CLOSE_NOT_FLAT"
        self._record(now_ts, "RECONCILIATION_REQUIRED", {"reason": "position remains after close"})
        return False

    def update_filters(self, filters: ExchangeFilters, now_ts: int) -> tuple[str, ...]:
        if not isinstance(filters, ExchangeFilters):
            raise DomainError("typed exchange filters are required")
        if not self._reconciled:
            return ()
        self.filters = filters
        canceled: list[str] = []
        invalid_exits: list[str] = []
        for snapshot in self.exchange.open_orders(self.symbol):
            notional = snapshot.price * snapshot.remaining_quantity * self.exchange.config.multiplier
            violates = (
                snapshot.price % filters.tick_size != ZERO
                or snapshot.remaining_quantity % filters.step_size != ZERO
                or snapshot.remaining_quantity < filters.min_qty
                or (filters.max_qty is not None and snapshot.remaining_quantity > filters.max_qty)
                or (filters.min_notional is not None and notional < filters.min_notional)
                or (filters.max_notional is not None and notional > filters.max_notional)
            )
            if not violates:
                continue
            if snapshot.role is OrderRole.EXIT:
                invalid_exits.append(snapshot.client_order_id)
                continue
            if self._cancel(snapshot.client_order_id, now_ts):
                canceled.append(snapshot.client_order_id)
            elif snapshot.status in OPEN_STATUSES:
                self.state = EngineState.RECONCILIATION_REQUIRED
                self.pause_reason = "FILTER_CANCEL_UNCONFIRMED"
                self._record(now_ts, "RECONCILIATION_REQUIRED", {"reason": "filter cancel unconfirmed"})
                return tuple(canceled)
        if canceled:
            self._record(now_ts, "FILTER_ENTRIES_CANCELED", {"client_order_ids": canceled})
        if invalid_exits:
            self._pause("EXIT_FILTER_VIOLATION", now_ts)
            self._record(now_ts, "EXIT_FILTER_VIOLATION", {"client_order_ids": invalid_exits})
            return tuple(canceled)
        if self._flat() and not self.exchange.open_orders(self.symbol):
            self.state = EngineState.STOPPED
            self._record(now_ts, "STOPPED", {"reason": "no entries pass updated filters"})
        return tuple(canceled)

    # -- reconciliation / restart ---------------------------------------

    def resume_after_reconciliation(self, now_ts: int) -> bool:
        """Explicitly leave a reconciled error state; never an automatic resume.

        Only a flat venue with no open orders may be returned to STOPPED so that a new
        generation can be started. Any residual order/position keeps the engine blocked.
        """
        if self.state is not EngineState.RECONCILIATION_REQUIRED:
            return False
        if not self.reconcile():
            return False
        if not self._flat() or self.exchange.open_orders(self.symbol):
            return False
        self.state = EngineState.STOPPED
        self.resume_state = None
        self.pause_reason = ""
        self._record(now_ts, "RESUMED_SAFE", {"reason": "reconciled flat venue"})
        return True

    def _position_mismatch(self) -> bool:
        if self.symbol is None:
            return False
        expected = {Side.LONG: ZERO, Side.SHORT: ZERO}
        for fill in self.exchange.fills:
            if fill.symbol != self.symbol:
                continue
            if fill.role is OrderRole.ENTRY:
                expected[fill.position_side] += fill.quantity
            else:
                expected[fill.position_side] -= fill.quantity
        for side in (Side.LONG, Side.SHORT):
            if self.position(side)[0] != expected[side]:
                return True
        return False

    def reconcile(self) -> bool:
        known = set(self._known_order_ids)
        venue_ids = {snapshot.client_order_id for snapshot in self.exchange.orders()}
        unknown = venue_ids - known
        missing = {client_order_id for client_order_id in known if self.exchange.order(client_order_id) is None}
        unhandled_position = (
            (self.state in (EngineState.IDLE, EngineState.STOPPED) and not self._flat())
            or self._position_mismatch()
        )
        if unknown or missing or unhandled_position:
            self._reconciled = False
            self.state = EngineState.RECONCILIATION_REQUIRED
            self.pause_reason = "RECONCILIATION_MISMATCH"
            self._record(
                0,
                "RECONCILIATION_REQUIRED",
                {
                    "unknown": sorted(unknown),
                    "missing": sorted(missing),
                    "unhandled_position": unhandled_position,
                },
            )
            return False
        self._reconciled = True
        self._record(0, "RECONCILED", {"orders": len(known)})
        return True

    def to_state(self) -> dict:
        return {
            "state": self.state.value,
            "resume_state": None if self.resume_state is None else self.resume_state.value,
            "pause_reason": self.pause_reason,
            "generation_id": self.generation_id,
            "symbol": self.symbol,
            "config_version": self.config_version,
            "entry_ids": {side.value: list(ids) for side, ids in self._entry_ids.items()},
            "exit_ids": {side.value: value for side, value in self._exit_ids.items() if value is not None},
            "exit_attempts": {side.value: value for side, value in self._exit_attempts.items()},
            "known_order_ids": list(self._known_order_ids),
            "journal": list(self._journal),
        }

    @classmethod
    def from_state(
        cls,
        *,
        exchange: PaperExchange,
        filters: ExchangeFilters,
        exit_policy: ExitPolicy,
        risk_context: PaperRiskContext,
        max_tick_age_ms: int,
        queue_ahead_quantity: Decimal,
        state: dict,
    ) -> "PaperGridEngine":
        engine = cls(
            exchange=exchange,
            filters=filters,
            exit_policy=exit_policy,
            risk_context=risk_context,
            max_tick_age_ms=max_tick_age_ms,
            queue_ahead_quantity=queue_ahead_quantity,
        )
        engine.state = EngineState(state["state"])
        resume = state.get("resume_state")
        engine.resume_state = None if resume is None else EngineState(resume)
        engine.pause_reason = state["pause_reason"]
        engine.generation_id = state["generation_id"]
        engine.symbol = state["symbol"]
        engine.config_version = state["config_version"]
        engine._entry_ids = {
            side: list(state["entry_ids"].get(side.value, [])) for side in (Side.LONG, Side.SHORT)
        }
        engine._exit_ids = {
            side: state["exit_ids"].get(side.value) for side in (Side.LONG, Side.SHORT)
        }
        engine._exit_attempts = {
            side: int(state["exit_attempts"].get(side.value, 0)) for side in (Side.LONG, Side.SHORT)
        }
        engine._known_order_ids = list(state["known_order_ids"])
        engine._journal = list(state["journal"])
        if engine.state in (EngineState.ACTIVE, EngineState.DRAINING, EngineState.PAUSED):
            if engine.symbol is None or engine.generation_id is None:
                raise DomainError("restored live engine state is missing generation identity")
        engine._reconciled = False
        return engine
