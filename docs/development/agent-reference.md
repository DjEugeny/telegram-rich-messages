# Техническая справка агента

Для изменения кода и восстановления существующих операций. Не читать вместо текущего ONBOARDING.md и не пересказывать новичку историю разработки. Планы, исследовательские адаптеры и результаты старых тестов не являются шагами настройки пользователя. Ниже сохранён технический снимок; текущий код и корневые инструкции имеют приоритет.

## Фактическая архитектура

- `core.py`: `Store(root).create/update/get/list`, `import_image/import_video/import_media(root, source)`, `validate_media(root, post)`, `compile_post(post) → markdown/html/media`. SQLite `data/state.sqlite3` в WAL — канонический источник; `versions` защищены SQL triggers от UPDATE/DELETE. Сохранение проверяет media, новая правка добавляет версию.
- `posts/POST_ID/v0001.json`, `.md`, `.html` — комплект экспорта версии; запись через временный файл и replace, ошибка сохранения откатывает комплект новой версии. Старые версии не меняются.
- `media.py`: `mp4_info(data) → (width, height, duration)`; структурная проверка ISO-BMFF, codec configuration, sample tables/offsets/timing и AVC NAL envelopes, без декодирования кадров.
- `demo_media.py`: `create_demo_media(root)` создаёт четыре дополнительных вымышленных v1 без изменения существующих записей; встроенный H.264 fixture `examples/fixtures/avc-96x160.mp4.base64` — 96×160, 1 s. Демонстрационный custom ID вымышленный.
- `delivery.py`: скрытый ввод/системное хранилище, транспорт Bot API, `Delivery.check/review/execute/recover/resolve/verify_visual`; таблицы `operations`, `operation_events`, `receipts` в той же БД. Межпроцессная OS-блокировка `data/delivery.lock`; захват операции и визуальная отметка согласованы с SQLite write lock. Review валидирует файлы по одному; execute собирает multipart в bytes.
- `scheduling.py`: `Scheduling(store, api=None).configure/get_settings/review/execute/list/recover/reconcile/cancel/reschedule`, `parse_schedule_time(value, timezone_name, now=None)`. Таблицы `schedule_settings`, `schedule_operations`, `schedule_events`, `schedule_receipts`, файловые receipts в data; общая delivery.lock, взаимный запрет активных доставок/расписаний. Review/execute требуют experimental opt-in; snapshot связывает версию/медиа/бота/назначение/дату. `list()` — безопасная проекция с `post_id`, без snapshot/fingerprint; `dashboard.build` берёт согласованную версию через `Store.get(post_id, version)`.
- `dashboard.py`: `build(root)` атомарно собирает `index.html`; `make_server/serve` обслуживают только loopback с Host/путевой allowlist и запретом symlink. Разрешены index, перечисленные web-ресурсы и локальные PNG/JPEG/MP4; HTTP API записи нет. MP4: `video/mp4`, GET/HEAD, один byte Range (206), некорректный диапазон (416); CSP разрешает только локальные assets/media и запрещает connect/object/form.
- `web/shell.html`, `web/app.js`, `web/style.css`, `web/fonts/`: автономный дашборд «Пост»/«История», embedded JSON, поиск/статус/даты/20 строк на страницу/календарь. Даты в локальной зоне браузера. Сохраняются выбранный пост и раздел; отказ localStorage не ломает страницу. Медиа сохраняют пропорции; slideshow — кнопки и клавиатура; video controls, pause при уходе от поста/слайда, fallback и ссылка оригинала. «Выбранный пост»/«Статус» — общий combobox с Onest, отступом стрелки и клавиатурным управлением; скрытый native select хранит значение/события. Фокус — 2 px ink для клавиатуры. Кнопки отправки нет.
- `tool.py`: CLI; `create` и `import` сохраняют локальный материал, `update` добавляет версию. Назначение в посте либо `data/settings.json`; `check` сохраняет проверенное назначение без токена. Повторяемые `--media/--image/--video` сохраняют общий порядок; `--media` определяет формат, алиасы ограничивают его. Один файл — одиночный блок, несколько — `collage`; `--media-layout slideshow|separate` меняет группировку. Default `--media-position before`, `after` добавляет файлы после блоков.
- `update --media …` без нового текста/JSON добавляет файлы к прежним блокам; `--text/--file` заменяет набор блоков текстом. Чтобы изменить текст и сохранить media, передай полный `--blocks`. `--blocks` задаёт полный порядок; дополнительные CLI-media добавляются с выбранной стороны.
- `tests/`: unittest core/dashboard/delivery/scheduling/schedule_cli; `tests/dashboard_fixture.py` — 130 вымышленных постов, `tests/rich_browser_fixture.py` — Rich Media fixture, `tests/schedule_browser_fixture.py` — состояния расписания. `examples/README.md` — CLI и канонический JSON.

## Исторический адаптер расписания: только восстановление

