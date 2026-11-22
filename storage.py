# -*- coding: utf-8 -*-
"""
Зберігання попереджень антиспаму.

Якщо задано DATABASE_URL (Postgres, наприклад Supabase) — попередження зберігаються в базі
і не скидаються після перезапуску чи «сну» бота на Render.
Якщо DATABASE_URL немає — працює як раніше, в оперативній пам'яті.
"""

import logging
import os
from collections import defaultdict

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS fct_warnings (
    chat_id    BIGINT      NOT NULL,
    user_id    BIGINT      NOT NULL,
    count      INTEGER     NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, user_id)
);
"""


class MemoryStore:
    """Попередження в пам'яті процесу (скидаються при перезапуску)."""

    kind = "пам'ять (скидається при перезапуску)"

    def __init__(self):
        self._data: dict[tuple[int, int], int] = defaultdict(int)

    async def init(self):
        pass

    async def get(self, chat_id: int, user_id: int) -> int:
        return self._data[(chat_id, user_id)]

    async def add(self, chat_id: int, user_id: int, delta: int) -> int:
        key = (chat_id, user_id)
        self._data[key] = max(0, self._data[key] + delta)
        return self._data[key]

    async def reset(self, chat_id: int, user_id: int) -> None:
        self._data.pop((chat_id, user_id), None)


class PostgresStore:
    """Попередження в Postgres. Кожна операція — коротке окреме з'єднання."""

    kind = "база даних"

    def __init__(self, url: str):
        self.url = url

    async def _connect(self):
        import psycopg  # імпорт тут, щоб без бази бот працював і без цієї бібліотеки

        # prepare_threshold=None — сумісність з пулерами з'єднань (Supabase pooler / PgBouncer)
        return await psycopg.AsyncConnection.connect(
            self.url, autocommit=True, connect_timeout=10, prepare_threshold=None
        )

    async def init(self):
        async with await self._connect() as conn:
            await conn.execute(SCHEMA)

    async def get(self, chat_id: int, user_id: int) -> int:
        async with await self._connect() as conn:
            cur = await conn.execute(
                "SELECT count FROM fct_warnings WHERE chat_id = %s AND user_id = %s",
                (chat_id, user_id),
            )
            row = await cur.fetchone()
            return row[0] if row else 0

    async def add(self, chat_id: int, user_id: int, delta: int) -> int:
        async with await self._connect() as conn:
            cur = await conn.execute(
                """
                INSERT INTO fct_warnings (chat_id, user_id, count)
                VALUES (%s, %s, GREATEST(0, %s))
                ON CONFLICT (chat_id, user_id) DO UPDATE
                    SET count = GREATEST(0, fct_warnings.count + %s),
                        updated_at = now()
                RETURNING count
                """,
                (chat_id, user_id, delta, delta),
            )
            row = await cur.fetchone()
            return row[0]

    async def reset(self, chat_id: int, user_id: int) -> None:
        async with await self._connect() as conn:
            await conn.execute(
                "DELETE FROM fct_warnings WHERE chat_id = %s AND user_id = %s",
                (chat_id, user_id),
            )


class SafeStore:
    """Обгортка: якщо база недоступна — бот не падає, а тимчасово рахує в пам'яті."""

    def __init__(self, primary, fallback: MemoryStore):
        self.primary = primary
        self.fallback = fallback

    @property
    def kind(self):
        return self.primary.kind

    async def init(self):
        try:
            await self.primary.init()
            logger.info("Попередження зберігаються: %s ✓", self.primary.kind)
        except Exception as e:
            logger.error("База даних недоступна, попередження тимчасово в пам'яті: %s", e)

    async def _call(self, name, *args):
        try:
            return await getattr(self.primary, name)(*args)
        except Exception as e:
            logger.error("Помилка бази (%s), використовую пам'ять: %s", name, e)
            return await getattr(self.fallback, name)(*args)

    async def get(self, chat_id, user_id):
        return await self._call("get", chat_id, user_id)

    async def add(self, chat_id, user_id, delta):
        return await self._call("add", chat_id, user_id, delta)

    async def reset(self, chat_id, user_id):
        return await self._call("reset", chat_id, user_id)


def make_store():
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        return MemoryStore()
    return SafeStore(PostgresStore(url), MemoryStore())
