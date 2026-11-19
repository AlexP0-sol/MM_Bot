# Этап 1 — доменная модель и расчёты без сети

**Проект:** локальный адаптивный grid-бот Binance USDⓈ-M Futures  
**Дата:** 2026-11-18  
**Статус:** этап 1 выполнен. В самом этапе 1 сетевого, приватного Binance- и ордерного кода нет; публичный read-only adapter появился позже на этапе 2 — [`phase-2.md`](phase-2.md).  
**Основание:** `itog_master_prompt_binance_grid_bot.md`, раздел 12 «Этап 1», и `docs/phase-0.md`.

## 1. Что реализовано

| Файл | Содержимое |
| --- | --- |
| `src/gridbot/domain.py` | чистые Decimal-типы, контрактные filters, округление, бюджеты/риск, paired-grid proposal, PnL/fees/funding, cost-aware exits, TR/ATR/V_idx, веса и выбор N, AUTO/MANUAL, close quantity, quote-volume gate |
| `tests/test_domain.py` | 36 unit/property-тестов на precision, границы, обе ноги, округление, отказы, close semantics, отсутствие сетевых импортов |
| `pyproject.toml` | пакет `mm-gridbot`, Python ≥ 3.12, без runtime-зависимостей |

Публичные элементы домена: `ExchangeFilters`, `QuoteVolumeRange`/`QuoteVolumeSnapshot`, `evaluate_quote_volume`, `Candle`, `true_ranges`, `wilder_atr`, `ATRValue`, `relative_volatility_index`, `normalized_weights`, `rounded_price`, `order_notional`, `CostAssumptions`, `expected_net_pnl`, `expected_net_pnl_for_fills`, `cost_aware_exit_price`, `validate_fee_edge`, `RiskLimits`, `RiskModel`, `AccountSnapshot`, `AllocationPolicy`, `calculate_side_budgets`, `allocate_side_budget`, `check_exposure`, `GridParameters`, `build_paired_grid`, `GridProposal`/`OrderProposal`, `select_level_count`, `resolve_parameter`, `calculate_close_quantity`, `Result`/`Rejection`/`RejectionCode`, `DomainError`.

## 2. Соответствие критериям этапа 1