`schedule-config --channel … --dm-chat-id … --topic-id … --timezone IANA` заменяет весь набор настроек; разбор времени/список/recover/reconcile и отказ cancel/reschedule работают без сети/секрета. Для review нужны явные DM/topic. `schedule-time` принимает ISO с временем либо «сегодня/завтра/послезавтра в ЧЧ:ММ»; код ограничивает 300…2678400 секунд (31 сутки), DST-неоднозначность отклоняет. Нужна доступная база IANA для ZoneInfo, native Windows не проверен.

Новые `schedule-review`/`schedule-execute` отключены в CLI; далее — историческое описание внутренних API, не инструкция возобновлять опыт. `schedule-review POST_ID WHEN --experimental` ранее выполнял preflight direct messages/parent/прав `can_post_messages` и `can_manage_direct_messages`. Права темы подтверждает лишь отправка. Покажи версию/хеш/медиа, бота, канал/DM/topic, абсолютную дату и зону. После явного согласия — `schedule-execute OPERATION_ID --fingerprint VALUE --experimental`. Флаг/fingerprint не заменяют согласие. При изменениях нужен новый review; неизвестный результат запрещает повтор.

`proposed` — принято предложение без подтверждённого расписания, `scheduled` — устойчивое одобрение, `published` — только ручное свидетельство с channel message ID и evidence. ID предложения в DM не равен будущему ID поста. `schedule-recover` восстанавливает локальные receipts без сети, `schedule-list/build` Telegram не сверяют. После срока показывается «Время наступило; публикация не подтверждена», статус автоматически не меняется. UI различает текущую и согласованную версии и показывает сохранённый материал; публикационных кнопок нет.

`schedule-cancel/reschedule` возвращают unsupported без сети. `schedule-reconcile` не выполняет отмену в Telegram: known scheduled со schedule receipt нельзя пометить cancelled; absent разрешён только для unknown без proposal/schedule receipts. Не обходить guard новой операцией. Никакого polling/webhook/updates общего бота без отдельного разрешения.

## Конвенции и границы

Канонические блоки: `heading`, `paragraph`, `list`, `image`, `video`, `media_group`, `quote`, `details`, `table`, `button`. `media_group` содержит только image/video leaves, `layout: collage|slideshow`. Inline принимает строковый subset bold/italic/code/URL или массив строк/узлов `text|bold|italic|underline|strikethrough|code|link|custom_emoji`; вложенность до 16, вложенные ссылки запрещены. Исходный HTML экранируется; метаданные выводятся через textContent. Не вставляй произвольный HTML и не обходи compiler. Вход из UTF-8 файла указывается явно; не трактуй строку текста как путь.

Unicode emoji (ZWJ/skin tone/variation selectors) сохраняются без нормализации. Custom emoji требует явный числовой `custom_emoji_id` (строка 1–32 цифры) и непустой fallback `text` без переноса, до 64 символов; ID не угадывается по символу. Rich Markdown содержит `tg://emoji?id=…`, HTML — доступный fallback без внешней анимации. Quote компилируется в `>`/`blockquote`.

Материалы атомарно копируются в `media/` под уникальными именами, оригиналы не меняются. В версиях — относительный путь, MIME, SHA256, source, размеры; у video обязательна конечная положительная duration в секундах. Перед сохранением/доставкой перепроверяются содержимое, хеш, metadata и нахождение внутри media без symlink. Прямой HTTP(S) URL принимается без редиректов; это не обход страницы.

Изображения до 10 MiB: PNG без interlace, JPEG 8-bit baseline/progressive grayscale/RGB с Huffman; lossless/arithmetic JPEG отклоняются. Progressive проверяет spectral selection, последовательность approximation levels, DC/AC Huffman-коды, refinement, EOB runs, restart и padding; сохраняются только маски ненулевых AC. Лимиты: 128 scans, 100 млн позиций коэффициентов. MP4 до 50 MiB: nonfragmented ISO-BMFF, один H.264/AVC `avc1` видеотрек с размерами 1–8192 и необязательный AAC `mp4a` аудиотрек; внешние media references, повреждённые таблицы, fragmented MP4 и иные codecs отклоняются. Сумма stts и mdhd могут различаться не больше чем на один тик timescale и одновременно не больше 1 ms; нулевые delta и несовпадение числа samples по-прежнему запрещены. HEVC/AV1/WebM/MOV не входят в профиль; скрытой конвертации нет. Структурная проверка не доказывает декодирование каждого файла.

Визуальная основа по указанию пользователя — `/Users/evgenyzach/Project/voice-typing-manual/style.css`; источник не менять. Текущие токены/компоненты — `DESIGN.md` и `.impeccable/design.json`, сценарий — `web/SURFACE-BRIEF.md`. Сохраняй Onest, JetBrains Mono, нейтральную палитру и рабочую плотность. Не переносить маркетинговые сценарии источника.

## Разрешение доставки и восстановление

Публикация и правка требуют явного разрешения конкретной версии в конкретный чат/тему данным ботом; макет не равен разрешению отправки. `review POST_ID --action send|edit` выполняет сетевую проверку и фиксирует snapshot с версией, назначением, ботом, хешами media и предыдущей публикацией. Покажи человеку эти данные. Только после разрешения выполняй `execute OPERATION_ID --fingerprint VALUE`. Изменение версии, назначения, бота, прав или предыдущей публикации требует нового review и разрешения.

