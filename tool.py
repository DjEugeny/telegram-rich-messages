#!/usr/bin/env python3
"""Локальные черновики и отдельно подтверждаемая доставка в Telegram."""

import argparse
import json
import re
from pathlib import Path
import sys
import time

from core import Store, create_demo, import_image, import_media, import_video
import dashboard
from delivery import Delivery, DeliveryError, _atomic_json, load_api, normalize_chat_reference, set_token
from scheduling import SchedulingError
from text_blocks import parse_text_blocks


class MediaArgument(argparse.Action):
    """Все алиасы пишут в один список, сохраняя порядок аргументов."""
    def __call__(self, parser, namespace, values, option_string=None):
        items = list(getattr(namespace, self.dest, None) or [])
        items.append((option_string, values))
        setattr(namespace, self.dest, items)


def parser():
    result = argparse.ArgumentParser(description='Telegram Rich Messages: подготовка, просмотр и публикация постов через агента.')
    result.add_argument('--root', type=Path, default=Path(__file__).resolve().parent, help='Корень материалов и SQLite')
    commands = result.add_subparsers(dest='command', required=True)
    commands.add_parser('demo', help='Вымышленный локальный пример без токена')
    commands.add_parser('demo-media', help='Четыре вымышленных примера с изображениями, видео и цитатами')
    preview = commands.add_parser('preview-demo', help='Временное демо без добавления постов в личную историю')
    preview.add_argument('--port', type=int, default=8765)
    for name in ('create', 'import', 'update'):
        command = commands.add_parser(name, help='Сохранить локальную версию, без отправки')
        if name == 'update':
            command.add_argument('post_id')
        command.add_argument('--title', required=name != 'update')
        source = command.add_mutually_exclusive_group(required=name != 'update')
        source.add_argument('--text', help='Текст поста без переписывания')
        source.add_argument('--file', type=Path, help='UTF-8 текстовый файл')
        source.add_argument('--blocks', type=Path, help='JSON массив канонических блоков')
        command.add_argument('--media', '--image', '--video', dest='media_inputs', action=MediaArgument,
                             help='Повторяемые пути/прямые URL; общий порядок сохраняется; image: PNG/JPEG, video: MP4')
        command.add_argument('--media-layout', choices=('collage', 'slideshow', 'separate'), default='collage')
        command.add_argument('--media-position', choices=('before', 'after'), default='before')
        command.add_argument('--chat', help='Точный @username либо проверенный числовой ID')
        command.add_argument('--thread-id', type=int)
    commands.add_parser('list', help='Текущие версии и статусы')
    show = commands.add_parser('show')
    show.add_argument('post_id')
    show.add_argument('--version', type=int)
    commands.add_parser('build', help='Собрать локальный HTML без сети')
    serve = commands.add_parser('serve', help='Loopback сервер: http://127.0.0.1:8765/')
    serve.add_argument('--port', type=int, default=8765)
    commands.add_parser('token-set', help='Только человек: скрытый ввод в терминале')
    check = commands.add_parser('check', help='Явный сетевой запрос: проверить бота, чат и права')
    check.add_argument('--chat', required=True, help='Целая ссылка на канал/пост, @username либо проверенный числовой ID')
    check.add_argument('--thread-id', type=int)
    check.add_argument('--media', action='store_true', help='Проверить разрешение отправки изображений')
    discover = commands.add_parser('discover', help='Один getUpdates выделенного бота, без offset/смены webhook')
    discover.add_argument('--isolated-bot-consent', action='store_true', help='Человек явно согласовал выделенный бот и одно чтение updates')
    review = commands.add_parser('review', help='Проверить сеть и сохранить точную версию для разрешения человеком')
    review.add_argument('post_id')
    review.add_argument('--action', choices=('send', 'edit'), required=True)
    review.add_argument('--new-publication', action='store_true', help='Отдельно согласованная сознательная повторная публикация')
    execute = commands.add_parser('execute', help='Только после разрешения показанной версии и назначения')
    execute.add_argument('operation_id')
    execute.add_argument('--fingerprint', required=True)
    commands.add_parser('recover', help='Восстановить receipts/unknown без сети и повторной отправки')
    resolve = commands.add_parser('resolve', help='Ручное решение unknown по проверенным человеком фактам, без сети')
    resolve.add_argument('operation_id')
    resolution = resolve.add_mutually_exclusive_group(required=True)
    resolution.add_argument('--message-id', type=int)
    resolution.add_argument('--absent', action='store_true', help='Человек доказал отсутствие результата, разрешает новый review')
    resolve.add_argument('--evidence', required=True)
    visual = commands.add_parser('verify-visual', help='Только после сообщения человека о проверке в Telegram')
    visual.add_argument('post_id')
    visual.add_argument('--confirmed', action='store_true', help='Человек подтвердил итоговый вид текущей опубликованной версии')
    config = commands.add_parser('schedule-config', help='Сохранить канал, DM-чат, topic и часовой пояс расписания')
    config.add_argument('--channel', help='Точный @username либо подтверждённый числовой ID канала')
    config.add_argument('--dm-chat-id', type=int, help='Числовой ID DM-чата канала')
    config.add_argument('--topic-id', type=int, help='Topic прямых сообщений (требует DM-чат)')
    config.add_argument('--timezone', required=True, help='IANA-зона, например Europe/Moscow')
    moment = commands.add_parser('schedule-time', help='Проверить дату: точный вывод даты и зоны')
    moment.add_argument('when', help='ISO дата+время или «сегодня/завтра/послезавтра в ЧЧ:ММ»')
    moment.add_argument('--timezone', help='IANA-зона; иначе сохранённая настройка')
    schedule_review = commands.add_parser('schedule-review', help='Отключено: отложенная публикация перенесена в план')
    schedule_review.add_argument('post_id')
    schedule_review.add_argument('when', help='ISO дата+время или «завтра в 18:00»')
    schedule_review.add_argument('--experimental', action='store_true', help='Исторический флаг; не включает отложку в текущей версии')
    schedule_execute = commands.add_parser('schedule-execute', help='Новые отправки отключены; известный итог доступен без сети')
    schedule_execute.add_argument('operation_id')
    schedule_execute.add_argument('--fingerprint', required=True)
    schedule_execute.add_argument('--experimental', action='store_true', help='Исторический флаг; не разрешает новые отправки')
    commands.add_parser('schedule-list', help='История эксперимента: дата, зона, канал, версия, состояние; без сверки Telegram')
    commands.add_parser('schedule-recover', help='Восстановить receipts расписания без сети и повторной отправки')
    reconcile = commands.add_parser('schedule-reconcile', help='Ручное решение расписания по проверенным человеком фактам, без сети')
    reconcile.add_argument('operation_id')
    reconcile.add_argument('--verdict', choices=('published', 'absent', 'cancelled'), required=True)
    reconcile.add_argument('--evidence', required=True)
    reconcile.add_argument('--message-id', type=int)
    cancel = commands.add_parser('schedule-cancel', help='Отмена approved: unsupported без сети')
    cancel.add_argument('operation_id', nargs='?')
    reschedule = commands.add_parser('schedule-reschedule', help='Перенос approved: unsupported без сети')
    reschedule.add_argument('operation_id', nargs='?')
    reschedule.add_argument('when', nargs='?')
    return result


