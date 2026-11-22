# -*- coding: utf-8 -*-
"""
FCT Assistant Bot — Telegram-бот Fight Club Tornado.

Функції:
  - Головне меню: розклад, ціни, реквізити, напрямки, тренери, адреса, контакти
  - Запис на тренування і пробне заняття (заявка приходить адміну з кнопками)
  - «Я оплатив»: квитанція → адмін підтверджує → абонемент
  - «Мій абонемент», нагадування про тренування й кінець абонемента
  - Розсилка, опитування в групі, дні народження, зміна текстів у боті (/set), статистика
  - Привітання нових учасників, антиспам із попередженнями (зберігаються в базі)

Запуск: python bot.py
Змінні середовища: BOT_TOKEN (обов'язково), DATABASE_URL, ADMIN_IDS, CRON_SECRET (необов'язково).
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import signal
import time
import warnings

from dotenv import load_dotenv
from telegram import BotCommand, BotCommandScopeAllPrivateChats, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.helpers import escape_markdown
from telegram.warnings import PTBUserWarning

import admin_tools
import config
import db
import flows
import privacy
from common import ADMIN_IDS, MAIN_MENU, MENU_TEXTS, get_text, is_admin, is_owner
from storage import make_store

# ---------- Налаштування логування ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx логує кожен запит разом з адресою, де є токен — приглушуємо
logging.getLogger("httpx").setLevel(logging.WARNING)
# tornado логує кожен запит разом із ключем cron в адресі — приглушуємо
logging.getLogger("tornado.access").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)
# Попередження PTB про CallbackQueryHandler у діалогах — очікувана поведінка
warnings.filterwarnings("ignore", category=PTBUserWarning)

# ---------- Змінні середовища ----------
load_dotenv()
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()

# ---------- Сховище попереджень (база даних або пам'ять) ----------
store = make_store()

GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)

# Кнопки, які обробляють окремі діалоги (flows.py) — загальне меню їх пропускає
FLOW_MENU = {"📝 Записатися", "🆓 Пробне заняття", "✅ Я оплатив", "🎫 Мій абонемент"}

# Кнопка → ключ тексту (config.py або змінений через /set)
TEXT_BUTTONS = {
    "📅 Розклад": "schedule",
    "💳 Вартість": "prices",
    "🥊 Напрямки": "directions",
    "👨‍🏫 Тренери": "trainers",
    "📍 Адреса": "address",
    "📞 Зв'язок": "contact",
    "📜 Правила": "rules",
}


async def reply_text_key(message, key: str, reply_markup=None):
    text, mode = await get_text(key)
    try:
        await message.reply_text(text, parse_mode=mode, disable_web_page_preview=True,
                                 reply_markup=reply_markup)
    except Exception as e:
        # Якщо форматування зламане — надсилаємо як звичайний текст, щоб клієнт усе одно отримав відповідь
        logger.warning("Текст %s з помилкою форматування: %s", key, e)
        await message.reply_text(text, disable_web_page_preview=True, reply_markup=reply_markup)


# ---------- Команди ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🥊 Привіт! Я бот {config.BOT_NAME}.\n\n"
        "Тут можна записатись на тренування чи пробне заняття, подивитись розклад і ціни, "
        "оплатити й перевірити абонемент.\n\nОбери пункт меню нижче 👇\n\n"
        "🔒 Як бот працює з даними: /privacy",
        reply_markup=MAIN_MENU,
    )
    if update.effective_chat.type == "private" and is_owner(update.effective_user.id):
        await update.message.reply_text("🛠 Ви адміністратор. Команди: /admin")


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Головне меню 👇", reply_markup=MAIN_MENU)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ Що вміє бот:\n\n"
        "📝 /book — запис на тренування\n"
        "🆓 /trial — пробне заняття\n"
        "✅ /paid — надіслати квитанцію про оплату\n"
        "🎫 /my — мій абонемент і записи\n"
        "🎂 /birthday — вказати день народження (привітаємо в групі)\n"
        "📋 /menu — головне меню\n"
        "❌ /cancel — скасувати поточну дію\n\n"
        "🔒 Ваші дані:\n"
        "/privacy — політика конфіденційності\n"
        "/mydata — які дані про вас зберігає бот\n"
        "/delete_me — видалити свої дані\n"
        "/news_off — відмовитись від розсилок",
        reply_markup=MAIN_MENU,
    )


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_text_key(update.message, "rules")


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показує ваш Telegram ID — щоб вписати його в ADMIN_IDS."""
    await update.message.reply_text(f"Ваш Telegram ID: {update.effective_user.id}")


