# -*- coding: utf-8 -*-
"""
Інструменти адміністратора: розсилка, опитування, зміна текстів, статистика,
абонементи, боржники, записи, дні народження. Плюс автоматичні нагадування (tick).
"""

import asyncio
import hashlib
import logging
import os
from datetime import date, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config
import db
from common import (
    ADMIN_IDS,
    CANCEL_KB,
    CANCEL_TEXT,
    MAIN_MENU,
    MENU_TEXTS,
    TEXT_KEYS,
    fmt_date,
    fmt_day,
    get_text,
    h,
    invalidate_text,
    is_admin,
    is_owner,
    notify_admins,
    now_kyiv,
    send_user,
    today_kyiv,
    user_link,
)
from flows import begin_flow, booking_summary, menu_interrupt

logger = logging.getLogger(__name__)

BC_MSG, BC_CONFIRM, SET_PICK, SET_TEXT = range(100, 104)
PRIVATE = filters.ChatType.PRIVATE
NOT_MENU = ~filters.Text(MENU_TEXTS + [CANCEL_TEXT]) & ~filters.COMMAND
GROUP_ADMIN_FLOWS = 4


def cron_secret() -> str:
    s = (os.getenv("CRON_SECRET") or "").strip()
    if s:
        return s
    token = os.getenv("BOT_TOKEN") or ""
    return hashlib.sha256(("fct-cron:" + token).encode()).hexdigest()[:32]


def public_url() -> str:
    return (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")


async def _owner_private(update: Update) -> bool:
    u = update.effective_user
    if not u or not is_owner(u.id):
        return False
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Ця команда працює в особистих повідомленнях з ботом.")
        return False
    return True


async def _end_admin(update, context, text):
    context.user_data.clear()
    await update.effective_message.reply_text(text, reply_markup=MAIN_MENU)
    return ConversationHandler.END


async def admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    return await _end_admin(update, context, "Скасовано.")


# ---------- /admin ----------

async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _owner_private(update):
        if update.effective_user and not ADMIN_IDS:
            await update.message.reply_text(
                "ADMIN_IDS не задано. Додайте свій Telegram ID (команда /myid) у змінну ADMIN_IDS на Render."
            )
        return
    lines = [
        "🛠 <b>Панель адміністратора</b>",
        "",
        "<b>Заявки й оплати</b> приходять сюди з кнопками «Підтвердити / Відхилити».",
        "",
        "/bookings — записи на найближчі 7 днів",
        "/subs — чинні абонементи",
        "/debtors — абонементи, що закінчились і не продовжені",
        "/stats — статистика",
        "/broadcast — розсилка всім, хто писав боту",
        "/set — змінити тексти (розклад, ціни, реквізити…)",
        "/birthdays — дні народження",
        "",
        "<b>У групі:</b> /poll — опитування «Хто буде на тренуванні», "
        "/warn /unwarn /warns — попередження, /stats — кількість учасників.",
        "",
        f"База даних: {'✅ підключена' if db.enabled() else '❌ не підключена (DATABASE_URL)'}",
    ]
    url = public_url()
    if url:
        lines += [
            "",
            "⏰ <b>Посилання для cron-job.org</b> (раз на годину, будить бота для нагадувань):",
            f"<code>{h(url)}/cron/tick?key={h(cron_secret())}</code>",
            "Нікому не показуйте це посилання.",
        ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True)


# ---------- Розсилка ----------

async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _owner_private(update):
        return ConversationHandler.END
    if not db.enabled():
        await update.message.reply_text("Потрібна база даних (DATABASE_URL).")
        return ConversationHandler.END
    begin_flow(context)
    await update.message.reply_text(
        "📣 Надішліть повідомлення для розсилки: текст, фото з підписом або відео.\n"
        "Його отримають усі, хто писав боту.",
        reply_markup=CANCEL_KB,
    )
    return BC_MSG


async def broadcast_got(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = await db.active_user_ids()
    context.user_data["bc_from"] = update.effective_chat.id
    context.user_data["bc_msg"] = update.message.message_id
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"📣 Надіслати ({len(users)})", callback_data="bcast:go"),
        InlineKeyboardButton("✖️ Скасувати", callback_data="bcast:no"),
    ]])
    await update.message.reply_text(
        f"Ось так виглядатиме повідомлення ☝️\nОтримувачів: {len(users)}. Надсилаємо?",
        reply_markup=kb,
    )
    return BC_CONFIRM


