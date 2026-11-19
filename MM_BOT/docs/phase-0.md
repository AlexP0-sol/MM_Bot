# Этап 0 — обследование и архитектурные решения

**Проект:** локальный адаптивный grid-бот Binance USDⓈ-M Futures  
**Дата проверки официальных источников:** 2026-11-16  
**Ревизия:** 2026-11-18 (этапы 1–3 core)  
**Статус:** этап 0 завершён (обследование и решения); этап 1 (Decimal-домен) — [`phase-1.md`](phase-1.md); этап 2 (paper engine на public data) — [`phase-2.md`](phase-2.md); этап 3 (Binance adapter core) — [`phase-3.md`](phase-3.md). Ручной Testnet smoke не выполнялся (нужен opt-in), LIVE-торговля заблокирована.

## 1. Источники требований и состояние папки

### Приоритет источников

1. Приложенный `itog_master_prompt_binance_grid_bot.md` — актуальная самостоятельная спецификация и основной источник решений.
2. `message.md` — исходные пожелания владельца, включая Windows, Python, Binance USDⓈ-M, Telegram Web App, реальные публичные данные в TEST и одновременно создаваемые Long/Short-сетки.
3. `master_prompt_test_variant.md`, `Грид-бот_хронология.md`, `formulas.md` и экспортированные материалы — исторический контекст для аудита, не источник торговых значений по умолчанию.

При конфликте действует более новый мастер-промпт и явное решение владельца. Старая версия предлагает сначала один вручную выбранный символ, несколько пользовательских режимов (`DRY_RUN/PAPER/TESTNET/LIVE`) и localhost UI как основную панель; это заменено новым scope: ровно два пользовательских режима `TEST/LIVE`, scanner в целевом продукте и Telegram Mini App с внешним HTTPS-доступом (пока hosting/relay не выбран, реализуются независимые локальные компоненты). История и `formulas.md` содержат несовместимые версии ATR/V_idx/spacing, некорректные примеры весов softmax и формулу ограничения числа уровней, упрощения комиссий и цены ликвидации, а также недоказанные обещания безопасности, доходности и гарантированного maker-исполнения. Ничего из этого не считается проверенным правилом стратегии. Числа из ранних материалов могут стать лишь явно маркированными гипотезами paper/replay-эксперимента.

### Обследование рабочей области

