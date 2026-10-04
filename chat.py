# -*- coding: utf-8 -*-
"""Консольный текстовый чат-клиент для chat_api.

Только текст: без картинок и файлов. Хост и порт можно поменять
переменными окружения CHAT_HOST / CHAT_PORT (по умолчанию силенок.рф:8000).

Запуск:  python chat.py
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request


def base_url():
    host = os.getenv("CHAT_HOST", "силенок.рф")
    try:
        host = host.encode("idna").decode("ascii")  # кириллический домен -> punycode
    except UnicodeError:
        pass
    port = os.getenv("CHAT_PORT", "8000")
    return "http://{}:{}".format(host, port)


BASE = base_url()


def post(path, payload, timeout=30):
    """Отправляет JSON на сервер и возвращает (status, body_bytes)."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def join(chat_id, username, password):
    """Подключиться к чату. print() при ошибке и завершение процесса."""
    status, body = post(
        "/chats/join",
        {"chat_id": chat_id, "username": username, "password": password},
    )
    if status != 200:
        text = body.decode("utf-8", "replace")
        print("Ошибка входа ({}): {}".format(status, text))
        raise SystemExit(1)
    return json.loads(body.decode("utf-8"))["chat_id"]


def send(chat_id, username, password, text):
    """Отправить текстовое сообщение; вернуть True в случае успеха."""
    status, body = post(
        "/chats/send",
        {"chat_id": chat_id, "username": username, "password": password, "text": text},
    )
    if status != 200:
        print("Ошибка отправки ({}): {}".format(status, body.decode("utf-8", "replace")))
        return False
    return True


def poll(chat_id, username, password, last_id, timeout=30):
    """Long-poll: вернуть новые сообщения (пустой список, если ничего нет)."""
    status, body = post(
        "/chats/poll",
        {
            "chat_id": chat_id,
            "username": username,
            "password": password,
            "last_message_id": last_id,
            "timeout": timeout,
        },
        timeout=timeout + 10,
    )
    if status != 200:
        print("Ошибка опроса ({}): {}".format(status, body.decode("utf-8", "replace")))
        return []
    return json.loads(body.decode("utf-8"))["messages"]


def message_str(msg):
    """Человекочитаемая строка сообщения."""
    if msg.get("content_type") == "system":
        return msg.get("text") or ""
    name = msg.get("username") or "?"
    text = msg.get("text") or "(файл/другое — пропущено)"
    return "{}: {}".format(name, text)


def main():
    print("Консольный текстовый чат (сервер: {})".format(BASE))
    print("Введите пустую строку, чтобы выйти.")
    chat_id = int(input("Номер чата: ").strip() or "0")
    username = input("Ваше имя: ").strip()
    password = input("Пароль: ").strip()

    chat_id = join(chat_id, username, password)

    print("Вход выполнен. Сообщения ниже:")
    last_id = 0

    def reader():
        """Фоновая нить: подтягивает новые сообщения."""
        nonlocal last_id
        while True:
            for msg in poll(chat_id, username, password, last_id, 30):
                if msg["id"] > last_id:
                    last_id = msg["id"]
                    print(message_str(msg), flush=True)
            time.sleep(0.2)

    threading.Thread(target=reader, daemon=True).start()

    try:
        while True:
            text = input().strip()
            if not text:
                break
            send(chat_id, username, password, text)
            print("> {}".format(text), flush=True)
    except (KeyboardInterrupt, EOFError):
        pass
    print("\nВыход.")

if __name__ == "__main__":
    main()