"""Tkinter-клиент для chat_api на silenok.рф:8000.

Подключается к серверу и использует его функционал:
  - POST /chats/join        — вход в чат / создание чата
  - POST /chats/send        — отправка текста
  - POST /chats/send_file   — отправка картинки/видео (multipart)
  - POST /chats/poll        — long polling получение новых сообщений
  - GET  /files/{id}        — скачивание прикреплённого файла
  - GET  /health            — проверка доступности сервера

Написано только на стандартной библиотеке (tkinter, urllib, threading),
дополнительные пакеты не требуются. Запуск:  python main.py
"""

from __future__ import annotations

import json
import io
import mimetypes
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional
from uuid import uuid4

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

# Pillow — опционально: если установлен, показываем в чате превью картинок.
try:
    from PIL import Image, ImageTk
except Exception:  # pragma: no cover — Pillow не обязателен
    Image = None
    ImageTk = None

# ---------------------------------------------------------------------------
# Домен silenok.рф переводим в punycode (urllib не умеет кириллические домены).
# ---------------------------------------------------------------------------
HOST = os.getenv("CHAT_HOST", "силенок.рф")
try:
    HOST_ASCII = HOST.encode("idna").decode("ascii")
except UnicodeError:
    HOST_ASCII = HOST
PORT = int(os.getenv("CHAT_PORT", "8000"))
BASE_URL = f"http://{HOST_ASCII}:{PORT}"

POLL_TIMEOUT = float(os.getenv("CHAT_POLL_TIMEOUT", "30"))  # до 60 на сервере
DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")

ALLOWED_EXTENSIONS = {
    ".jpeg", ".jpg", ".png", ".gif", ".mp4", ".webm", ".mov",
}
TEXT_TYPES = {"text", "system"}

MESSAGE_COLORS = {
    "text": {"my": "#DCF8C6", "other": "#FFFFFF"},
    "image": "#F0F8FF",
    "video": "#FFF5EE",
    "file": "#F5F5F5",
    "system": "#ECEFF1",
}
def _media_label(msg: Dict[str, Any]) -> str:
    """Человекочитаемое имя файла для медиа-сообщений.

    Сервер отдаёт только file_url вида /files/{id}, поэтому строим имя
    сами: photo_{id}.png и т.п. Если имя было передано (file_name) —
    используем его.
    """
    fname = msg.get("file_name")
    if fname:
        return fname
    ctype = (msg.get("content_type") or "").split(";")[0].strip().lower()
    ext = mimetypes.guess_extension(ctype) or ".bin"
    if ext in (".jpe", ".jfif"):
        ext = ".jpg"
    mid = msg.get("id")
    base = f"photo_{mid}" if mid is not None else "file"
    return f"{base}{ext}"


def _media_category(ctype: str) -> str:
    """Сводит MIME-тип к короткой категории: image/video/audio/file/text."""
    ctype = (ctype or "text").split(";")[0].strip().lower()
    if ctype in ("text", "system"):
        return ctype
    if ctype.startswith("image/"):
        return "image"
    if ctype.startswith("video/"):
        return "video"
    if ctype.startswith("audio/"):
        return "audio"
    return "file"


def _msg_display(msg: Dict[str, Any]) -> tuple:
    """Возвращает (категория, username, текст для отображения, created_at)."""
    raw_ctype = msg.get("content_type") or "text"
    cat = _media_category(raw_ctype)
    created = msg.get("created_at", "")
    username = msg.get("username", "?")
    if cat in TEXT_TYPES:
        return cat, username, msg.get("text") or "", created
    text = msg.get("text")
    if text:
        return "text", username, text, created
    # Для файлов берём имя из file_name или строим из content_type/id.
    return cat, username, _media_label(msg), created


