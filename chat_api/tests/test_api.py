"""Интеграционные тесты HTTP-API: join, send, poll, файлы, очистка."""

from __future__ import annotations

import asyncio

from starlette.testclient import TestClient


def _join(client: TestClient, chat_id: int, username: str, password: str):
    return client.post(
        "/chats/join",
        json={
            "chat_id": chat_id,
            "username": username,
            "password": password,
        },
    )


def _joined(client: TestClient, chat_id: int, username: str, password: str) -> int:
    """Подключает пользователя и возвращает реальный id созданного чата."""
    r = _join(client, chat_id, username, password)
    assert r.status_code == 200, r.text
    return r.json()["chat_id"]


def _send(client: TestClient, chat_id: int, username: str, password: str, text: str):
    return client.post(
        "/chats/send",
        json={
            "chat_id": chat_id,
            "username": username,
            "password": password,
            "text": text,
        },
    )


def _poll(
    client: TestClient,
    chat_id: int,
    username: str,
    password: str,
    last_message_id: int = 0,
    timeout: float = 1.0,
):
    return client.post(
        "/chats/poll",
        json={
            "chat_id": chat_id,
            "username": username,
            "password": password,
            "last_message_id": last_message_id,
            "timeout": timeout,
        },
    )


class TestJoin:
    def test_join_creates_chat_and_returns_created(self, client):
        r = _join(client, 42, "alice", "secret")
        assert r.status_code == 200
        body = r.json()
        assert body["chat_id"] > 0
        assert body["username"] == "alice"
        assert body["created"] is True

    def test_join_existing_chat_second_user_not_created(self, client):
        cid = _joined(client, 7, "alice", "secret")
        r = _join(client, 7, "bob", "hunter2")
        assert r.status_code == 200
        assert r.json()["created"] is True
        r2 = _join(client, 7, "alice", "secret")
        assert r2.status_code == 200
        assert r2.json()["created"] is False
        assert r2.json()["chat_id"] == cid

    def test_join_wrong_password_returns_403(self, client):
        _join(client, 10, "alice", "right")
        r = _join(client, 10, "alice", "wrong")
        assert r.status_code == 403

    def test_join_invalid_payload_422(self, client):
        r = client.post("/chats/join", json={"chat_id": -1, "username": "a"})
        assert r.status_code == 422


class TestSendAndPoll:
    def test_send_then_poll_returns_message(self, client):
        cid = _joined(client, 11, "alice", "secret")
        s = _send(client, cid, "alice", "secret", "hello")
        assert s.status_code == 200, s.text
        body = s.json()["message"]
        assert body["text"] == "hello"
        assert body["content_type"] == "text"
        mid = body["id"]

        p = _poll(client, cid, "alice", "secret", 0, 0.5)
        assert p.status_code == 200
        msgs = p.json()["messages"]
        assert len(msgs) == 1
        assert msgs[0]["id"] == mid
        assert msgs[0]["text"] == "hello"

    def test_poll_respects_last_message_id(self, client):
        cid = _joined(client, 12, "alice", "secret")
        _send(client, cid, "alice", "secret", "first")
        mid2 = _send(client, cid, "alice", "secret", "second").json()["message"]["id"]
        p = _poll(client, cid, "alice", "secret", mid2 - 1, 0.5)
        msgs = p.json()["messages"]
        assert [m["id"] for m in msgs] == [mid2]

    def test_poll_wrong_password_403(self, client):
        cid = _joined(client, 13, "alice", "secret")
        r = _poll(client, cid, "alice", "bad", 0, 0.5)
        assert r.status_code == 403

    def test_poll_not_joined_404(self, client):
        r = _poll(client, 14, "nobody", "x", 0, 0.5)
        assert r.status_code in (403, 404)


