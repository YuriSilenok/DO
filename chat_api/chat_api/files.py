"""Хранение и вычитка загруженных файлов (картинок и видео)."""

from __future__ import annotations

import os
import re
import uuid

import aiofiles

# Допустимые типы контента и расширения для изображений и видео.
IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif"}
VIDEO_TYPES = {"video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov"}
ALLOWED_TYPES = dict(IMAGE_TYPES)
ALLOWED_TYPES.update(VIDEO_TYPES)

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def is_allowed_content_type(content_type: str) -> bool:
    return content_type in ALLOWED_TYPES


def normalize_content_type(content_type: str) -> str:
    """Приводит media type из multipart-запроса к каноническому виду."""
    if content_type is None:
        return ""
    return content_type.split(";")[0].strip().lower()


class FileStore:
    """Сохраняет загруженные файлы на диск и отдаёт их по id сообщения."""

    def __init__(self, storage_dir: str):
        self._storage_dir = storage_dir
        os.makedirs(self._storage_dir, exist_ok=True)

    def _path_for(self, message_id: int) -> str:
        return os.path.join(self._storage_dir, "msg_{0}.bin".format(message_id))

    async def save_stream(
        self, message_id: int, stream, max_size: int
    ) -> str:
        """Записывает содержимое потока на диск.

        Возвращает путь к сохранённому файлу или путь к временному файлу,
        если запись не удалась (файл нужно удалить).
        Поднимает ValueError при превышении max_size.
        """
        tmp_path = os.path.join(
            self._storage_dir, ".tmp_{0}".format(uuid.uuid4().hex)
        )
        size = 0
        try:
            async with aiofiles.open(tmp_path, "wb") as f:
                while True:
                    chunk = await stream.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_size:
                        raise ValueError("file too large")
                    await f.write(chunk)
            final_path = self._path_for(message_id)
            os.replace(tmp_path, final_path)
            return final_path
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            raise

    async def save_bytes(self, message_id: int, data: bytes) -> str:
        """Сохраняет файл из байтов (для тестов и небольших файлов)."""
        path = self._path_for(message_id)
        async with aiofiles.open(path, "wb") as f:
            await f.write(data)
        return path

    def resolve_path(self, stored_path: str) -> str:
        """Возвращает абсолютный путь, убедившись, что он внутри storage_dir."""
        base = os.path.abspath(self._storage_dir)
        candidate = os.path.abspath(stored_path)
        if os.path.commonpath([base, candidate]) != base:
            raise ValueError("path escapes storage")
        return candidate

    def exists(self, stored_path: str) -> bool:
        return os.path.isfile(stored_path)