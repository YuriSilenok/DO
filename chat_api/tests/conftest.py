"""Общие фикстуры для интеграционных тестов.

Тесты запускают настоящее FastAPI-приложение через TestClient
(starlette.testclient, httpx), с временной базой SQLite и временным
каталогом для файлов. Клиент открывается как контекстный менеджер внутри
теста — так поднимается и гасится lifespan приложения.
"""

from __future__ import annotations

import pytest

from chat_api.config import Settings


@pytest.fixture
def settings_factory(tmp_path):
    """Возвращает фабрику Settings с временными БД и хранилищем."""

    def _make(
        inactivity_days: int = 30,
        poll_timeout_default: int = 1,
        poll_timeout_max: int = 3,
        purge_interval_seconds: int = 3600,
        max_upload_size: int = 1024 * 1024,
        poll_batch_limit: int = 50,
    ) -> Settings:
        db_dir = tmp_path / "db"
        storage = tmp_path / "files"
        return Settings(
            database_path=str(db_dir / "chat.db"),
            storage_dir=str(storage),
            max_upload_size=max_upload_size,
            inactivity_days=inactivity_days,
            poll_timeout_default=poll_timeout_default,
            poll_timeout_max=poll_timeout_max,
            purge_interval_seconds=purge_interval_seconds,
            poll_batch_limit=poll_batch_limit,
        )

    return _make


@pytest.fixture
def store_factory(tmp_path):
    """Возвращает фабрику Store с временной БД и хранилищем."""
    from chat_api.db import ChatStore
    from chat_api.files import FileStore

    def _make(inactivity_days: int = 30) -> tuple:
        db_path = str(tmp_path / "store.sqlite")
        storage = str(tmp_path / "store_files")
        store = ChatStore(db_path, inactivity_days)
        files = FileStore(storage)
        return store, files

    return _make


@pytest.fixture
def client_factory(settings_factory):
    """Возвращает фабрику TestClient для поднятия приложения."""

    def _make(settings=None) -> "TestClient":
        from chat_api.main import create_app
        from starlette.testclient import TestClient

        fsettings = settings or settings_factory()
        app = create_app(fsettings)
        return TestClient(app)

    return _make


@pytest.fixture
def client(client_factory):
    """TestClient с поднятым lifespan (startup + shutdown)."""
    with client_factory() as c:
        yield c