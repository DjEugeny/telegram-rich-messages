"""Автономное превью и локальный сервер материалов."""

import json
import mimetypes
import os
import re
import shutil
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


CSP = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; media-src 'self'; font-src 'self'; connect-src 'none'; base-uri 'none'; form-action 'none'; object-src 'none'"
PUBLIC_WEB_FILES = {
    "web/app.js", "web/style.css", "web/fonts/Onest-VariableFont_wght.ttf",
    "web/fonts/JetBrainsMono-VariableFont_wght.ttf", "web/fonts/Onest-OFL.txt",
    "web/fonts/JetBrainsMono-OFL.txt",
}
SCHEDULE_FIELDS = (
    "id", "post_id", "status", "version", "channel", "channel_title", "send_at",
    "send_date", "timezone", "proposal_message_id", "published_message_id",
    "source", "evidence", "last_error", "experimental", "live_verified",
)


def build(root):
    """Собрать index.html из текущих версий; сетевых запросов нет."""
    from core import Store, compile_post
    from scheduling import Scheduling

    root = Path(root).resolve()
    store = Store(root)
    try:
        posts = []
        schedules = {}
        for operation in Scheduling(store).list():
            safe = {key: operation.get(key) for key in SCHEDULE_FIELDS}
            try:
                if type(safe["version"]) is not int or safe["version"] < 1:
                    raise ValueError("Нет сохранённой версии")
                saved = store.get(safe["post_id"], safe["version"])
                safe["html"] = compile_post(saved)["html"]
                safe["content_error"] = None
            except (KeyError, ValueError):
                safe["html"] = ""
                safe["content_error"] = "Сохранённая версия недоступна. Нужна проверка агентом."
            schedules.setdefault(safe["post_id"], []).append(safe)
        for post in store.list():
            item = {key: post.get(key) for key in (
                "id", "title", "version", "status", "created_at", "updated_at", "visual_verified"
            )}
            item["destination"] = {key: post.get("destination", {}).get(key) for key in (
                "chat_id", "chat_title", "bot_id", "bot_username", "message_thread_id"
            )}
            publication = post.get("publication") or {}
            item["publication"] = {key: publication.get(key) for key in (
                "message_id", "version", "published_version", "url", "link", "published_at"
            )}
            item["html"] = compile_post(post)["html"]
            item["schedule"] = schedules.get(post["id"], [])
            posts.append(item)
    finally:
        store.conn.close()
    assets = Path(__file__).resolve().parent / "web"
    target_assets = root / "web"
    if assets != target_assets:
        for relative in PUBLIC_WEB_FILES:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(assets.parent / relative, target)
    payload = json.dumps(posts, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    page = (assets / "shell.html").read_text(encoding="utf-8").replace("__CSP__", CSP).replace("__POST_DATA__", payload)
    handle, temporary = tempfile.mkstemp(prefix=".index-", suffix=".html", dir=root)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(page)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / "index.html")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return root / "index.html"


def make_server(root, port=0):
    """Создать loopback-сервер с закрытым списком доступных путей."""
    root = Path(root).resolve()

    class PublicHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_HEAD(self):
            self._respond(head=True)

        def do_GET(self):
            self._respond()

        def _respond(self, head=False):
            host = self.headers.get("Host", "")
            if host not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}:
                self.send_error(403)
                return
            path = unquote(urlsplit(self.path).path)
            if path == "/":
                path = "/index.html"
            parts = path.lstrip("/").split("/")
            allowed = path == "/index.html"
            if len(parts) > 1 and parts[0] == "web":
                allowed = path.lstrip("/") in PUBLIC_WEB_FILES
            elif len(parts) > 1 and parts[0] == "media":
                allowed = Path(path).suffix.lower() in {".png", ".jpg", ".jpeg", ".mp4"}
            if not allowed or "\\" in path or "\0" in path or any(p in {"", ".", ".."} or p.startswith(".") for p in parts):
                self.send_error(404)
                return
            requested = root / path.lstrip("/")
            try:
                target = requested.resolve()
            except (OSError, ValueError):
                self.send_error(404)
                return
            base = root if path == "/index.html" else root / parts[0]
            if any((root.joinpath(*parts[:index])).is_symlink() for index in range(1, len(parts) + 1)) or not target.is_relative_to(base) or not target.is_file():
                self.send_error(404)
                return
            try:
                stream = target.open("rb")
            except OSError:
                self.send_error(404)
                return
            with stream:
                size = os.fstat(stream.fileno()).st_size
                start, end = 0, size - 1
                is_video = target.suffix.lower() == ".mp4"
                range_value = self.headers.get("Range") if is_video else None
                valid_range = True
                if range_value is not None:
                    match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", range_value)
                    valid_range = bool(match and any(match.groups()) and size and len(self.headers.get_all("Range", [])) == 1)
                    if valid_range:
                        first, last = match.groups()
                        if first:
                            start = int(first)
                            end = min(int(last), size - 1) if last else size - 1
                            valid_range = start < size and start <= end
                        else:
                            suffix = int(last)
                            valid_range = suffix > 0
                            start = max(0, size - suffix)
                self.send_response(416 if not valid_range else 206 if range_value is not None else 200)
                content_type = "video/mp4" if is_video else mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                self.send_header("Content-Type", content_type)
                if is_video:
                    self.send_header("Accept-Ranges", "bytes")
                if not valid_range:
                    self.send_header("Content-Range", f"bytes */{size}")
                elif range_value is not None:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                remaining = max(0, end - start + 1) if valid_range else 0
                self.send_header("Content-Length", str(remaining))
                self.send_header("Content-Security-Policy", CSP + "; frame-ancestors 'none'")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if not head and remaining:
                    stream.seek(start)
                    try:
                        while remaining:
                            data = stream.read(min(65536, remaining))
                            if not data:
                                break
                            self.wfile.write(data)
                            remaining -= len(data)
                    except (BrokenPipeError, ConnectionResetError):
                        # Browsers cancel metadata/range requests when media is hidden.
                        return

    return ThreadingHTTPServer(("127.0.0.1", port), PublicHandler)


def serve(root, port=8765):
    """Обслуживать ранее собранный дашборд до Ctrl+C."""
    with make_server(root, port) as server:
        print(f"http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