| Требование мастер-промпта | Реализация | Проверка |
| --- | --- | --- |
| Decimal types и явные денежные единицы | все money/price/qty поля — `Decimal`; float/NaN/Infinity отклоняются, а не приводятся молча | `test_non_finite_values_reject_at_domain_boundaries`, `test_non_decimal_money_is_rejected_not_coerced` |
| Контрактные метаданные | `ExchangeFilters`: tick, step, min/max qty, min/max notional, contract multiplier, base/quote asset, contract type, trading flag | `test_min_notional_rejects_instead_of_increasing_quantity`, `test_symbol_and_effective_auto_values_must_be_explicit` |
| Symbol rounding | quantity только вниз к `stepSize`; Long BUY вниз, Short SELL вверх к `tickSize`; notional пересчитывается после округления | `test_directional_price_rounding_is_passive`, `test_price_rounding_requires_a_side_and_never_reverses_direction`, `test_paired_test_grid_and_downward_quantity_rounding` |
| Budget/risk | allocation = min(cap, deployable − reserve); обе ноги, gross, абсолютный net, stress, initial margin, per-leg лимиты, свежий доступный collateral | `test_account_budget_is_capped_and_reserve_protected`, `test_combined_gross_and_stress_guards_include_both_legs`, `test_initial_margin_cannot_exceed_fresh_account_collateral`, `test_per_leg_stress_budget_is_enforced_independently` |
| Paired grid proposal | единая all-or-nothing операция для Long и Short от одного anchor/snapshot; дубли уровней после округления, marketable entry и превышение бюджета отклоняются | `test_paired_test_grid_and_downward_quantity_rounding`, `test_rounded_duplicate_grid_levels_reject`, `test_non_marketable_constraints_and_invalid_book_reject` |
| Fees/funding model | явные effective entry/exit rates; signed funding (плюс — издержка, минус — rebate); PnL по фактическим entry fills с индивидуальными ставками | `test_pnl_sign_and_fees_are_side_correct`, `test_fill_level_pnl_uses_each_entry_fee_and_signed_funding` |
| Cost-aware exits | целевая цена выхода решает линейное уравнение net PnL (вход/выход fees, funding, buffer, required profit), пассивно округляется, проверяется по min/max notional и повторно проверяется hurdle | `test_cost_aware_exit_clears_hurdle_after_rounding`, `test_paired_proposal_rejects_unreachable_cost_hurdle`, `test_unreachable_short_hurdle_rejects` |
| Pure deterministic functions | нет импортов сети, времени, random, OS, async; только `dataclasses`/`decimal`/`enum`/`typing` | `test_domain_module_is_pure_and_has_no_network_or_clock_imports` |
| Статический AUTO/MANUAL evaluator | `resolve_parameter`: bounds, свежесть source timestamp, algorithm version, explanation; AUTO без данных/объяснения не разрешается | `test_auto_manual_resolution_is_bounded_and_fresh`, `test_auto_parameters_require_explainability` |
| Explainable validation errors | каждый отказ — `Result(rejection=...)` с `RejectionCode` и причиной; неверная конструкция — `DomainError` | все negative-тесты выше |
| Unit/property tests (precision, weights, limits, обе ноги, объём, округление, close) | покрыто; веса проверяются циклом N, суммы весов точны | `test_geometric_weights_sum_exactly_and_are_bounded`, `test_close_quantity_rounds_down_and_never_reverses`, `test_quote_volume_gate_is_unit_explicit_fresh_and_inclusive` |
| Готово: неверные/неполные данные не создают proposal | `build_paired_grid` возвращает `Result` без `value` при любом нарушении; нет fallback-значений | `test_stale_or_future_market_snapshot_fails_closed`, `test_paired_budget_cannot_exceed_account_allocation_cap`, `test_each_side_budget_is_checked_against_its_own_allocation` |

Канонический конвейер раздела 5.3 мастер-промпта реализован для TR/ATR и `V_idx`; шаг/spacing и λ-формула не вычисляются (см. §4).

## 3. Зафиксированные инварианты

- Неизвестное, устаревшее, чужое (другой режим/счёт), неполное или немоделируемое состояние → отказ, а не «ноль» и не догадка.
- После округления заявка не превышает свой weight-budget; остаток не перераспределяется и quantity не увеличивается ради `minNotional`/`minQty`.
- Long entry строго ниже best ask, Short entry строго выше best bid; округление цены не делает заявку taker.
- `sum(weights) = 1` точно; веса положительны; расстояния положительны и строго возрастают.
- Net exposure не отменяет gross/stress/margin проверки; обе ноги считаются вместе и по отдельности.
- Закрывающий объём округляется вниз и не может превысить или перевернуть позицию.
- `AUTO` не подставляет значение без свежего source timestamp, bounds и объяснения.
- Один и тот же вход даёт один и тот же proposal (нет времени, сети и случайности внутри домена).

## 4. Явные соглашения и отклонения