async def broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "bcast:no" or "bc_msg" not in context.user_data:
        await q.edit_message_text("Розсилку скасовано.")
        context.user_data.clear()
        return ConversationHandler.END
    from_chat, msg_id = context.user_data["bc_from"], context.user_data["bc_msg"]
    context.user_data.clear()
    await q.edit_message_text("📣 Розсилка почалась… Звіт прийде, коли завершиться.")
    context.application.create_task(_do_broadcast(context.bot, from_chat, msg_id, q.from_user.id))
    return ConversationHandler.END


async def _do_broadcast(bot, from_chat, msg_id, admin_id):
    users = await db.active_user_ids()
    ok = blocked = failed = 0
    for uid in users:
        for attempt in range(2):
            try:
                await bot.copy_message(uid, from_chat, msg_id)
                ok += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(float(e.retry_after) + 1)
            except Forbidden:
                blocked += 1
                await db.set_blocked(uid)
                break
            except Exception as e:
                logger.warning("Розсилка: помилка для %s: %s", uid, e)
                failed += 1
                break
        await asyncio.sleep(0.05)
    try:
        await bot.send_message(admin_id, f"📣 Розсилку завершено.\n✅ Доставлено: {ok}\n"
                                         f"🚫 Заблокували бота: {blocked}\n⚠️ Помилки: {failed}")
    except Exception:
        pass


def broadcast_conversation():
    return ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_start)],
        states={
            BC_MSG: [MessageHandler(PRIVATE & NOT_MENU & ~filters.StatusUpdate.ALL, broadcast_got)],
            BC_CONFIRM: [CallbackQueryHandler(broadcast_confirm, pattern=r"^bcast:(go|no)$")],
        },
        fallbacks=[
            CommandHandler("cancel", admin_cancel),
            MessageHandler(filters.Text([CANCEL_TEXT]), admin_cancel),
            MessageHandler(filters.Text(MENU_TEXTS), menu_interrupt),
        ],
        allow_reentry=True,
        name="broadcast",
    )


# ---------- Зміна текстів ----------

async def set_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _owner_private(update):
        return ConversationHandler.END
    if not db.enabled():
        await update.message.reply_text("Потрібна база даних (DATABASE_URL).")
        return ConversationHandler.END
    begin_flow(context)
    rows = [[InlineKeyboardButton(label, callback_data=f"set:{key}")] for key, (label, _) in TEXT_KEYS.items()]
    rows.append([InlineKeyboardButton("✖️ Закрити", callback_data="set:close")])
    await update.message.reply_text("✏️ Який текст змінити?", reply_markup=InlineKeyboardMarkup(rows))
    return SET_PICK


async def set_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    key = q.data.split(":", 1)[1]
    if key == "close" or key not in TEXT_KEYS:
        await q.edit_message_text("Готово.")
        context.user_data.clear()
        return ConversationHandler.END
    context.user_data["set_key"] = key
    text, mode = await get_text(key)
    await q.edit_message_text(f"✏️ {TEXT_KEYS[key][0]} — зараз так:")
    try:
        await q.message.reply_text(text, parse_mode=mode, disable_web_page_preview=True)
    except Exception:
        await q.message.reply_text(text, disable_web_page_preview=True)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Повернути стандартний (з config.py)",
                                                     callback_data=f"setreset:{key}")]])
    await q.message.reply_text(
        "Надішліть новий текст одним повідомленням. Жирний, курсив і посилання збережуться.\n"
        "Або поверніть стандартний текст кнопкою нижче.",
        reply_markup=kb,
    )
    await q.message.reply_text("…або ❌ Скасувати", reply_markup=CANCEL_KB)
    return SET_TEXT


