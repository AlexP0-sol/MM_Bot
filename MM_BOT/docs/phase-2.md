# Этап 2 — Paper engine и сценарии

**Проект:** локальный адаптивный grid-бот Binance USDⓈ-M Futures  
**Дата:** 2026-11-18  
**Статус:** этап 2 выполнен в TEST/paper-объёме; live order endpoint отсутствует.  
**Основание:** `itog_master_prompt_binance_grid_bot.md`, раздел 12 «Этап 2», и `docs/phase-1.md`.

## 1. Файлы этапа

| Файл | Содержимое |
| --- | --- |
| `src/gridbot/exchange.py` | контракты биржи: `MarketTick`, `OrderRequest`, `OrderSnapshot`, `Fill`, статусы, `OrderPort`; `live_order_port()` всегда выбрасывает `LiveTradingUnavailable` |
| `src/gridbot/paper.py` | `PaperAccount`, `PaperExchange`: виртуальный баланс, Long/Short-позиции, conservative maker fills, частичные fills, maker/taker fees, funding, market close, снимок состояния |
| `src/gridbot/engine.py` | `PaperGridEngine`: paired-grid lifecycle, entry/exit intents, cancel/drain, market close, обновление filters, reconciliation, restart |
| `src/gridbot/market_data.py` | read-only public Binance USDⓈ-M REST adapter с внедряемым HTTP-transport, `ReplayFeed` |
| `tests/test_paper.py` | 19 тестов paper venue |
| `tests/test_engine.py` | 27 тестов lifecycle, сценариев и recovery |
| `tests/test_market_data.py` | 8 тестов нормализации, свежести, replay |
| `tests/support.py` | детерминированные fixtures (proposal, tick, config) |

## 2. Соответствие этапу 2

| Требование мастер-промпта | Реализация |
| --- | --- |
| Реальные public Binance data, read-only | `BinancePublicMarketData`: `/fapi/v1/ticker/bookTicker`, `/fapi/v1/aggTrades`, `/fapi/v1/klines`; только HTTPS, без подписи и ключей; закрытые klines (последняя незавершённая отбрасывается) |
| Paper execution с виртуальным балансом | `PaperExchange` + `PaperAccount`: cash, отдельные позиции Long/Short, realized/unrealized, fees/funding ledger |
| Fake/replay adapter | `ReplayFeed` с детерминированной выдачей тиков |
| Conservative maker fills | fill только по наблюдаемой агрессорной сделке на своей стороне; цена исполнения — собственный limit, не лучше; объём одной сделки распределяется между ордерами, а не переиспользуется каждым уровнем |
| Queue uncertainty | явный `queue_ahead`; касание цены не исполняет заявку, пока очередь не выкуплена; сделка строго «через» limit обнуляет очередь |
| Partial fills | частичное исполнение с корректным remaining и повторными fills |
| Commission | maker rate на limit-fills, taker rate на market close; fee в quote вычитается из баланса, учитывается отдельно и входит в cost-aware exit target |
| Funding | явный вызов `apply_funding(rate, ts)`: положительная ставка — Long платит, Short получает; сумма и событие фиксируются |
| Spread | каждый тик содержит bid/ask; post-only проверяется по актуальному стакану; market close платит bid/ask + явный slippage в тиках |
| Latency | `activation_ts = created_ts + latency_ms`; до активации fill невозможен |
| Rejects | post-only would-cross, tick/step/minQty violation, insufficient reserve-inclusive margin, exit > position, no market snapshot, out-of-order tick |
| Downtime | stale tick переводит engine в `PAUSED`; обработка не идёт до свежего тика |
| Order state machine | `NEW → PARTIALLY_FILLED → FILLED`, `CANCELED`, `REJECTED`; терминальные заявки не отменяются повторно |
| Paired grid / positions | обе стороны одной generation; отдельные позиции и exit на каждую ногу |
| Cancel/stop/drain | `drain` отменяет entries, сохраняет exits; `close_market_and_stop` отменяет конфликтующие ордера и закрывает остаток |
| Force-close simulation | market close по bid/ask с taker fee и явным slippage |
| Restart/reconciliation | JSON-снимок exchange/engine, восстановление, сверка неизвестных/потерянных ордеров и необработанных позиций |
| Готово: детерминированные повторяемые тесты | полный набор проекта на момент проверки — 142 теста, без внешней сети и случайности |
| Готово: нет двойных intents/потерянных fills | детерминированные client IDs, идемпотентный duplicate, state guard, журнал fills |
| Готово: live endpoint недоступен из TEST | engine принимает только `PaperExchange` и отклоняет `RunMode.LIVE`; `live_order_port()` не имеет реализации |

