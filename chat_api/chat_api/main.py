"""Фабрика приложения и FastAPI-эндпоинты."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from . import errors
from .config import Settings, load_settings
from .db import ChatStore
from .files import (
    FileStore,
    is_allowed_content_type,
    normalize_content_type,
)
from .models import (
    JoinRequest,
    JoinResponse,
    MessageOut,
    PollRequest,
    PollResponse,
    SendResponse,
    SendTextRequest,
)
from .polling import PollBroker

ALLOWED_TYPES = (
    "image/jpeg, image/png, image/gif, video/mp4, video/webm, video/quicktime"
)


def _to_message_out(raw: dict) -> MessageOut:
    """Преобразует строку БД в ответ API: file_path -> URL файла."""
    url = None
    if raw.get("file_path"):
        url = "/files/{0}".format(raw["id"])
    return MessageOut(
        id=raw["id"],
        chat_id=raw["chat_id"],
        username=raw["username"],
        content_type=raw["content_type"],
        text=raw.get("text"),
        file_url=url,
        created_at=raw["created_at"],
    )


class ChatAppState:
    """Контейнер зависимостей приложения."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = ChatStore(settings.database_path, settings.inactivity_days)
        self.files = FileStore(settings.storage_dir)
        self.broker = PollBroker()
        self.purge_task = None


def create_app(settings: Settings = None) -> FastAPI:
    fsettings = settings or load_settings()
    state = ChatAppState(fsettings)

    # ------------------------------------------------------------------ #
    # Фоновая очистка неактивных пользователей
    # ------------------------------------------------------------------ #
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await state.store.connect()

        async def purge_loop() -> None:
            """Периодически удаляет пользователей, не заходивших > N дней."""
            while True:
                await asyncio.sleep(state.settings.purge_interval_seconds)
                try:
                    await state.store.purge_inactive_users()
                except Exception:
                    pass

        state.purge_task = asyncio.create_task(purge_loop())
        try:
            yield
        finally:
            if state.purge_task is not None:
                state.purge_task.cancel()
                try:
                    await state.purge_task
                except asyncio.CancelledError:
                    pass
            await state.store.close()

    app = FastAPI(
        title="Анонимная система чатов",
        description=(
            "Анонимные чаты с long polling, текстом, картинками и видео."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.chat = state

    # ------------------------------------------------------------------ #
    # Эндпоинты
    # ------------------------------------------------------------------ #
    @app.post("/chats/join", response_model=JoinResponse)
    async def join_chat(payload: JoinRequest) -> JoinResponse:
        """Подключение к чату.

        Если чата с указанным номером нет — он создаётся. Если пользователь
        подключается впервые, пароль запоминается для этого чата. Повторный
        вход с тем же псевдонимом и другим паролем отклоняется (403).
        """
        username = payload.username.strip()
        try:
            final_id, created_user = await state.store.join(
                payload.chat_id, username, payload.password
            )
        except errors.ChatError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        await state.broker.notify(final_id)
        return JoinResponse(
            chat_id=final_id,
            username=username,
            created=created_user,
            message="Вы подключены к чату {0}".format(final_id),
        )

    @app.post("/chats/send", response_model=SendResponse)
    async def send_message(payload: SendTextRequest) -> SendResponse:
        """Отправка текстового сообщения в чат."""
        try:
            msg = await state.store.send_text(
                payload.chat_id, payload.username, payload.password, payload.text
            )
        except errors.ChatError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        await state.broker.notify(payload.chat_id)
        return SendResponse(message=_to_message_out(msg))

    @app.post("/chats/send_file", response_model=SendResponse)
    async def send_file(
        chat_id: int = Form(..., gt=0),
        username: str = Form(..., min_length=1),
        password: str = Form(..., min_length=1),
        file: UploadFile = File(...),
    ) -> SendResponse:
        """Отправка картинки или видео в чат (multipart/form-data)."""
        media_type = normalize_content_type(file.content_type)
        if not is_allowed_content_type(media_type):
            raise HTTPException(
                415,
                "Недопустимый тип контента '{0}'. Разрешено: {1}".format(
                    media_type or "не указан", ALLOWED_TYPES
                ),
            )
        try:
            placeholder = await state.store.add_media_message(
                chat_id, username, password, media_type
            )
        except errors.ChatError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

        message_id = placeholder["id"]
        try:
            path = await state.files.save_stream(
                message_id, file, state.settings.max_upload_size
            )
        except ValueError:
            await state.store.delete_message(message_id)
            raise HTTPException(
                413,
                "Файл слишком большой (лимит {0} байт)".format(
                    state.settings.max_upload_size
                ),
            )
        except OSError:
            await state.store.delete_message(message_id)
            raise HTTPException(500, "Не удалось сохранить файл")

        await state.store.attach_file(message_id, path)
        msg = await state.store.get_message(message_id)
        await state.broker.notify(chat_id)
        return SendResponse(message=_to_message_out(msg))

    @app.post("/chats/poll", response_model=PollResponse)
    async def poll_messages(payload: PollRequest) -> PollResponse:
        """Long polling: получить новые сообщения.

        Ждёт новые сообщения до timeout секунд (по умолчанию 30). Если новых
        сообщений нет — возвращает пустой список. Авторизует пользователя и
        обновляет метку его активности (last_seen_at).
        """
        try:
            await state.store.authenticate(
                payload.chat_id, payload.username, payload.password
            )
        except errors.ChatError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

        timeout = (
            float(state.settings.poll_timeout_default)
            if payload.timeout is None
            else max(
                0.0,
                min(float(payload.timeout), float(state.settings.poll_timeout_max)),
            )
        )
        messages = await state.store.list_messages(
            payload.chat_id, payload.last_message_id, state.settings.poll_batch_limit
        )
        if not messages and timeout > 0:
            notified = await state.broker.wait(payload.chat_id, timeout)
            if notified:
                messages = await state.store.list_messages(
                    payload.chat_id,
                    payload.last_message_id,
                    state.settings.poll_batch_limit,
                )
        return PollResponse(messages=[_to_message_out(m) for m in messages])

    @app.get("/files/{message_id}")
    async def get_file(message_id: int) -> FileResponse:
        """Скачивание файла, прикреплённого к сообщению."""
        info = await state.store.file_for_message(message_id)
        if info is None:
            raise HTTPException(404, "Файл не найден")
        _, stored_path, content_type = info
        try:
            path = state.files.resolve_path(stored_path)
        except ValueError:
            raise HTTPException(404, "Файл не найден")
        if not state.files.exists(path):
            raise HTTPException(404, "Файл не найден")
        return FileResponse(path, media_type=content_type)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()