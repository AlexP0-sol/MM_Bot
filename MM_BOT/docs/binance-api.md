# Binance USDⓈ-M adapter — endpoints и semantics

**Дата:** 2026-11-18  
**Статус:** реализован только код и offline-тесты; реальные запросы и Testnet smoke не выполнялись.  
**Проверка документации:** базовые ссылки зафиксированы в `phase-0.md` §7 (2026-11-16). Перед ручным Testnet smoke endpoint-схемы, поля и лимиты нужно перечитать на официальном сайте и обновить этот файл.

## 1. Окружения

| Окружение | Base URL | Trading |
| --- | --- | --- |
| Testnet | `https://testnet.binancefuture.com` | только при `enable_trading=True` и после `preflight` |
| LIVE | `https://fapi.binance.com` | заблокирован в этапе 3; чтение возможно только при явном `allow_live` |

Ключи и конфигурация Testnet/LIVE разделены: `BinanceConfig` содержит environment и base URL, `BinanceCredentials` не смешиваются между клиентами. Секрет не выводится в `repr`, логах и ошибках. Метода вывода средств нет и не будет.

## 2. Аутентификация

- Публичные запросы: без ключа.
- Подписанные: `X-MBX-APIKEY`, параметры `timestamp` (server time) и `recvWindow`, `signature = HMAC_SHA256(secret, query_string)`.
- Перед первым подписанным запросом обязателен `sync_time()`: `GET /fapi/v1/time`, смещение локального времени больше `max_clock_drift_ms` блокирует торговлю (`ClockDriftError`).
- Параметры передаются в query string; подпись считается по фактически отправленной строке.
- Секрет и ключ никогда не попадают в сообщения исключений.

## 3. Используемые endpoints

| Метод | Path | Назначение | Auth |
| --- | --- | --- | --- |
| GET | `/fapi/v1/time` | server time и синхронизация | нет |
| GET | `/fapi/v1/exchangeInfo` | contract type/status, base/quote, filters | нет |
| GET | `/fapi/v1/ticker/bookTicker` | bid/ask и top-of-book quantity | нет |
| GET | `/fapi/v1/aggTrades` | последняя публичная сделка, aggressor (`m`) | нет |
| GET | `/fapi/v1/klines` | завершённые свечи; последняя незавершённая отбрасывается | нет |
| GET | `/fapi/v1/premiumIndex` | mark price, funding rate, next funding time | нет |
| GET | `/fapi/v1/positionSide/dual` | Hedge/One-way режим (account-wide) | да |
| GET | `/fapi/v3/account` | wallet/available balance, unrealized PnL | да |
| GET | `/fapi/v3/positionRisk` | позиции по символам/сторонам | да |
| GET | `/fapi/v1/leverageBracket` | notional cap, initial leverage, maintenance-margin ratio | да |
| GET | `/fapi/v1/openOrders` | открытые ордера (опционально symbol) | да |
| GET | `/fapi/v1/order` | ордер по `origClientOrderId` | да |
| POST | `/fapi/v1/order` | новая LIMIT GTX заявка | да |
| DELETE | `/fapi/v1/order` | cancel по `origClientOrderId` | да |
| POST | `/fapi/v1/listenKey` | создать user-data listen key | да |
| PUT | `/fapi/v1/listenKey` | keepalive listen key | да |
| DELETE | `/fapi/v1/listenKey` | закрыть listen key | да |

## 4. Metadata → domain filters

`PRICE_FILTER.tickSize`, `LOT_SIZE.stepSize/minQty/maxQty`, `MIN_NOTIONAL.notional`, `contractType`, `status`, `baseAsset`, `quoteAsset` и `contractMultiplier` (если присутствует; для USDⓈ-M обычно 1) мапятся в `ExchangeFilters`.

`preflight(symbol)` дополнительно требует `contractType == "PERPETUAL"`, `status == "TRADING"` и `symbol == baseAsset + quoteAsset`; иначе торговля блокируется. `PERCENT_PRICE` (`multiplierUp`/`multiplierDown`) сохраняется и проверяется против mark price. Обязательны `canTrade=true` и `multiAssetsMargin=false`; иначе adapter fail-closed. Preflight получает свежий mark price и leverage brackets и блокируется при stale mark или неподдерживаемом margin mode.

## 5. Order semantics