async def cancel_idle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cancel поза діалогом — просто показуємо меню."""
    if context.user_data.get("in_flow"):
        return
    await update.message.reply_text("Немає дії для скасування. Головне меню 👇", reply_markup=MAIN_MENU)


# ---------- Обробники кнопок меню ----------

async def handle_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text in FLOW_MENU:
        return  # обробляє відповідний діалог

    if text == "🧾 Реквізити":
        payment, mode = await get_text("payment")
        if not payment or (mode == ParseMode.MARKDOWN and "[" in payment):
            # Реквізити ще не заповнені
            await update.message.reply_text(
                "🧾 Реквізити для оплати уточнюйте в адміністратора: @Denis_Bukhtarov"
            )
            return
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Я оплатив — надіслати квитанцію",
                                                         callback_data="menu:pay")]])
        await reply_text_key(update.message, "payment", reply_markup=kb)
        return
    if text == "🌐 Сайт":
        await update.message.reply_text(f"🌐 {config.WEBSITE_URL}")
        return
    if text == "📸 Instagram":
        await update.message.reply_text(f"📸 {config.INSTAGRAM_URL}")
        return
    if text in TEXT_BUTTONS:
        await reply_text_key(update.message, TEXT_BUTTONS[text])


async def private_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Будь-який інший текст в особистому чаті — підказка з меню (якщо людина не в діалозі)."""
    if context.user_data.get("in_flow"):
        return
    await update.message.reply_text(
        "Оберіть пункт меню нижче 👇\nЗапис: 📝 Записатися · Допомога: /help", reply_markup=MAIN_MENU
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
    count = await store.add(message.chat_id, user.id, 1)

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
            await store.reset(message.chat_id, user.id)
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

    count = await store.add(msg.chat_id, target.id, 1)
    await msg.reply_text(
        f"⚠️ {target.mention_html()} отримав попередження ({count}/{config.MAX_WARNINGS}).",
        parse_mode=ParseMode.HTML,
    )

    if count >= config.MAX_WARNINGS:
        try:
            await context.bot.ban_chat_member(chat_id=msg.chat_id, user_id=target.id)
            await msg.reply_text(f"🚫 {target.mention_html()} заблокований.", parse_mode=ParseMode.HTML)
            await store.reset(msg.chat_id, target.id)
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
    count = await store.add(msg.chat_id, target.id, -1)
    await msg.reply_text(
        f"✅ Знято попередження у {target.mention_html()} (зараз: {count}).",
        parse_mode=ParseMode.HTML,
    )


async def show_warns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/warns у відповідь на повідомлення — скільки попереджень у цього учасника."""
    msg = update.message
    if not await is_admin(context, msg.chat_id, msg.from_user.id):
        return
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("Використайте /warns у відповідь на повідомлення користувача.")
        return
    target = msg.reply_to_message.from_user
    count = await store.get(msg.chat_id, target.id)
    await msg.reply_text(
        f"📋 У {target.mention_html()} попереджень: {count}/{config.MAX_WARNINGS}.",
        parse_mode=ParseMode.HTML,
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.chat.type == ChatType.PRIVATE:
        await admin_tools.stats_private(update, context)
        return
    if not await is_admin(context, msg.chat_id, msg.from_user.id):
        return
    member_count = await context.bot.get_chat_member_count(msg.chat_id)
    await msg.reply_text(f"📊 Учасників у групі: {member_count}")


# ---------- Облік користувачів і груп + «ліниві» нагадування ----------

_seen_users: dict[int, float] = {}
_seen_groups: set[int] = set()
_last_tick = 0.0
TICK_EVERY = 300  # секунд


async def track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запускається перед усіма обробниками: запам'ятовує клієнтів і групи."""
    global _last_tick
    if not db.enabled():
        return
    try:
        chat, user = update.effective_chat, update.effective_user
        if chat and chat.type == ChatType.PRIVATE and user and not user.is_bot:
            if time.time() - _seen_users.get(user.id, 0) > 600:
                _seen_users[user.id] = time.time()
                await db.touch_user(user)
        elif chat and chat.type in GROUP_TYPES and chat.id not in _seen_groups:
            _seen_groups.add(chat.id)
            await db.touch_group(chat.id, chat.title)
    except Exception as e:
        logger.warning("Облік користувача/групи не вдався: %s", e)

    if time.time() - _last_tick > TICK_EVERY:
        _last_tick = time.time()
        context.application.create_task(_safe_tick(context.application))


async def bot_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Бота додали в групу або видалили з неї."""
    if not db.enabled():
        return
    cmu = update.my_chat_member
    if cmu.chat.type not in GROUP_TYPES:
        return
    try:
        if cmu.new_chat_member.status in ("left", "kicked"):
            _seen_groups.discard(cmu.chat.id)
            await db.remove_group(cmu.chat.id)
        else:
            _seen_groups.add(cmu.chat.id)
            await db.touch_group(cmu.chat.id, cmu.chat.title)
    except Exception as e:
        logger.warning("Не вдалось оновити список груп: %s", e)


async def _safe_tick(app):
    try:
        return await admin_tools.tick(app)
    except Exception as e:
        logger.error("Помилка нагадувань: %s", e, exc_info=e)
        return {"error": str(e)}


async def _ticker_loop(app):
    """Поки бот не спить — перевіряє нагадування кожні 5 хвилин."""
    global _last_tick
    while True:
        await asyncio.sleep(TICK_EVERY)
        _last_tick = time.time()
        await _safe_tick(app)


# ---------- Помилки ----------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Помилка при обробці оновлення: %s", context.error, exc_info=context.error)


# ---------- Збирання бота ----------

USER_COMMANDS = [
    BotCommand("start", "Головне меню"),
    BotCommand("book", "Записатися на тренування"),
    BotCommand("trial", "Пробне заняття"),
    BotCommand("paid", "Я оплатив — надіслати квитанцію"),
    BotCommand("my", "Мій абонемент і записи"),
    BotCommand("birthday", "Вказати день народження"),
    BotCommand("help", "Допомога"),
    BotCommand("privacy", "Політика конфіденційності"),
    BotCommand("mydata", "Мої дані в боті"),
    BotCommand("delete_me", "Видалити мої дані"),
    BotCommand("cancel", "Скасувати дію"),
]


async def startup(app: Application):
    await store.init()
    await db.open_pool()
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS не задано: заявки й оплати не буде кому надсилати. "
                       "Дізнайтесь свій ID командою /myid і додайте ADMIN_IDS на Render.")
    try:
        await app.bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    except Exception as e:
        logger.warning("Не вдалось встановити список команд: %s", e)


async def shutdown(app: Application):
    await db.close_pool()


def build_app(request=None) -> Application:
    builder = Application.builder().token(BOT_TOKEN).post_shutdown(shutdown)
    if request is not None:  # використовується в тестах
        builder = builder.request(request).get_updates_request(request)
    app = builder.build()

    # Група -1: облік користувачів і груп (працює для кожного оновлення)
    app.add_handler(TypeHandler(Update, track), group=-1)
    app.add_handler(ChatMemberHandler(bot_membership, ChatMemberHandler.MY_CHAT_MEMBER), group=-1)

    # Діалоги (кожен у своїй групі) і кнопки адміністратора
    flows.register(app)
    admin_tools.register(app)
    privacy.register(app)

    # Група 0: загальні команди й меню
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("myid", my_id))
    app.add_handler(CommandHandler("cancel", cancel_idle))
    app.add_handler(CommandHandler("warn", warn_user, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("unwarn", unwarn_user, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("warns", show_warns, filters=filters.ChatType.GROUPS))
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


# ---------- Вебсервер для Render: вебхук Telegram + cron ----------

def webhook_secret() -> str:
    """Секрет для перевірки, що запит прийшов саме від Telegram (виводиться з токена)."""
    return hashlib.sha256(("fct-webhook:" + BOT_TOKEN).encode()).hexdigest()[:48]


def make_web_app(app: Application):
    import tornado.web

    secret = webhook_secret()

    class TelegramHandler(tornado.web.RequestHandler):
        async def post(self):
            got = self.request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not hmac.compare_digest(got, secret):
                self.set_status(403)
                return
            try:
                data = json.loads(self.request.body)
                await app.update_queue.put(Update.de_json(data, app.bot))
            except Exception as e:
                logger.warning("Некоректне оновлення від Telegram: %s", e)
            self.set_status(200)

    class CronHandler(tornado.web.RequestHandler):
        async def get(self):
            key = self.get_query_argument("key", "")
            if not hmac.compare_digest(key, admin_tools.cron_secret()):
                self.set_status(401)
                self.write({"ok": False})
                return
            result = await _safe_tick(app)
            self.write({"ok": True, "result": {k: str(v) for k, v in result.items()}})

    class RootHandler(tornado.web.RequestHandler):
        def get(self):
            self.write("FCT bot OK")

        def head(self):
            self.set_status(200)

    return tornado.web.Application([
        (r"/telegram", TelegramHandler),
        (r"/cron/tick", CronHandler),
        (r"/", RootHandler),
    ])


async def run_webhook_server(app: Application, base_url: str, port: int):
    import tornado.httpserver

    await app.initialize()
    await startup(app)
    await app.start()
    await app.bot.set_webhook(
        url=f"{base_url}/telegram",
        secret_token=webhook_secret(),
        allowed_updates=Update.ALL_TYPES,
    )
    server = tornado.httpserver.HTTPServer(make_web_app(app), xheaders=True)
    server.listen(port, address="0.0.0.0")
    logger.info("Бот запущено у режимі webhook: %s/telegram", base_url)
    ticker = asyncio.create_task(_ticker_loop(app))
    asyncio.create_task(_safe_tick(app))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    await stop.wait()

    logger.info("Зупиняюсь…")
    ticker.cancel()
    server.stop()
    await app.stop()
    await shutdown(app)
    await app.shutdown()


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN не знайдено. Скопіюйте .env.example у .env і вставте туди токен від @BotFather "
            "(на Render — додайте BOT_TOKEN у Environment)."
        )

    app = build_app()

    # На Render адреса сервісу підставляється автоматично (RENDER_EXTERNAL_URL)
    base_url = admin_tools.public_url()

    if base_url:
        # Режим webhook — для Render (Web Service). Telegram сам «будить» сервіс новим повідомленням.
        port = int(os.getenv("PORT", 8080))
        asyncio.run(run_webhook_server(app, base_url, port))
    else:
        # Режим polling — для локального запуску без публічної адреси.
        logger.info("Бот запущено у режимі polling. Натисніть Ctrl+C для зупинки.")

        async def _post_init(a):
            await startup(a)
            a.create_task(_ticker_loop(a))

        app.post_init = _post_init
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