## 3. Модель исполнения (явные правила)

1. **Никакого fill по касанию.** Для BUY нужна сделка с aggressor `SELL` и `last ≤ limit`; для SELL — `BUY` и `last ≥ limit`.
2. **Очередь.** Сначала выкупается `queue_ahead`; только остаток объёма сделки исполняет заявку. Сделка строго лучше limit (`last < limit` для BUY) означает, что цена прошла через заявку, и очередь обнуляется.
3. **Цена fill = limit заявки.** Более выгодная цена сделки не завышает результат.
4. **Объём fill ≤ объём сделки** и ≤ remaining; один trade-объём расходуется между ордерами последовательно; exit дополнительно ≤ фактической позиции.
5. **Maker/taker.** Limit fills — maker; market close — taker. Ставки задаются в `PaperConfig`, значения по умолчанию отсутствуют.
6. **Funding** не начисляется автоматически: у paper venue нет биржевого расписания. Вызывающий код передаёт ставку и timestamp; событие попадает в `funding_events`.
7. **Market close** исполняется по `bid − slippage_ticks × tick_size` для Long и `ask + slippage_ticks × tick_size` для Short; запрос больше позиции отклоняется (не обрезается молча); при отсутствии стакана — отказ.
8. **Valuation** для margin/equity/funding — середина bid/ask. Это не Binance mark price; замена — этап 3.

## 4. Lifecycle и принятые решения

- **TEST-only.** `start` отклоняет proposal с `RunMode.LIVE` через `UnsupportedModeError`; live-порт физически отсутствует.
- **Одна generation.** `start` работает только из `IDLE`/`STOPPED` и только на плоской площадке без открытых ордеров и позиций.
- **Идемпотентность.** Client ID: `{generation_id}:{SIDE}:{level}:ENTRY` и `{generation_id}:{SIDE}:TP:{attempt}`. Повторный `place` с тем же ID возвращает существующий intent без второго fill.
- **Reject при постановке.** Если хотя бы один entry отклонён, уже принятые entries отменяются, generation не активируется, состояние — `RECONCILIATION_REQUIRED`.
- **После первого fill ноги** оставшиеся entry-уровни этой ноги отменяются. Мастер-промпт разрешает отмену, если полный worst-case не гарантирован; выбран консервативный вариант. Gap-сценарий исполняет все уровни в одном тике до отмены.
- **После каждого fill и каждого тика** пересчитывается combined exposure: фактические позиции по текущему mid плюс все оставшиеся entries. Если gross/net/stress/margin контекста нарушены, entries обеих ног отменяются, engine переходит в `PAUSED` с `COMBINED_RISK:*`; открытые exit-ордера сохраняются.
- **Один агрегированный exit** на фактическую позицию ноги; цена считается `cost_aware_exit_price` по VWAP с фактической средней entry fee rate (из ledger), maker exit rate, funding/buffer/required из `ExitPolicy`.
- **Post-only exit.** Если цена выхода стала marketable, ордер отклоняется; market fallback отсутствует, engine переходит в `PAUSED`.
- **Exit hurdle невозможен** → `PAUSED`, все entries отменяются, позиция остаётся без нового риска.
- **Partial exit** уменьшает позицию и remaining одного и того же exit; новый ордер не создаётся, если remaining совпадает с позицией.
- **Закрытая нога не перезапускается**, пока generation жива: её entries отменены и не восстанавливаются; противоположная нога продолжает работу.
- **Drain** отменяет entries обеих ног, сохраняет exits; `STOPPED` — только когда обе позиции нулевые и открытых ордеров нет.
- **Close market and stop** требует свежий market snapshot: при stale/отсутствующем стакане действие не исполняется, состояние — `RECONCILIATION_REQUIRED`. Иначе отменяет entries и exits, закрывает фактический остаток; `STOPPED` — только после подтверждённой плоскости.
- **Изменение filters** отменяет entry-ордера, которые перестали проходить; если действующий exit перестал проходить по tick/step/notional, engine не отменяет его молча, а переходит в `PAUSED` с `EXIT_FILTER_VIOLATION`.
- **Reconciliation** считает конфликтом неизвестный ордер, потерянный известный ордер, необъяснённую fills позицию (в любом состоянии) или позицию в `IDLE`/`STOPPED`; состояние → `RECONCILIATION_REQUIRED`.
- **Restart** восстанавливает exchange и engine из JSON-снимка. До успешного `reconcile()` восстановленный engine не обрабатывает тики и не выполняет drain/close/filters; только затем возобновляется обработка рынка.
- **Выход из `RECONCILIATION_REQUIRED`** — только явный `resume_after_reconciliation()` после успешной сверки, нулевых позиций и отсутствия открытых ордеров; автоматического resume нет.

