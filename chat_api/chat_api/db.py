"""Слой доступа к данным на базе SQLite (aiosqlite).

Схема:
    chats     — номера чатов (id) и их служебные данные;
    users     — псевдоним + хэш пароля в рамках конкретного чата;
    messages  — текстовые и медиа-сообщения (картинки/видео).

Правила:
    * чат создаётся автоматически при первом подключении к номеру;
    * пароль записывается при первом подключении пользователя;
    * повторный вход с тем же псевдонимом, но другим паролем отклоняется;
    * пользователь, не заходивший в чат inactivity_days дней, удаляется
      вместе со своими сообщениями (лениво при запросах и фоном).
"""

from __future__ import annotations

import datetime
import os
from typing import List, Optional, Tuple

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id        INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    username       TEXT    NOT NULL,
    password_hash  TEXT    NOT NULL,
    joined_at      TEXT    NOT NULL,
    last_seen_at   TEXT    NOT NULL,
    UNIQUE (chat_id, username)
);
CREATE INDEX IF NOT EXISTS idx_users_chat ON users(chat_id);
CREATE INDEX IF NOT EXISTS idx_users_seen ON users(last_seen_at);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    username     TEXT    NOT NULL,
    content_type TEXT    NOT NULL,
    text         TEXT,
    file_path    TEXT,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