- Целевая папка найдена по указанному пути: `C:\Users\thecd\OneDrive\Desktop\РАБОТАААААААААААААААААААААА\MM_BOT`.
- Папка `MM_BOT` была пустой; исходные материалы находятся рядом, в родительской папке.
- В `MM_BOT` и его проверенных родителях нет `AGENTS.md` или `CLAUDE.md`; сам `MM_BOT` не является Git-репозиторием. Git проверка статуса невозможна/неприменима: нет `.git` в проекте; имеющийся Git верхнего уровня на `C:\` отклонён как dubious ownership. Глобальные Git-настройки не менялись.
- Исходные материалы читались только из родительской папки; изменения туда не вносились.

## 2. MVP scope и безопасные исключения

### Входит в MVP

- Python-приложение для локального запуска на Windows; один активный символ в торговом цикле.
- Binance USDⓈ-M Futures как первая и единственная биржевая интеграция.
- Два пользовательских торговых режима: **TEST** и **LIVE**. TEST использует настоящие публичные рыночные данные и виртуальный paper account; LIVE по умолчанию выключен.
- Две независимые ноги Long и Short как одна парная generation с общим preflight и account-level risk manager. Обе сетки планируются до отправки первой заявки; фактическая постановка не считается атомарной.
- Перед каждой generation/сменой символа/возобновлением после остановки — свежий account snapshot в LIVE и повторный расчёт бюджета обеих ног; в TEST — snapshot виртуального счёта. Неизвестное или устаревшее состояние запрещает новый риск.
- Binance market-data, расчётные domain-функции, scanner, paired-grid proposal, paper execution, ledger/journal, state/reconciliation, локальный API и Telegram Bot/Mini App остаются отдельными слоями.
- Telegram Mini App — панель состояния, конфигурации, предложений и подтверждений, а не источник истины и не исполнитель биржевых операций.

### Явно вне этапа 0 и не готово к LIVE

- Торговые расчёты, ордерный код, приватный Binance adapter, вызовы REST/WS Binance, секреты, реальные/тестовые ордера и торговый smoke test.
- Выбор/настройка hosting или relay, Telegram bot token, Binance API keys, установка зависимостей и запуск торгового процесса.
- Исторические численные настройки не становятся defaults; научная/статистическая прибыльность стратегии не заявляется.
- Никакой пользовательский вопрос из раздела 15 мастер-промпта не снимается документированием. Пока ответы не получены, допустимы проектирование и TEST-ориентированная работа, но не LIVE.

## 3. Глоссарий и модель истины

- **TEST:** реальные публичные Binance market data + виртуальная equity + симуляция fills/costs. В пути TEST технически не должно быть доступно ни одного endpoint изменения реальных ордеров. Подключённый read-only account допустим только как явно подписанный справочный snapshot и не меняет paper equity.
- **LIVE:** реальные account data и ордера, только после отдельного локального arming, preflight, проверок допуска и явно выбранной политики подтверждения grid proposal. LIVE не реализуется и не включается на этапе 0.
- **Grid generation:** одна парная конфигурация Long/Short на одном символе и согласованном market/config snapshot. Запрещено наслаивать поколения; generation завершена только после плоских ног, терминальных ордеров и успешной сверки.
- **Bot-owned state:** заявки/позиции принадлежат боту только при подтверждаемой связи с его сохранёнными intent/client ID и журналом событий. Неизвестные сторонние позиции и ордера не присваиваются и не закрываются автоматически.
- **NO_TRADE:** штатный результат расчёта при нарушении данных, риска, filters, минимального номинала или других hard constraints.
- **Fail closed:** при пропуске, устаревании, конфликте или неопределённом исходе запрещать создание/увеличение риска; сначала восстановить факты и reconciliate.
- **Цена/номинал/количество/комиссия:** значения имеют явные единицы и источник; финансовые расчёты — Decimal. Не смешивать equity, wallet/available balance, unrealized PnL, margin, order reserve, notional, leverage и funding.

## 4. Архитектурная схема и границы доверия

```text
Binance public REST/WS ─┐
Binance private REST/WS ├→ normalizers → market/account state → reconciler
                         │                                     │
                         └─────────────────────────────────────┘
                                                               ↓
Local UI/API or Telegram Bot/Mini App → application commands → policy/state machine
                                                    ↓
                              Parameter Registry → scanner → grid proposal
                                                    ↓
                              account-level risk/preflight → persistent intent/journal
                                                    ↓
                                    TEST: Paper Adapter only
                                    LIVE: Binance Adapter (future, disabled)
                                                    ↓
                                       ledger, audit, status views

Telegram Mini App → HTTPS frontend / (optional authenticated relay) → loopback API
                                                               ↑
                                           local Windows engine is source of truth
