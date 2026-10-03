"""Ошибки предметной области, возвращаемые как HTTP-ответы."""

from __future__ import annotations


class ChatError(Exception):
    """Ошибка бизнес-логики с HTTP-статусом и текстом для клиента."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def wrong_password(chat_id: int, username: str) -> ChatError:
    return ChatError(
        403,
        "В чате {0} уже существует пользователь с псевдонимом '{1}' "
        "и другим паролем.".format(chat_id, username),
    )


def not_joined(chat_id: int, username: str) -> ChatError:
    return ChatError(
        404,
        "Пользователь '{0}' не подключён к чату {1}. "
        "Сначала выполните подключение (POST /chats/join).".format(username, chat_id),
    )


def file_not_found(message_id: int) -> ChatError:
    return ChatError(404, "Файл сообщения {0} не найден.".format(message_id))