"""

_ROW_SELECT = (
    "id, chat_id, username, content_type, text, file_path, created_at"
)


def utc_now() -> datetime.datetime:
    return datetime.datetime.utcnow()


def _iso(dt: "datetime.datetime" = None) -> str:
    dt = dt or utc_now()
    return dt.isoformat(timespec="seconds")


def _parse_iso(value: str) -> "datetime.datetime":
    return datetime.datetime.fromisoformat(value)


class ChatStore:
    """Асинхронная обёртка над SQLite-базой данных."""

    def __init__(self, database_path: str, inactivity_days: int):
        self._db_path = database_path
        self._inactivity_days = inactivity_days
        self._conn: Optional[aiosqlite.Connection] = None

    # ------------------------------------------------------------------ #
    # Жизненный цикл
    # ------------------------------------------------------------------ #
    async def connect(self) -> None:
        parent = os.path.dirname(self._db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _execute(self, sql: str, params: Tuple = ()) -> aiosqlite.Cursor:
        cursor = await self._conn.execute(sql, params)
        await self._conn.commit()
        return cursor
    # ------------------------------------------------------------------ #
    # Внутренние помощники
    # ------------------------------------------------------------------ #
    def _inactivity_threshold(self, now: "datetime.datetime" = None) -> str:
        now = now or utc_now()
        cutoff = now - datetime.timedelta(days=self._inactivity_days)
        return _iso(cutoff)

    async def _chat_exists(self, chat_id: int) -> bool:
        cursor = await self._conn.execute(
            "SELECT 1 FROM chats WHERE id = ? LIMIT 1", (chat_id,)
        )
        return await cursor.fetchone() is not None

    async def _get_user(self, chat_id: int, username: str) -> Optional[aiosqlite.Row]:
        cursor = await self._conn.execute(
            "SELECT id, password_hash FROM users WHERE chat_id = ? AND username = ?",
            (chat_id, username),
        )
        return await cursor.fetchone()

    async def _touch(self, user_id: int, now: str = None) -> None:
        await self._execute(
            "UPDATE users SET last_seen_at = ? WHERE id = ?",
            (now or _iso(), user_id),
        )

    async def _delete_inactive_users_in_chat(
        self, chat_id: int, cutoff: str
    ) -> List[str]:
        """Удаляет неактивных пользователей чата; возвращает пути удалённых файлов."""
        cursor = await self._conn.execute(
            "SELECT file_path FROM messages "
            "WHERE chat_id = ? AND file_path IS NOT NULL "
            "AND user_id IN (SELECT id FROM users "
            "                 WHERE chat_id = ? AND last_seen_at < ?)",
            (chat_id, chat_id, cutoff),
        )
        paths = [row["file_path"] for row in await cursor.fetchall()]
        await self._execute(
            "DELETE FROM users WHERE chat_id = ? AND last_seen_at < ?",
            (chat_id, cutoff),
        )
        return paths

    # ------------------------------------------------------------------ #
    # Подключение к чату
    # ------------------------------------------------------------------ #
    async def join(
        self, chat_id: int, username: str, password: str, now: str = None
    ) -> Tuple[bool, bool]:
        """Подключает пользователя к чату.

        Возвращает (created_chat, created_user). Если чата нет — создаёт его.
        Если пользователь заходит впервые — запоминает хэш пароля.
        Если псевдоним занят другим паролем — бросает ChatError(403).
        """
        from .auth import hash_password, verify_password
        from .errors import wrong_password

        now = now or _iso()

        # Ленивое удаление "протухших" пользователей именно этого чата.
        paths = await self._delete_inactive_users_in_chat(
            chat_id, self._inactivity_threshold()
        )
        for path in paths:
            await self._unlink_file(path)

        created_chat = True
        if await self._chat_exists(chat_id):
            created_chat = False
        else:
            cursor = await self._execute(
                "INSERT INTO chats (id, created_at) VALUES (?, ?)",
                (chat_id, now),
            )
            chat_id = cursor.lastrowid

        user = await self._get_user(chat_id, username)
        if user is None:
            cursor = await self._execute(
                "INSERT INTO users (chat_id, username, password_hash, "
                "joined_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
                (chat_id, username, hash_password(password), now, now),
            )
            created_user = True
        else:
            created_user = False
            if not verify_password(password, user["password_hash"]):
                raise wrong_password(chat_id, username)
            await self._touch(user["id"], now)
        return chat_id, created_user

    async def authenticate(
        self, chat_id: int, username: str, password: str, now: str = None
    ) -> None:
        """Проверяет, что пользователь подключён к чату с верным паролем.

        Если псевдоним существует, но пароль другой — 403.
        Если пользователь не подключался (или его запись удалена) — 404.
        При успехе обновляет last_seen_at.
        """
        from .auth import verify_password
        from .errors import not_joined, wrong_password

        now = now or _iso()
        user = await self._get_user(chat_id, username)
        if user is None:
            raise not_joined(chat_id, username)
        if not verify_password(password, user["password_hash"]):
            raise wrong_password(chat_id, username)
        await self._touch(user["id"], now)

    # ------------------------------------------------------------------ #
    # Сообщения
    # ------------------------------------------------------------------ #
    async def send_text(
        self, chat_id: int, username: str, password: str, text: str, now: str = None
    ) -> dict:
        now = now or _iso()
        await self.authenticate(chat_id, username, password, now)
        user = await self._get_user(chat_id, username)
        cursor = await self._execute(
            "INSERT INTO messages (chat_id, user_id, username, content_type, "
            "text, created_at) VALUES (?, ?, ?, 'text', ?, ?)",
            (chat_id, user["id"], username, text, now),
        )
        return await self.get_message(cursor.lastrowid)

    async def send_media(
        self,
        chat_id: int,
        username: str,
        password: str,
        content_type: str,
        file_path: str,
        now: str = None,
    ) -> dict:
        now = now or _iso()
        await self.authenticate(chat_id, username, password, now)
        user = await self._get_user(chat_id, username)
        cursor = await self._execute(
            "INSERT INTO messages (chat_id, user_id, username, content_type, "
            "file_path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, user["id"], username, content_type, file_path, now),
        )
        return await self.get_message(cursor.lastrowid)

    async def add_media_message(
        self,
        chat_id: int,
        username: str,
        password: str,
        content_type: str,
        now: str = None,
    ) -> dict:
        """Создаёт запись медиа-сообщения без файла на диске.

        Возвращает словарь сообщения; file_path заполняется позже через
        attach_file(), когда файл успешно сохранён.
        """
        now = now or _iso()
        await self.authenticate(chat_id, username, password, now)
        user = await self._get_user(chat_id, username)
        cursor = await self._execute(
            "INSERT INTO messages (chat_id, user_id, username, content_type, "
            "file_path, created_at) VALUES (?, ?, ?, ?, NULL, ?)",
            (chat_id, user["id"], username, content_type, now),
        )
        return await self.get_message(cursor.lastrowid)

    async def attach_file(self, message_id: int, file_path: str) -> None:
        await self._execute(
            "UPDATE messages SET file_path = ? WHERE id = ?",
            (file_path, message_id),
        )

    async def delete_message(self, message_id: int) -> None:
        await self._execute("DELETE FROM messages WHERE id = ?", (message_id,))

    async def get_message(self, message_id: int) -> Optional[dict]:
        cursor = await self._conn.execute(
            "SELECT {0} FROM messages WHERE id = ?".format(_ROW_SELECT),
            (message_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    async def list_messages(
        self, chat_id: int, after_id: int, limit: int
    ) -> List[dict]:
        cursor = await self._conn.execute(
            "SELECT {0} FROM messages WHERE chat_id = ? AND id > ? "
            "ORDER BY id ASC LIMIT ?".format(_ROW_SELECT),
            (chat_id, after_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    async def file_for_message(
        self, message_id: int
    ) -> Optional[Tuple[int, str, str]]:
        """Возвращает (chat_id, file_path, content_type) по id сообщения."""
        cursor = await self._conn.execute(
            "SELECT chat_id, file_path, content_type FROM messages WHERE id = ?",
            (message_id,),
        )
        row = await cursor.fetchone()
        if row is None or row["file_path"] is None:
            return None
        return row["chat_id"], row["file_path"], row["content_type"]

    def _row_to_dict(self, row: aiosqlite.Row) -> dict:
        return {
            "id": row["id"],
            "chat_id": row["chat_id"],
            "username": row["username"],
            "content_type": row["content_type"],
            "text": row["text"],
            "file_path": row["file_path"],
            "created_at": row["created_at"],
        }

    @staticmethod
    async def _unlink_file(path: str) -> None:
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # Очистка (фоновая задача)
    # ------------------------------------------------------------------ #
    async def purge_inactive_users(self) -> int:
        """Удаляет всех пользователей, не заходивших в чаты дольше срока.

        Возвращает количество удалённых пользователей.
        """
        cutoff = self._inactivity_threshold()
        cursor = await self._conn.execute(
            "SELECT file_path FROM messages "
            "WHERE file_path IS NOT NULL "
            "AND user_id IN (SELECT id FROM users WHERE last_seen_at < ?)",
            (cutoff,),
        )
        paths = [row["file_path"] for row in await cursor.fetchall()]
        cursor = await self._conn.execute(
            "DELETE FROM users WHERE last_seen_at < ?", (cutoff,)
        )
        deleted = cursor.rowcount
        await self._conn.commit()
        for path in paths:
            await self._unlink_file(path)
        return deleted