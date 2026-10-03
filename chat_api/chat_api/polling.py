"""Long Polling слой.

Потребитель (poll) вызывает get_new_messages() и ждёт либо появления новых
сообщений, либо таймаута. Производитель (send) вызывает notify(), чтобы
разбудить всех ожидающих клиентов чата.
"""

from __future__ import annotations

import asyncio
from typing import Dict, List


class PollBroker:
    """Рассылка уведомлений о новых сообщениях по чатам."""

    def __init__(self) -> None:
        # id чата -> asyncio.Condition, по которой будят ждущих.
        self._conditions: Dict[int, asyncio.Condition] = {}

    def _condition(self, chat_id: int) -> asyncio.Condition:
        cond = self._conditions.get(chat_id)
        if cond is None:
            loop = asyncio.get_event_loop()
            cond = asyncio.Condition()
            cond._loop = loop
            self._conditions[chat_id] = cond
        return cond

    async def notify(self, chat_id: int) -> None:
        cond = self._conditions.get(chat_id)
        if cond is None:
            return
        async with cond:
            cond.notify_all()

    async def wait(self, chat_id: int, timeout: float) -> bool:
        """Ждёт уведомления о новых сообщениях.

        Возвращает True, если уведомление получено, False — таймаут.
        """
        cond = self._condition(chat_id)
        async with cond:
            try:
                await asyncio.wait_for(cond.wait(), timeout)
            except asyncio.TimeoutError:
                return False
        return True