## 5. Покрытые сценарии

| Сценарий | Тест |
| --- | --- |
| Тренд без отката | `test_trend_without_pullback_keeps_the_short_exit_resting` |
| Гэп через все уровни | `test_gap_through_levels_uses_vwap_for_a_single_exit` |
| Нет исполнения ни одной стороны | `test_no_fill_drain_cancels_everything_and_stops` |
| Закрытие одной ноги не перезапускает её | `test_one_side_flat_does_not_rearm_that_side` |
| Одновременные Long/Short позиции | `test_simultaneous_positions_close_market_and_stop` |
| Stale feed и возврат к свежим данным | `test_stale_tick_pauses_and_a_fresh_tick_resumes` |
| Partial exit | `test_partial_exit_keeps_the_remaining_target_and_does_not_reverse` |
| Издержки выше цели | `test_exit_hurdle_failure_pauses_without_market_fallback`, `test_paired_proposal_rejects_unreachable_cost_hurdle` (этап 1) |
| Изменившийся `minNotional` | `test_filters_update_cancels_only_entries_that_now_violate_bounds` |
| Падение баланса/mark | `test_mark_to_market_loss_reduces_available_margin`, `test_margin_is_checked_against_fresh_paper_equity` |
| Очередь и частичные fills | `test_touch_does_not_fill_until_explicit_queue_is_consumed`, `test_partial_then_full_fill_charges_maker_fees` |
| Один trade-объём на несколько ордеров | `test_one_trade_volume_is_shared_across_matching_orders` |
| Резерв маржи открытыми entries | `test_open_entry_orders_reserve_margin_before_fill` |
| Combined risk после fill/mark | `test_mark_to_market_breach_cancels_the_other_legs_entries` |
| Entry fee в exit target | `test_exit_target_includes_the_actual_entry_fee` |
| Восстановление после reject | `test_rejected_entry_can_be_explicitly_recovered_after_reconciliation` |
| Необъяснённая позиция | `test_unexplained_position_breaks_reconciliation` |
| Post-only reject | `test_post_only_rejects_crossing_order_and_keeps_it_visible` |
| Cancel/fill race | `test_cancel_prevents_later_fill_and_completed_fill_blocks_cancel` |
| Дублирование intent | `test_duplicate_client_id_is_idempotent_and_fills_once` |
| Restart/reconciliation | `test_restart_restores_state_reconciles_and_continues`, `test_unknown_external_order_breaks_reconciliation` |
| Новая generation после STOPPED без старых intents | `test_new_generation_after_stopped_uses_fresh_per_generation_ids` |
| Funding Long/Short | `test_funding_sign_is_position_side_aware` |
| Market close | `test_market_close_uses_bid_ask_with_explicit_slippage_and_taker_fee`, `test_close_market_refuses_a_stale_snapshot` |
| Public data normalization | `tests/test_market_data.py` (8 тестов) |

