# -*- coding: utf-8 -*-
"""
FCT Assistant Bot — Telegram-бот для групи спортивного залу.

Функції:
  - Головне меню з кнопками (розклад, ціни, напрямки, запис, контакти...)
  - Автоматичне привітання нових учасників
  - Правила групи по команді
  - Базовий антиспам (видалення посилань від не-адмінів + попередження)
  - Адмін-команди: /warn, /unwarn, /stats

Запуск: python bot.py
Токен читається зі змінної середовища BOT_TOKEN (файл .env).
"""

import logging
import os
from collections import defaultdict

from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config

# ---------- Налаштування логування ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- Завантаження токена ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

# ---------- Пам'ять попереджень (в оперативній пам'яті; при перезапуску скидається) ----------
warnings_count: dict[int, int] = defaultdict(int)

# ---------- Кнопки головного меню ----------
MENU_BUTTONS = [
    ["📅 Розклад", "📝 Записатися"],
    ["💳 Вартість", "🥊 Напрямки"],
    ["👨‍🏫 Тренери", "📍 Адреса"],
    ["🌐 Сайт", "📸 Instagram"],
    ["📞 Зв'язок", "📜 Правила"],
]
MAIN_MENU = ReplyKeyboardMarkup(MENU_BUTTONS, resize_keyboard=True)


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


# ---------- Команди ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🥊 Привіт! Я {config.BOT_NAME}.\nОбери пункт меню нижче 👇",
        reply_markup=MAIN_MENU,
    )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Головне меню 👇", reply_markup=MAIN_MENU)


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(config.RULES_TEXT, parse_mode=ParseMode.MARKDOWN)


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
        await update.message.reply_text(replies[text], parse_mode=ParseMode.MARKDOWN)


# ---------- Привітання нового учасника ----------

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        name = member.full_name
        text = config.WELCOME_TEXT.format(name=name, bot_name=config.BOT_NAME)
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ---------- Антиспам ----------

async def anti_spam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Видаляє повідомлення з посиланнями від звичайних учасників."""
    if not config.DELETE_LINKS_FROM_MEMBERS:
        return

    message = update.message
    if message is None or message.from_user is None:
        return

    if is_admin(message.from_user.id):
        return

    has_link = False
    if message.entities:
        for entity in message.entities:
            if entity.type in ("url", "text_link", "mention"):
                has_link = True
                break
    if message.text and ("http://" in message.text or "https://" in message.text or "t.me/" in message.text):
        has_link = True

    if has_link:
        try:
            await message.delete()
        except Exception as e:
            logger.warning("Не вдалось видалити повідомлення: %s", e)
            return

        user_id = message.from_user.id
        warnings_count[user_id] += 1
        count = warnings_count[user_id]

        warn_text = (
            f"⚠️ {message.from_user.mention_html()}, посилання в групі заборонені "
            f"({count}/{config.MAX_WARNINGS} попереджень)."
        )
        await context.bot.send_message(
            chat_id=message.chat_id, text=warn_text, parse_mode=ParseMode.HTML
        )

        if count >= config.MAX_WARNINGS:
            try:
                await context.bot.ban_chat_member(chat_id=message.chat_id, user_id=user_id)
                await context.bot.send_message(
                    chat_id=message.chat_id,
                    text=f"🚫 {message.from_user.mention_html()} заблокований за повторний спам.",
                    parse_mode=ParseMode.HTML,
                )
                warnings_count[user_id] = 0
            except Exception as e:
                logger.warning("Не вдалось заблокувати користувача: %s", e)


# ---------- Адмін-команди ----------

async def warn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Використання: відповісти на повідомлення користувача командою /warn"""
    if not is_admin(update.message.from_user.id):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Використайте /warn у відповідь на повідомлення користувача.")
        return

    target = update.message.reply_to_message.from_user
    warnings_count[target.id] += 1
    count = warnings_count[target.id]
    await update.message.reply_text(
        f"⚠️ {target.mention_html()} отримав попередження ({count}/{config.MAX_WARNINGS}).",
        parse_mode=ParseMode.HTML,
    )

    if count >= config.MAX_WARNINGS:
        try:
            await context.bot.ban_chat_member(chat_id=update.message.chat_id, user_id=target.id)
            await update.message.reply_text(f"🚫 {target.mention_html()} заблокований.", parse_mode=ParseMode.HTML)
            warnings_count[target.id] = 0
        except Exception as e:
            logger.warning("Не вдалось заблокувати користувача: %s", e)


async def unwarn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.message.from_user.id):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Використайте /unwarn у відповідь на повідомлення користувача.")
        return

    target = update.message.reply_to_message.from_user
    if warnings_count[target.id] > 0:
        warnings_count[target.id] -= 1
    await update.message.reply_text(
        f"✅ Знято попередження у {target.mention_html()} (зараз: {warnings_count[target.id]}).",
        parse_mode=ParseMode.HTML,
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.message.from_user.id):
        return
    member_count = await context.bot.get_chat_member_count(update.message.chat_id)
    await update.message.reply_text(f"📊 Учасників у групі: {member_count}")


# ---------- Точка входу ----------

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN не знайдено. Скопіюйте .env.example у .env і вставте туди токен від @BotFather."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("warn", warn_user))
    app.add_handler(CommandHandler("unwarn", unwarn_user))
    app.add_handler(CommandHandler("stats", stats))

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))

    menu_texts = [
        "📅 Розклад", "📝 Записатися", "💳 Вартість", "🥊 Напрямки",
        "👨‍🏫 Тренери", "📍 Адреса", "🌐 Сайт", "📸 Instagram",
        "📞 Зв'язок", "📜 Правила",
    ]
    app.add_handler(MessageHandler(filters.Text(menu_texts), handle_menu_button))

    # Антиспам — перевіряє всі інші текстові повідомлення в групі
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, anti_spam))

    webhook_url = os.getenv("WEBHOOK_URL")  # напр. https://fct-bot.onrender.com

    if webhook_url:
        # Режим webhook — використовується на Render (Web Service).
        port = int(os.getenv("PORT", 8080))
        logger.info("Бот запущено у режимі webhook: %s", webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=BOT_TOKEN,  # токен у шляху — щоб адресу вебхука не вгадали сторонні
            webhook_url=f"{webhook_url}/{BOT_TOKEN}",
        )
    else:
        # Режим polling — для локального запуску без публічної адреси.
        logger.info("Бот запущено у режимі polling. Натисніть Ctrl+C для зупинки.")
        app.run_polling()


if __name__ == "__main__":
    main()
