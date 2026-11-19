# Этап 3 — Binance adapter и Testnet (opt-in)

**Проект:** локальный адаптивный grid-бот Binance USDⓈ-M Futures  
**Дата:** 2026-11-18  
**Статус:** adapter core, WebSocket-транспорт, парсеры потоков и offline-тесты реализованы. Ручной Testnet smoke **не выполнялся** — для него нужен явный opt-in владельца. LIVE-торговля заблокирована.  
**Основание:** `itog_master_prompt_binance_grid_bot.md`, раздел 12 «Этап 3»; `docs/binance-api.md`.

## 1. Файлы этапа

| Файл | Содержимое |
| --- | --- |
| `src/gridbot/binance.py` | окружения Testnet/LIVE, credentials с redaction, HMAC-подпись, server time sync, public/private REST, metadata/filters/price bands, account/positions/orders, canTrade/multi-assets guard, leverage brackets, mark price, listen key REST, LIMIT GTX, локальная проверка ордера, unknown outcome, reconciliation |
| `src/gridbot/websocket.py` | минимальный RFC 6455 клиент: TLS-проверка для `wss`, handshake, masked client frames, ping/pong, close, fragmentation, лимит сообщения; `ws` только для loopback-тестов |
| `src/gridbot/streams.py` | нормализация public/private событий, `StreamHealth`, `ListenKeyState`, `MarketTickAssembler`, `StreamSession` с reconnect/backoff, URL-хелперы |
| `src/gridbot/market_data.py` | общая нормализация book/trade/klines для polling и adapter |
| `src/gridbot/exchange.py` | terminal-статус `EXPIRED` |
| `tests/test_binance.py` | 30 offline-тестов adapter-а и локальных guard-ов |
| `tests/test_streams.py` | 17 offline-тестов событий, health, listen key, assembler, reconnect |
| `tests/test_websocket.py` | 5 тестов handshake/frames/ping/fragmentation против локального сервера |
| `docs/binance-api.md` | endpoints, auth, order semantics, ошибки, лимиты, reconciliation |

## 2. Соответствие этапу 3

| Требование | Реализация | Проверка |
| --- | --- | --- |
| Account snapshots | wallet/available balance, unrealized PnL, Long/Short позиции, open orders, `canTrade`, `multiAssetsMargin` | `test_account_state_maps_balances_positions_and_orders`, `test_account_must_be_trade_enabled_and_single_asset` |
| Market metadata | `SymbolMetadata` из `exchangeInfo`: filters, contract type/status, `PERCENT_PRICE`, multiplier | `test_exchange_info_maps_to_domain_filters`, `test_local_filter_price_band_and_exit_guards_run_before_network` |
| Public stream | WebSocket-клиент + `aggTrade`/`bookTicker` парсинг + conservative `MarketTick` assembler | `WebSocketTests`, `test_combined_agg_trade_is_unwrapped_and_parsed`, `test_assembler_requires_both_sides_and_rejects_backwards_events` |
| Private/user stream | `ORDER_TRADE_UPDATE`, `ACCOUNT_UPDATE`, `listenKeyExpired`; listen key REST; expiry/keepalive timing | `PrivateStreamTests`, `test_listen_key_expiry_and_keepalive_timing` |
| Time sync | `sync_time()`, `ClockDriftError`, server timestamp в подписи | `test_sync_time_and_hmac_signature`, `test_clock_drift_above_the_bound_blocks_signing` |
| Symbol/account state | `preflight`: perpetual/TRADING, Hedge Mode, canTrade, single-asset margin, fresh mark, leverage brackets | `test_preflight_requires_hedge_mode`, `test_account_must_be_trade_enabled_and_single_asset` |
| REST reconciliation | неизвестные open orders/позиции, отсутствующие известные client IDs | `test_reconcile_detects_unknown_orders_positions_and_missing_ids`, `test_reconcile_ok_when_everything_is_owned_and_known` |
| Orders | только LIMIT GTX, `positionSide`, без `reduceOnly` в Hedge Mode | `test_place_uses_gtx_limits_and_hedge_side` |
| Local order guards | tick/step/minQty/maxQty/minNotional, price band, exit qty ≤ preflight position, preflight freshness | `test_local_filter_price_band_and_exit_guards_run_before_network`, `test_stale_preflight_blocks_a_new_order` |
| Rate limits | `X-MBX-USED-WEIGHT-1M`, локальный weight-потолок, 429/418 + `Retry-After` | `test_rate_limit_and_weight_budget`, `test_http_429_exposes_retry_after` |
| Credentials isolation | отдельные environment/base URL/credentials; LIVE trading запрещён; redaction | `CredentialsAndConfigTests` |
| No withdraw | метода вывода нет | `test_no_withdraw_surface` |
| Disconnect/timeout | `OutcomeUnknown`, stream `DISCONNECTED`/`STALE`, reconnect backoff, WebSocket close/fragment guards | `test_unknown_create_outcome_is_resolved_without_retry`, `StreamSessionTests`, `WebSocketTests` |
| Partial fill | `ExchangeOrder.executedQty/avgPrice`, `OrderUpdate.remaining_quantity` | `test_account_state_maps_balances_positions_and_orders`, `test_partial_fill_order_update_is_normalized` |
| Restart | восстановление клиента и reconciliation по известным client IDs | `test_reconcile_ok_when_everything_is_owned_and_known` |
| Unknown create outcome | timeout/5xx → `UNKNOWN`, затем query по `origClientOrderId`, без повторного create | `test_unknown_create_outcome_is_resolved_without_retry`, `test_not_found_after_unknown_is_reported_and_not_retried` |
| Отдельный Testnet opt-in | торговый adapter требует `enable_trading=True`, Testnet и успешный preflight; LIVE-торговля запрещена | `test_trading_adapter_requires_explicit_enable`, `test_live_is_disabled_and_live_trading_is_impossible` |
| Ручной Testnet smoke | **не выполнялся** (нужен opt-in) | — |