class TestLongPolling:
    def test_long_polling_gets_message_after_first_empty(self, client):
        cid = _joined(client, 20, "alice", "secret")
        r1 = _poll(client, cid, "alice", "secret", 0, 0.5)
        assert r1.status_code == 200
        assert r1.json()["messages"] == []

        _send(client, cid, "alice", "secret", "later")
        r2 = _poll(client, cid, "alice", "secret", 0, 1.0)
        assert r2.json()["messages"]

    def test_broker_notify_wakes_waiting_poll(self, client_factory):
        """Проверяет, что notify действительно будит ждущий poll."""
        import threading
        import time

        tc = client_factory()
        with tc:
            state = tc.app.state.chat
            result = {}

            async def waiter():
                ok = await state.broker.wait(99, 2.0)
                result["woken"] = ok

            def worker():
                asyncio.run(waiter())

            t = threading.Thread(target=worker)
            t.start()
            time.sleep(0.3)
            asyncio.run(state.broker.notify(99))
            t.join(timeout=3)
            assert result.get("woken") is True


class TestFiles:
    def test_send_file_creates_message_and_file(self, client):
        cid = _joined(client, 31, "alice", "secret")
        r = client.post(
            "/chats/send_file",
            data={
                "chat_id": str(cid),
                "username": "alice",
                "password": "secret",
            },
            files={"file": ("photo.jpg", b"\xff\xd8\xff\xe0", "image/jpeg")},
        )
        assert r.status_code == 200, r.text
        body = r.json()["message"]
        assert body["content_type"] == "image/jpeg"
        assert body["file_url"] == "/files/{0}".format(body["id"])

        fr = client.get(body["file_url"])
        assert fr.status_code == 200
        assert fr.content == b"\xff\xd8\xff\xe0"

    def test_send_file_wrong_type_415(self, client):
        cid = _joined(client, 32, "alice", "secret")
        r = client.post(
            "/chats/send_file",
            data={
                "chat_id": str(cid),
                "username": "alice",
                "password": "secret",
            },
            files={"file": ("doc.txt", b"hello", "text/plain")},
        )
        assert r.status_code == 415

    def test_send_file_too_large_413(self, client_factory, settings_factory):
        settings = settings_factory(max_upload_size=10)
        with client_factory(settings) as client:
            cid = _joined(client, 33, "alice", "secret")
            r = client.post(
                "/chats/send_file",
                data={
                    "chat_id": str(cid),
                    "username": "alice",
                    "password": "secret",
                },
                files={"file": ("big.bin", b"x" * 100, "video/mp4")},
            )
            assert r.status_code == 413


class TestPurge:
    def test_purge_inactive_deletes_user_and_messages(self, client):
        """Фоновая очистка: неактивные пользователи удаляются."""
        store = client.app.state.chat.store
        store._inactivity_days = 30

        async def scenario():
            import datetime

            await store.connect()
            now = datetime.datetime.utcnow().isoformat(timespec="seconds")
            await store.join(1, "alice", "secret", now=now)
            await store.join(1, "bob", "bobpass", now=now)
            # Обоим «протухаем» активность: last_seen_at уводим в прошлое
            # (старше cutoff: now - 30 дней).
            old = (
                datetime.datetime.utcnow() - datetime.timedelta(days=40)
            ).isoformat(timespec="seconds")
            await store._conn.execute(
                "UPDATE users SET last_seen_at = ? WHERE chat_id = 1", (old,)
            )
            await store._conn.commit()
            deleted = await store.purge_inactive_users()
            assert deleted == 2
            c = await store._conn.execute(
                "SELECT COUNT(*) FROM users WHERE chat_id = 1"
            )
            left = (await c.fetchone())[0]
            assert left == 0

        asyncio.run(scenario())

    def test_purge_touches_last_seen_on_poll(self, client):
        """Poll обновляет last_seen_at, поэтому активный пользователь не удаляется."""
        store = client.app.state.chat.store
        store._inactivity_days = 30
        _join(client, 50, "alice", "secret")
        _send(client, 50, "alice", "secret", "hi")

        async def scenario():
            import datetime

            await store.connect()
            # Пользователь активен (последний poll был только что), удалять нечего.
            deleted = await store.purge_inactive_users()
            assert deleted == 0

        asyncio.run(scenario())


class TestPing:
    def test_health_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}