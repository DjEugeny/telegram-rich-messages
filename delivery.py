"""Защищённая публикация: одна операция, явное подтверждение и устойчивый receipt.

Секрет загружается только при явном сетевом действии CLI. Recovery работает
без сети. После потери ответа повтор запрещён, включая новую публикацию.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import uuid
import re

from core import compile_post, validate_media, validate_publication_structure, MAX_IMAGE_BYTES
from media import MAX_VIDEO_BYTES


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat()


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


class DeliveryError(ValueError):
    """Безопасная для показа человеку ошибка без данных transport."""


def publication_format(post, compiled=None):
    """Локальный барьер структуры, общий для CLI и review/execute."""
    try:
        return validate_publication_structure(post, compiled)
    except ValueError as error:
        raise DeliveryError(str(error)) from None


def normalize_chat_reference(value):
    """Разобрать целую Telegram-ссылку локально, без чтения поста/сети."""
    from urllib.parse import parse_qs, urlsplit

    error = 'Нужна ссылка на канал или его пост, @username либо проверенный числовой ID.'
    if type(value) is int:
        return value
    if not isinstance(value, str):
        raise DeliveryError(error)
    value = value.strip()
    if re.fullmatch(r'@[A-Za-z0-9_]{5,32}|-?[0-9]+', value):
        return value
    if value.lower().startswith(('t.me/', 'telegram.me/')):
        value = 'https://' + value
    try:
        url = urlsplit(value)
        if url.scheme not in ('https', 'http') or url.netloc.lower() not in ('t.me', 'telegram.me') or url.fragment:
            raise ValueError
        parts = url.path.strip('/').split('/')
        query = parse_qs(url.query, keep_blank_values=True)
        if set(query) - {'single', 't'}:
            raise DeliveryError('Нужна ссылка на пост самого канала, без перехода к комментариям, теме или приглашению.')
        if parts[0].startswith('+') or parts[0] == 'joinchat':
            raise DeliveryError('Это приглашение. Откройте существующий пост канала и скопируйте ссылку на сообщение целиком.')
        if parts[0] == 'c':
            if len(parts) != 3 or not all(re.fullmatch(r'[1-9][0-9]*', part) for part in parts[1:]):
                raise ValueError
            channel_id = int(parts[1])
            if not 1 <= channel_id <= 997852516352:
                raise ValueError
            return -(1000000000000 + channel_id)
        if (len(parts) in (1, 2) and re.fullmatch(r'[A-Za-z0-9_]{5,32}', parts[0])
                and (len(parts) == 1 or re.fullmatch(r'[1-9][0-9]*', parts[1]))):
            return '@' + parts[0]
    except DeliveryError:
        raise
    except ValueError:
        pass
    raise DeliveryError(error)


class APIRejected(DeliveryError):
    def __init__(self, code=400):
        self.code = code if type(code) is int else 400
        super().__init__(f'Telegram отклонил запрос (код {self.code}).')


class TelegramAPI:
    """HTTPS transport без retry; исключения никогда не содержат URL/token."""
    def __init__(self, token):
        if not isinstance(token, str) or not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{20,}', token):
            raise DeliveryError('Некорректный формат токена. Повторите token-set самостоятельно.')
        self.__token = token

    def __repr__(self):
        return '<TelegramAPI token=hidden>'

    def _clean(self, value):
        if isinstance(value, str):
            return re.sub(r'[0-9]{5,}:[A-Za-z0-9_-]{20,}', '[скрыто]', value.replace(self.__token, '[скрыто]'))
        if isinstance(value, list):
            return [self._clean(item) for item in value]
        if isinstance(value, dict):
            return {self._clean(key): self._clean(item) for key, item in value.items()}
        return value

    def call(self, method, payload=None, files=None):
        import urllib.request
        import urllib.error
        if method not in {'getMe', 'getChat', 'getChatMember', 'getWebhookInfo', 'getUpdates', 'sendRichMessage', 'editMessageText', 'approveSuggestedPost'}:
            raise DeliveryError('Метод Telegram не разрешён.')
        payload, files = payload or {}, files or {}
        if files:
            boundary = 'TelegramRich' + secrets.token_hex(24)
            parts = []
            for key, value in payload.items():
                if not re.fullmatch(r'[A-Za-z0-9_]+', key):
                    raise DeliveryError('Некорректное поле запроса.')
                encoded = _json(value) if isinstance(value, (dict, list, bool)) else str(value)
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode() + encoded.encode() + b'\r\n')
            for key, (name, mime, data) in files.items():
                extensions = {'image/png': '.png', 'image/jpeg': '.jpg', 'video/mp4': '.mp4'}
                if not re.fullmatch(r'[A-Za-z0-9_]+', key) or mime not in extensions:
                    raise DeliveryError('Некорректное вложение.')
                # Передаём собственное безопасное имя, не имя пользовательского файла.
                safe_name = key + extensions[mime]
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{safe_name}"\r\nContent-Type: {mime}\r\n\r\n'.encode() + data + b'\r\n')
            body = b''.join(parts) + f'--{boundary}--\r\n'.encode()
            content_type = 'multipart/form-data; boundary=' + boundary
        else:
            body, content_type = _json(payload).encode(), 'application/json'
        request = urllib.request.Request('https://api.telegram.org/bot' + self.__token + '/' + method,
                                         body, {'Content-Type': content_type}, method='POST')
        # Не следуем redirect: это могло бы отправить secret URL/тело чужому хосту.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        try:
            opener = urllib.request.build_opener(NoRedirect)
            with opener.open(request, timeout=30) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise DeliveryError('Ответ Telegram превышает допустимый размер.')
                result = json.loads(raw)
        except urllib.error.HTTPError as error:
            if 400 <= error.code < 500:
                try:
                    result = json.loads(error.read(65536))
                except Exception:
                    raise DeliveryError('Нет подтверждённого ответа Telegram.') from None
                if isinstance(result, dict) and result.get('ok') is False:
                    raise APIRejected(error.code) from None
            raise DeliveryError('Нет подтверждённого ответа Telegram.') from None
        except Exception:
            raise DeliveryError('Нет подтверждённого ответа Telegram; transport скрыт.') from None
        if not isinstance(result, dict) or type(result.get('ok')) is not bool:
            raise DeliveryError('Ответ Telegram не подтверждён.')
        if not result['ok']:
            raise APIRejected(result.get('error_code', 400))
        if 'result' not in result:
            raise DeliveryError('Ответ Telegram не содержит результата.')
        return self._clean(result['result'])


def _keychain_account(root):
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()


def _dpapi(data, decrypt=False):
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_byte))]
    memory = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(memory, ctypes.POINTER(ctypes.c_byte)))
    target = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    # CRYPTPROTECT_UI_FORBIDDEN; user/machine-bound DPAPI, без plaintext fallback.
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise DeliveryError('DPAPI недоступен; токен не сохранён.')
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.WinDLL('kernel32').LocalFree(target.pbData)


def set_token(root):
    """Только человек в собственном TTY; getpass fallback с echo запрещён."""
    import getpass
    import shlex
    import subprocess
    import sys
    import warnings
    if not sys.stdin.isatty():
        raise DeliveryError('Запустите token-set самостоятельно в интерактивном терминале.')
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            token = getpass.getpass('Токен бота (ввод скрыт): ')
        except (getpass.GetPassWarning, EOFError):
            raise DeliveryError('Скрытый ввод недоступен. Токен не прочитан.') from None
    TelegramAPI(token)  # только локальная валидация формата
    try:
        if sys.platform == 'darwin':
            command = 'add-generic-password -U -a ' + shlex.quote(_keychain_account(root)) + ' -s TelegramRichMessages -w ' + shlex.quote(token) + '\n'
            result = subprocess.run(['security', '-i'], input=command, text=True, capture_output=True)
            if result.returncode:
                raise DeliveryError('Keychain не сохранил токен; детали скрыты.')
            # security -i может закончить REPL с кодом 0 после ошибки команды.
            verified = subprocess.run(['security', 'find-generic-password', '-a', _keychain_account(root), '-s', 'TelegramRichMessages', '-w'], capture_output=True, text=True)
            if verified.returncode or not hmac.compare_digest(verified.stdout.strip(), token):
                raise DeliveryError('Keychain не подтвердил сохранение токена; детали скрыты.')
        elif os.name == 'nt':
            directory = Path(root) / 'secrets'
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            identity = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True, text=True, check=True)
            sid = re.search(r'S-1-5-[0-9-]+', identity.stdout)
            if sid is None:
                raise DeliveryError('Не удалось определить Windows SID.')
            # Новый descriptor удаляет также прежние explicit ACE, которые
            # icacls /grant:r текущему SID оставил бы доступными другим людям.
            literal_path = str(directory).replace("'", "''")
            acl_script = ("$sid=New-Object System.Security.Principal.SecurityIdentifier('" + sid.group() + "');"
                          "$acl=New-Object System.Security.AccessControl.DirectorySecurity;"
                          "$acl.SetOwner($sid);$acl.SetAccessRuleProtection($true,$false);"
                          "$rule=New-Object System.Security.AccessControl.FileSystemAccessRule($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow');"
                          "$acl.AddAccessRule($rule);Set-Acl -LiteralPath '" + literal_path + "' -AclObject $acl -ErrorAction Stop")
            subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', acl_script], capture_output=True, check=True)
            target = directory / 'token.dpapi'
            encrypted = _dpapi(token.encode())
            # Новый файл наследует только ACL уже закрытого каталога.
            fd, temporary = tempfile.mkstemp(dir=directory)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(encrypted)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
                # temporary унаследовал только ACL уже закрытого каталога;
                # os.replace не наследует старые ACE целевого файла.
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        else:
            raise DeliveryError('Поддерживаются macOS Keychain и Windows DPAPI. Plaintext fallback отсутствует.')
    except DeliveryError:
        raise
    except Exception:
        raise DeliveryError('Защищённое хранилище недоступно; детали скрыты.') from None
    return {'token': 'сохранён в защищённом хранилище'}


def load_api(root):
    """Вызывается только CLI при явно выбранной сетевой команде."""
    import subprocess
    import sys
    try:
        if sys.platform == 'darwin':
            result = subprocess.run(['security', 'find-generic-password', '-a', _keychain_account(root), '-s', 'TelegramRichMessages', '-w'], capture_output=True, text=True)
            if result.returncode:
                raise DeliveryError('Токен не найден; выполните token-set самостоятельно.')
            token = result.stdout.strip()
        elif os.name == 'nt':
            token = _dpapi((Path(root) / 'secrets' / 'token.dpapi').read_bytes(), decrypt=True).decode()
        else:
            raise DeliveryError('Защищённое хранилище этой ОС не поддерживается.')
        return TelegramAPI(token)
    except DeliveryError:
        raise
    except Exception:
        raise DeliveryError('Не удалось открыть защищённое хранилище.') from None


@contextmanager
def execution_lock(root):
    """OS lock освобождается ядром при завершении процесса; файл не удаляется."""
    path = Path(root) / 'data' / 'delivery.lock'
    with path.open('a+b') as stream:
        try:
            if os.name == 'nt':
                import msvcrt
                stream.seek(0)
                if not stream.read(1):
                    stream.write(b'0')
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DeliveryError('Другой исполнитель активен. Дождитесь его завершения.') from None
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix='.receipt-', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            stream.write(_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Delivery:
    def __init__(self, store, api=None):
        self.store, self.api = store, api
        self.conn, self.root = store.conn, store.root
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, post_id TEXT NOT NULL, status TEXT NOT NULL,
                snapshot TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
                result TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS operation_events (
                seq INTEGER PRIMARY KEY, operation_id TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS receipts (
                operation_id TEXT PRIMARY KEY, receipt TEXT NOT NULL
            );
        ''')
        self.conn.commit()

    def operation(self, operation_id):
        row = self.conn.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
        if row is None:
            raise DeliveryError('Операция не найдена.')
        result = dict(row)
        result['snapshot'] = json.loads(result['snapshot'])
        result['result'] = json.loads(result['result']) if result['result'] else None
        return result

    def _event(self, operation_id, status, detail=''):
        self.conn.execute('INSERT INTO operation_events(operation_id,status,created_at,detail) VALUES (?,?,?,?)',
                          (operation_id, status, _now(), detail))

    def _call(self, method, payload=None, files=None):
        if self.api is None:
            raise DeliveryError('Сетевой transport не задан.')
        try:
            return self.api.call(method, payload, files)
        except APIRejected:
            raise
        except Exception:
            raise DeliveryError('Нет подтверждённого ответа Telegram; детали transport скрыты.') from None

    def check(self, chat, thread_id=None, media=False, action='send'):
        # True сохраняет прежнее значение: требуется право photo.
        media_types = {'photo'} if media is True else set() if media is False else set(media)
        if not media_types <= {'photo', 'video'}:
            raise DeliveryError('Неизвестный тип медиа для проверки прав.')
        chat = normalize_chat_reference(chat)
        if thread_id is not None and (type(thread_id) is not int or thread_id < 1):
            raise DeliveryError('message_thread_id должен быть положительным целым.')
        bot = self._call('getMe')
        target = self._call('getChat', {'chat_id': chat})
        if not bot.get('is_bot') or type(bot.get('id')) is not int or type(target.get('id')) is not int:
            raise DeliveryError('Не удалось проверить бота и чат.')
        if isinstance(chat, str) and chat.startswith('@'):
            if str(target.get('username', '')).lower() != chat[1:].lower():
                raise DeliveryError('Telegram вернул другое публичное назначение.')
        elif int(chat) != target['id']:
            raise DeliveryError('Telegram вернул другой chat_id.')
        member = self._call('getChatMember', {'chat_id': target['id'], 'user_id': bot['id']})
        status = member.get('status')
        if status in ('left', 'kicked') or status not in ('creator', 'administrator', 'member', 'restricted'):
            raise DeliveryError('Бот не состоит в целевом чате.')
        if target.get('type') == 'channel':
            if status != 'creator' and (status != 'administrator' or not member.get('can_post_messages')):
                raise DeliveryError('Нет права публикации в канале.')
        else:
            permissions = member if status == 'restricted' else target.get('permissions', {})
            if status not in ('creator', 'administrator'):
                if permissions.get('can_send_messages') is False or status == 'restricted' and (not member.get('is_member', False) or not permissions.get('can_send_messages')):
                    raise DeliveryError('Нет права отправки сообщений.')
                for kind, permission, label in (('photo', 'can_send_photos', 'изображений'), ('video', 'can_send_videos', 'видео')):
                    if kind in media_types and (permissions.get(permission) is False or status == 'restricted' and not permissions.get(permission)):
                        raise DeliveryError(f'Нет права отправки {label}.')
        if thread_id is not None and (target.get('type') != 'supergroup' or not target.get('is_forum')):
            raise DeliveryError('Тема допустима только в проверенной forum supergroup.')
        return {'bot_id': bot['id'], 'bot_username': bot.get('username', ''),
                'chat_id': target['id'], 'chat_title': target.get('title', target.get('first_name', '')),
                'chat_username': target.get('username'), 'chat_type': target.get('type'),
                'message_thread_id': thread_id,
                'topic_check': 'Права темы подтвердит только отправка; Bot API не читает произвольную тему.' if thread_id else None}

    def _destination(self, post):
        destination = post.get('destination') or {}
        if not destination.get('chat_id'):
            path = self.root / 'data' / 'settings.json'
            if not path.exists():
                raise DeliveryError('Назначение не задано: выполните check --chat.')
            try:
                destination = json.loads(path.read_text(encoding='utf-8'))['destination']
            except (OSError, ValueError, KeyError, TypeError):
                raise DeliveryError('Настройки назначения повреждены.') from None
        return destination

    def _media(self, compiled):
        files = {}
        for item in compiled['media']:
            original = self.root / item['path']
            target = original.resolve()
            base = self.root / 'media'
            if not target.is_relative_to(base) or target == base:
                raise DeliveryError('Вложение вне media/.')
            cursor = original
            while cursor != self.root:
                if cursor.is_symlink():
                    raise DeliveryError('Символические ссылки в media/ запрещены.')
                cursor = cursor.parent
            try:
                limit = MAX_VIDEO_BYTES if item['mime'] == 'video/mp4' else MAX_IMAGE_BYTES
                with target.open('rb') as stream:
                    data = stream.read(limit + 1)
            except OSError:
                raise DeliveryError('Вложение отсутствует или недоступно.') from None
            if len(data) > limit or hashlib.sha256(data).hexdigest() != item['sha256']:
                raise DeliveryError('Вложение изменилось: SHA256 не совпадает.')
            files[item['attachment']] = (target.name, item['mime'], data)
        return files

    def _blocked(self, post_id):
        if self.conn.execute("SELECT id FROM operations WHERE post_id=? AND status IN ('sending','unknown') LIMIT 1", (post_id,)).fetchone():
            return True
        try:
            return self.conn.execute(
                "SELECT id FROM schedule_operations WHERE post_id=? AND status IN "
                "('reviewed','sending','proposed','approving','scheduled','unknown') LIMIT 1",
                (post_id,)).fetchone()
        except Exception:
            return None

    def review(self, post_id, action='send', new_publication=False):
        if action not in ('send', 'edit') or new_publication and action != 'send':
            raise DeliveryError('Неверное действие публикации.')
        with execution_lock(self.root):
            post = self.store.get(post_id)
            if self._blocked(post_id):
                raise DeliveryError('Результат прежней операции неизвестен. Сначала recover/resolve.')
            publication = post.get('publication')
            if action == 'send' and publication and not new_publication:
                raise DeliveryError('Пост уже опубликован; используйте edit или отдельно согласованный --new-publication.')
            if action == 'edit' and not publication:
                raise DeliveryError('Для правки нужен сохранённый message_id.')
            compiled = compile_post(post)
            formatting = publication_format(post, compiled)
            try:
                validate_media(self.root, post)
            except ValueError:
                raise DeliveryError('Медиа повреждено или изменилось; нужен повторный импорт и review.') from None
            # validate_media проверяет файлы по одному. Bytes для multipart
            # нужны только execute; review хранит лишь snapshot метаданных.
            source_destination = self._destination(post)
            destination = self.check(source_destination.get('chat_id'), source_destination.get('message_thread_id'), {item['media']['type'] for item in compiled['media']}, action)
            if source_destination.get('bot_id') is not None and source_destination['bot_id'] != destination['bot_id']:
                raise DeliveryError('Токен принадлежит другому боту. Смена бота требует явного обновления назначения и нового review.')
            if action == 'edit' and (publication.get('bot_id') != destination['bot_id'] or publication.get('chat_id') != destination['chat_id'] or publication.get('message_thread_id') != destination['message_thread_id']):
                raise DeliveryError('Правка требует того же бота, чата и темы.')
            snapshot = {'post_id': post_id, 'version': post['version'], 'version_hash': post['fingerprint'],
                        'destination': destination, 'source_destination': source_destination,
                        'action': action, 'new_publication': bool(new_publication),
                        'publication_anchor': _digest(publication),
                        'prior_message_id': publication['message_id'] if action == 'edit' else None,
                        'markdown': compiled['markdown'], 'media': compiled['media'],
                        'formatting': formatting}
            operation_id = str(uuid.uuid4())
            fingerprint = _digest({'operation_id': operation_id, 'snapshot': snapshot, 'nonce': secrets.token_hex(24)})
            with self.conn:
                self.conn.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,NULL,NULL)',
                                  (operation_id, post_id, 'reviewed', _json(snapshot), fingerprint, _now()))
                self._event(operation_id, 'reviewed')
            return self.operation(operation_id)

    def _receipt_path(self, operation_id):
        # ID исходит только из найденной операции, не из произвольного пути CLI.
        return self.root / 'data' / 'receipts' / (str(uuid.UUID(operation_id)) + '.json')

    def _apply_receipt(self, operation, receipt):
        snapshot = operation['snapshot']
        if receipt.get('operation_id') != operation['id'] or receipt.get('snapshot_hash') != _digest(snapshot):
            raise DeliveryError('Receipt не соответствует операции.')
        publication = dict(receipt)
        publication.update(version=snapshot['version'], published_version=snapshot['version'])
        with self.conn:
            self.conn.execute('INSERT OR IGNORE INTO receipts VALUES (?,?)', (operation['id'], _json(receipt)))
            self.conn.execute("UPDATE operations SET status='published',result=?,error=NULL WHERE id=?", (_json(receipt), operation['id']))
            self.conn.execute("UPDATE post_state SET status='published',publication=?,visual_verified=0 WHERE post_id=?", (_json(publication), operation['post_id']))
            self._event(operation['id'], 'published')
        return receipt

    def _mark(self, operation, status, error):
        with self.conn:
            self.conn.execute('UPDATE operations SET status=?,error=? WHERE id=?', (status, error, operation['id']))
            self.conn.execute('UPDATE post_state SET status=? WHERE post_id=?', (status, operation['post_id']))
            self._event(operation['id'], status, error)

    def execute(self, operation_id, fingerprint, rebuild=None):
        with execution_lock(self.root):
            operation = self.operation(operation_id)
            if not isinstance(fingerprint, str) or not hmac.compare_digest(fingerprint, operation['fingerprint']):
                raise DeliveryError('Подтверждение не соответствует операции.')
            if operation['status'] == 'published':
                return operation['result']
            if operation['status'] != 'reviewed' or self._blocked(operation['post_id']):
                raise DeliveryError('Повтор операции заблокирован. Используйте recover/resolve.')
            snapshot = operation['snapshot']
            post = self.store.get(operation['post_id'])
            if 'publication_anchor' not in snapshot or _digest(post.get('publication')) != snapshot['publication_anchor']:
                raise DeliveryError('Предыдущая публикация изменилась; требуется отдельный новый review и разрешение.')
            if post['version'] != snapshot['version'] or post['fingerprint'] != snapshot['version_hash'] or self._destination(post) != snapshot['source_destination']:
                raise DeliveryError('Версия или назначение изменились; требуется новый review.')
            compiled = compile_post(post)
            formatting = publication_format(post, compiled)
            if (snapshot.get('formatting') != formatting
                    or compiled['markdown'] != snapshot['markdown']
                    or compiled['media'] != snapshot['media']):
                raise DeliveryError('Правила оформления или результат компиляции изменились; нужны новый review и разрешение.')
            try:
                validate_media(self.root, post)
            except ValueError:
                raise DeliveryError('Медиа повреждено или изменилось; нужен повторный импорт и review.') from None
            files = self._media(compiled)
            destination = self.check(snapshot['source_destination']['chat_id'], snapshot['destination']['message_thread_id'], {item['media']['type'] for item in compiled['media']}, snapshot['action'])
            if destination != snapshot['destination']:
                raise DeliveryError('Бот, чат или права изменились; требуется новый review.')
            publication = post.get('publication')
            if snapshot['action'] == 'send' and publication and not snapshot['new_publication']:
                raise DeliveryError('Другой операцией пост уже опубликован.')
            if snapshot['action'] == 'edit' and (not publication or publication['message_id'] != snapshot['prior_message_id'] or publication['bot_id'] != destination['bot_id'] or publication['chat_id'] != destination['chat_id']):
                raise DeliveryError('Сохранённая публикация изменилась; нужен новый review.')
            # Полученные bytes неизменяемы; смена файла после SHA не меняет запрос.
            payload = {'chat_id': destination['chat_id'], 'rich_message': {'markdown': snapshot['markdown']}}
            if snapshot['media']:
                payload['rich_message']['media'] = [{'id': item['id'], 'media': item['media']} for item in snapshot['media']]
            if destination['message_thread_id'] is not None:
                payload['message_thread_id'] = destination['message_thread_id']
            method = 'sendRichMessage'
            if snapshot['action'] == 'edit':
                method = 'editMessageText'
                payload['message_id'] = snapshot['prior_message_id']
                payload.pop('message_thread_id', None)
            self.conn.execute('BEGIN IMMEDIATE')
            try:
                # Повторная проверка актуальности под write lock закрывает гонку с Store.update.
                current = self.store.get(post['id'])
                if current['version'] != snapshot['version'] or current['fingerprint'] != snapshot['version_hash'] or self._destination(current) != snapshot['source_destination']:
                    raise DeliveryError('Версия изменилась во время проверки.')
                if _digest(current.get('publication')) != snapshot['publication_anchor']:
                    raise DeliveryError('Публикация изменилась во время проверки; требуется новый review.')
                changed = self.conn.execute("UPDATE operations SET status='sending' WHERE id=? AND status='reviewed'", (operation_id,)).rowcount
                if changed != 1:
                    raise DeliveryError('Операция уже занята.')
                self.conn.execute("UPDATE post_state SET status='sending',visual_verified=0 WHERE post_id=?", (post['id'],))
                self._event(operation_id, 'sending')
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
            try:
                result = self._call(method, payload, files)
            except APIRejected as error:
                self._mark(operation, 'error', str(error))
                raise
            except BaseException:
                self._mark(operation, 'unknown', 'Ответ не подтверждён. Автоматический повтор запрещён.')
                raise DeliveryError('Результат неизвестен. Повтор запрещён; выполните recover/resolve.') from None
            if (not isinstance(result, dict) or type(result.get('message_id')) is not int or result['message_id'] < 1
                    or result.get('chat', {}).get('id') != destination['chat_id']
                    or snapshot['action'] == 'edit' and result['message_id'] != snapshot['prior_message_id']):
                self._mark(operation, 'unknown', 'Некорректное подтверждение Telegram.')
                raise DeliveryError('Результат неизвестен: некорректное подтверждение.')
            receipt = {'operation_id': operation_id, 'snapshot_hash': _digest(snapshot),
                       'message_id': result['message_id'], 'chat_id': destination['chat_id'],
                       'bot_id': destination['bot_id'], 'bot_username': destination['bot_username'],
                       'message_thread_id': destination['message_thread_id'], 'published_at': _now(),
                       'api_status': 'ok', 'markdown': snapshot['markdown'],
                       'media_sha256': [item['sha256'] for item in snapshot['media']],
                       'file_ids': self._file_ids(result), 'url': self._message_url(destination, result['message_id'])}
            # Независимый fsync receipt предшествует SQLite projection и HTML.
            try:
                _atomic_json(self._receipt_path(operation_id), receipt)
            except OSError:
                # Вторая независимая запись сохраняет ID при отказе файлового пути.
                with self.conn:
                    self.conn.execute('INSERT OR IGNORE INTO receipts VALUES (?,?)', (operation_id, _json(receipt)))
                self._mark(operation, 'unknown', 'API success; receipt в SQLite, требуется recover.')
                raise DeliveryError('Telegram подтвердил message_id; запись receipt требует recover. Не повторяйте отправку.') from None
            try:
                self._apply_receipt(operation, receipt)
            except Exception:
                raise DeliveryError('Telegram подтвердил message_id; receipt сохранён. Выполните recover без повторной отправки.') from None
        if rebuild is not None:
            try:
                rebuild(self.root)
            except Exception:
                return dict(receipt, local_warning='Публикация подтверждена; HTML не обновлён. Выполните build.')
        return receipt

    @staticmethod
    def _message_url(destination, message_id):
        if destination.get('chat_type') not in ('channel', 'supergroup'):
            return None
        if destination.get('chat_username'):
            return f"https://t.me/{destination['chat_username']}/{message_id}"
        chat_id = str(destination['chat_id'])
        if destination.get('chat_type') in ('channel', 'supergroup') and chat_id.startswith('-100'):
            return f'https://t.me/c/{chat_id[4:]}/{message_id}'
        return None

    @staticmethod
    def _file_ids(value):
        result = []
        if isinstance(value, dict):
            for key, item in value.items():
                if key == 'file_id' and isinstance(item, str):
                    result.append(item)
                else:
                    result.extend(Delivery._file_ids(item))
        elif isinstance(value, list):
            for item in value:
                result.extend(Delivery._file_ids(item))
        return result

    def recover(self):
        recovered = []
        with execution_lock(self.root):
            rows = self.conn.execute("SELECT id FROM operations WHERE status IN ('sending','unknown')").fetchall()
            for row in rows:
                operation = self.operation(row['id'])
                path = self._receipt_path(operation['id'])
                stored = self.conn.execute('SELECT receipt FROM receipts WHERE operation_id=?', (operation['id'],)).fetchone()
                try:
                    receipt = json.loads(path.read_text(encoding='utf-8')) if path.exists() else json.loads(stored['receipt']) if stored else None
                    if receipt:
                        self._apply_receipt(operation, receipt)
                    elif operation['status'] == 'sending':
                        self._mark(operation, 'unknown', 'Исполнитель завершился без устойчивого receipt. Повтор запрещён.')
                except (OSError, ValueError):
                    self._mark(operation, 'unknown', 'Receipt повреждён; требуется ручная проверка.')
                recovered.append(self.operation(operation['id']))
        return recovered

    def resolve(self, operation_id, message_id=None, evidence='', absent=False):
        if not isinstance(evidence, str) or len(evidence.strip()) < 8:
            raise DeliveryError('Укажите проверяемое основание ручного решения (минимум 8 символов).')
        if (absent and message_id is not None) or (not absent and (type(message_id) is not int or message_id < 1)):
            raise DeliveryError('Укажите message_id либо отдельно доказанное --absent.')
        with execution_lock(self.root):
            operation = self.operation(operation_id)
            if operation['status'] != 'unknown':
                raise DeliveryError('Ручное решение допустимо только для unknown после recover.')
            snapshot = operation['snapshot']
            if self._receipt_path(operation_id).exists() or self.conn.execute('SELECT 1 FROM receipts WHERE operation_id=?', (operation_id,)).fetchone():
                raise DeliveryError('Есть устойчивый receipt. Сначала recover; подтверждённый message_id нельзя объявить отсутствующим.')
            if absent:
                self._mark(operation, 'error', 'Отсутствие вручную подтверждено: ' + evidence.strip())
                return self.operation(operation_id)
            if snapshot['action'] == 'edit' and message_id != snapshot['prior_message_id']:
                raise DeliveryError('Правка сохраняет прежний message_id.')
            destination = snapshot['destination']
            receipt = {'operation_id': operation_id, 'snapshot_hash': _digest(snapshot),
                       'message_id': message_id, 'chat_id': destination['chat_id'], 'bot_id': destination['bot_id'],
                       'message_thread_id': destination['message_thread_id'], 'published_at': _now(),
                       'api_status': 'manually_confirmed', 'evidence': evidence.strip(),
                       'markdown': snapshot['markdown'], 'media_sha256': [item['sha256'] for item in snapshot['media']],
                       'file_ids': [], 'url': self._message_url(destination, message_id)}
            _atomic_json(self._receipt_path(operation_id), receipt)
            return self._apply_receipt(operation, receipt)

    def verify_visual(self, post_id, confirmed=False):
        # Та же SQLite write lock, что и Store.update: новая версия либо
        # появится до проверки, либо после commit сама сбросит visual flag.
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            post = self.store.get(post_id)
            if not confirmed or self._blocked(post_id) or post['status'] != 'published' or not post.get('publication') or post['publication']['version'] != post['version']:
                raise DeliveryError('Нужно подтверждение человека и опубликованная текущая версия.')
            self.conn.execute('UPDATE post_state SET visual_verified=1 WHERE post_id=?', (post_id,))
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return self.store.get(post_id)