## 6. Выполненные проверки

```text
cd MM_BOT
PYTHONPATH=src python -X utf8 -m unittest discover -s tests -v
  → Ran 142 tests ... OK
python -X utf8 -m compileall -q src tests
  → exit 0
```

Тесты не обращаются к сети: HTTP transport подменён, replay-фиды детерминированы. Реальных Binance запросов и ордеров не было; `live_order_port()` не реализован.

## 7. Ограничения этапа и сознательные отклонения

- **REST polling, не WebSocket.** Этап 2 использует read-only REST-снимки; WebSocket-транспорт, user data stream и reconnect добавлены на этапе 3.
- **Valuation = mid, funding — вручную.** Mark price, funding schedule и next funding time появляются с adapter на этапе 3.
- **Нет загрузки metadata/leverage brackets/fee tiers.** Filters, ставки и margin fraction задаются явно; сверка с биржей — этап 3. Paper venue проверяет только собственные tick/step/minQty и reserve, не полные Binance filters.
- **Нет rate limits, order expiry, exchange reject codes.** Paper venue моделирует только свои отказы; реальные коды/лимиты — этап 3.
- **Состояние в памяти + JSON snapshot.** SQLite/journal/migrations — этап 6 (hardening); снимок уже покрывает restart-сценарий этапа 2.
- **Один символ и одна generation.** Мультисимвольность, scanner и ротация — этап 5.
- **Queue ahead — явная константа конфигурации**, а не калиброванная модель очереди по уровню стакана. Значение задаёт вызывающий код; это осознанно консервативное упрощение.
- **Нет повторного набора закрытой ноги.** Мастер-промпт допускает отдельный режим, но MVP его не включает.
- **Нет drawdown/daily-loss/health circuit breakers** уровня сессии — этап 6 и LIVE-гейты; engine проверяет только заданные ему границы и состояние.
- **Поля public payload и cancel/fill race требуют сверки на этапе 3.** Адаптер полагается на документированные `bookTicker.time` и `aggTrades.m` и падает fail-closed при их отсутствии; реальные коды отказа, частичные cancel/fill гонки и rate limits paper venue не воспроизводит.
- **Плоская структура `tests/`** вместо `unit/property/integration/...` из предварительного дерева мастер-промпта: осознанно до появления adapter- и security-тестов; разделение — этап 3/6.

## 8. Оставшийся риск

- Paper-модель не доказывает прибыльность и не воспроизводит реальную очередь, latency и проскальзывание; она лишь консервативно проверяет инженерную логику.
- Top-of-book market close не учитывает глубину стакана; slippage в тиках — явная, но не калиброванная оценка.
- Ошибочные fee/funding/margin-параметры неотличимы от верных внутри paper venue; сверка с Binance — этап 3.
- До появления adapter, reconciliation с реальной биржей и LIVE-гейтов результат этапа 2 нельзя трактовать как разрешение на реальные ордера.

## 9. Следующий этап

**Этап 3 — Binance adapter и отдельный opt-in Testnet:** account snapshots, market metadata, public/private streams, user data stream, time sync, REST reconciliation, реальные filters/лимиты/reject-коды, изоляция Testnet/Live и ручной smoke test. LIVE остаётся выключенным и требует решений владельца из `phase-0.md` §9.