## 3. Границы и решения

- **LIVE заблокирован дважды:** `BinanceConfig` не даёт включить LIVE-торговлю, а `BinanceTradingAdapter` отказывается работать с LIVE независимо от конфигурации. Прямые `new_order`/`cancel_order` и низкоуровневый `_signed` для order mutation требуют `enable_trading=True` и официальный Testnet host, поэтому read-only/TEST клиент не может случайно отправить заявку.
- **Hedge Mode обязателен.** `preflight` падает при `dualSidePosition=false`; это соответствует paired-grid и close semantics MVP. Смена режима аккаунта ботом запрещена.
- **Preflight перед торговлей.** `place()` невозможен без успешного `preflight(symbol)` в этом же adapter instance; preflight старше `max_preflight_age_ms` блокирует новую заявку.
- **Локальные guard-ы до сети.** Adapter независимо проверяет tick/step/min/max/notional, `PERCENT_PRICE` и непревышение exit-объёма над preflight-позицией. Это defense in depth, а не замена risk engine.
- **Только Post-Only.** `LIMIT` + `GTX`; market/conditional/batch/amend отсутствуют. Reject не превращается в market fallback.
- **Никаких слепых повторов.** create/cancel при timeout/5xx/429 → `UNKNOWN`; далее только `GET /fapi/v1/order` по client ID. `NOT_FOUND` не означает разрешение создать заново.
- **Rate limit — fail closed.** Достижение weight-потолка блокирует следующий запрос до конца минутного окна; 429/418 отдаются вызывающему слою с `Retry-After`. Order-count лимиты пока не учитываются.
- **Reconciliation — symbol-scoped.** Неизвестные open orders и ненулевые позиции вне `owned_positions` блокируют `ok`; чужие уже закрытые ордера не сканируются (вне MVP).
- **Streams — реальный транспорт.** `WebSocketConnection` работает по RFC 6455 с обязательным TLS для `wss`; `ws` разрешён только для loopback-тестов. `StreamSession` переподключается с capped backoff; `ListenKeyState` считает expiry/keepalive, но автоматический цикл REST-продления встроит application layer этапа 4/6.
- **Свечи.** `klines` отбрасывает последнюю незавершённую свечу; timestamp должен быть выровнен к интервалу.
- **Синхронный транспорт.** REST и WebSocket клиент блокирующие; event loop ещё не введён, поэтому смешивания blocking I/O с asyncio нет. При добавлении application/API слоя (этап 4) транспорт должен работать в отдельном потоке/executor либо быть переведён на async; это зафиксировано как ограничение, а не скрытая реализация.

## 4. Проверки

```text
cd MM_BOT
PYTHONPATH=src python -X utf8 -m unittest discover -s tests -v
  → Ran 142 tests ... OK (30 adapter + 17 streams + 5 WebSocket + остальные этапы)
python -X utf8 -m compileall -q src tests
  → exit 0
```

Сеть не использовалась: HTTP/WebSocket transports подменены fake/local-сервером, все ответы — фикстуры. Реальных запросов к Binance, Testnet или LIVE не было; API-ключи не запрашивались и не хранились.

## 5. Что осталось до закрытия этапа 3

1. **Opt-in владельца на Testnet:** выделенный Testnet-аккаунт/subaccount, ключи без withdraw, ввод через локальный защищённый setup (не в чат), подтверждение HEDGE + margin mode.
2. **Ручной Testnet smoke** по чек-листу: `preflight`, metadata, Post-Only reject, частичный fill/status/cancel, restart+reconciliation, close semantics; только вручную, никогда автоматическим тестом.
3. **Повторная сверка** endpoint-путей, полей, кодов ошибок, весов и фактического WebSocket-поведения на официальном сайте/Testnet перед smoke; обновить `binance-api.md` и дату.
4. **Order-count rate limits** (`X-MBX-ORDER-COUNT-*`) сейчас не учитываются — только weight.
5. **`allOrders`-скан** закрытых чужих ордеров (вне MVP, если понадобится).
6. **Интеграция mark/funding, preflight и stream supervisor в торговый engine** — этап 4/5; сейчас engine остаётся paper-only.

## 6. Оставшийся риск

- Endpoint-схемы и поля не перепроверены в этом запуске: возможны расхождения с текущей документацией; код падает fail-closed при неожиданном payload, но smoke обязателен.
- `exchangeInfo` парсится целиком; один malformed symbol блокирует preflight (безопасно, но широко).
- Reconciliation не видит чужие закрытые ордера и ограничен одним символом.
- Weight-потолок использует локальное минутное окно; фактические веса endpoint'ов не зашиты.
- WebSocket-клиент протестирован на локальном сервере, не на Binance; реальные ping/close/limit-поведения и keepalive-цикл требуют Testnet smoke.
- `place()` проверяет exit-объём по preflight-снимку; после fill нужен новый preflight, иначе adapter консервативно откажет.

## 7. Следующий шаг

Этап 3 закрывается только после ручного Testnet smoke с вашим подтверждением. Без него следующий этап по плану — **этап 4 (Dashboard и Telegram Mini App/relay)** — может разрабатываться на paper/TEST, но Testnet/LIVE-готовность не заявляется.
