# 01 — Устойчивый experimental-адаптер расписания

**Требования:** R01, R02, R03, R04, R05, R06, R07, R08, R09, R10, R12, R13, R14, R18, R19, R20, R21, R22, R23
**Blocked by:** нет
**Зона:** scheduling.py, delivery.py, tests/test_scheduling.py, при необходимости tests/test_delivery.py
**Волна:** 1
**Status:** ready

## Что должно заработать

Локальный разбор даты/настройки и review/execute цепочки suggested post → durable proposal → approve → durable schedule. Самоодобрение не доказано: experimental opt-in, честные ошибки, no fallback. Полный public API для CLI и UI. Не менять tool/dashboard/web/docs.

## Из брифа дословно

> «только после устойчивого подтверждения сообщает “Запланирован”»
> «При timeout/обрыве не создавать повторный пост автоматически»
> «Сохранять подтверждения устойчиво до производных HTML/SQLite-проекций»

## Спецификация

spec.md §1–5 и §8; api-research.md — документированные ограничения. interfaces.md целиком.

## Приёмка

- configure/get_settings без token, явные channel/dm/topic/IANA-zone. parse_schedule_time(value, timezone, now=None) принимает ISO и today/tomorrow Russian subset, отклоняет ambiguity/DST folds/gaps/диапазон.
- review связывает exact version/bot/channel/DM parent/topic/date/zone/source settings/publication anchor/media. execute перепроверяет всё под write lock и общей delivery.lock.
- sendRichMessage только DM+topic+Rich payload+suggested_post_parameters.send_date. Нет price/paid broadcast/классических media. Проверяет proposal identity/state; proposal receipt до approve. Строго True подтверждает scheduled. Нельзя считать неизвестный ответ успехом.
- Каждый шаг durable до projection/HTML. recover без сети, перебитые sending/approving → unknown. Никакого автоматического продолжения approve после crash и повторной отправки.
- Блокировка активных scheduled/proposed/unknown/sending/approving между Scheduling и Delivery в обе стороны. Несколько reviewed не должны создавать несколько публикаций. Блокировки не отключаются локальной правкой версии.
- list без token с безопасной projection для UI: schedule ID, статус, версия, канал/название, send_at/zone/send_date, proposal ID, published message ID при отдельном подтверждении, source/evidence, last_error, experimental/live_verified=false. Ошибка не утечёт в HTML.
- reconcile локально по evidence, не имитирует API. Конфликт/известный receipt не стирается. Published должен работать с verify_visual текущего опубликованного варианта либо явно запретить до подтверждённого publication anchor. cancelled только по явному проверенному факту; отсутствие не заявлять по времени.
- cancel/reschedule approved возвращают unsupported без API/секрета. Не deleteMessage/decline как недоказанная отмена.
- Значимые тесты success/rights/stale/hash/timeouts обоих шагов/crash recover/repeats/concurrency/guard/unsupported/reconcile/media и прежний полный прогон зелёный.

Верни краткий контракт ≤25 строк с FILES, TESTS, INTERFACES, REQUIREMENTS, CONCERNS, BLOCKERS. Не коммить. Не меняй interfaces.md сам.