def _download_media_if_present(msg: Dict[str, Any]) -> Optional[str]:
    """Скачивает прикреплённый файл в downloads/ и возвращает путь или None."""
    file_url = msg.get("file_url")
    if not file_url:
        return None
    try:
        full = HTTP.get("/" + file_url.lstrip("/"), timeout=30.0)
    except ConnectionError:
        return None
    if not full.ok or not full.body:
        return None
    try:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        ctype = (msg.get("content_type") or "").lower()
        ext = mimetypes.guess_extension(ctype.split(";")[0].strip()) if ctype else None
        if not ext:
            ext = os.path.splitext(
                urllib.parse.unquote(urllib.parse.urlparse(file_url).path))[1] or ".bin"
        path = os.path.join(
            DOWNLOAD_DIR, f"{msg.get('id', time.time())}{ext}")
        with open(path, "wb") as fh:
            fh.write(full.body)
        return path
    except OSError:
        return None


class HTTPResponse:
    """Обёртка над ответом urllib: status, body (bytes), json()."""

    def __init__(self, status: int, body: bytes):
        self.status = status
        self.body = body

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Dict[str, Any]:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"detail": self.body.decode("utf-8", "replace")}

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class HTTPBase:
    """Минимальный HTTP-клиент на urllib (без requests)."""

    def __init__(self, base_url: str, default_timeout: float = 40.0):
        self.base_url = base_url.rstrip("/")
        self.default_timeout = default_timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        data: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> HTTPResponse:
        url = self.base_url + path
        body = data
        hdrs: Dict[str, str] = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        if body is not None and "Content-Length" not in hdrs:
            hdrs["Content-Length"] = str(len(body))
        hdrs.setdefault("User-Agent", "chat_tk_client/1.0")
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(
                req, timeout=timeout if timeout is not None else self.default_timeout
            ) as resp:
                return HTTPResponse(resp.status, resp.read())
        except urllib.error.HTTPError as e:
            return HTTPResponse(e.code, e.read())
        except (urllib.error.URLError, socket.timeout, ssl.SSLError) as e:
            raise ConnectionError(str(e))

    def get(self, path: str, timeout: Optional[float] = None) -> HTTPResponse:
        return self.request("GET", path, timeout=timeout)

    def post_json(
        self,
        path: str,
        payload: Dict[str, Any],
        timeout: Optional[float] = None,
    ) -> HTTPResponse:
        return self.request("POST", path, json_body=payload, timeout=timeout)


# Блокировка, чтобы запросы из разных фоновых потоков не пересекались.
_HTTP_LOCK = threading.Lock()


def _locked_request(
    method: str,
    path: str,
    *,
    json_body: Optional[Dict[str, Any]] = None,
    data: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: Optional[float] = None,
) -> HTTPResponse:
    with _HTTP_LOCK:
        return HTTP.request(
            method, path,
            json_body=json_body, data=data, headers=headers, timeout=timeout,
        )


HTTP = HTTPBase(BASE_URL)


def _prepare_image_to_send(file_path: str):
    """Возвращает (payload, filename, content_type) для multipart-отправки.

    Если файл — изображение в формате, который сервер не принимает
    (webp, bmp, tif, ico и т.п.), конвертируем его в PNG через Pillow.
    Если Pillow недоступен или конвертация не удалась — поднимает ValueError.
    """
    _, ext = os.path.splitext(file_path)
    ext = ext.lower()
    ctype = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    main_type = ctype.split("/")[0] if "/" in ctype else ""

    # Стандартные типы сервера — шлём как есть.
    if ctype in (
        "image/jpeg", "image/png", "image/gif",
        "video/mp4", "video/webm", "video/quicktime",
    ):
        with open(file_path, "rb") as fh:
            return fh.read(), os.path.basename(file_path), ctype

    # Изображение в неподдерживаемом сервером формате — конвертируем в PNG.
    if Image is not None and main_type == "image":
        try:
            with Image.open(file_path) as im:
                im.load()
                buf = io.BytesIO()
                im.save(buf, format="PNG")
                buf.seek(0)
                name = os.path.splitext(os.path.basename(file_path))[0] + ".png"
                return buf.read(), name, "image/png"
        except Exception:
            raise ValueError(
                "Не удалось сконвертировать изображение в PNG (формат не "
                "поддерживается). Разрешены: JPG/PNG/GIF, видео MP4/WebM/MOV."
            )

    raise ValueError(
        "Недопустимый тип файла. Разрешены: картинки JPG/PNG/GIF "
        "и видео MP4/WebM/MOV."
    )