```

Рекомендуемые bounded contexts, создаваемые только по мере надобности и без заранее пустых пакетов: `domain/config`, `indicators`, `market_data`, `scanner`, `strategy`, `risk`, `exchange`, `execution`, `reconciliation`, `ledger/persistence`, `application`, `api`, `telegram`, `observability`. Зависимость стратегии от vendor SDK запрещена; порт биржи закрывает её за адаптером. Money/value types и чистые вычисления отделены от I/O. Язык исполнения — Python, сеть — async, синхронный I/O не блокирует event loop. Конкретные библиотеки и их закреплённые версии выбрать на этапе реализации после проверки совместимости и поддержки Windows; преждевременная зависимость от универсального SDK не утверждается.

### Потоки команд и данных

- Команда интерфейса проходит аутентификацию, проверку среды/состояния, сериализацию и idempotency, затем локальный policy/risk guard. Ни Telegram, ни relay не вызывают exchange adapter напрямую.
- LIVE proposal строится по согласованным market/account/config snapshots, проходит округление и hard limits для обеих сторон и журналируется вместе с причинами принятия/отказа. В LIVE ещё до первой заявки резервируется совокупный бюджет. Неуспех одной заявки не делает пару атомарной: сверить fills/позиции и перейти в безопасное состояние.
- REST snapshot служит bootstrap/reconciliation; streams дают оперативные события. Потеря приватного потока, неизвестный результат create/cancel, несогласованность REST/локального журнала запрещают новые entries до сверки.
- Новые поколения и символы — только после drain/reconcile предыдущих и свежего preflight. Нельзя использовать данные одного символа/снимка баланса как актуальные для следующего.

## 5. Глобальная state machine и ключевые переходы

Основной жизненный цикл: `STOPPED → PREFLIGHT → STANDBY → SCANNING → GRID_PROPOSED → ACTIVE → DRAINING → STOPPED`.

Защитные состояния: `PAUSED`, `COOLDOWN`, `RECONCILIATION_REQUIRED`, `AGENT_OFFLINE`, `ERROR`, `EMERGENCY_CLOSING`, `STANDBY_NO_CANDIDATE`. Любой переход журналируется; start/stop/resume/environment switch и confirmations сериализованы и идемпотентны.

- **Start / resume:** разрешить только после сверки состояния, свежих данных, валидной конфигурации и нового preflight. В LIVE требуется локальное подтверждение; до отдельного решения владельца — подтверждение каждого нового proposal, а не автономный режим.
- **Пустая сетка / re-anchor:** если оба плеча ещё без fills и trigger проходит hysteresis, сначала cancel, получить терминальные статусы и fills, повторить reconciliation; только потом новый proposal. Неизвестная отмена блокирует создание дубля.
- **Любой fill:** зафиксировать execution, немедленно пересчитать совокупный риск. Не стирать fills и не переносить уже исполненные уровни. Остальные entries оставлять только если их полное исполнение вместе с другой ногой проходит guards; иначе отменить и сверить.
- **Позиции и выход:** сопровождать фактический остаток каждой ноги отдельно; один агрегированный exit target на остаток в MVP. Проверять расходы/fees/funding/cost buffer и корректную биржевую сторону/quantity. Post-only rejection или неисполнение не приводят к скрытому market fallback.
- **Обычный drain/stop:** запрет новых входов, scanner и rotation; cancel entries обеих ног; сверка fills; сопровождение открытых позиций по exit policy; STOPPED только после фактической плоскости и reconciliation. Убыточную позицию не закрывать внезапно рынком без отдельно согласованной политики.
- **Close market and stop:** отдельное подтверждаемое действие. Запрет входов, cancel/conflict resolution, новый position snapshot, close только фактического остатка корректной стороной, повторная сверка; при timeout не повторять вслепую. Остановить работу только когда close подтверждён; иначе явно показать неопределённость.
- **Rotation:** запускать drain активного символа, не открывать другой пока любая нога/ордер/неизвестное событие остаётся; после полной сверки — новый snapshot, scanner и proposal.
- **Перезапуск/offline:** не считать, что локальный cancel состоялся при выключенном Windows-компьютере. На запуске загрузить журнал, не выставлять ордера, получить полный account/open-order/position snapshot и reconciliate прежде любых действий.

## 6. Hard guards и аудит противоречий

Hard guards являются неотключаемыми инвариантами, а не обычными UI-порогами:

1. TEST order-mutation path технически изолирован; TEST использует только paper adapter, виртуальный капитал, не имеет Binance private keys и не может вызывать create/cancel/amend LIVE order methods.
2. LIVE off-by-default. Не активируется Telegram-командой или переключением поля в Mini App. Для старта нужна отдельная локальная активация, проверенная среда/ключи и явный preflight.
3. Неизвестное состояние/внешний order/position/неполный account snapshot/stale market or account feed/filter mismatch/потеря сверки → запрет нового риска и `PAUSED`/`RECONCILIATION_REQUIRED`.
4. Общие для обеих ног ограничения на allocation, символ/аккаунт, gross и абсолютный net exposure, worst-case исполнения всех входов, маржу, fees/funding reserve, drawdown/daily loss и стресс. Обязательные пределы LIVE задаются/одобряются владельцем; если не настроены — LIVE заблокирован. Net-нейтральность не заменяет gross/margin checks.
5. Округление quantity только вниз по фактическим filters; round-trip проверки после tick/step/minNotional и до отправки. Не увеличивать order quantity ради минимального номинала; невозможный proposal → NO_TRADE.
6. Ни мартингейл, ни скрытое перераспределение бюджета другой ноги. Встречная позиция не считается защитой или бесплатной маржой.
7. Любой риск-создающий action требует действующего пользователя/allowlist, точного summary, nonce/expiry/replay protection, audit event без секретов.
8. Секреты не в чате, Git, SQLite, `.env.example`, логах, frontend или relay. Для Windows setup предпочтителен Credential Manager/DPAPI; API keys с минимальными правами и без withdrawals.
9. Никаких гарантий доходности, maker fill, закрытия/исполнения до funding/ликвидации или вычисления безопасной цены ликвидации простой формулой.
10. Сканер и AUTO не обходят пользовательские bounds/hard risk limits. Отсутствующие/устаревшие данные не заменяются нулём.

### Существенные конфликтные/исправленные предположения

- «Нейтральная/безопасная» встречная сетка не является гарантированно delta-neutral и может увеличить gross exposure, margin, funding и стресс-убыток.
- Две стороны не независимы в бюджете риска: их worst-case исполнение оценивается вместе, хотя lifecycle fills и позиции разделены по ногам.
- Post-only не гарантирует maker execution или fill; в MVP нет неявного fallback на market/taker.
- Обычная остановка — drain до предусмотренного выхода, не обещание срока/цены/прибыли. Emergency market close — другое явное действие.
- Никакого простого P_liq и универсального правила «двойной бюджет безопасен»: нужны конкретный контракт, account mode, bracket, маржа и фактический биржевой snapshot; формула ликвидации не входит в safety proof.
- Значения из истории (`ATR` коэффициенты, шаги, lambda/веса, Z-score/ADX/funding пороги, cooldown, leverage, drawdown, TP/time stop, volume ranges и прочие) не утверждены этим этапом.

## 7. Сверка официальной документации

Проверены 2026-11-16 официальные страницы Binance USDⓈ-M Futures API, Telegram Mini Apps и Microsoft Windows security. Перед реализацией adapter требуется повторить сверку непосредственно по актуальным endpoint-схемам/изменениям; этот документ не утверждает, что торговый API реализован или полностью протестирован.

| Область | Зафиксированный факт/влияние на проект | Официальный источник |
| --- | --- | --- |
| Binance USDⓈ-M general API | Официальная API-документация; производственная база REST `fapi.binance.com`. Версии endpoint/time/rate limits повторно проверить перед кодом. | [General Info](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info) |
| Контракты/filters | Для отбора и округления нужны актуальные `exchangeInfo`, статус/тип каждого контракта и symbol filters; precision нельзя угадывать по числу знаков. | [Exchange Information](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data#exchange-information) |
| Position mode | API предоставляет текущий account position mode; это account-wide настройка. Бот только читает и блокирует несовпадающую конфигурацию, не переключает её автоматически. | [Get Current Position Mode](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account#get-current-position-mode), [Change Position Mode](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade#change-position-mode) |
| Hedge Mode order semantics | `positionSide` обязателен для Hedge Mode и принимает LONG/SHORT; `reduceOnly` не допускается в Hedge Mode. Следовательно, нельзя копировать generic reduce-only close logic; сторону/quantity/close semantics надо сверить и отдельно протестировать. | [New Order](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade#new-order) |
| Mark/funding | Mark price и funding fields нужно получать/показывать отдельно с timestamp и next funding time; funding interval может зависеть от контракта/изменений. Нельзя жёстко считать, что расчёт всегда каждые 8 часов. | [Mark Price](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data#mark-price), [Funding Rate Info](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/market-data/rest-api/Get-Funding-Info) |
| 24h quote volume | Universe scanner должен использовать контрактную валюту и биржевую `quoteVolume` rolling-метрики, а единицы разных quote assets нельзя молча смешивать. | [24hr Ticker Price Change Statistics](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data#ticker24hr-price-change-statistics) |
| Маржа/позиции | Sizing опирается на реальные account/position и symbol leverage brackets; margin зависит от notional/tier и account режима, простая оценка по плечу не является защитным расчётом. | [Account Information V3](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account#account-information-v3), [Position Information V3](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade#position-information-v3), [Notional and Leverage Brackets](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account#notional-and-leverage-brackets) |
| Private stream | User data stream требует keepalive; соединения требуют восстановления/reconciliation. REST snapshot остаётся обязательным после потери/рестарта. | [Start User Data Stream](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/user-data-streams#start-user-data-stream), [WebSocket Connect](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/user-data-streams/Connect) |
| Telegram Mini App | `initData` следует верифицировать сервером подписью HMAC, проверять `auth_date`; не доверять неподписанному JS user id. Frontend требует URL, который доступен клиенту Telegram; один localhost не является решением для внешнего Mini App. | [Telegram Mini Apps](https://core.telegram.org/bots/webapps) |
| Windows secrets | Microsoft рекомендует Windows Credential Manager и/или DPAPI; секрет нельзя hardcode или сохранять открытым в config/logs/БД. | [Windows password/credential handling](https://learn.microsoft.com/en-us/windows/win32/secbp/handling-passwords), [CryptProtectData](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata) |

Проверка в этом этапе — документальный аудит официальных источников; Binance endpoint не вызывались, live/testnet поведение не проверялось. Документация биржи меняется: повторная сверка и запись версий/даты обязательны в этапе 3 до adapter.

## 8. Threat model / data flow

### Активы и зоны доверия

- Наиболее ценные данные: Binance API key/secret, Telegram bot token, возможные relay credentials, локальная база positions/orders/fills/config, команда на открытие/закрытие позиции.
- Доверенная зона исполнения: локальный Windows-процесс, policy/risk engine, local loopback API, локальный защищённый secret store.
- Недоверенная зона: Mini App/browser/Telegram callback payload, внешняя сеть, опциональный relay/hosting, market feed до проверки свежести/последовательности, произвольные внешние позиции и ордера.
- Relay (если будет выбран) — только транспорт и минимум данных, без Binance secret и без права обходить локальную политику.

### Угрозы и меры

| Угроза | Минимальная мера / поведение при отказе |
| --- | --- |
| Подмена Telegram user/user_id или initData, устаревшее/replayed подтверждение | Серверная подпись и срок `auth_date`, Telegram numeric-ID allowlist, nonce/idempotency/expiry для каждой команды; отказ без изменения состояния. |
| Компрометация API key/log/DB/relay | Минимальные права, withdrawals off, Credential Manager/DPAPI, redaction, никакого секрета в БД/relay/UI; при credential uncertainty — остановить LIVE и потребовать локального вмешательства. |
| Случайный TEST→LIVE или неподтверждённая команда | Раздельные adapter/config/secret scope, LIVE off-by-default, local arming и подтверждение proposal; TEST code-path не имеет live mutation capability. |
| Replay, гонки Start/Stop/Close, duplicate callbacks | Сериализация команд, короткоживущие подтверждения, уникальные nonce/client order IDs, журналировать intent до сети; неизвестный исход reconciliate, не повторять blind. |
| Внешний/ручной ордер или позиция, утерянное событие | Не усыновлять и не закрывать автоматически; halt entries, REST reconcile, объяснить конфликт. |
| Stale/manipulated/incomplete market/account data, clock drift | Source timestamps, freshness/sequence checks, server time check; fail closed, risk-reducing actions only по определённой policy. |
| Relay/frontend offline, локальная машина спит/выключена | Честный `AGENT_OFFLINE`; не ставить отложенную команду увеличения риска; документировать биржевые остаточные ордера и restart reconciliation. |
| Локальный API DNS rebinding/CSRF/CORS/unauthorized clients | Bind loopback, Host/Origin validation, строгая auth, ограниченный CORS/CSRF и rate limits; не выставлять raw API в интернет. |
| Повреждение/блокировка БД, crash после exchange ACK | Durable intent до запроса, transaction/unique keys, backup/migration plan, single instance lock; startup только через reconciliation. |

## 9. Открытые пользовательские решения

Вопросы не блокируют доменную модель и paper/test scaffolding, но должны быть решены прежде соответствующего функционала:

1. **Hosting/relay Mini App:** согласие на HTTPS frontend и outbound authenticated channel/relay или альтернативная схема; кто размещает, обслуживает и оплачивает. Не открывать роутер и не публиковать unauthenticated localhost API.
2. **LIVE hard limits:** абсолютный и процентный allocation cap, per-symbol/account/gross/net exposure, margin/leverage bounds, max drawdown/daily loss, open-order reserve, funding policy и поведение каждого breaker. До ответа LIVE запрещён.
3. **Binance account:** dedicated account/subaccount, выбранный margin mode и position mode; бот не меняет account-wide настройки автоматически. До reconciliation setup LIVE блокируется.
4. **Долгий drain:** оставить позицию до штатного выхода/ручного решения (исходный безопасный вариант) или отдельно согласовать time-stop/force close. Не вводить скрытый market exit.
5. **AUTO bounds/timeframes:** если владелец желает параметры сверх явно предусмотренных, согласовать min/max и алгоритм для каждого AUTO-поля; AUTO отсутствующих данных запрещён.

Секреты пользователю в чате не запрашивать. Позже setup flow должен вводить их локально через защищённый интерфейс.

## 10. Критерии готовности этапа 0 и план проверок

Этап 0 считается завершённым, если:

- обследована папка и precedence документов зафиксирован;
- MVP scope, glossary/source-of-truth и схема потоков документированы;
- state machine, paired-grid lifecycle, drain/close/rotation/recovery и hard guards зафиксированы;
- конфликты исходных формул и непроверенные параметры выделены;
- официальные ссылки и дата предварительной сверки API/Telegram/Windows security записаны;
- threat model, открытые вопросы владельцу и тестовая стратегия заданы;
- к Binance не отправлялись запросы/ордера и разработка не заявлена.

### Базовый тест-план следующих этапов

- **Unit/property:** Decimal precision; ATR/TR warmup, gaps и stale inputs; веса/суммы/границы; округление filters вниз; side/price geometry; бюджет до/после округления; PnL/fills/fees/funding; close quantity; hard limit invariants.
- **State/concurrency:** парные заявки, partial/simultaneous fills, неизвестный create/cancel, duplicate/out-of-order events, Start vs Stop, TEST/LIVE switch, re-anchor cancel/reconcile, drain/close only after flat verification.
- **Paper/replay:** тренд без отката, скачок через уровни, spread/fees выше цели, partial exits, Post-Only rejection/non-fill, filter/balance/funding change, stale market/private feed, disconnect/restart/crash/disk issues; детерминированное воспроизведение.
- **TEST isolation/security:** assert что TEST не вызывает endpoint создания/отмены/изменения биржевых ордеров; invalid/expired/replayed Telegram initData/callback, spoofed IDs, loopback authorization, CSRF/CORS/Host, secret redaction, relay/offline.
- **Exchange adapter (позже):** только fake/replay автоматически; Testnet smoke test — лишь отдельное ручное opt-in после проверки endpoint/keys/environment, никогда не LIVE. Сверить account mode, symbol filters, market/private streams, reconnect, unknown outcome, reconciliation, hedge close semantics.

## 11. Следующее безопасное действие

Этап 0 завершён как проектное обследование. Ревизии 2026-11-18 подтвердили, что его границы не изменены: реальные ордера не отправлялись, секреты не запрашивались, LIVE выключен.

**Этап 1 выполнен** в объёме мастер-промпта: Decimal-домен, контрактные filters, направленное округление, бюджеты/риск обеих ног, paired-grid proposal, fees/funding и cost-aware exits, TR/ATR/V_idx, веса/выбор N, статический AUTO/MANUAL, close quantity, quote-volume gate. Ошибки/неполные данные дают `Rejection` или `DomainError`, proposal не создаётся. Детали — [`phase-1.md`](phase-1.md).

**Этап 2 выполнен** в TEST/paper-объёме: read-only public Binance REST adapter с внедряемым transport, paper-счёт с виртуальным балансом, conservative maker fills с очередью и частичными исполнениями, fees/funding, paired-grid lifecycle, drain/close market, обновление filters, restart и reconciliation. `live_order_port()` не реализован, engine отклоняет LIVE-proposal. Детали — [`phase-2.md`](phase-2.md).

**Этап 3 core выполнен:** HMAC-подпись, server time sync, metadata/filters/price bands, account snapshots с canTrade/multi-assets guard, open orders/positions, leverage brackets, mark price, LIMIT GTX с Hedge Mode и локальной проверкой filters/exit quantity, rate-limit/unknown-outcome handling, reconciliation, stream-события с health/backoff, listen-key timing и WebSocket-транспорт с reconnect-сессией. LIVE-торговля заблокирована; Testnet включается только явным `enable_trading` и после `preflight`. Ручной Testnet smoke **не выполнялся** — нужен opt-in владельца. Детали — [`phase-3.md`](phase-3.md), endpoints — [`binance-api.md`](binance-api.md). Полный набор: 142 теста и компиляция без сети.

Следующий безопасный шаг — ручной Testnet smoke после вашего подтверждения (выделенный аккаунт, ключи без withdraw, HEDGE/margin mode, повторная сверка документации). После него — **этап 4: Dashboard и Telegram Mini App/relay**. До Testnet/LIVE-готовности действуют ограничения: TEST/paper — единственный исполняемый путь, LIVE выключен, обязательные решения владельца — в §9.
