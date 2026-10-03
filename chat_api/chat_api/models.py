"""Pydantic-модели запросов и ответов API."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class JoinRequest(BaseModel):
    """Подключение к чату по псевдониму, паролю и номеру чата."""

    chat_id: int = Field(..., gt=0, description="Номер чата")
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class JoinResponse(BaseModel):
    chat_id: int
    username: str
    created: bool
    message: str


class MessageOut(BaseModel):
    id: int
    chat_id: int
    username: str
    content_type: str
    text: Optional[str] = None
    file_url: Optional[str] = None
    created_at: str


class PollRequest(BaseModel):
    """Long polling: получить сообщения с id > last_message_id."""

    chat_id: int = Field(..., gt=0)
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)
    last_message_id: int = Field(0, ge=0)
    timeout: Optional[float] = Field(
        None, ge=0, le=60, description="Секунды ожидания (по умолчанию 30)"
    )


class PollResponse(BaseModel):
    messages: List[MessageOut]


class SendTextRequest(BaseModel):
    """Отправка текстового сообщения (используется как справочная модель)."""

    chat_id: int = Field(..., gt=0)
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)
    text: str = Field(..., min_length=1, max_length=4000)


class SendResponse(BaseModel):
    message: MessageOut