class ChatClient:
    """Тонкий клиент поверх HTTP: join / send / send_file / poll / health."""

    def __init__(self, http: HTTPBase):
        self.http = http

    def health(self, timeout: float = 10.0) -> HTTPResponse:
        return self.http.get("/health", timeout=timeout)

    def join(self, chat_id: int, username: str, password: str) -> HTTPResponse:
        return self.http.post_json("/chats/join", {
            "chat_id": chat_id,
            "username": username,
            "password": password,
        })

    def send(self, chat_id: int, username: str, password: str, text: str) -> HTTPResponse:
        return self.http.post_json("/chats/send", {
            "chat_id": chat_id,
            "username": username,
            "password": password,
            "text": text,
        })

    def send_file(
        self,
        chat_id: int,
        username: str,
        password: str,
        file_path: str,
    ) -> HTTPResponse:
        try:
            file_bytes, filename, ctype = _prepare_image_to_send(file_path)
        except OSError:
            raise ValueError("Не удалось прочитать файл.")
        # Простой multipart/form-data без внешних библиотек.
        boundary = "----TkChatBoundary" + uuid4().hex
        parts = []
        for key, value in (("chat_id", str(chat_id)),
                           ("username", username),
                           ("password", password)):
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n"
            )
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        )
        body = ("".join(parts).encode("utf-8") + file_bytes
                + f"\r\n--{boundary}--\r\n".encode("utf-8"))
        return self.http.request(
            "POST", "/chats/send_file",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            timeout=90.0,
        )

    def poll(
        self,
        chat_id: int,
        username: str,
        password: str,
        last_message_id: int,
        timeout: float,
    ) -> HTTPResponse:
        return self.http.post_json("/chats/poll", {
            "chat_id": chat_id,
            "username": username,
            "password": password,
            "last_message_id": last_message_id,
            "timeout": timeout,
        })