Порядок/тип/группировка media, quote и custom ID входят в версию/fingerprint. Preflight проверяет `can_send_photos/can_send_videos` по фактическим типам поста. Multipart MP4 передаётся как `video/mp4`/`.mp4`; API получает только InputRichMessage media, локальные path/SHA256/width/height/duration туда не попадают. Классические sendPhoto/sendMediaGroup не добавлены.

Не повторяй неизвестный результат. При timeout/обрыве `unknown` блокирует отправку; сначала `recover` без сети. Durable receipt с fsync сохраняется до SQLite projection/HTML. Подтверждённый `message_id` запрещает повторную отправку из-за ошибки HTML: достаточно `recover`/`build`. `resolve` — только по проверенным человеком фактам с `--evidence`; известный receipt нельзя объявить отсутствующим. Правка сохраняет тот же `message_id`; сознательная повторная публикация требует отдельного разрешения и `--new-publication`. Exactly-once не обещается.

Успех API не означает проверку вида в клиенте. `verify-visual POST_ID --confirmed` — только после подтверждения человеком текущей опубликованной версии в Telegram. Не удаляй сообщения, не меняй webhook, не читай updates общего бота. `discover --isolated-bot-consent` разрешён лишь после явного согласования одного чтения выделенного бота; при активном webhook команда останавливается.

## Секреты и перенос

Токен никогда не проси в чате, не читай из хранилища и не выводи. `token-set` запускает **человек** в своём терминале со скрытым вводом; fallback с echo запрещён. Секрет использует только локальный процесс при явно запрошенном действии. macOS — Keychain, передача через stdin; Windows — DPAPI с ACL текущего пользователя. Не читай `secrets/`, `*.env`, `*.pem` инструментами агента. Не выводи credential-bearing URL.

Пользовательские `posts/`, `media/`, `data/`, `index.html`, `secrets/`, `config.local.json` исключены из Git. Не публикуй проект на GitHub без прямого поручения. Папку переносят вместе с posts/media/data при остановленных операциях; на новом компьютере человек заново вводит токен. Сохраняй message_id/receipts/unknown, затем recover/build и проверка назначения.

## Проверки и известные ограничения

```sh
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s tests -p test_core.py -v
node --check web/app.js
```

Для отдельного модуля замени `test_core.py` на `test_dashboard.py`, `test_delivery.py`, `test_scheduling.py` или `test_schedule_cli.py`. Совместимость синтаксиса Python: `ast.parse(source, feature_version=(3, 11))` для исходных `.py`; это не запуск в Python 3.11. После изменения UI проверяй browser desktop/mobile, детали, поиск/фильтры/календарь, клавиатуру, overflow, storage denied, playback/seek/pause/fallback и доступный `file://` с локальными шрифтами/media. Статическая проверка шаблона `web/shell.html` с ошибочным относительным `web/web/style.css` может ложно показать плоскую шкалу 16 px: проверяй собранный index и computed styles.

Актуальный root прогон 18 октября 2026: `python3 -m unittest discover -s tests -q` — 86/86, OK, 24.159 s; `node --check web/app.js` OK. ResourceWarning SQLite остаются. HTTP browser расписания 1280×720/390×844: v2 отдельно от v1, saved PNG naturalWidth900, дата/зона, предупреждение после реального пересечения срока, фильтр 1/10, без overflow/console errors. Полные доказательства — HANDOFF.md. Исторический Rich Media: 54/54, OK, 21.738 s, независимый — 54/54, 21.790 s; грамматика Python 3.11 — 12 файлов OK. Это не native Python 3.11. Host — macOS 27.0.1/Python 3.14.7.

Синтетический HTTP browser проверен на desktop 1280×720/mobile 390×844: верхнее media, пропорции/группы/slideshow, quote/Unicode/custom fallback, fallback отсутствующего video, playback/seek/pause H.264 144×256 длительностью 4 s и storage denied. Основной demo-media/build: 4 новых v1, всего 5 постов/13 media, повтор идемпотентен, hash прежнего demo JSON сохранён. Чистый source export и перенос в путь с пробелами/кириллицей прошли demo-media/build с сохранением 4 v1.

Новая `file://` offline-проверка Rich Media и native Windows/DPAPI не выполнены; старый baseline browser-pass не переносится на расширение. Пользователь самостоятельно выполнил `token-set` на macOS и показал результат «сохранён в защищённом хранилище»; агент токен не читал. Затем состоялся ограниченный live-опыт: pending-предложение создано, одобрение не подтверждено, расписания/выхода нет по сохранённым данным и сообщению человека. Автономность не проверена. Полные доказательства и ограничения — в HANDOFF.md.

Git: main, исторический коммит db08a75 существует, новых commit/push в продолжении нет. Ранняя блокировка индекса `Operation not permitted` — историческое ограничение среды; не обходи sandbox. Проверенный source export по `.gitignore` — не реальный git clone.
<!-- autopilot:end -->
