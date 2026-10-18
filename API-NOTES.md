<!-- autopilot:start -->
# Telegram Bot API: заметки

Актуальный live-результат 18 октября 2026: `sendRichMessage` создал pending-предложение с медиа; одобрение тем же ботом не подтверждено, schedule receipt отсутствует, recover восстановил `proposed`. Пользователь сообщил, что пост в канале не появился. Это не проверка независимости от Mac. Подробный [итог опыта](HANDOFF.md) хранится без размножения приватных ID. Код потерял причину API-ошибки: конкретный HTTP status, точный диагноз и неисправимость всего подхода не установлены.

[Официальное описание Suggested Posts](https://core.telegram.org/api/suggested-posts) описывает подтверждение пользователем-получателем в направлении channel → user. Второй аккаунт участвовал только в тесте; пользователь исключил его из продуктового постинга и одобрения. Ручное принятие получателем не является решением задачи автоматизации. Дальше требуется исследование официального пути канал + его бот без второго аккаунта; текущую схему не объявлять готовой. Повторные отправки и новый опыт не разрешены этой записью.

Официальные страницы исходного запуска проверены 17 октября 2026 года; Rich Media и inline-синтаксис повторно сверены 18 октября 2026 года:

- [Rich Messages: обзор](https://core.telegram.org/bots/features#rich-messages).
- [Rich Message Formatting Options](https://core.telegram.org/bots/api#rich-message-formatting-options).
- [Rich Message Formatting](https://core.telegram.org/bots/api#rich-message-formatting).
- [InputRichMessage](https://core.telegram.org/bots/api#inputrichmessage).
- [sendRichMessage](https://core.telegram.org/bots/api#sendrichmessage).
- [editMessageText](https://core.telegram.org/bots/api#editmessagetext).

Rich Markdown отличается от MarkdownV2. Компилятор строит его из проверенных канонических блоков; пользовательский HTML рассматривается как текст.

Пределы rich message: 32768 UTF-8 символов, 500 блоков с учётом вложенных элементов списков и строк таблиц, 16 уровней вложенности, 50 медиа, 20 колонок таблицы. Это ограничения Telegram; проект поддерживает лишь часть возможностей формата.

Локальный компилятор применяет более осторожный подсчёт длины в кодовых единицах UTF-16: например, `😀` занимает две единицы. Это не байты UTF-8. Поэтому сообщение с множеством emoji может быть отклонено локально раньше указанного Telegram предела в символах. Такой отказ не означает, что API проверяет длину тем же способом.

Раскрытие: `<details><summary>Заголовок</summary>Содержимое</details>`; `open` раскрывает блок по умолчанию. URL-кнопка: `<tg-button type="url" url="https://example.org">Открыть</tg-button>`. Callback не входит в v1.

Локальное изображение связывается с rich-текстом через `![alt](tg://photo?id=ID)`, видео — `![alt](tg://video?id=ID)`, запись `InputRichMessage.media` и multipart-вложение `attach://...`. Группы компилируются в `<tg-collage>…</tg-collage>` и `<tg-slideshow>…</tg-slideshow>` со стабильным порядком. Одна группа остаётся частью одного Rich Message. Отправка использует `sendRichMessage`; правка — `editMessageText` с rich message, сохраняя ID сообщения. Классические `sendPhoto`, `sendMediaGroup`, `editMessageMedia` не реализованы; нельзя подменять rich-тело обычным `parse_mode=MarkdownV2`.

Компилятор возвращает `markdown`, `html`, `media`. Каждый элемент локального `media` содержит `id`, `media: {type: photo|video, media: attach://name}` и служебные attachment/path/MIME/SHA256/размеры/длительность. Транспорт передаёт в `InputRichMessage.media` только документированные `id` и `media`; локальные пути, происхождение, хеши и прочие метаданные туда не включаются. MP4 передаётся multipart как `video/mp4` с именем `.mp4`. Права photo/video проверяются по составу поста.

Локальный `media_group` принимает 2–50 image/video; одиночный файл — leaf. Это граница локальной схемы группы, а не отдельно заявленный лимит Telegram. Общий лимит — 50 media на Rich Message. Изменение типа/порядка/группировки, цитаты или custom ID меняет версию и делает прежний review неактуальным; проверка media перед доставкой включает SHA256 и параметры видео.

Цитата `quote` компилируется префиксом `> ` для каждой строки. Custom emoji задаётся явным числовым ID: `![fallback](tg://emoji?id=ID)`. Unicode emoji сохраняются исходными последовательностями. В локальном HTML custom emoji показан fallback-текстом с доступной подписью приблизительного вида, без обращения за анимацией. Наличие ID не доказывает доступность emoji данному боту и клиенту; отказ API не заменяется скрытой отправкой Unicode-варианта.

Структурные inline-узлы: `text`, `bold`, `italic`, `underline`, `strikethrough`, `code`, `link`, `custom_emoji`. Подчёркивание генерирует `<u>…</u>`, зачёркивание — `~~…~~`; введённый пользователем HTML по-прежнему экранируется. Канонические примеры и правила безопасных ссылок — в [examples/README.md](examples/README.md).

Поддерживаемый локальный MP4-профиль уже возможностей Telegram: до 50 MiB, nonfragmented ISO-BMFF, один AVC `avc1` видеотрек, необязательный AAC `mp4a` аудиотрек. Структурный валидатор проверяет контейнер, таблицы samples, offsets и AVC NAL envelopes; это не декодирование каждого кадра. Реальное browser-воспроизведение синтетического fixture и mock-проверка транспорта фиксируются отдельно в [HANDOFF.md](HANDOFF.md).

HTML — приблизительный предпросмотр. Внешний вид и поддержка rich messages зависят от клиента Telegram; совместимость всех клиентов не обещается. Успех Bot API и визуальная проверка человеком — разные факты.

## Экспериментальный адаптер расписания

Фактический код `scheduling.py` использует следующий путь; это описание реализации, не отчёт об успешном live-опыте. Официальные точки проверки: [SuggestedPostParameters](https://core.telegram.org/bots/api#suggestedpostparameters), [approveSuggestedPost](https://core.telegram.org/bots/api#approvesuggestedpost), [sendRichMessage](https://core.telegram.org/bots/api#sendrichmessage).

1. `getMe`, `getChat(DM_CHAT_ID)`, `getChatMember` проверяют бота, `is_direct_messages`, parent channel и права `can_post_messages`/`can_manage_direct_messages`. Topic передаётся явно; его пригодность доказывает только отправка. Если канал задан числовым ID, проверяется совпадение с parent; username сам по себе этой сверки не даёт.
2. `sendRichMessage` получает `chat_id` DM-чата, `direct_messages_topic_id`, `rich_message` с исходным Rich Markdown и допустимой media-проекцией, `suggested_post_parameters: {send_date: UNIX_SECONDS}`. Ответ должен содержать положительный `message_id`, тот же DM chat ID и `suggested_post_info.state == "pending"`; иначе исход unknown. ID относится к предложению в DM.
3. До одобрения сохраняется отдельный proposal receipt. Затем `approveSuggestedPost` получает DM `chat_id`, ID предложения и `send_date`. Только строгое `True` позволяет сохранить schedule receipt и статус `scheduled`. При неясном ответе повтор запрещён.

Даты проверяются локально: явная IANA-зона, минимум 300 секунд, максимум 2678400 секунд (31 сутки), повторная проверка перед execute. Это граница текущего кода; совместимость даты с реальным API ещё не доказана. У `sendRichMessage` не добавляется вымышленный `schedule_date`; путь Suggested Posts не является универсальной очередью обычного канала.

`scheduled` подтверждает принятие одобрения, не фактическую публикацию. Автоматической сверки Telegram нет; `schedule-recover` читает только локальные receipts. Публикация фиксируется ручным `schedule-reconcile` с известным schedule receipt, channel message ID и evidence. `live_verified` остаётся false, даже после такой отметки. Дата сама не переводит статус в published.

`schedule-cancel`/`schedule-reschedule` не вызывают сеть: для approved-предложения поддержанный механизм не реализован. `reconcile` не отменяет публикацию на сервере. Не подменять это удалением, новой отправкой, `sendPhoto` или локальным таймером.

Mock-тесты проверяют транспорт, состояния и восстановление; они не доказывают самоодобрение ботом, сохранение оформления при выходе и независимость от Mac. До частичного опыта выше live-методы не запускались; это историческое состояние. Сейчас доказано создание предложения, но не расписание. Повторные вызовы не выполнялись. Для дальнейшего опыта нужны исследованный официальный путь и отдельное разрешение назначения, версии, бота и даты. Эта документальная правка сетевые вызовы не выполняет.
<!-- autopilot:end -->
