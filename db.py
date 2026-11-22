# -*- coding: utf-8 -*-
"""
База даних бота (Postgres / Supabase): клієнти, записи, оплати, абонементи,
дні народження, тексти, змінені через бота.

Підключення — змінна середовища DATABASE_URL. Без неї ці функції бота вимкнені,
а меню, розклад і антиспам працюють як раніше.
"""

import logging
import os
from datetime import date, timedelta

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS fct_users (
    user_id    BIGINT PRIMARY KEY,
    first_name TEXT,
    last_name  TEXT,
    username   TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    blocked    BOOLEAN NOT NULL DEFAULT false
);

ALTER TABLE fct_users ADD COLUMN IF NOT EXISTS consent_at TIMESTAMPTZ;
ALTER TABLE fct_users ADD COLUMN IF NOT EXISTS consent_version TEXT;
ALTER TABLE fct_users ADD COLUMN IF NOT EXISTS news_off BOOLEAN NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS fct_groups (
    chat_id  BIGINT PRIMARY KEY,
    title    TEXT,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fct_bookings (
    id          SERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    is_trial    BOOLEAN NOT NULL DEFAULT false,
    direction   TEXT NOT NULL,
    slot_title  TEXT NOT NULL,
    slot_time   TEXT NOT NULL,
    day         DATE NOT NULL,
    name        TEXT NOT NULL,
    phone       TEXT NOT NULL,
    age         INTEGER,
    experience  TEXT,
    goal        TEXT,
    status      TEXT NOT NULL DEFAULT 'new',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at  TIMESTAMPTZ,
    reminded_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS fct_bookings_day ON fct_bookings (day, status);

CREATE TABLE IF NOT EXISTS fct_payments (
    id         SERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    student    TEXT NOT NULL,
    file_id    TEXT NOT NULL,
    file_kind  TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    sub_type   TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS fct_subscriptions (
    id            SERIAL PRIMARY KEY,
    user_id       BIGINT NOT NULL,
    student       TEXT NOT NULL,
    sub_type      TEXT NOT NULL,
    title         TEXT NOT NULL,
    amount        INTEGER NOT NULL DEFAULT 0,
    starts        DATE NOT NULL,
    ends          DATE NOT NULL,
    payment_id    INTEGER,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    soon_reminded BOOLEAN NOT NULL DEFAULT false,
    end_reminded  BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS fct_subs_user ON fct_subscriptions (user_id, student);

CREATE TABLE IF NOT EXISTS fct_birthdays (
    id         SERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    name       TEXT NOT NULL,
    day        SMALLINT NOT NULL,
    month      SMALLINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS fct_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fct_warnings (
    chat_id    BIGINT      NOT NULL,
    user_id    BIGINT      NOT NULL,
    count      INTEGER     NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS fct_kv (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_pool = None


def enabled() -> bool:
    return _pool is not None


async def open_pool() -> bool:
    """Відкриває пул з'єднань і створює таблиці. Повертає True, якщо база доступна."""
    global _pool
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        logger.warning("DATABASE_URL не задано — запис, оплати й абонементи вимкнені.")
        return False
    try:
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            url,
            min_size=0,
            max_size=3,
            open=False,
            timeout=15,
            max_idle=120,
            # prepare_threshold=None — сумісність з пулером Supabase / PgBouncer
            kwargs={"autocommit": True, "prepare_threshold": None, "connect_timeout": 10},
        )
        await pool.open(wait=False)
        async with pool.connection() as conn:
            await conn.execute(SCHEMA)
        _pool = pool
        logger.info("База даних: таблиці бота готові ✓")
        return True
    except Exception as e:
        logger.error("Не вдалось підключитись до бази: %s", e)
        return False


async def close_pool():
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def _fetch(sql, params=(), one=False):
    async with _pool.connection() as conn:
        cur = await conn.execute(sql, params)
        if cur.description is None:
            return None
        cols = [c.name for c in cur.description]
        rows = await cur.fetchall()
        result = [dict(zip(cols, r)) for r in rows]
        if one:
            return result[0] if result else None
        return result


async def _exec(sql, params=()):
    async with _pool.connection() as conn:
        await conn.execute(sql, params)


# ---------- Користувачі й групи ----------

async def touch_user(u):
    await _exec(
        """
        INSERT INTO fct_users (user_id, first_name, last_name, username)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            first_name = EXCLUDED.first_name, last_name = EXCLUDED.last_name,
            username = EXCLUDED.username, last_seen = now(), blocked = false
        """,
        (u.id, u.first_name, u.last_name, u.username),
    )


async def set_blocked(user_id: int):
    await _exec("UPDATE fct_users SET blocked = true WHERE user_id = %s", (user_id,))


async def active_user_ids():
    """Отримувачі розсилки: не заблокували бота і не відмовились від розсилок."""
    rows = await _fetch("SELECT user_id FROM fct_users WHERE NOT blocked AND NOT news_off ORDER BY user_id")
    return [r["user_id"] for r in rows]


# ---------- Згода й права клієнта (Закон «Про захист персональних даних») ----------

async def consent_version(user_id: int):
    row = await _fetch("SELECT consent_version FROM fct_users WHERE user_id = %s", (user_id,), one=True)
    return row["consent_version"] if row else None


async def set_consent(u, version: str):
    await _exec(
        """
        INSERT INTO fct_users (user_id, first_name, last_name, username, consent_at, consent_version)
        VALUES (%s, %s, %s, %s, now(), %s)
        ON CONFLICT (user_id) DO UPDATE SET consent_at = now(), consent_version = EXCLUDED.consent_version
        """,
        (u.id, u.first_name, u.last_name, u.username, version),
    )


async def set_news_off(user_id: int, off: bool):
    await _exec("UPDATE fct_users SET news_off = %s WHERE user_id = %s", (off, user_id))


async def export_user_data(user_id: int) -> dict:
    return {
        "user": await _fetch("SELECT * FROM fct_users WHERE user_id = %s", (user_id,), one=True),
        "bookings": await _fetch("SELECT * FROM fct_bookings WHERE user_id = %s ORDER BY id", (user_id,)),
        "payments": await _fetch(
            "SELECT id, student, status, sub_type, created_at FROM fct_payments WHERE user_id = %s ORDER BY id",
            (user_id,)),
        "subscriptions": await _fetch(
            "SELECT * FROM fct_subscriptions WHERE user_id = %s ORDER BY id", (user_id,)),
        "birthdays": await _fetch("SELECT * FROM fct_birthdays WHERE user_id = %s ORDER BY id", (user_id,)),
        "warnings": await _fetch(
            "SELECT chat_id, count, updated_at FROM fct_warnings WHERE user_id = %s", (user_id,)),
    }


async def delete_user_data(user_id: int) -> dict:
    """Видаляє всі дані користувача з бази (право на видалення / відкликання згоди)."""
    counts = {}
    async with _pool.connection() as conn:
        async with conn.transaction():
            for table in ("fct_bookings", "fct_payments", "fct_subscriptions", "fct_birthdays",
                          "fct_warnings", "fct_users"):
                cur = await conn.execute(f"DELETE FROM {table} WHERE user_id = %s", (user_id,))
                counts[table] = cur.rowcount
    return counts


async def cleanup(bookings_days: int, payments_days: int, inactive_days: int) -> dict:
    """Видаляє дані, строк зберігання яких минув (див. config.py, розділ КОНФІДЕНЦІЙНІСТЬ)."""
    counts = {}
    async with _pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "DELETE FROM fct_bookings WHERE day < current_date - %s::int", (bookings_days,))
            counts["bookings"] = cur.rowcount
            cur = await conn.execute(
                "DELETE FROM fct_payments WHERE created_at < now() - make_interval(days => %s)",
                (payments_days,))
            counts["payments"] = cur.rowcount
            cur = await conn.execute(
                "DELETE FROM fct_subscriptions WHERE ends < current_date - %s::int", (payments_days,))
            counts["subscriptions"] = cur.rowcount
            # Неактивні користувачі без чинних оплат/абонементів — видаляємо разом з їхніми даними
            cur = await conn.execute(
                """
                SELECT user_id FROM fct_users u
                WHERE last_seen < now() - make_interval(days => %s)
                  AND NOT EXISTS (SELECT 1 FROM fct_payments p WHERE p.user_id = u.user_id)
                  AND NOT EXISTS (SELECT 1 FROM fct_subscriptions s WHERE s.user_id = u.user_id)
                """,
                (inactive_days,),
            )
            ids = [r[0] for r in await cur.fetchall()]
            for table in ("fct_bookings", "fct_birthdays", "fct_warnings", "fct_users"):
                await conn.execute(f"DELETE FROM {table} WHERE user_id = ANY(%s)", (ids,))
            counts["inactive_users"] = len(ids)
            cur = await conn.execute(
                "DELETE FROM fct_kv WHERE created_at < now() - interval '400 days'")
            counts["kv"] = cur.rowcount
    return counts


async def touch_group(chat_id: int, title: str):
    await _exec(
        """
        INSERT INTO fct_groups (chat_id, title) VALUES (%s, %s)
        ON CONFLICT (chat_id) DO UPDATE SET title = EXCLUDED.title
        """,
        (chat_id, title),
    )


async def remove_group(chat_id: int):
    await _exec("DELETE FROM fct_groups WHERE chat_id = %s", (chat_id,))


async def group_ids():
    rows = await _fetch("SELECT chat_id FROM fct_groups")
    return [r["chat_id"] for r in rows]


# ---------- Записи ----------

async def create_booking(b: dict) -> dict:
    return await _fetch(
        """
        INSERT INTO fct_bookings (user_id, is_trial, direction, slot_title, slot_time, day,
                                  name, phone, age, experience, goal)
        VALUES (%(user_id)s, %(is_trial)s, %(direction)s, %(slot_title)s, %(slot_time)s, %(day)s,
                %(name)s, %(phone)s, %(age)s, %(experience)s, %(goal)s)
        RETURNING *
        """,
        b,
        one=True,
    )


async def get_booking(bid: int):
    return await _fetch("SELECT * FROM fct_bookings WHERE id = %s", (bid,), one=True)


async def set_booking_status(bid: int, status: str, only_from=("new",)):
    """Змінює статус, лише якщо поточний — один з only_from. Повертає оновлений запис або None."""
    return await _fetch(
        """
        UPDATE fct_bookings SET status = %s, decided_at = now()
        WHERE id = %s AND status = ANY(%s)
        RETURNING *
        """,
        (status, bid, list(only_from)),
        one=True,
    )


async def upcoming_bookings(user_id: int, today: date):
    return await _fetch(
        """
        SELECT * FROM fct_bookings
        WHERE user_id = %s AND day >= %s AND status IN ('new', 'confirmed')
        ORDER BY day, slot_time
        """,
        (user_id, today),
    )


async def bookings_to_remind(day: date):
    return await _fetch(
        """
        SELECT * FROM fct_bookings
        WHERE day = %s AND status = 'confirmed' AND reminded_at IS NULL
        """,
        (day,),
    )


async def mark_booking_reminded(bid: int) -> bool:
    row = await _fetch(
        "UPDATE fct_bookings SET reminded_at = now() WHERE id = %s AND reminded_at IS NULL RETURNING id",
        (bid,),
        one=True,
    )
    return row is not None


async def last_booking_name(user_id: int):
    row = await _fetch(
        "SELECT name FROM fct_bookings WHERE user_id = %s ORDER BY id DESC LIMIT 1",
        (user_id,),
        one=True,
    )
    return row["name"] if row else None


# ---------- Оплати й абонементи ----------

async def create_payment(user_id, student, file_id, file_kind) -> dict:
    return await _fetch(
        """
        INSERT INTO fct_payments (user_id, student, file_id, file_kind)
        VALUES (%s, %s, %s, %s) RETURNING *
        """,
        (user_id, student, file_id, file_kind),
        one=True,
    )


async def decide_payment(pid: int, status: str, sub_type=None):
    return await _fetch(
        """
        UPDATE fct_payments SET status = %s, sub_type = %s, decided_at = now()
        WHERE id = %s AND status = 'pending' RETURNING *
        """,
        (status, sub_type, pid),
        one=True,
    )


async def latest_end(user_id: int, student: str, sub_type: str):
    row = await _fetch(
        """
        SELECT max(ends) AS ends FROM fct_subscriptions
        WHERE user_id = %s AND lower(student) = lower(%s) AND sub_type = %s
        """,
        (user_id, student, sub_type),
        one=True,
    )
    return row["ends"] if row else None


async def create_subscription(user_id, student, st: dict, today: date, payment_id=None) -> dict:
    """Абонемент продовжується від кінця поточного, якщо той ще діє."""
    start = today
    if st["days"] > 1:
        prev = await latest_end(user_id, student, st["key"])
        if prev and prev >= today:
            start = prev + timedelta(days=1)
    ends = start + timedelta(days=max(1, st["days"]) - 1)
    return await _fetch(
        """
        INSERT INTO fct_subscriptions (user_id, student, sub_type, title, amount, starts, ends, payment_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (user_id, student, st["key"], st["title"], st["price"], start, ends, payment_id),
        one=True,
    )


async def user_subscriptions(user_id: int, today: date):
    """Чинні та останні (до 60 днів тому) абонементи клієнта."""
    return await _fetch(
        """
        SELECT * FROM fct_subscriptions
        WHERE user_id = %s AND ends >= %s
        ORDER BY student, ends DESC
        """,
        (user_id, today - timedelta(days=60)),
    )


async def subs_ending_soon(today: date, days: int):
    return await _fetch(
        """
        SELECT s.* FROM fct_subscriptions s
        WHERE s.sub_type IN (SELECT unnest(%s::text[]))
          AND NOT s.soon_reminded
          AND s.ends BETWEEN %s AND %s
          AND NOT EXISTS (
              SELECT 1 FROM fct_subscriptions n
              WHERE n.user_id = s.user_id AND lower(n.student) = lower(s.student)
                AND n.sub_type = s.sub_type AND n.ends > s.ends)
        """,
        (_long_types(), today, today + timedelta(days=days)),
    )


async def subs_just_ended(today: date):
    return await _fetch(
        """
        SELECT s.* FROM fct_subscriptions s
        WHERE s.sub_type IN (SELECT unnest(%s::text[]))
          AND NOT s.end_reminded
          AND s.ends < %s AND s.ends >= %s
          AND NOT EXISTS (
              SELECT 1 FROM fct_subscriptions n
              WHERE n.user_id = s.user_id AND lower(n.student) = lower(s.student)
                AND n.sub_type = s.sub_type AND n.ends > s.ends)
        """,
        (_long_types(), today, today - timedelta(days=7)),
    )


async def mark_sub(sid: int, field: str) -> bool:
    assert field in ("soon_reminded", "end_reminded")
    row = await _fetch(
        f"UPDATE fct_subscriptions SET {field} = true WHERE id = %s AND NOT {field} RETURNING id",
        (sid,),
        one=True,
    )
    return row is not None


async def debtors(today: date, lookback_days: int = 45):
    """Абонементи, що закінчились за останні lookback_days днів і не продовжені."""
    return await _fetch(
        """
        SELECT s.*, u.username, u.first_name FROM fct_subscriptions s
        LEFT JOIN fct_users u ON u.user_id = s.user_id
        WHERE s.sub_type IN (SELECT unnest(%s::text[]))
          AND s.ends < %s AND s.ends >= %s
          AND NOT EXISTS (
              SELECT 1 FROM fct_subscriptions n
              WHERE n.user_id = s.user_id AND lower(n.student) = lower(s.student)
                AND n.sub_type = s.sub_type AND n.ends > s.ends)
        ORDER BY s.ends
        """,
        (_long_types(), today, today - timedelta(days=lookback_days)),
    )


async def active_subscriptions(today: date):
    return await _fetch(
        """
        SELECT s.*, u.username, u.first_name FROM fct_subscriptions s
        LEFT JOIN fct_users u ON u.user_id = s.user_id
        WHERE s.sub_type IN (SELECT unnest(%s::text[])) AND s.ends >= %s AND s.starts <= %s
        ORDER BY s.ends
        """,
        (_long_types(), today, today),
    )


def _long_types():
    import config

    return [t["key"] for t in config.SUBSCRIPTION_TYPES if t["days"] >= 7]


# ---------- Дні народження ----------

async def add_birthday(user_id, name, day, month):
    return await _fetch(
        """
        INSERT INTO fct_birthdays (user_id, name, day, month) VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id, name) DO UPDATE SET day = EXCLUDED.day, month = EXCLUDED.month
        RETURNING *
        """,
        (user_id, name, day, month),
        one=True,
    )


async def user_birthdays(user_id):
    return await _fetch(
        "SELECT * FROM fct_birthdays WHERE user_id = %s ORDER BY month, day", (user_id,)
    )


async def delete_birthday(bid: int, user_id: int):
    await _exec("DELETE FROM fct_birthdays WHERE id = %s AND user_id = %s", (bid, user_id))


async def birthdays_on(day: int, month: int, include_feb29=False):
    if include_feb29:
        return await _fetch(
            "SELECT * FROM fct_birthdays WHERE (day = %s AND month = %s) OR (day = 29 AND month = 2)",
            (day, month),
        )
    return await _fetch(
        "SELECT * FROM fct_birthdays WHERE day = %s AND month = %s", (day, month)
    )


async def upcoming_birthdays():
    return await _fetch("SELECT * FROM fct_birthdays ORDER BY month, day")


# ---------- Тексти, змінені через бота ----------

async def get_setting(key: str):
    row = await _fetch("SELECT value FROM fct_settings WHERE key = %s", (key,), one=True)
    return row["value"] if row else None


async def set_setting(key: str, value: str):
    await _exec(
        """
        INSERT INTO fct_settings (key, value) VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        (key, value),
    )


async def delete_setting(key: str):
    await _exec("DELETE FROM fct_settings WHERE key = %s", (key,))


# ---------- Позначки «вже зроблено» (щоб не надсилати двічі) ----------

async def once(key: str) -> bool:
    """True, якщо ключ записано вперше (дію ще не виконували)."""
    row = await _fetch(
        "INSERT INTO fct_kv (key) VALUES (%s) ON CONFLICT (key) DO NOTHING RETURNING key",
        (key,),
        one=True,
    )
    return row is not None


# ---------- Статистика ----------

async def stats(today: date):
    week_ago = today - timedelta(days=7)
    return await _fetch(
        """
        SELECT
          (SELECT count(*) FROM fct_users) AS users,
          (SELECT count(*) FROM fct_users WHERE blocked) AS users_blocked,
          (SELECT count(*) FROM fct_users WHERE created_at >= %(week)s) AS users_new,
          (SELECT count(*) FROM fct_bookings WHERE created_at >= %(week)s) AS bookings,
          (SELECT count(*) FROM fct_bookings WHERE created_at >= %(week)s AND is_trial) AS trials,
          (SELECT count(*) FROM fct_bookings WHERE created_at >= %(week)s AND status = 'confirmed') AS b_confirmed,
          (SELECT count(*) FROM fct_bookings WHERE status = 'new') AS b_pending,
          (SELECT count(*) FROM fct_payments WHERE status = 'pending') AS p_pending,
          (SELECT count(*) FROM fct_subscriptions
             WHERE created_at >= %(month)s) AS subs_month,
          (SELECT coalesce(sum(amount), 0) FROM fct_subscriptions
             WHERE created_at >= %(month)s) AS sum_month,
          (SELECT count(*) FROM fct_groups) AS groups
        """,
        {"week": week_ago, "month": today - timedelta(days=30)},
        one=True,
    )
