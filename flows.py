# -*- coding: utf-8 -*-
"""
Діалоги з клієнтами: запис, пробне заняття, «Я оплатив», «Мій абонемент», дні народження.
Плюс кнопки адміністратора для підтвердження заявок і оплат.
"""

import logging
import re
from datetime import date, datetime, timedelta

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
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
    CANCEL_KB,
    CANCEL_TEXT,
    KYIV,
    MAIN_MENU,
    MENU_TEXTS,
    deep_link_kb,
    fmt_date,
    fmt_day,
    h,
    is_owner,
    notify_admins,
    now_kyiv,
    send_user,
    today_kyiv,
    user_link,
)

logger = logging.getLogger(__name__)

# Стани діалогів
(DIR, SLOT, DAY, NAME, PHONE, CONFIRM, T_AGE, T_EXP, T_GOAL,
 P_STUDENT, P_PROOF, BD_NAME, BD_DATE) = range(13)

PRIVATE = filters.ChatType.PRIVATE
NOT_MENU = ~filters.Text(MENU_TEXTS + [CANCEL_TEXT]) & ~filters.COMMAND
FLOW_BUTTONS = ["📝 Записатися", "🆓 Пробне заняття", "✅ Я оплатив"]
DB_OFF_TEXT = (
    "Ця функція тимчасово недоступна. Напишіть, будь ласка, тренеру: @Denis_Bukhtarov"
)


def _dir_by_key(key):
    for d in config.BOOKING_OPTIONS:
        if d["key"] == key:
            return d
    return None


def _sub_type(key):
    for t in config.SUBSCRIPTION_TYPES:
        if t["key"] == key:
            return t
    return None


def normalize_phone(text: str):
    digits = re.sub(r"\D", "", text or "")
    if len(digits) == 10 and digits.startswith("0"):
        return "+38" + digits
    if len(digits) == 12 and digits.startswith("380"):
        return "+" + digits
    if 10 <= len(digits) <= 15:
        return "+" + digits
    return None


def booking_summary(b: dict) -> str:
    d = b["day"] if isinstance(b["day"], date) else date.fromisoformat(b["day"])
    lines = [
        f"🥊 {h(b['direction'])}",
        f"👥 {h(b['slot_title'])}",
        f"📅 {fmt_day(d)} о {h(b['slot_time'])}",
        f"🙋 {h(b['name'])}",
    ]
    if b.get("phone"):
        lines.append(f"📱 {h(b['phone'])}")
    return "\n".join(lines)