async def set_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = context.user_data.get("set_key")
    if not key:
        return await _end_admin(update, context, "Сесію втрачено, почніть знову: /set")
    value = update.message.text_html
    if len(value) > 3500:
        await update.message.reply_text("Задовгий текст (максимум ~3500 символів). Скоротіть, будь ласка.")
        return SET_TEXT
    await db.set_setting(f"text:{key}", value)
    invalidate_text(key)
    await update.message.reply_text(f"✅ Збережено: {TEXT_KEYS[key][0]}. Клієнти бачитимуть так:")
    await update.message.reply_text(value, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    return await _end_admin(update, context, "Змінити ще щось: /set")


async def set_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Лише для адміністратора", show_alert=True)
        return ConversationHandler.END
    key = q.data.split(":", 1)[1]
    if key in TEXT_KEYS:
        await db.delete_setting(f"text:{key}")
        invalidate_text(key)
    await q.answer("Повернуто стандартний текст")
    await q.edit_message_text(f"↩️ {TEXT_KEYS.get(key, ('',))[0]}: повернуто текст із config.py.")
    context.user_data.clear()
    await q.message.reply_text("Готово.", reply_markup=MAIN_MENU)
    return ConversationHandler.END


def set_conversation():
    return ConversationHandler(
        entry_points=[CommandHandler("set", set_start)],
        states={
            SET_PICK: [CallbackQueryHandler(set_pick, pattern=r"^set:")],
            SET_TEXT: [
                MessageHandler(PRIVATE & NOT_MENU & filters.TEXT, set_text),
                CallbackQueryHandler(set_reset, pattern=r"^setreset:"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", admin_cancel),
            MessageHandler(filters.Text([CANCEL_TEXT]), admin_cancel),
            MessageHandler(filters.Text(MENU_TEXTS), menu_interrupt),
        ],
        allow_reentry=True,
        name="set_text",
    )


# ---------- Опитування в групі ----------

async def poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.chat.type not in ("group", "supergroup"):
        await msg.reply_text("Команда /poll працює в групі клубу.")
        return
    if not await is_admin(context, msg.chat_id, msg.from_user.id):
        return
    question = " ".join(context.args).strip() or config.POLL_QUESTION
    await context.bot.send_poll(
        msg.chat_id, question[:300], config.POLL_OPTIONS, is_anonymous=False, allows_multiple_answers=False
    )
    try:
        await msg.delete()
    except Exception:
        pass


# ---------- Списки й статистика ----------

async def stats_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _owner_private(update):
        return
    if not db.enabled():
        await update.message.reply_text("Потрібна база даних (DATABASE_URL).")
        return
    s = await db.stats(today_kyiv())
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Користувачів бота: {s['users']} (нових за тиждень: {s['users_new']}, "
        f"заблокували: {s['users_blocked']})\n"
        f"💬 Груп: {s['groups']}\n\n"
        f"📝 Заявок за тиждень: {s['bookings']} (пробних: {s['trials']}, підтверджено: {s['b_confirmed']})\n"
        f"⏳ Заявок чекають рішення: {s['b_pending']}\n"
        f"💳 Оплат чекають перевірки: {s['p_pending']}\n\n"
        f"🎫 Підтверджено оплат за 30 днів: {s['subs_month']} на {s['sum_month']} грн"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def list_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _owner_private(update) or not db.enabled():
        return
    today = today_kyiv()
    rows = await db._fetch(
        """
        SELECT * FROM fct_bookings WHERE day BETWEEN %s AND %s AND status IN ('new', 'confirmed')
        ORDER BY day, slot_time
        """,
        (today, today + timedelta(days=7)),
    )
    if not rows:
        await update.message.reply_text("На найближчі 7 днів записів немає.")
        return
    lines = ["📅 <b>Записи на 7 днів</b>"]
    current = None
    for b in rows:
        if b["day"] != current:
            current = b["day"]
            lines.append(f"\n<b>{fmt_day(current)}</b>")
        mark = "✅" if b["status"] == "confirmed" else "⏳"
        trial = " 🆓" if b["is_trial"] else ""
        lines.append(f"{mark} {h(b['slot_time'])} {h(b['direction'])} · {h(b['slot_title'])} — "
                     f"{h(b['name'])}, {h(b['phone'])}{trial}")
    await _send_long(update, "\n".join(lines))


async def list_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _owner_private(update) or not db.enabled():
        return
    today = today_kyiv()
    rows = await db.active_subscriptions(today)
    if not rows:
        await update.message.reply_text("Чинних абонементів немає.")
        return
    lines = [f"🎫 <b>Чинні абонементи: {len(rows)}</b>", ""]
    for s in rows:
        left = (s["ends"] - today).days
        warn = " ⚠️" if left <= config.REMIND_BEFORE_DAYS else ""
        lines.append(f"• {h(s['student'])} — до {fmt_date(s['ends'])}{warn} "
                     f"({user_link(s['user_id'], s.get('first_name'), s.get('username'))})")
    await _send_long(update, "\n".join(lines))


async def list_debtors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _owner_private(update) or not db.enabled():
        return
    text = await debtors_text(today_kyiv())
    await _send_long(update, text or "Боржників немає 👍")


async def debtors_text(today: date):
    rows = await db.debtors(today)
    if not rows:
        return None
    lines = [f"💸 <b>Абонемент закінчився і не продовжений: {len(rows)}</b>", ""]
    for s in rows:
        lines.append(f"• {h(s['student'])} — закінчився {fmt_date(s['ends'])} "
                     f"({user_link(s['user_id'], s.get('first_name'), s.get('username'))})")
    return "\n".join(lines)


async def list_birthdays(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _owner_private(update) or not db.enabled():
        return
    rows = await db.upcoming_birthdays()
    if not rows:
        await update.message.reply_text("Днів народження ще не додано. Клієнти додають їх командою /birthday.")
        return
    today = today_kyiv()

    def days_until(r):
        try:
            d = date(today.year, r["month"], r["day"])
        except ValueError:
            d = date(today.year, 2, 28)
        if d < today:
            try:
                d = date(today.year + 1, r["month"], r["day"])
            except ValueError:
                d = date(today.year + 1, 2, 28)
        return (d - today).days

    rows.sort(key=days_until)
    lines = ["🎂 <b>Найближчі дні народження</b>", ""]
    for r in rows[:40]:
        n = days_until(r)
        when = "сьогодні 🎉" if n == 0 else f"через {n} дн."
        lines.append(f"• {h(r['name'])} — {r['day']:02d}.{r['month']:02d} ({when})")
    await _send_long(update, "\n".join(lines))


async def _send_long(update: Update, text: str):
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3800:
            await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML,
                                                      disable_web_page_preview=True)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await update.effective_message.reply_text(chunk, parse_mode=ParseMode.HTML,
                                                  disable_web_page_preview=True)


# =====================================================================
#             АВТОМАТИЧНІ НАГАДУВАННЯ (викликається кожні 5 хв + cron)
# =====================================================================

_tick_lock = asyncio.Lock()


async def tick(app) -> dict:
    """Надсилає все, що «настав час» надіслати. Повторний виклик нічого не дублює."""
    if not db.enabled():
        return {"db": False}
    if _tick_lock.locked():
        return {"busy": True}
    async with _tick_lock:
        bot = app.bot
        now = now_kyiv()
        today = now.date()
        done = {"booking_reminders": 0, "subs_soon": 0, "subs_ended": 0, "birthdays": 0, "weekly": False}

        # 1) Нагадування про завтрашнє тренування
        if config.BOOKING_REMINDER_HOUR <= now.hour < 22:
            for b in await db.bookings_to_remind(today + timedelta(days=1)):
                if await db.mark_booking_reminded(b["id"]):
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Скасувати запис",
                                                                     callback_data=f"bc:{b['id']}")]])
                    if await send_user(bot, b["user_id"],
                                       f"⏰ <b>Нагадування: завтра тренування!</b>\n\n{booking_summary(b)}\n\n"
                                       "Якщо не зможете — скасуйте, будь ласка, запис кнопкою нижче.",
                                       reply_markup=kb):
                        done["booking_reminders"] += 1

        if config.MORNING_HOUR <= now.hour < 21:
            # 2) Абонемент скоро закінчиться
            for s in await db.subs_ending_soon(today, config.REMIND_BEFORE_DAYS):
                if await db.mark_sub(s["id"], "soon_reminded"):
                    left = (s["ends"] - today).days
                    when = "сьогодні останній день" if left == 0 else f"діє до {fmt_date(s['ends'])}"
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Я оплатив", callback_data="menu:pay")]])
                    if await send_user(bot, s["user_id"],
                                       f"🎫 Абонемент <b>{h(s['student'])}</b> {when}.\n\n"
                                       "Щоб продовжити: 🧾 Реквізити → оплата → ✅ Я оплатив.",
                                       reply_markup=kb):
                        done["subs_soon"] += 1
            # 3) Абонемент закінчився
            for s in await db.subs_just_ended(today):
                if await db.mark_sub(s["id"], "end_reminded"):
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Я оплатив", callback_data="menu:pay")]])
                    if await send_user(bot, s["user_id"],
                                       f"⌛️ Абонемент <b>{h(s['student'])}</b> закінчився {fmt_date(s['ends'])}.\n\n"
                                       "Продовжити: 🧾 Реквізити → оплата → ✅ Я оплатив.",
                                       reply_markup=kb):
                        done["subs_ended"] += 1

            # 4) Щопонеділка — звіт адміну: статистика + боржники
            if today.weekday() == 0:
                iso = today.isocalendar()
                if await db.once(f"weekly:{iso[0]}-{iso[1]}"):
                    s = await db.stats(today)
                    text = (f"🗓 <b>Тижневий звіт</b>\n\n📝 Заявок за тиждень: {s['bookings']} "
                            f"(пробних: {s['trials']})\n👥 Нових користувачів: {s['users_new']}\n"
                            f"⏳ Заявок чекають рішення: {s['b_pending']}\n"
                            f"💳 Оплат чекають перевірки: {s['p_pending']}")
                    debt = await debtors_text(today)
                    await notify_admins(bot, text + ("\n\n" + debt if debt else "\n\nБоржників немає 👍"))
                    done["weekly"] = True

            # 5) Дні народження
            leap = today.year % 4 == 0 and (today.year % 100 != 0 or today.year % 400 == 0)
            include_feb29 = (not leap) and today.month == 2 and today.day == 28
            birthdays = await db.birthdays_on(today.day, today.month, include_feb29)
            if birthdays:
                groups = await db.group_ids()
                for b in birthdays:
                    if not await db.once(f"bday:{today.isoformat()}:{b['id']}"):
                        continue
                    text = config.BIRTHDAY_TEXT.format(name=h(b["name"]), bot_name=h(config.BOT_NAME))
                    for chat_id in groups:
                        try:
                            await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
                        except Exception as e:
                            logger.warning("Привітання в групу %s не надіслано: %s", chat_id, e)
                    await send_user(bot, b["user_id"], text)
                    done["birthdays"] += 1

        # 6) Раз на добу — видалення даних, строк зберігання яких минув
        if await db.once(f"cleanup:{today.isoformat()}"):
            try:
                removed = await db.cleanup(config.KEEP_BOOKINGS_DAYS, config.KEEP_PAYMENTS_DAYS,
                                           config.KEEP_INACTIVE_USER_DAYS)
                done["cleanup"] = sum(removed.values())
            except Exception as e:
                logger.error("Не вдалось очистити старі дані: %s", e)

        if any(v for v in done.values()):
            logger.info("Нагадування: %s", done)
        return done


def register(app):
    app.add_handler(broadcast_conversation(), group=GROUP_ADMIN_FLOWS)
    app.add_handler(set_conversation(), group=GROUP_ADMIN_FLOWS + 1)
    app.add_handler(CommandHandler("admin", admin_help))
    app.add_handler(CommandHandler("poll", poll))
    app.add_handler(CommandHandler("bookings", list_bookings))
    app.add_handler(CommandHandler("subs", list_subs))
    app.add_handler(CommandHandler("debtors", list_debtors))
    app.add_handler(CommandHandler("birthdays", list_birthdays))
