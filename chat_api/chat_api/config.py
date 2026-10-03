"""Конфигурация приложения.

Все параметры можно переопределить переменными окружения (CHAT_API_*),
что удобно для тестов и развёртывания.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def _env_str(name: str, default: str) -> str:
    return os.getenv(name) or default


@dataclass(frozen=True)
class Settings:
    # Путь к файлу базы данных SQLite.
    database_path: str
    # Каталог для хранения загруженных картинок и видео.
    storage_dir: str
    # Максимальный размер загружаемого файла (байты).
    max_upload_size: int
    # Через сколько дней неактивности пользователь и его сообщения удаляются.
    inactivity_days: int
    # Таймаут long polling по умолчанию (секунды).
    poll_timeout_default: int
    # Максимальный разрешённый таймаут long polling.
    poll_timeout_max: int
    # Период фоновой проверки неактивных пользователей.
    purge_interval_seconds: int
    # Ограничение на количество сообщений, отдаваемых за один poll.
    poll_batch_limit: int


def load_settings() -> Settings:
    return Settings(
        database_path=_env_str(
            "CHAT_API_DB",
            os.path.join(_BASE_DIR, "chat_data", "chat.db"),
        ),
        storage_dir=_env_str(
            "CHAT_API_STORAGE",
            os.path.join(_BASE_DIR, "chat_data", "files"),
        ),
        max_upload_size=_env_int("CHAT_API_MAX_UPLOAD", 50 * 1024 * 1024),
        inactivity_days=_env_int("CHAT_API_INACTIVITY_DAYS", 30),
        poll_timeout_default=_env_int("CHAT_API_POLL_TIMEOUT", 30),
        poll_timeout_max=_env_int("CHAT_API_POLL_TIMEOUT_MAX", 60),
        purge_interval_seconds=_env_int("CHAT_API_PURGE_INTERVAL", 3600),
        poll_batch_limit=_env_int("CHAT_API_POLL_BATCH", 200),
    )


settings = load_settings()