def _output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _read(path):
    # В CLI нельзя указать хранилище секретов как пользовательский материал.
    path = Path(path).expanduser().resolve()
    if path.suffix.lower() in ('.env', '.pem') or path.name.startswith('.env') or 'secrets' in path.parts:
        raise DeliveryError('Этот файл не предназначен для материала поста.')
    return path.read_text(encoding='utf-8')


def main(argv=None):
    arguments = parser().parse_args(argv)
    root = arguments.root.expanduser().resolve()
    command = arguments.command
    store = None
    try:
        if command == 'check':
            # Сначала локальный разбор ссылки, затем Store/token/network.
            arguments.chat = normalize_chat_reference(arguments.chat)
        if command == 'token-set':
            _output(set_token(root))
            return 0
        if command == 'preview-demo':
            import tempfile
            from demo_media import create_demo_media
            with tempfile.TemporaryDirectory(prefix='telegram-rich-demo-') as directory:
                demo_root = Path(directory)
                create_demo(demo_root)
                create_demo_media(demo_root)
                dashboard.build(demo_root, demo=True)
                dashboard.serve(demo_root, arguments.port)
            return 0
        store = Store(root)
        if command == 'demo':
            _output(create_demo(root))
        elif command == 'demo-media':
            from demo_media import create_demo_media
            _output(create_demo_media(root))
        elif command in ('create', 'import', 'update'):
            blocks = None
            if arguments.blocks:
                blocks = json.loads(_read(arguments.blocks))
            elif arguments.text is not None or arguments.file:
                text = arguments.text if arguments.text is not None else _read(arguments.file)
                blocks = parse_text_blocks(text)
            if arguments.media_inputs:
                # Новые файлы добавляются к сохранённым блокам, без изменения старых.
                blocks = blocks if blocks is not None else store.get(arguments.post_id)['blocks'] if command == 'update' else []
                additions = []
                for alias, source in arguments.media_inputs:
                    if not str(source).startswith(('http://', 'https://')):
                        source_path = Path(source).expanduser().resolve()
                        if source_path.suffix.lower() in ('.env', '.pem') or source_path.name.startswith('.env') or 'secrets' in source_path.parts:
                            raise DeliveryError('Этот файл не предназначен для импорта.')
                    importer = {'--image': import_image, '--video': import_video}.get(alias, import_media)
                    item = importer(root, source)
                    additions.append(dict(type='video' if item['mime'] == 'video/mp4' else 'image', alt='', **item))
                if len(additions) > 1 and arguments.media_layout != 'separate':
                    additions = [{'type': 'media_group', 'layout': arguments.media_layout, 'items': additions}]
                blocks = additions + blocks if arguments.media_position == 'before' else blocks + additions
            destination = None
            if arguments.chat is not None:
                destination = {'chat_id': arguments.chat}
                if arguments.thread_id is not None:
                    destination['message_thread_id'] = arguments.thread_id
            elif arguments.thread_id is not None:
                raise DeliveryError('--thread-id требует --chat.')
            if command == 'update':
                if blocks is None and arguments.title is None and destination is None:
                    raise DeliveryError('Укажите текст, блоки, медиа, название или назначение для изменения.')
                post = store.update(arguments.post_id, title=arguments.title, blocks=blocks, destination=destination)
            else:
                post = store.create(arguments.title, blocks, destination=destination,
                                    source=str(arguments.file) if arguments.file else None)
            _output(post)
        elif command == 'list':
            _output(store.list())
        elif command == 'show':
            _output(store.get(arguments.post_id, arguments.version))
        elif command == 'build':
            print(dashboard.build(root))
        elif command == 'serve':
            dashboard.build(root)
            dashboard.serve(root, arguments.port)
        elif not command.startswith('schedule-'):
            if command == 'discover' and not arguments.isolated_bot_consent:
                raise DeliveryError('discover требует явного согласования выделенного бота: --isolated-bot-consent.')
            manager = Delivery(store)
            if command in ('check', 'discover', 'review', 'execute'):
                # Повтор опубликованной операции/unknown не должен даже читать secret.
                if command == 'execute':
                    operation = manager.operation(arguments.operation_id)
                    import hmac
                    if not hmac.compare_digest(arguments.fingerprint, operation['fingerprint']):
                        raise DeliveryError('Подтверждение не соответствует операции.')
                    if operation['status'] == 'published':
                        _output(operation['result'])
                        return 0
                    if operation['status'] != 'reviewed':
                        raise DeliveryError('Повтор заблокирован; выполните recover/resolve.')
                manager.api = load_api(root)
            if command == 'check':
                destination = manager.check(arguments.chat, arguments.thread_id, arguments.media)
                settings_destination = {key: destination[key] for key in ('chat_id', 'chat_title', 'bot_id', 'bot_username', 'message_thread_id')}
                _atomic_json(root / 'data' / 'settings.json', {'destination': settings_destination})
                _output(destination)
            elif command == 'discover':
                hook = manager._call('getWebhookInfo')
                if hook.get('url'):
                    raise DeliveryError('У бота активен webhook. Чтение updates не выполнено; передайте проверенный ID чата.')
                updates = manager._call('getUpdates', {'timeout': 0, 'limit': 100})
                chats = {}
                for update in updates:
                    for field in ('message', 'channel_post', 'my_chat_member'):
                        chat = update.get(field, {}).get('chat', {})
                        if type(chat.get('id')) is int:
                            chats[chat['id']] = {key: chat.get(key) for key in ('id', 'title', 'type', 'username')}
                _output({'chats': list(chats.values()), 'note': 'Одно чтение без offset. Updates не подтверждены.'})
            elif command == 'review':
                _output(manager.review(arguments.post_id, arguments.action, arguments.new_publication))
            elif command == 'execute':
                _output(manager.execute(arguments.operation_id, arguments.fingerprint, rebuild=dashboard.build))
            elif command == 'recover':
                _output(manager.recover())
            elif command == 'resolve':
                _output(manager.resolve(arguments.operation_id, arguments.message_id, arguments.evidence, arguments.absent))
            elif command == 'verify-visual':
                _output(manager.verify_visual(arguments.post_id, arguments.confirmed))
        elif command.startswith('schedule-'):
            from scheduling import Scheduling, parse_schedule_time
            scheduler = Scheduling(store)
            if command == 'schedule-config':
                channel = arguments.channel
                if isinstance(channel, str) and re.fullmatch(r'-?\d+', channel.strip()):
                    channel = int(channel.strip())
                try:
                    _output(scheduler.configure(channel=channel, dm_chat_id=arguments.dm_chat_id,
                                                topic_id=arguments.topic_id, timezone_name=arguments.timezone))
                except ValueError as error:
                    raise SchedulingError(str(error)) from None
            elif command == 'schedule-time':
                zone = arguments.timezone
                if zone is None:
                    try:
                        zone = scheduler.get_settings()['timezone']
                    except (ValueError, KeyError):
                        raise SchedulingError('Укажите --timezone или сначала выполните schedule-config.') from None
                try:
                    _output(parse_schedule_time(arguments.when, zone))
                except ValueError as error:
                    raise SchedulingError(str(error)) from None
            elif command == 'schedule-list':
                items = scheduler.list()
                for item in items:
                    if item['status'] == 'scheduled' and item['send_date'] <= time.time():
                        item['status_note'] = 'Время наступило; публикация не подтверждена'
                _output(items)
            elif command == 'schedule-recover':
                _output(scheduler.recover())
            elif command == 'schedule-reconcile':
                _output(scheduler.reconcile(arguments.operation_id, arguments.verdict,
                                            arguments.evidence, arguments.message_id))
            elif command in ('schedule-cancel', 'schedule-reschedule'):
                try:
                    if command == 'schedule-cancel':
                        scheduler.cancel(arguments.operation_id)
                    else:
                        scheduler.reschedule(arguments.operation_id, arguments.when)
                except ValueError as error:
                    raise SchedulingError(str(error)) from None
            elif command in ('schedule-review', 'schedule-execute'):
                if not arguments.experimental:
                    raise SchedulingError('Отложенная публикация отключена; --experimental не включает её в текущей версии. План: docs/ROADMAP.md.')
                if command == 'schedule-review':
                    raise SchedulingError('Отложенная публикация отключена в текущей версии и перенесена в план: docs/ROADMAP.md. Токен и сеть не использованы.')
                else:
                    current = scheduler.operation(arguments.operation_id)
                    import hmac
                    if not hmac.compare_digest(str(arguments.fingerprint), str(current.get('fingerprint'))):
                        raise SchedulingError('Fingerprint не совпадает: покажите точный вывод schedule-review.')
                    if current.get('status') != 'reviewed':
                        # Completed возвращает receipt; прочие повторы отвергает
                        # Scheduling до подключения transport и чтения секрета.
                        _output(scheduler.execute(arguments.operation_id, arguments.fingerprint, experimental=True))
                    else:
                        raise SchedulingError('Отложенная публикация отключена в текущей версии и перенесена в план: docs/ROADMAP.md. Токен и сеть не использованы.')
        return 0
    except (DeliveryError, SchedulingError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except (ValueError, OSError, KeyError, TypeError):
        print('Локальные данные недоступны или некорректны. Проверьте ID, файлы и аргументы; детали скрыты.', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('Остановлено. Для прерванной отправки выполните recover; повтор запрещён.', file=sys.stderr)
        return 130
    except Exception:
        print('Операция прервана. Перед любым повтором отправки выполните recover; детали скрыты.', file=sys.stderr)
        return 2
    finally:
        if store is not None:
            store.conn.close()


if __name__ == '__main__':
    raise SystemExit(main())