class ChatApp:
    """Главное окно tkinter."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Чат на silenok.рф")
        self.root.geometry("720x620")
        self.root.minsize(560, 420)

        self.client = ChatClient(HTTP)
        # Состояние сессии (до входа — None).
        self.chat_id: Optional[int] = None
        self.username: Optional[str] = None
        self.password: Optional[str] = None
        self.last_message_id: int = 0
        self._polling = False
        self._closing = False
        # Хранилище PhotoImage-превью, чтобы изображения не удалялись сборщиком.
        self._media_cache: Dict[int, Any] = {}

        self._create_login_view()
        self._create_chat_view()

        # Проверка связи с сервером в фоне, чтобы не блокировать окно.
        self.root.after(300, self._async_check_health)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI #
    def _create_login_view(self) -> None:
        self.login_frame = ttk.Frame(self.root, padding=24)
        ttk.Label(
            self.login_frame,
            text="Вход в чат",
            font=("Segoe UI", 18, "bold"),
        ).grid(row=0, column=0, columnspan=2, pady=(0, 16))

        ttk.Label(self.login_frame, text="Номер чата").grid(
            row=1, column=0, sticky="e", pady=4)
        self.chat_id_var = tk.StringVar(value="1")
        self.chat_id_entry = ttk.Entry(self.login_frame, textvariable=self.chat_id_var, width=30)
        self.chat_id_entry.grid(row=1, column=1, sticky="we", padx=8, pady=4)

        ttk.Label(self.login_frame, text="Имя (псевдоним)").grid(
            row=2, column=0, sticky="e", pady=4)
        self.username_var = tk.StringVar(value="")
        self.username_entry = ttk.Entry(self.login_frame, textvariable=self.username_var, width=30)
        self.username_entry.grid(row=2, column=1, sticky="we", padx=8, pady=4)

        ttk.Label(self.login_frame, text="Пароль").grid(
            row=3, column=0, sticky="e", pady=4)
        self.password_var = tk.StringVar(value="")
        self.password_entry = ttk.Entry(
            self.login_frame, textvariable=self.password_var, width=30, show="\u2022")
        self.password_entry.grid(row=3, column=1, sticky="we", padx=8, pady=4)

        self.login_status_var = tk.StringVar(value="")
        ttk.Label(self.login_frame, textvariable=self.login_status_var, foreground="#888").grid(
            row=4, column=0, columnspan=2, pady=(8, 4))

        self.join_btn = ttk.Button(
            self.login_frame, text="Войти в чат", command=self._on_join_clicked)
        self.join_btn.grid(row=5, column=0, columnspan=2, pady=(4, 2))

        ttk.Label(
            self.login_frame,
            text=(
                f"Сервер: {BASE_URL}\n"
                "Чат создаётся автоматически при первом подключении к номеру.\n"
                "Пароль запоминается: повторный вход с чужим паролем будет отклонён (403)."
            ),
            foreground="#777",
            justify="center",
        ).grid(row=6, column=0, columnspan=2, pady=(12, 0))

        self.login_frame.columnconfigure(1, weight=1)
        self.login_frame.pack(fill="both", expand=True)

    def _create_chat_view(self) -> None:
        self.chat_frame = ttk.Frame(self.root, padding=8)

        # Верхняя панель: статус соединения + кнопка выхода.
        top = ttk.Frame(self.chat_frame)
        top.pack(fill="x", pady=(0, 6))
        self.status_var = tk.StringVar(value="Статус соединения: проверка…")
        ttk.Label(top, textvariable=self.status_var).pack(side="left", padx=4)
        ttk.Button(top, text="Выйти", command=self._on_logout).pack(side="right", padx=4)

        # Сообщения.
        self.messages_text = scrolledtext.ScrolledText(
            self.chat_frame,
            wrap="word",
            state="disabled",
            font=("Segoe UI", 10),
        )
        self.messages_text.pack(fill="both", expand=True)
        self.messages_text.tag_configure("my", background="#DCF8C6",
                                         lmargin1=12, lmargin2=12, rmargin=12, spacing1=2, spacing3=2)
        self.messages_text.tag_configure("other", background="#FFFFFF",
                                         lmargin1=12, lmargin2=12, rmargin=12, spacing1=2, spacing3=2)
        self.messages_text.tag_configure("system", background="#ECEFF1",
                                         foreground="#546E7A", spacing1=2, spacing3=2)
        self.messages_text.tag_configure("media", background="#F0F8FF",
                                         foreground="#01579B", spacing1=2, spacing3=2)
        self.messages_text.tag_configure("error", foreground="#C62828",
                                         spacing1=2, spacing3=2)

        # Панель ввода: текст + кнопка отправки.
        bottom = ttk.Frame(self.chat_frame)
        bottom.pack(fill="x", pady=(6, 0))
        self.entry = ttk.Entry(self.chat_frame)
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.entry.bind("<Return>", lambda _e: self._on_send_text())
        self.send_btn = ttk.Button(self.chat_frame, text="Отправить", command=self._on_send_text)
        self.send_btn.pack(side="right")

        # Панель файлов.
        file_bar = ttk.Frame(self.chat_frame)
        file_bar.pack(fill="x", pady=(6, 0))
        self.attach_btn = ttk.Button(
            file_bar, text="📎 Прикрепить файл…", command=self._on_attach_file)
        self.attach_btn.pack(side="left")
        self.pending_file_label = tk.Label(
            file_bar, text="", fg="#0078D4", anchor="w")
        self.pending_file_label.pack(side="left", padx=8)

        self.chat_frame.pack(fill="both", expand=True)

    # ------------------------------------------------------------ helpers #
    def _thread(self, target, *args):
        threading.Thread(target=target, args=args, daemon=True).start()

    def _schedule(self, fn, *args, **kwargs):
        # Вызов из фонового потока: гарантированно выполняем на UI-потоке.
        self.root.after(0, lambda: fn(*args, **kwargs))

    # ------------------------------------------------------------ actions #
    def _async_check_health(self) -> None:
        self._thread(self._check_health)

    def _check_health(self) -> None:
        try:
            resp = self.client.health()
        except ConnectionError as e:
            self._schedule(self._set_connection_status, f"Сервер недоступен: {e}")
            return
        if resp.ok:
            self._schedule(
                self._set_connection_status,
                f"Соединено — {resp.text().strip()}")
        else:
            self._schedule(
                self._set_connection_status,
                f"Сервер ответил {resp.status} ({resp.text()[:80]})")

    def _set_connection_status(self, text: str) -> None:
        self.status_var.set("Статус соединения: " + text)

    def _on_join_clicked(self) -> None:
        chat_id = self.chat_id_var.get().strip()
        username = self.username_var.get().strip()
        password = self.password_var.get()
        if not chat_id.isdigit() or int(chat_id) <= 0:
            self.login_status_var.set("Номер чата должен быть положительным целым числом.")
            return
        if not username:
            self.login_status_var.set("Введите псевдоним.")
            return
        if not password:
            self.login_status_var.set("Введите пароль.")
            return
        self.join_btn.config(state="disabled")
        self.login_status_var.set("Подключение к серверу…")
        self._thread(self._do_join, int(chat_id), username, password)

    def _do_join(self, chat_id: int, username: str, password: str) -> None:
        try:
            resp = self.client.join(chat_id, username, password)
        except ConnectionError as e:
            self._schedule(self._join_error, f"Сервер недоступен: {e}")
            return
        if not resp.ok:
            detail = resp.json().get("detail", resp.text())
            self._schedule(self._join_error, f"Ошибка {resp.status}: {detail}")
            return

        data = resp.json()
        self.chat_id = data.get("chat_id", chat_id)
        self.username = username
        self.password = password
        self.last_message_id = 0
        self._schedule(self._join_success, data)

    def _join_success(self, data: Dict[str, Any]) -> None:
        self.login_frame.pack_forget()
        self.chat_frame.pack(fill="both", expand=True)
        self.root.title(f"Чат {self.chat_id} — {self.username} — silenok.рф")
        self.join_btn.config(state="normal")
        self.append_system(
            f"Вы вошли в чат {data.get('chat_id')} как {self.username}"
            + (" (чат создан)" if data.get("created") else ""))

        # Запускаем long polling в фоне.
        if not self._polling:
            self._polling = True
            self._thread(self._poll_loop)

        self.entry.focus_set()

    def _join_error(self, message: str) -> None:
        self.join_btn.config(state="normal")
        self.login_status_var.set(message)
# ------------------------------------------------------------- sending #
    def _on_send_text(self) -> None:
        text = self.entry.get().strip()
        if not text or not self.chat_id:
            return
        self.entry.delete(0, "end")
        self._thread(self._do_send_text, text)

    def _do_send_text(self, text: str) -> None:
        try:
            resp = self.client.send(
                self.chat_id, self.username, self.password, text)
        except ConnectionError as e:
            self._schedule(self.append_error, f"Не удалось отправить: {e}")
            return
        if not resp.ok:
            self._schedule(
                self.append_error,
                f"Ошибка отправки {resp.status}: "
                f"{resp.json().get('detail', resp.text())}")
            return
        # Локально НЕ показываем: сообщение вернёт long polling с сервера,
        # иначе оно отобразится дважды (добавили вручную + принесло poll).
        self._schedule(self._set_connection_status, f"Отправлено — {BASE_URL}")

    def _on_attach_file(self) -> None:
        if not self.chat_id:
            messagebox.showinfo("Сначала войдите", "Войдите в чат, чтобы отправлять файлы.")
            return
        path = filedialog.askopenfilename(title="Выберите картинку или видео")
        if not path:
            return
        self.pending_file_label.config(text=os.path.basename(path))
        self._thread(self._do_send_file, path)

    def _do_send_file(self, path: str) -> None:
        try:
            resp = self.client.send_file(
                self.chat_id, self.username, self.password, path)
        except ValueError as e:
            self._schedule(self._file_send_done, f"Ошибка: {e}")
            return
        except ConnectionError as e:
            self._schedule(self._file_send_done, f"Сервер недоступен: {e}")
            return
        if not resp.ok:
            self._schedule(
                self._file_send_done,
                f"Ошибка {resp.status}: {resp.json().get('detail', resp.text())}")
            return
        # Локально НЕ показываем: сообщение вернёт long polling с сервера,
        # иначе оно отобразится дважды (добавили вручную + принесло poll).
        self._schedule(self._file_send_done, "")

    def _file_send_done(self, text: str) -> None:
        self.pending_file_label.config(text=text)

    # -------------------------------------------------------------- polling #
    def _poll_loop(self) -> None:
        """Long polling: получаем сообщения с id > last_message_id."""
        while self._polling and not self._closing and self.chat_id is not None:
            try:
                resp = self.client.poll(
                    self.chat_id, self.username, self.password,
                    self.last_message_id, timeout=POLL_TIMEOUT)
            except ConnectionError:
                # Разрыв сети / таймаут — повторяем попытку.
                self._schedule(
                    self._set_connection_status,
                    "Соединение прервано, переподключение…")
                time.sleep(2)
                continue
            if not resp.ok:
                if resp.status == 403:
                    self._schedule(
                        self.append_error,
                        "Доступ отклонён (403). Выполните повторный вход.")
                    self._polling = False
                    break
                self._schedule(
                    self._set_connection_status,
                    f"Poll: ошибка {resp.status} {resp.text()[:60]}")
                continue
            messages = resp.json().get("messages", [])
            if messages:
                last_id = max(m["id"] for m in messages)
                self.last_message_id = max(self.last_message_id, last_id)
                for m in messages:
                    self._schedule(self._deliver_message, m)
                self._schedule(self._set_connection_status, f"Соединено — {BASE_URL}")

    def _deliver_message(self, msg: Dict[str, Any]) -> None:
        mine = msg.get("username") == self.username
        self.append_message(msg, mine)

    # ------------------------------------------------------------ rendering #
    def _append(self, text: str, tag: str) -> None:
        self.messages_text.config(state="normal")
        self.messages_text.insert("end", text, tag)
        self.messages_text.see("end")
        self.messages_text.config(state="disabled")

    def append_message(self, msg: Dict[str, Any], mine: bool = False) -> None:
        ctype, username, display, created = _msg_display(msg)
        title = "Я" if mine else username
        when = created.replace("T", " ")[:16] if created else ""
        if ctype == "system":
            self._append(f"\n[{title}] {display}\n", "system")
        elif ctype in ("image", "video", "file", "application", "audio"):
            self._append(f"\n[📎 {title}] {display} ({when})\n", "media")
            # Закладка сразу под шапкой: превью/ссылка вставится сюда, а не в
            # конец ленты, чтобы фото шли в хронологическом порядке.
            mid = msg.get("id")
            if mid is not None:
                mark = "_media_{}".format(mid)
                # Позиция перед финальным \n: закладка стоит в конце строки шапки.
                self.messages_text.mark_set(mark, "end-1c")
                # Gravity left: при вставке новых сообщений в конец закладка
                # не двигается, окно вставится сразу после своей шапки.
                self.messages_text.mark_gravity(mark, "left")
            # Скачивание в фоне, чтобы не блокировать UI.
            self._thread(self._download_media_for, msg)
        else:
            base = MESSAGE_COLORS["text"].get("my" if mine else "other", "#FFFFFF")
            tag = "my" if mine else "other"
            self.messages_text.tag_configure(tag, background=base)
            self._append(f"\n[{title} {when}] {display}\n", tag)

    def _download_media_for(self, msg: Dict[str, Any]) -> None:
        path = _download_media_if_present(msg)
        if path:
            self._schedule(self._show_media, msg, path)

    def append_system(self, text: str) -> None:
        self._append(f"\n[система] {text}\n", "system")

    def append_error(self, text: str) -> None:
        self._append(f"\n[ошибка] {text}\n", "error")

    def _show_media(self, msg: Dict[str, Any], path: str) -> None:
        """Встраивает превью изображения в чат (клик — открыть файл)."""
        ctype = (msg.get("content_type") or "").split(";")[0].strip().lower()
        mid = msg.get("id")
        mark = "_media_{}".format(mid) if mid is not None else None
        self.messages_text.config(state="normal")
        try:
            # Позиция вставки: под своей шапкой (хронологический порядок).
            # Если закладки нет (шапка ещё не отрисована) — вставляем в конец.
            insert_at = mark if (mark and self._mark_exists(mark)) else "end"
            if ctype.startswith("image/") and ImageTk is not None:
                try:
                    img = Image.open(path)
                    img.thumbnail((280, 280))
                    photo = ImageTk.PhotoImage(img)
                    if mid is not None:
                        self._media_cache[mid] = photo
                    label = tk.Label(
                        self.messages_text,
                        image=photo,
                        cursor="hand2",
                        bd=1,
                        relief="solid",
                        bg="#111111",
                    )
                    label.image = photo  # защита от сборщика мусора
                    label.bind(
                        "<Button-1>",
                        lambda ev, p=path: self._open_file(p))
                    self.messages_text.window_create(
                        insert_at, window=label, padx=4, pady=2)
                    if mark:
                        self.messages_text.insert(insert_at, "\n")
                    return
                except Exception:
                    self._append(
                        f"\n      [не удалось показать превью] {path}\n", "media")
                    return
            elif ctype.startswith("video/") or ctype.startswith("audio/"):
                # Видео/аудио не встраиваем — добавляем кликабельную ссылку.
                link = tk.Label(
                    self.messages_text,
                    text="▶ Открыть файл в просмотрщике",
                    cursor="hand2",
                    fg="#3558a8",
                )
                link.bind("<Button-1>", lambda ev, p=path: self._open_file(p))
                self.messages_text.window_create(
                    insert_at, window=link, padx=4, pady=2)
                if mark:
                    self.messages_text.insert(insert_at, "\n")
                return
            self._append(f"\n      Сохранено: {path}\n", "media")
        finally:
            self.messages_text.config(state="disabled")
            self.messages_text.see("end")

    def _mark_exists(self, mark: str) -> bool:
        try:
            self.messages_text.index(mark)
            return True
        except tk.TclError:
            return False

    def _open_file(self, path: str) -> None:
        """Открывает файл системным просмотрщиком."""
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                import subprocess
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            self.append_error(f"Не удалось открыть файл: {e}")

    # ------------------------------------------------------------- logout #
    def _on_logout(self) -> None:
        self.chat_frame.pack_forget()
        self._polling = False
        self.chat_id = None
        self.username = None
        self.password = None
        self.last_message_id = 0
        self.messages_text.config(state="normal")
        self.messages_text.delete("1.0", "end")
        self.messages_text.config(state="disabled")
        self.pending_file_label.config(text="")
        self.login_frame.pack(fill="both", expand=True)

    def _on_close(self) -> None:
        self._closing = True
        self._polling = False
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    ChatApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
