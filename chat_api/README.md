# chat_api — анонимная система чатов на FastAPI

Анонимные чаты с доставкой сообщений по **Long Polling**. Пользователь
подключается к чату по трём данным:

- **номер чата** — любой положительный идентификатор; чат создаётся
  автоматически при первом подключении;
- **псевдоним** — имя пользователя в рамках чата;
- **пароль** — запоминается при первом входе; повторный вход с тем же
  псевдонимом, но другим паролем отклоняется (HTTP 403).

Сообщения: текст, картинки (JPEG/PNG/GIF) и видео (MP4/WebM/MOV),
загружаемые как `multipart/form-data`.

## Возможности

- Автосоздание чата при первом подключении к номеру: чат
  регистрируется именно с указанным пользователем `chat_id`.
- Пароли хранятся только в виде хэша **PBKDF2-HMAC-SHA256** с солью.
- Long Polling на `asyncio.Condition` по каждому чату.
- Файлы хранятся на диске отдельно от базы данных.
- Автоматическая очистка: пользователь, не заходивший в чат дольше
  `CHAT_API_INACTIVITY_DAYS` (по умолчанию 30) дней, удаляется вместе со
  своими сообщениями и файлами (лениво при запросах и фоном).
- SQLite + `aiosqlite`: WAL, включённые внешние ключи, `busy_timeout`.

## Структура

```
chat_api/
    __init__.py     экспорт create_app/app
    main.py         фабрика FastAPI, эндпоинты, lifespan, фоновая очистка
    db.py           слой данных (aiosqlite): чаты, пользователи, сообщения
    auth.py         PBKDF2-хэширование паролей
    config.py       настройки (переменные окружения CHAT_API_*)
    errors.py       ошибки домена с HTTP-статусами
    files.py        сохранение/вычитка файлов
    models.py       Pydantic-схемы запросов/ответов
    polling.py      Long Polling брокер (asyncio.Condition)
tests/              интеграционные тесты (TestClient)
```

## Установка

```bash
pip install -e .[dev]      # Windows: pip install -e ".[dev]"
```

Зависимости: `fastapi`, `uvicorn[standard]`, `aiosqlite`,
`python-multipart`, `aiofiles`. Для тестов дополнительно `pytest`,
`pytest-asyncio`, `httpx`.

## Запуск

```bash
python -m chat_api
# или
uvicorn chat_api.main:app --host 127.0.0.1 --port 8000
```

Интерактивная документация: http://127.0.0.1:8000/docs

### Переменные окружения

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `CHAT_API_DB` | `chat_data/chat.db` | путь к SQLite-файлу |
| `CHAT_API_STORAGE` | `chat_data/files` | каталог для файлов |
| `CHAT_API_MAX_UPLOAD` | 52428800 | лимит размера файла (байты) |
| `CHAT_API_INACTIVITY_DAYS` | 30 | срок неактивности пользователя |
| `CHAT_API_POLL_TIMEOUT` | 30 | таймаут poll по умолчанию (сек) |
| `CHAT_API_POLL_TIMEOUT_MAX` | 60 | максимум для poll (сек) |
| `CHAT_API_PURGE_INTERVAL` | 3600 | период фоновой очистки (сек) |
| `CHAT_API_POLL_BATCH` | 200 | сообщений за один poll |

## API

### POST `/chats/join` — подключение к чату

Тело (JSON):

```json
{
  "chat_id": 42,
  "username": "alice",
  "password": "секрет"
}
```

Ответ `200`:

```json
{
  "chat_id": 42,
  "username": "alice",
  "created": true,
  "message": "Вы подключены к чату 42"
}
```

`created: true` — пользователь подключился впервые (или создан чат).

- Запись с существующим псевдонимом и **чужим** паролем → `403`.
- Невалидные данные (пустой псевдоним, отрицательный номер чата) → `422`.

### POST `/chats/send` — отправка текста

```json
{
  "chat_id": 42,
  "username": "alice",
  "password": "секрет",
  "text": "Привет!"
}
```

Ответ — `{"message": {...}}` с полями `id, chat_id, username,
content_type ("text"), text, file_url, created_at`.

### POST `/chats/send_file` — отправка картинки/видео

`multipart/form-data` с полями `chat_id`, `username`, `password` и полем
`file` (файл). Допустимые типы: `image/jpeg`, `image/png`, `image/gif`,
`video/mp4`, `video/webm`, `video/quicktime`.

- Неверный тип → `415`.
- Файл больше `CHAT_API_MAX_UPLOAD` → `413`.
- Неверный пароль → `403`.

Ответ — то же `{"message": {...}}`, но у сообщения заполнен
`content_type` и `file_url` (например `/files/7`). Скачивание — по этому
URL через `GET /files/{message_id}`.

### POST `/chats/poll` — получение новых сообщений (Long Polling)

```json
{
  "chat_id": 42,
  "username": "alice",
  "password": "секрет",
  "last_message_id": 5,
  "timeout": 30
}
```

Ответ:

```json
{
  "messages": [
    {
      "id": 6,
      "chat_id": 42,
      "username": "bob",
      "content_type": "text",
      "text": "Привет!",
      "file_url": null,
      "created_at": "2026-10-03T12:00:00"
    }
  ]
}
```

Сервер ждёт до `timeout` секунд (по умолчанию 30, не более 60), затем
возвращает сообщения с `id > last_message_id` (не более
`CHAT_API_POLL_BATCH`). Если новых сообщений нет — пустой список.

### GET `/health` — проверка работоспособности

```json
{"status": "ok"}
```

## Тесты

```bash
python -m pytest
```

Тесты интеграционные: поднимают приложение через `TestClient`(httpx) с
временной базой и хранилищем в `tmp_path`.

## Модель данных

- `chats(id, created_at)` — номера чатов;
- `users(id, chat_id, username, password_hash, joined_at, last_seen_at)` —
  псевдонимы с хэшами паролей в рамках чата;
- `messages(id, chat_id, user_id, username, content_type, text,
  file_path, created_at)` — сообщения. `file_path` заполнен только для
  картинок/видео.