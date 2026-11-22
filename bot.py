# -*- coding: utf-8 -*-
"""
FCT Assistant Bot — Telegram-бот для групи спортивного залу.

Функції:
  - Головне меню з кнопками (розклад, ціни, напрямки, запис, контакти...)
  - Автоматичне привітання нових учасників
  - Правила групи по команді
  - Базовий антиспам у групах (видалення посилань від не-адмінів + попередження)
  - Адмін-команди: /warn, /unwarn, /stats, /myid

Запуск: python bot.py
Токен читається зі змінної середовища BOT_TOKEN (файл .env або Environment на Render).
"""

import hashlib
import logging
import os
import time
from collections import defaultdict

from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

import config

# ---------- Налаштування логування ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx логує кожен запит разом з адресою, де є токен — приглушуємо
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ---------- Змінні середовища ----------
load_dotenv()
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()


def _env_admin_ids() -> set[int]:
    """ADMIN_IDS у змінних середовища: числа через кому, напр. 123456789,987654321"""
    ids = set()
    for part in (os.getenv("ADMIN_IDS") or "").replace(" ", "").split(","):
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


ADMIN_IDS = set(config.ADMIN_IDS) | _env_admin_ids()

# ---------- Пам'ять попереджень (в оперативній пам'яті; при перезапуску скидається) ----------
warnings_count: dict[int, int] = defaultdict(int)

# Кеш адміністраторів груп: chat_id -> (час, {user_id})
_group_admins: dict[int, tuple[float, set[int]]] = {}
ADMIN_CACHE_SECONDS = 600

# ---------- Кнопки головного меню ----------
MENU_BUTTONS = [
    ["📅 Розклад", "📝 Записатися"],
    ["💳 Вартість", "🥊 Напрямки"],
    ["👨‍🏫 Тренери", "📍 Адреса"],
    ["🌐 Сайт", "📸 Instagram"],
    ["📞 Зв'язок", "📜 Правила"],
]
MAIN_MENU = ReplyKeyboardMarkup(MENU_BUTTONS, resize_keyboard=True)
MENU_TEXTS = [t for row in MENU_BUTTONS for t in row]

GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)


async def is_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """Адмін = вказаний у ADMIN_IDS або адміністратор цієї групи в Telegram."""
    if user_id in ADMIN_IDS:
        return True
    if chat_id is None or chat_id > 0:  # особистий чат
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


# ---------- Команди ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🥊 Привіт! Я бот {config.BOT_NAME}.\nОбери пункт меню нижче 👇",
        reply_markup=MAIN_MENU,
    )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Головне меню 👇", reply_markup=MAIN_MENU)


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(config.RULES_TEXT, parse_mode=ParseMode.MARKDOWN)


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показує ваш Telegram ID — щоб вписати його в ADMIN_IDS."""
    user = update.effective_user
    await update.message.reply_text(f"Ваш Telegram ID: {user.id}")


# ---------- Обробники кнопок меню ----------

async def handle_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    replies = {
        "📅 Розклад": config.SCHEDULE_TEXT,
        "📝 Записатися": config.SIGNUP_TEXT,
        "💳 Вартість": config.PRICES_TEXT,
        "🥊 Напрямки": config.DIRECTIONS_TEXT,
        "👨‍🏫 Тренери": config.TRAINERS_TEXT,
        "📍 Адреса": config.ADDRESS_TEXT,
        "📞 Зв'язок": config.CONTACT_TEXT,
        "📜 Правила": config.RULES_TEXT,
    }

    if text == "🌐 Сайт":
        await update.message.reply_text(f"🌐 {config.WEBSITE_URL}")
        return
    if text == "📸 Instagram":
        await update.message.reply_text(f"📸 {config.INSTAGRAM_URL}")
        return

    if text in replies:
        await update.message.reply_text(
            replies[text], parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True
        )


async def private_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Будь-який інший текст в особистому чаті — підказка з меню."""
    await update.message.reply_text(
        "Оберіть пункт меню нижче 👇\nДля запису: 📝 Записатися", reply_markup=MAIN_MENU
    )


# ---------- Привітання нового учасника ----------

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        name = escape_markdown(member.full_name or "друже", version=1)
        text = config.WELCOME_TEXT.format(name=name, bot_name=config.BOT_NAME)
        try:
            await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.warning("Не вдалось надіслати привітання: %s", e)


# ---------- Антиспам (тільки в групах) ----------

def _has_link(message) -> bool:
    entities = list(message.entities or []) + list(message.caption_entities or [])
    for entity in entities:
        if entity.type in ("url", "text_link"):
            return True
    text = (message.text or "") + " " + (message.caption or "")
    return "http://" in text or "https://" in text or "t.me/" in text