1. **Один модуль вместо дерева пакетов.** Мастер-промпт задаёт структуру `domain/`, `indicators/`, `strategy/`, `risk/` как рекомендуемую; `phase-0.md` фиксирует «создавать bounded contexts по мере надобности, без пустых пакетов». На этапе 1 весь код чистый и без I/O, поэтому он собран в `gridbot/domain.py`. Разделение на контексты выполняется на этапе 2–3, когда появляются market data, paper execution и адаптеры.
2. **ATR anchor.** Для seed нужны `p` истинных TR, каждый — с предыдущим close. Поэтому `wilder_atr` требует `p + 1` завершённых свечей: первая — anchor, её range `high − low` не входит в seed; `warmup_bars = p + 1`. Интервалы свечей должны точно совпадать с 1m/5m/15m/1h и быть выровнены к сетке интервала; последняя свеча — закрыта и не старше лимита. `V_idx` сравнивает только одинаковые timeframe/method/reference/bar timestamp.
3. **Стоимость — явные суммы на моделируемый order cycle.** `entry_fee_rate`/`exit_fee_rate` — effective ставки фактического типа заявки (maker/taker задаёт вызывающий код); `execution_buffer_quote`, `funding_cost_quote`, `required_net_profit_quote` — абсолютные суммы в quote на один моделируемый round-trip, не ставки и не доли веса. Домен не предполагает 8-часовой funding и не конвертирует ставку в сумму: это делает adapter/strategy по реальному расписанию Binance.
4. **AUTO — только статическая резолюция.** `lambda`, spacing, период ATR и прочие коэффициенты не выводятся: мастер-промпт требует versioned-параметры и replay-оценку, а план относит AUTO шаг/N/веса к этапу 5. `resolve_parameter` проверяет уже рассчитанный кандидат; hysteresis/cooldown и перебор N по сеткам — этап 5.
5. **Метаданные биржи не загружаются.** `ExchangeFilters` — явный вход; leverage brackets, maintenance margin, price bands, max position и реальные fee tiers появятся вместе с adapter на этапе 3. `RiskModel` принимает явные доли stress/margin, а не выводит их из плеча.
6. **LIVE-предложение не означает LIVE-допуск.** Домен строит proposal для любого переданного режима, но не имеет ни arming, ни ордерных методов; gating LIVE — этапы 3–4 и решение владельца.
7. **`minNotional`/`minQty`.** Заявка ниже минимума отклоняется (`EXCHANGE_FILTER`); увеличение quantity и `NO_TRADE`-fallback отсутствуют. Уровень, не проходящий cost hurdle, отклоняет всю generation (`COST_HURDLE`), а не только себя.
8. **Явные границы модели.** `symbol` обязан совпадать с `base_asset + quote_asset` (иначе `EXCHANGE_FILTER`); `initial_margin_fraction` ограничена `(0, 1]` как доля notional, а `stress_loss_fraction` может превышать 1; `exit_fee_rate < 1`, иначе линейная целевая цена не определена. Эти проверки защищают от несогласованных metadata, а не задают стратегические значения.

## 5. Выполненные проверки

```text
cd MM_BOT
PYTHONPATH=src python -X utf8 -m unittest discover -s tests -v
  → Ran 36 tests ... OK
python -X utf8 -m compileall -q src tests
  → exit 0
```

Дополнительно: `__pycache__` после проверок удалены; сетевые вызовы не выполнялись; Binance endpoint не использовались.

## 6. Что не покрыто этапом 1

- Реальные Binance candles/метаданные, leverage brackets, price bands, max position, фактический funding schedule и fee tier — этап 3.
- Partial fills, позиции, exit lifecycle, drain/close, restart/reconciliation — этап 2–3.
- Session-level circuit breakers (drawdown, daily loss, feed health), state machine, подтверждения и аудит — этап 2 и последующие; домен проверяет только переданные ему hard limits.
- Paper/replay-симуляция исполнения, queue uncertainty, latency, rejects — этап 2.
- AUTO-вывод λ/spacing/N и hysteresis/cooldown — этап 5 после replay.
- Property-based тесты на Hypothesis (сейчас детерминированные циклы), mutation/fuzz-тесты — этап 6.
- Любая проверка прибыльности стратегии: домен проверяет только корректность ограничений и cost hurdle.

## 7. Оставшийся риск

- Линейная quote-settled модель PnL верна для USDT/USDC-перпетуалов с multiplier; контрактные особенности Binance нужно подтвердить на этапе 3.
- Fee/funding amounts задаёт вызывающий код: неверные значения неотличимы от верных внутри домена, поэтому этап 3 обязан сверять их с биржей.
- Округление к tick/step может сделать сетку невыполнимой на грубых filters — это осознанный fail-closed исход (`NO_VALID_LEVEL_COUNT`/`EXCHANGE_FILTER`), а не дефект.
- Домен не является доказательством безопасности LIVE: до реализации gating, reconciliation и подтверждений LIVE-предложения нельзя выставлять.
- Открытые решения владельца из `phase-0.md` §9 (LIVE-лимиты, hosting/relay, position/margin mode, drain-политика, AUTO bounds) по-прежнему обязательны для соответствующих этапов.