async def _private_only(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str, label: str):
    """У групі — підказка перейти в особисті повідомлення бота."""
    await update.effective_message.reply_text(
        "Це робиться в особистих повідомленнях з ботом 👇",
        reply_markup=deep_link_kb(context.bot.username, payload, label),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text("Скасовано.", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text("Скасовано. Головне меню 👇", reply_markup=MAIN_MENU)
    return ConversationHandler.END


async def menu_interrupt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Натиснули кнопку меню посеред діалогу — тихо завершуємо діалог.
    Саму кнопку обробить загальне меню (воно в іншій групі обробників)."""
    # Якщо кнопка запускає інший діалог — він сам очистить дані, тут нічого не чіпаємо
    if (update.effective_message.text or "") not in FLOW_BUTTONS:
        context.user_data.clear()
    return ConversationHandler.END


def begin_flow(context):
    """Позначка «користувач у діалозі» — щоб загальне меню не відповідало на його введення."""
    context.user_data.clear()
    context.user_data["in_flow"] = True


# =====================================================================
#                         ЗАПИС І ПРОБНЕ ЗАНЯТТЯ
# =====================================================================

async def book_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _start_flow(update, context, trial=False)


async def trial_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _start_flow(update, context, trial=True)


async def _start_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, trial: bool):
    if update.effective_chat.type != "private":
        payload, label = ("trial", "🆓 Записатись на пробне") if trial else ("book", "📝 Записатись")
        await _private_only(update, context, payload, label)
        return ConversationHandler.END
    if not db.enabled():
        await update.message.reply_text(DB_OFF_TEXT, reply_markup=MAIN_MENU)
        return ConversationHandler.END

    begin_flow(context)
    context.user_data["trial"] = trial
    if trial:
        await update.message.reply_text(
            "🆓 *Пробне заняття*\n\nКілька коротких питань, щоб тренер підготувався.\n\n"
            "Скільки років учню? Напишіть число.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=CANCEL_KB,
        )
        return T_AGE
    await update.message.reply_text("📝 Запис на тренування", reply_markup=CANCEL_KB)
    return await _ask_direction(update.message)


async def trial_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    digits = re.sub(r"\D", "", update.message.text or "")
    if not digits or not (3 <= int(digits) <= 90):
        await update.message.reply_text("Напишіть, будь ласка, вік числом, наприклад: 12")
        return T_AGE
    context.user_data["age"] = int(digits)
    kb = [[InlineKeyboardButton(x, callback_data=f"texp:{i}")] for i, x in enumerate(config.TRIAL_EXPERIENCE)]
    await update.message.reply_text("Чи був досвід тренувань?", reply_markup=InlineKeyboardMarkup(kb))
    return T_EXP


async def trial_exp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    i = int(q.data.split(":")[1])
    context.user_data["experience"] = config.TRIAL_EXPERIENCE[i]
    await q.edit_message_text(f"Досвід: {config.TRIAL_EXPERIENCE[i]}")
    kb = [[InlineKeyboardButton(x, callback_data=f"tgoal:{i}")] for i, x in enumerate(config.TRIAL_GOALS)]
    await q.message.reply_text("Яка головна мета?", reply_markup=InlineKeyboardMarkup(kb))
    return T_GOAL


async def trial_goal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    i = int(q.data.split(":")[1])
    context.user_data["goal"] = config.TRIAL_GOALS[i]
    await q.edit_message_text(f"Мета: {config.TRIAL_GOALS[i]}")
    return await _ask_direction(q.message)


async def _ask_direction(message):
    kb = [[InlineKeyboardButton(d["title"], callback_data=f"bdir:{d['key']}")] for d in config.BOOKING_OPTIONS]
    await message.reply_text("Оберіть напрямок:", reply_markup=InlineKeyboardMarkup(kb))
    return DIR


async def pick_direction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = _dir_by_key(q.data.split(":", 1)[1])
    if not d:
        return DIR
    context.user_data["dir"] = d["key"]
    kb = [
        [InlineKeyboardButton(f"{title} — {t}", callback_data=f"bslot:{i}")]
        for i, (title, t) in enumerate(d["slots"])
    ]
    await q.edit_message_text(f"Напрямок: {d['title']}\n\nОберіть групу / час:",
                              reply_markup=InlineKeyboardMarkup(kb))
    return SLOT


def available_days(d: dict, slot_time: str, now: datetime = None):
    now = now or now_kyiv()
    hh, mm = (int(x) for x in slot_time.split(":"))
    days = []
    for offset in range(0, 21):
        day = now.date() + timedelta(days=offset)
        if day.weekday() not in d["days"]:
            continue
        start = datetime(day.year, day.month, day.day, hh, mm, tzinfo=KYIV)
        if start - now < timedelta(hours=1):
            continue
        days.append(day)
        if len(days) >= config.BOOKING_DAYS_AHEAD:
            break
    return days


async def pick_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = _dir_by_key(context.user_data.get("dir"))
    i = int(q.data.split(":")[1])
    if not d or i >= len(d["slots"]):
        return SLOT
    title, t = d["slots"][i]
    context.user_data["slot"] = i
    days = available_days(d, t)
    rows, row = [], []
    for day in days:
        row.append(InlineKeyboardButton(fmt_day(day), callback_data=f"bday:{day.isoformat()}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    await q.edit_message_text(f"{d['title']} · {title} о {t}\n\nОберіть день:",
                              reply_markup=InlineKeyboardMarkup(rows))
    return DAY


async def pick_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        day = date.fromisoformat(q.data.split(":", 1)[1])
    except ValueError:
        return DAY
    if day < today_kyiv():
        await q.answer("Цей день уже минув", show_alert=True)
        return DAY
    context.user_data["day"] = day.isoformat()
    await q.edit_message_text(f"День: {fmt_day(day)}")

    suggestions = []
    last = await db.last_booking_name(update.effective_user.id)
    if last:
        suggestions.append(last)
    tg_name = update.effective_user.full_name
    if tg_name and tg_name not in suggestions:
        suggestions.append(tg_name)
    kb = ReplyKeyboardMarkup([[s] for s in suggestions[:2]] + [[CANCEL_TEXT]], resize_keyboard=True)
    await q.message.reply_text(
        "Як звати учня? Напишіть ім'я та прізвище (або натисніть кнопку нижче).",
        reply_markup=kb,
    )
    return NAME


async def got_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = " ".join((update.message.text or "").split())
    if not (2 <= len(name) <= 60):
        await update.message.reply_text("Напишіть, будь ласка, ім'я (від 2 до 60 символів).")
        return NAME
    context.user_data["name"] = name
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Надіслати мій номер", request_contact=True)], [CANCEL_TEXT]],
        resize_keyboard=True,
    )
    await update.message.reply_text(
        "Номер телефону для зв'язку: натисніть кнопку нижче або напишіть номер.", reply_markup=kb
    )
    return PHONE


async def got_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.contact:
        phone = normalize_phone(update.message.contact.phone_number)
    else:
        phone = normalize_phone(update.message.text)
    if not phone:
        await update.message.reply_text("Не схоже на номер телефону. Приклад: 0501234567")
        return PHONE
    context.user_data["phone"] = phone
    b = _draft(context)
    extra = ""
    if context.user_data.get("trial"):
        extra = (f"\n🎂 Вік: {context.user_data['age']}\n🏅 Досвід: {h(context.user_data['experience'])}"
                 f"\n🎯 Мета: {h(context.user_data['goal'])}")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Надіслати заявку", callback_data="bconf:yes"),
        InlineKeyboardButton("✖️ Скасувати", callback_data="bconf:no"),
    ]])
    await update.message.reply_text("Перевірте, будь ласка:", reply_markup=MAIN_MENU)
    title = "🆓 Пробне заняття" if context.user_data.get("trial") else "📝 Запис"
    await update.message.reply_text(f"<b>{title}</b>\n\n{booking_summary(b)}{extra}",
                                    parse_mode=ParseMode.HTML, reply_markup=kb)
    return CONFIRM


def _draft(context) -> dict:
    ud = context.user_data
    d = _dir_by_key(ud["dir"])
    title, t = d["slots"][ud["slot"]]
    return {
        "direction": d["title"],
        "slot_title": title,
        "slot_time": t,
        "day": date.fromisoformat(ud["day"]),
        "name": ud["name"],
        "phone": ud.get("phone"),
        "is_trial": bool(ud.get("trial")),
        "age": ud.get("age"),
        "experience": ud.get("experience"),
        "goal": ud.get("goal"),
    }


async def confirm_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "bconf:no":
        context.user_data.clear()
        await q.edit_message_text("Запис скасовано.")
        return ConversationHandler.END
    if "phone" not in context.user_data:
        await q.edit_message_text("Сесію запису втрачено, почніть, будь ласка, знову: 📝 Записатися")
        return ConversationHandler.END

    b = _draft(context)
    b["user_id"] = update.effective_user.id
    try:
        saved = await db.create_booking(b)
    except Exception as e:
        logger.error("Не вдалось зберегти запис: %s", e)
        await q.edit_message_text("Не вдалось зберегти заявку 😔 Спробуйте пізніше або напишіть @Denis_Bukhtarov")
        return ConversationHandler.END
    context.user_data.clear()

    await q.edit_message_text(
        f"✅ <b>Заявку надіслано!</b>\n\n{booking_summary(saved)}\n\n"
        "Тренер підтвердить запис найближчим часом — повідомлення прийде сюди.",
        parse_mode=ParseMode.HTML,
    )
    await notify_admins(context.bot, admin_booking_text(saved, update.effective_user),
                        reply_markup=admin_booking_kb(saved["id"]))
    return ConversationHandler.END


def admin_booking_text(b: dict, user=None, username=None) -> str:
    kind = "🆓 Пробне заняття" if b["is_trial"] else "📝 Нова заявка на тренування"
    who = user_link(b["user_id"], getattr(user, "full_name", None) or b["name"],
                    getattr(user, "username", None) or username)
    lines = [f"<b>{kind}</b> #{b['id']}", "", booking_summary(b), f"💬 Telegram: {who}"]
    if b["is_trial"]:
        lines += [f"🎂 Вік: {h(b['age'])}", f"🏅 Досвід: {h(b['experience'])}", f"🎯 Мета: {h(b['goal'])}"]
    return "\n".join(lines)


def admin_booking_kb(bid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Підтвердити", callback_data=f"bk:ok:{bid}"),
        InlineKeyboardButton("❌ Відхилити", callback_data=f"bk:no:{bid}"),
    ]])


async def admin_booking_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Лише для адміністратора", show_alert=True)
        return
    _, action, bid = q.data.split(":")
    status = "confirmed" if action == "ok" else "declined"
    b = await db.set_booking_status(int(bid), status)
    if not b:
        await q.answer("Заявку вже оброблено або скасовано")
        await q.edit_message_reply_markup(None)
        return
    await q.answer("Готово")
    mark = "✅ Підтверджено" if status == "confirmed" else "❌ Відхилено"
    await q.edit_message_text(f"{q.message.text_html}\n\n<b>{mark}</b>", parse_mode=ParseMode.HTML)

    if status == "confirmed":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Скасувати запис", callback_data=f"bc:{b['id']}")]])
        await send_user(context.bot, b["user_id"],
                        f"✅ <b>Запис підтверджено!</b>\n\n{booking_summary(b)}\n\n"
                        "Чекаємо на тренуванні 🥊 Напередодні бот нагадає.", reply_markup=kb)
    else:
        await send_user(context.bot, b["user_id"],
                        f"😔 На жаль, на цей час записатись не вийде:\n\n{booking_summary(b)}\n\n"
                        "Оберіть, будь ласка, інший день: 📝 Записатися — або напишіть тренеру @Denis_Bukhtarov")


async def client_cancel_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    bid = int(q.data.split(":")[1])
    b = await db.get_booking(bid)
    if not b or b["user_id"] != q.from_user.id:
        await q.answer("Запис не знайдено", show_alert=True)
        return
    if b["day"] < today_kyiv():
        await q.answer("Це тренування вже минуло", show_alert=True)
        return
    b = await db.set_booking_status(bid, "cancelled", only_from=("new", "confirmed"))
    if not b:
        await q.answer("Запис уже скасовано")
        await q.edit_message_reply_markup(None)
        return
    await q.answer("Запис скасовано")
    await q.edit_message_reply_markup(None)
    await q.message.reply_text(f"Запис скасовано:\n\n{booking_summary(b)}", parse_mode=ParseMode.HTML)
    await notify_admins(context.bot, f"🚫 <b>Клієнт скасував запис</b> #{b['id']}\n\n{booking_summary(b)}")


def booking_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            MessageHandler(filters.Text(["📝 Записатися"]), book_start),
            MessageHandler(filters.Text(["🆓 Пробне заняття"]), trial_start),
            CommandHandler("book", book_start),
            CommandHandler("trial", trial_start),
            MessageHandler(PRIVATE & filters.Regex(r"^/start book$"), book_start),
            MessageHandler(PRIVATE & filters.Regex(r"^/start trial$"), trial_start),
        ],
        states={
            T_AGE: [MessageHandler(PRIVATE & NOT_MENU & filters.TEXT, trial_age)],
            T_EXP: [CallbackQueryHandler(trial_exp, pattern=r"^texp:\d+$")],
            T_GOAL: [CallbackQueryHandler(trial_goal, pattern=r"^tgoal:\d+$")],
            DIR: [CallbackQueryHandler(pick_direction, pattern=r"^bdir:")],
            SLOT: [CallbackQueryHandler(pick_slot, pattern=r"^bslot:\d+$")],
            DAY: [CallbackQueryHandler(pick_day, pattern=r"^bday:")],
            NAME: [MessageHandler(PRIVATE & NOT_MENU & filters.TEXT, got_name)],
            PHONE: [
                MessageHandler(PRIVATE & filters.CONTACT, got_phone),
                MessageHandler(PRIVATE & NOT_MENU & filters.TEXT, got_phone),
            ],
            CONFIRM: [CallbackQueryHandler(confirm_booking, pattern=r"^bconf:(yes|no)$")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Text([CANCEL_TEXT]), cancel),
            MessageHandler(filters.Text(MENU_TEXTS), menu_interrupt),
        ],
        allow_reentry=True,
        name="booking",
    )


# =====================================================================
#                               ОПЛАТА
# =====================================================================

async def pay_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if update.callback_query:
        await update.callback_query.answer()
    if update.effective_chat.type != "private":
        await _private_only(update, context, "pay", "✅ Надіслати квитанцію")
        return ConversationHandler.END
    if not db.enabled():
        await message.reply_text(DB_OFF_TEXT, reply_markup=MAIN_MENU)
        return ConversationHandler.END
    begin_flow(context)
    suggestions = []
    last = await db.last_booking_name(update.effective_user.id)
    if last:
        suggestions.append(last)
    tg_name = update.effective_user.full_name
    if tg_name and tg_name not in suggestions:
        suggestions.append(tg_name)
    kb = ReplyKeyboardMarkup([[s] for s in suggestions[:2]] + [[CANCEL_TEXT]], resize_keyboard=True)
    await message.reply_text(
        "✅ <b>Підтвердження оплати</b>\n\nЗа кого оплата? Напишіть ім'я та прізвище учня.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    return P_STUDENT


async def pay_student(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = " ".join((update.message.text or "").split())
    if not (2 <= len(name) <= 60):
        await update.message.reply_text("Напишіть, будь ласка, ім'я учня (від 2 до 60 символів).")
        return P_STUDENT
    context.user_data["student"] = name
    await update.message.reply_text(
        "Надішліть фото або скриншот квитанції (можна PDF).", reply_markup=CANCEL_KB
    )
    return P_PROOF


async def pay_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.photo:
        file_id, kind = msg.photo[-1].file_id, "photo"
    elif msg.document:
        mime = (msg.document.mime_type or "").lower()
        if not (mime.startswith("image/") or mime == "application/pdf"):
            await msg.reply_text("Підходить фото, скриншот або PDF-файл квитанції.")
            return P_PROOF
        file_id, kind = msg.document.file_id, "document"
    else:
        await msg.reply_text("Надішліть, будь ласка, фото або PDF квитанції.")
        return P_PROOF

    student = context.user_data.get("student") or update.effective_user.full_name
    try:
        p = await db.create_payment(update.effective_user.id, student, file_id, kind)
    except Exception as e:
        logger.error("Не вдалось зберегти оплату: %s", e)
        await msg.reply_text("Не вдалось зберегти квитанцію 😔 Спробуйте пізніше.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    context.user_data.clear()

    await msg.reply_text(
        "🙏 Дякуємо! Квитанцію надіслано тренеру на перевірку. "
        "Коли оплату підтвердять, прийде повідомлення сюди.",
        reply_markup=MAIN_MENU,
    )
    u = update.effective_user
    caption = (f"💳 <b>Нова оплата</b> #{p['id']}\n\n🙋 Учень: {h(student)}\n"
               f"💬 Telegram: {user_link(u.id, u.full_name, u.username)}")
    if msg.caption:
        caption += f"\n📝 Коментар: {h(msg.caption[:300])}"
    caption += "\n\nОберіть, що саме оплачено:"
    kb = admin_payment_kb(p["id"])
    if kind == "photo":
        await notify_admins(context.bot, caption, reply_markup=kb, photo=file_id)
    else:
        await notify_admins(context.bot, caption, reply_markup=kb, document=file_id)
    return ConversationHandler.END


def admin_payment_kb(pid: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"✅ {t['title']} — {t['price']} грн", callback_data=f"pay:{t['key']}:{pid}")]
            for t in config.SUBSCRIPTION_TYPES]
    rows.append([InlineKeyboardButton("❌ Не підтверджувати", callback_data=f"pay:no:{pid}")])
    return InlineKeyboardMarkup(rows)


async def admin_payment_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("Лише для адміністратора", show_alert=True)
        return
    _, action, pid = q.data.split(":")
    pid = int(pid)
    caption = q.message.caption_html or q.message.text_html or ""
    caption = caption.replace("\n\nОберіть, що саме оплачено:", "")

    if action == "no":
        p = await db.decide_payment(pid, "rejected")
        if not p:
            await q.answer("Оплату вже оброблено")
            await q.edit_message_reply_markup(None)
            return
        await q.answer("Позначено як непідтверджену")
        await _edit_caption(q, f"{caption}\n\n<b>❌ Не підтверджено</b>")
        await send_user(context.bot, p["user_id"],
                        f"😔 Оплату для <b>{h(p['student'])}</b> не вдалося підтвердити. "
                        "Напишіть, будь ласка, тренеру: @Denis_Bukhtarov")
        return

    st = _sub_type(action)
    if not st:
        await q.answer("Невідомий тип оплати", show_alert=True)
        return
    p = await db.decide_payment(pid, "confirmed", st["key"])
    if not p:
        await q.answer("Оплату вже оброблено")
        await q.edit_message_reply_markup(None)
        return
    sub = await db.create_subscription(p["user_id"], p["student"], st, today_kyiv(), payment_id=pid)
    await q.answer("Оплату підтверджено")
    period = _period_text(sub)
    await _edit_caption(q, f"{caption}\n\n<b>✅ {h(st['title'])} — {st['price']} грн</b>{period}")
    await send_user(context.bot, p["user_id"],
                    f"✅ <b>Оплату підтверджено!</b>\n\n🙋 {h(p['student'])}\n🎫 {h(st['title'])} — "
                    f"{st['price']} грн{period}\n\nДякуємо! 🥊")


def _period_text(sub: dict) -> str:
    if sub["ends"] > sub["starts"]:
        return f"\n📅 Діє з {fmt_date(sub['starts'])} до {fmt_date(sub['ends'])}"
    return f"\n📅 {fmt_date(sub['starts'])}"


async def _edit_caption(q, text: str):
    try:
        if q.message.photo or q.message.document:
            await q.edit_message_caption(caption=text[:1024], parse_mode=ParseMode.HTML)
        else:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning("Не вдалось оновити повідомлення адміна: %s", e)
        await q.edit_message_reply_markup(None)


def payment_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            MessageHandler(filters.Text(["✅ Я оплатив"]), pay_start),
            CommandHandler("paid", pay_start),
            CallbackQueryHandler(pay_start, pattern=r"^menu:pay$"),
            MessageHandler(PRIVATE & filters.Regex(r"^/start pay$"), pay_start),
        ],
        states={
            P_STUDENT: [MessageHandler(PRIVATE & NOT_MENU & filters.TEXT, pay_student)],
            P_PROOF: [
                MessageHandler(PRIVATE & (filters.PHOTO | filters.Document.ALL), pay_proof),
                MessageHandler(PRIVATE & NOT_MENU & filters.TEXT, pay_proof),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Text([CANCEL_TEXT]), cancel),
            MessageHandler(filters.Text(MENU_TEXTS), menu_interrupt),
        ],
        allow_reentry=True,
        name="payment",
    )


# =====================================================================
#                          МІЙ АБОНЕМЕНТ / ЗАПИСИ
# =====================================================================

async def my_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await _private_only(update, context, "my", "🎫 Мій абонемент")
        return
    if not db.enabled():
        await update.message.reply_text(DB_OFF_TEXT)
        return
    today = today_kyiv()
    uid = update.effective_user.id
    subs = await db.user_subscriptions(uid, today)
    bookings = await db.upcoming_bookings(uid, today)

    parts = ["🎫 <b>Мій абонемент</b>"]
    if subs:
        seen = set()
        for s in subs:
            key = (s["student"].lower(), s["sub_type"])
            if s["ends"] < today and key in seen:
                continue
            seen.add(key)
            if s["ends"] >= today:
                left = (s["ends"] - today).days + 1
                state = f"діє до {fmt_date(s['ends'])} (ще {left} дн.)" if s["ends"] > s["starts"] \
                    else f"на {fmt_date(s['starts'])}"
                if s["starts"] > today:
                    state = f"з {fmt_date(s['starts'])} до {fmt_date(s['ends'])}"
                parts.append(f"✅ {h(s['student'])}: {h(s['title'])} — {state}")
            else:
                parts.append(f"⌛️ {h(s['student'])}: {h(s['title'])} — закінчився {fmt_date(s['ends'])}")
    else:
        parts.append("Оплачених абонементів поки немає.")
    parts.append("\nОплатити: 🧾 Реквізити → ✅ Я оплатив")

    await update.message.reply_text("\n".join(parts), parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU)

    if bookings:
        await update.message.reply_text("📅 <b>Найближчі записи:</b>", parse_mode=ParseMode.HTML)
        for b in bookings[:5]:
            state = "✅ підтверджено" if b["status"] == "confirmed" else "⏳ очікує підтвердження"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Скасувати запис", callback_data=f"bc:{b['id']}")]])
            await update.message.reply_text(f"{booking_summary(b)}\n{state}", parse_mode=ParseMode.HTML,
                                            reply_markup=kb)


# =====================================================================
#                             ДНІ НАРОДЖЕННЯ
# =====================================================================

async def birthday_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await _private_only(update, context, "birthday", "🎂 Вказати день народження")
        return ConversationHandler.END
    if not db.enabled():
        await update.message.reply_text(DB_OFF_TEXT)
        return ConversationHandler.END
    begin_flow(context)
    existing = await db.user_birthdays(update.effective_user.id)
    if existing:
        rows = [[InlineKeyboardButton(f"🗑 {b['name']} — {b['day']:02d}.{b['month']:02d}",
                                      callback_data=f"bdel:{b['id']}")] for b in existing]
        await update.message.reply_text(
            "🎂 Збережені дні народження (натисніть, щоб видалити):",
            reply_markup=InlineKeyboardMarkup(rows),
        )
    suggestions = []
    last = await db.last_booking_name(update.effective_user.id)
    if last:
        suggestions.append(last)
    if update.effective_user.first_name and update.effective_user.first_name not in suggestions:
        suggestions.append(update.effective_user.first_name)
    kb = ReplyKeyboardMarkup([[s] for s in suggestions[:2]] + [[CANCEL_TEXT]], resize_keyboard=True)
    await update.message.reply_text(
        "🎂 Бот привітає в групі клубу в день народження.\n\nЧий день народження? Напишіть ім'я.",
        reply_markup=kb,
    )
    return BD_NAME


async def birthday_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = " ".join((update.message.text or "").split())
    if not (2 <= len(name) <= 40):
        await update.message.reply_text("Напишіть, будь ласка, ім'я (від 2 до 40 символів).")
        return BD_NAME
    context.user_data["bd_name"] = name
    await update.message.reply_text("Дата народження у форматі ДД.ММ, наприклад: 15.03",
                                    reply_markup=CANCEL_KB)
    return BD_DATE


def parse_day_month(text: str):
    m = re.match(r"^\s*(\d{1,2})[.\-/ ](\d{1,2})(?:[.\-/ ]\d{2,4})?\s*$", text or "")
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    try:
        date(2024, month, day)  # 2024 — високосний, тож 29.02 теж допустима
    except ValueError:
        return None
    return day, month


async def birthday_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parsed = parse_day_month(update.message.text)
    if not parsed:
        await update.message.reply_text("Не вдалось розібрати дату. Приклад: 15.03")
        return BD_DATE
    day, month = parsed
    name = context.user_data.get("bd_name", update.effective_user.first_name)
    await db.add_birthday(update.effective_user.id, name, day, month)
    context.user_data.clear()
    await update.message.reply_text(
        f"🎂 Збережено: {name} — {day:02d}.{month:02d}. Привітаємо в групі клубу!",
        reply_markup=MAIN_MENU,
    )
    return ConversationHandler.END


async def birthday_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await db.delete_birthday(int(q.data.split(":")[1]), q.from_user.id)
    await q.answer("Видалено")
    rows = await db.user_birthdays(q.from_user.id)
    if rows:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🗑 {b['name']} — {b['day']:02d}.{b['month']:02d}",
                                                         callback_data=f"bdel:{b['id']}")] for b in rows])
        await q.edit_message_reply_markup(kb)
    else:
        await q.edit_message_text("Збережених днів народження немає.")


def birthday_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("birthday", birthday_start),
            MessageHandler(PRIVATE & filters.Regex(r"^/start birthday$"), birthday_start),
        ],
        states={
            BD_NAME: [MessageHandler(PRIVATE & NOT_MENU & filters.TEXT, birthday_name)],
            BD_DATE: [MessageHandler(PRIVATE & NOT_MENU & filters.TEXT, birthday_date)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Text([CANCEL_TEXT]), cancel),
            MessageHandler(filters.Text(MENU_TEXTS), menu_interrupt),
        ],
        allow_reentry=True,
        name="birthday",
    )


# Групи обробників: кожен діалог — окремо, щоб кнопка меню могла перервати один діалог
# і водночас запустити інший (або показати розклад тощо).
GROUP_BOOKING, GROUP_PAYMENT, GROUP_BIRTHDAY = 1, 2, 3


def register(app):
    """Підключає всі діалоги й кнопки до бота."""
    app.add_handler(booking_conversation(), group=GROUP_BOOKING)
    app.add_handler(payment_conversation(), group=GROUP_PAYMENT)
    app.add_handler(birthday_conversation(), group=GROUP_BIRTHDAY)
    app.add_handler(CallbackQueryHandler(admin_booking_decision, pattern=r"^bk:(ok|no):\d+$"))
    app.add_handler(CallbackQueryHandler(client_cancel_booking, pattern=r"^bc:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_payment_decision, pattern=r"^pay:\w+:\d+$"))
    app.add_handler(CallbackQueryHandler(birthday_delete, pattern=r"^bdel:\d+$"))
    app.add_handler(CommandHandler("my", my_subscription))
    app.add_handler(MessageHandler(filters.Text(["🎫 Мій абонемент"]), my_subscription))
    app.add_handler(MessageHandler(PRIVATE & filters.Regex(r"^/start my$"), my_subscription))