- Только `LIMIT` + `timeInForce=GTX` (Post-Only). Market/conditional/batch/amend не используются.
- Для Hedge Mode обязательны `positionSide=LONG|SHORT`; `reduceOnly` не отправляется (в Hedge Mode он не допускается).
- Вход Long — BUY, выход Long — SELL; вход Short — SELL, выход Short — BUY.
- `newClientOrderId` — детерминированный idempotency key (1..36 символов Binance charset). Повторная отправка того же intent не выполняется: сначала reconciliation по client ID.
- `preflight` обязан подтвердить `dualSidePosition=true`; One-way режим блокирует paired grid.
- Перед первой заявкой требуется успешный `preflight` этого символа в том же adapter instance; preflight старше `max_preflight_age_ms` блокирует заявку.
- До сети adapter локально проверяет tick/step/minQty/maxQty/minNotional, `PERCENT_PRICE` против mark и для EXIT — непревышение объёма над preflight-позицией. Exit без известной позиции отклоняется; adapter не переворачивает экспозицию.

## 6. Ошибки, таймауты и неизвестный исход

| Ситуация | Поведение |
| --- | --- |
| HTTP 4xx с `code/msg` | `BinanceApiError`; create/cancel → `REJECTED`, без повторов |
| HTTP 429/418 | `RateLimited` с `Retry-After`; create/cancel → `UNKNOWN` |
| HTTP 5xx | create/cancel → `UNKNOWN`; чтение → `BinanceApiError` |
| timeout/network (`OSError`) | create/cancel → `UNKNOWN`; чтение → `BinanceNetworkError` |
| `-2013 Order does not exist` | query → `None`; unknown outcome → `NOT_FOUND` |
| `-2022 ReduceOnly rejected` | явный reject, без fallback на market |

`UNKNOWN` никогда не повторяет create. `resolve_unknown()` делает `GET /fapi/v1/order?origClientOrderId=...`; если ордер найден — возвращает фактический статус/исполнение, если нет — `NOT_FOUND` и требует ручного/безопасного решения.

## 7. Rate limits

- Учитывается `X-MBX-USED-WEIGHT-1M`; при достижении настроенного `max_weight_1m` следующий запрос отклоняется локально (`RateLimited`) до конца минутного окна.
- 429/418 не ретраятся автоматически; backoff и повтор — ответственность вызывающего слоя.
- Для order mutations автоматических retry нет вообще: только reconciliation по client ID.
- Точные веса endpoint'ов и лимиты ордеров нужно перепроверить перед Testnet smoke; в коде не зашиты конкретные веса, кроме общего weight-потолка. Order-count заголовки (`X-MBX-ORDER-COUNT-*`) пока не учитываются.

## 8. User data stream

- `listenKey` создаётся/продлевается/закрывается через REST; `ListenKeyState` хранит created/keepalive/expiry и рассчитывает необходимость продления (60 минут validity, 30 минут keepalive — значения конфигурируемы).
- События `ORDER_TRADE_UPDATE`, `ACCOUNT_UPDATE`, `listenKeyExpired` нормализуются в `streams.py`.
- `streams.py` содержит реальный RFC 6455 клиент (`websocket.py`): TLS-проверка для `wss`, masked client frames, ping/pong, close, fragmentation, лимит сообщения. `ws` разрешён только для loopback-тестов.
- `MarketTickAssembler` объединяет `bookTicker` и `aggTrade`; `StreamSession` выполняет reconnect с capped backoff и обновляет `StreamHealth`.
- `StreamHealth` считает поток `STALE`/`DISCONNECTED`/`KEY_EXPIRED`; торговля разрешена только в `HEALTHY`.
- Автоматический цикл REST-продления listen key и подключение supervisor к engine — этап 4/6. `ORDER_TRADE_UPDATE` не является источником истины: после рестарта/разрыва обязателен REST reconciliation.

## 9. Reconciliation

`BinanceTradingAdapter.reconcile(symbol, known_client_ids, owned_positions)`:

1. получает account/positions/open orders;
2. открытый ордер с неизвестным client ID → `unknown_orders`;
3. каждый известный client ID запрашивается по `origClientOrderId`; отсутствующий → `missing_known_orders`;
4. ненулевая позиция вне `owned_positions` → `unknown_positions`;
5. `ok` только если все три списка пусты.

Reconciliation ограничен одним символом (MVP) и не обнаруживает чужие уже закрытые ордера; для этого потребовался бы полный `allOrders`-скан, который вынесен за MVP.

## 10. Официальные источники

Ссылки и дата первичной сверки — `phase-0.md` §7:

- General Info / base URL;
- Exchange Information;
- Position mode / Change Position Mode;
- New Order (Hedge Mode, `positionSide`, `reduceOnly`);
- Mark Price / Funding Info;
- 24hr Ticker;
- Account Information V3 / Position Information V3 / Leverage Brackets;
- User Data Stream / WebSocket Connect.

Перед ручным Testnet smoke: повторно открыть эти страницы, сверить пути, поля, коды ошибок, веса и правила GTX/reduceOnly; обновить дату и этот файл.
