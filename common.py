# -*- coding: utf-8 -*-
"""Спільні помічники: час за Києвом, адміни, сповіщення, тексти, меню."""

import html
import logging
import os
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import Forbidden

import config
import db

logger = logging.getLogger(__name__)

KYIV = ZoneInfo("Europe/Kyiv")
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


def now_kyiv() -> datetime:
    return datetime.now(KYIV)


def today_kyiv() -> date:
    return now_kyiv().date()


def h(value) -> str:
    """Екранування для HTML-повідомлень."""
    return html.escape(str(value if value is not None else ""))


def fmt_day(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.strftime('%d.%m')}"


def fmt_date(d: date) -> str:
    return d.strftime("%d.%m.%Y")


# ---------- Головне меню ----------

MENU_BUTTONS = [
    ["📝 Записатися", "🆓 Пробне заняття"],
    ["📅 Розклад", "💳 Вартість"],
    ["🧾 Реквізити", "✅ Я оплатив"],
    ["🎫 Мій абонемент", "🥊 Напрямки"],
    ["👨‍🏫 Тренери", "📍 Адреса"],
    ["📞 Зв'язок", "🌐 Сайт"],
    ["📸 Instagram", "📜 Правила"],
]
MAIN_MENU = ReplyKeyboardMarkup(MENU_BUTTONS, resize_keyboard=True)
MENU_TEXTS = [t for row in MENU_BUTTONS for t in row]
CANCEL_TEXT = "❌ Скасувати"
CANCEL_KB = ReplyKeyboardMarkup([[CANCEL_TEXT]], resize_keyboard=True)


# ---------- Адміністратори ----------

def _env_admin_ids() -> set[int]:
    ids = set()
    for part in (os.getenv("ADMIN_IDS") or "").replace(" ", "").split(","):
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


ADMIN_IDS = set(config.ADMIN_IDS) | _env_admin_ids()

_group_admins: dict[int, tuple[float, set[int]]] = {}
ADMIN_CACHE_SECONDS = 600


def is_owner(user_id: int) -> bool:
    """Адмін бота (ADMIN_IDS) — отримує заявки, підтверджує оплати, робить розсилки."""
    return user_id in ADMIN_IDS


async def is_admin(context, chat_id, user_id: int) -> bool:
    """Адмін = ADMIN_IDS або адміністратор цієї групи в Telegram."""
    if user_id in ADMIN_IDS:
        return True
    if chat_id is None or chat_id > 0:
        return False
    cached = _group_admins.get(chat_id)
    if cached and time.time() - cached[0] < ADMIN_CACHE_SECONDS:
        return user_id in cached[1]
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        ids = {a.user.id for a in admins}
    except Exception as e:
        logger.warning("Не вдалось отримати адмінів групи: %s", e)
        ids = cached[1] if cached else set()
    _group_admins[chat_id] = (time.time(), ids)
    return user_id in ids


async def notify_admins(bot, text: str, reply_markup=None, photo=None, document=None) -> int:
    """Надсилає повідомлення всім ADMIN_IDS. Повертає, скільком вдалося."""
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS не задано — сповіщення адміну нікуди надіслати.")
        return 0
    sent = 0
    for admin_id in ADMIN_IDS:
        try:
            if photo:
                await bot.send_photo(admin_id, photo, caption=text, parse_mode=ParseMode.HTML,
                                     reply_markup=reply_markup)
            elif document:
                await bot.send_document(admin_id, document, caption=text, parse_mode=ParseMode.HTML,
                                        reply_markup=reply_markup)
            else:
                await bot.send_message(admin_id, text, parse_mode=ParseMode.HTML,
                                       reply_markup=reply_markup, disable_web_page_preview=True)
            sent += 1
        except Exception as e:
            logger.warning("Не вдалось надіслати адміну %s: %s", admin_id, e)
    return sent


async def send_user(bot, user_id: int, text: str, reply_markup=None) -> bool:
    """Повідомлення клієнту; якщо він заблокував бота — позначаємо в базі."""
    try:
        await bot.send_message(user_id, text, parse_mode=ParseMode.HTML, reply_markup=reply_markup,
                               disable_web_page_preview=True)
        return True
    except Forbidden:
        if db.enabled():
            try:
                await db.set_blocked(user_id)
            except Exception:
                pass
        return False
    except Exception as e:
        logger.warning("Не вдалось надіслати користувачу %s: %s", user_id, e)
        return False


def user_link(user_id: int, name: str = None, username: str = None) -> str:
    if username:
        return f"@{h(username)}"
    return f'<a href="tg://user?id={user_id}">{h(name or user_id)}</a>'


def deep_link_kb(bot_username: str, payload: str, label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, url=f"https://t.me/{bot_username}?start={payload}")]]
    )


# ---------- Тексти (config.py або змінені через /set) ----------

# key -> (назва для адміна, атрибут у config.py)
TEXT_KEYS = {
    "schedule": ("📅 Розклад", "SCHEDULE_TEXT"),
    "prices": ("💳 Вартість", "PRICES_TEXT"),
    "payment": ("🧾 Реквізити", "PAYMENT_TEXT"),
    "directions": ("🥊 Напрямки", "DIRECTIONS_TEXT"),
    "trainers": ("👨‍🏫 Тренери", "TRAINERS_TEXT"),
    "address": ("📍 Адреса", "ADDRESS_TEXT"),
    "contact": ("📞 Зв'язок", "CONTACT_TEXT"),
    "rules": ("📜 Правила", "RULES_TEXT"),
    "signup": ("📝 Текст про запис", "SIGNUP_TEXT"),
}

_settings_cache: dict[str, tuple[float, str | None]] = {}
SETTINGS_TTL = 60


def invalidate_text(key: str):
    _settings_cache.pop(key, None)


async def get_text(key: str) -> tuple[str, str]:
    """Повертає (текст, parse_mode). Текст із бази (через /set) має пріоритет над config.py."""
    if db.enabled():
        cached = _settings_cache.get(key)
        if cached and time.time() - cached[0] < SETTINGS_TTL:
            value = cached[1]
        else:
            try:
                value = await db.get_setting(f"text:{key}")
            except Exception as e:
                logger.warning("Не вдалось прочитати текст %s з бази: %s", key, e)
                value = cached[1] if cached else None
            _settings_cache[key] = (time.time(), value)
        if value:
            return value, ParseMode.HTML
    attr = TEXT_KEYS[key][1]
    return getattr(config, attr, ""), ParseMode.MARKDOWN