async def anti_spam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Видаляє повідомлення з посиланнями від звичайних учасників групи."""
    if not config.DELETE_LINKS_FROM_MEMBERS:
        return

    message = update.message
    if message is None or message.from_user is None:
        return
    # Повідомлення від імені групи/каналу (анонімний адмін, прив'язаний канал) не чіпаємо
    if message.sender_chat is not None or message.is_automatic_forward:
        return
    if message.from_user.is_bot:
        return
    if not _has_link(message):
        return
    if await is_admin(context, message.chat_id, message.from_user.id):
        return

    try:
        await message.delete()
    except Exception as e:
        logger.warning("Не вдалось видалити повідомлення (бот має бути адміном групи): %s", e)
        return

    user = message.from_user
    warnings_count[user.id] += 1
    count = warnings_count[user.id]

    await context.bot.send_message(
        chat_id=message.chat_id,
        text=(
            f"⚠️ {user.mention_html()}, посилання в групі заборонені "
            f"({count}/{config.MAX_WARNINGS} попереджень)."
        ),
        parse_mode=ParseMode.HTML,
    )

    if count >= config.MAX_WARNINGS:
        try:
            await context.bot.ban_chat_member(chat_id=message.chat_id, user_id=user.id)
            await context.bot.send_message(
                chat_id=message.chat_id,
                text=f"🚫 {user.mention_html()} заблокований за повторний спам.",
                parse_mode=ParseMode.HTML,
            )
            warnings_count[user.id] = 0
        except Exception as e:
            logger.warning("Не вдалось заблокувати користувача: %s", e)


# ---------- Адмін-команди (у групі) ----------

async def warn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Використання: відповісти на повідомлення користувача командою /warn"""
    msg = update.message
    if not await is_admin(context, msg.chat_id, msg.from_user.id):
        return
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("Використайте /warn у відповідь на повідомлення користувача.")
        return

    target = msg.reply_to_message.from_user
    if target.is_bot or await is_admin(context, msg.chat_id, target.id):
        await msg.reply_text("Адміністраторам і ботам попередження не видаються.")
        return

    warnings_count[target.id] += 1
    count = warnings_count[target.id]
    await msg.reply_text(
        f"⚠️ {target.mention_html()} отримав попередження ({count}/{config.MAX_WARNINGS}).",
        parse_mode=ParseMode.HTML,
    )

    if count >= config.MAX_WARNINGS:
        try:
            await context.bot.ban_chat_member(chat_id=msg.chat_id, user_id=target.id)
            await msg.reply_text(f"🚫 {target.mention_html()} заблокований.", parse_mode=ParseMode.HTML)
            warnings_count[target.id] = 0
        except Exception as e:
            logger.warning("Не вдалось заблокувати користувача: %s", e)


async def unwarn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not await is_admin(context, msg.chat_id, msg.from_user.id):
        return
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("Використайте /unwarn у відповідь на повідомлення користувача.")
        return

    target = msg.reply_to_message.from_user
    if warnings_count[target.id] > 0:
        warnings_count[target.id] -= 1
    await msg.reply_text(
        f"✅ Знято попередження у {target.mention_html()} (зараз: {warnings_count[target.id]}).",
        parse_mode=ParseMode.HTML,
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.chat.type not in GROUP_TYPES:
        await msg.reply_text("Команда /stats працює в групі.")
        return
    if not await is_admin(context, msg.chat_id, msg.from_user.id):
        return
    member_count = await context.bot.get_chat_member_count(msg.chat_id)
    await msg.reply_text(f"📊 Учасників у групі: {member_count}")


# ---------- Помилки ----------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Помилка при обробці оновлення: %s", context.error, exc_info=context.error)


# ---------- Точка входу ----------

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("myid", my_id))
    app.add_handler(CommandHandler("warn", warn_user, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("unwarn", unwarn_user, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("stats", stats))

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(MessageHandler(filters.Text(MENU_TEXTS), handle_menu_button))

    # Антиспам — тільки в групах: текст і підписи до фото/відео
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            anti_spam,
        )
    )
    # Особистий чат: будь-який інший текст — показати меню
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, private_fallback)
    )

    app.add_error_handler(on_error)
    return app


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN не знайдено. Скопіюйте .env.example у .env і вставте туди токен від @BotFather "
            "(на Render — додайте BOT_TOKEN у Environment)."
        )

    app = build_app()

    # На Render адреса сервісу підставляється автоматично (RENDER_EXTERNAL_URL)
    webhook_url = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")

    if webhook_url:
        # Режим webhook — для Render (Web Service). Telegram сам «будить» сервіс новим повідомленням.
        port = int(os.getenv("PORT", 8080))
        # Секрет для перевірки, що запит прийшов саме від Telegram (виводиться з токена, не сам токен)
        secret = hashlib.sha256(("fct-webhook:" + BOT_TOKEN).encode()).hexdigest()[:48]
        logger.info("Бот запущено у режимі webhook: %s/telegram", webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="telegram",
            webhook_url=f"{webhook_url}/telegram",
            secret_token=secret,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        # Режим polling — для локального запуску без публічної адреси.
        logger.info("Бот запущено у режимі polling. Натисніть Ctrl+C для зупинки.")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
