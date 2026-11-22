# -*- coding: utf-8 -*-
"""
Конфіденційність: згода на обробку даних, політика (/privacy),
доступ до своїх даних (/mydata), видалення (/delete_me), відмова від розсилок (/news_off).
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

import config
import db
from common import MAIN_MENU, fmt_date, h, notify_admins
from privacy_text import consent_html, policy_parts

logger = logging.getLogger(__name__)

_consented: set[int] = set()  # кеш: хто вже погодився з поточною версією


async def has_consent(user_id: int) -> bool:
    if user_id in _consented:
        return True
    try:
        version = await db.consent_version(user_id)
    except Exception as e:
        logger.warning("Не вдалось перевірити згоду: %s", e)
        return False
    if version == config.PRIVACY_VERSION:
        _consented.add(user_id)
        return True
    return False


async def ask_consent(update: Update, flow: str):
    """Показує коротку згоду з кнопкою «Погоджуюсь». flow — що запустити після згоди."""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Погоджуюсь", callback_data=f"consent:{flow}")],
        [InlineKeyboardButton("📄 Повний текст політики", callback_data="privacy:show")],
    ])
    await update.effective_message.reply_text(consent_html(), parse_mode=ParseMode.HTML,
                                              reply_markup=kb, disable_web_page_preview=True)


async def save_consent(update: Update):
    u = update.effective_user
    await db.set_consent(u, config.PRIVACY_VERSION)
    _consented.add(u.id)


# ---------- /privacy ----------

async def show_policy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    for part in policy_parts():
        await update.effective_message.reply_text(part, parse_mode=ParseMode.HTML,
                                                  disable_web_page_preview=True)


# ---------- /mydata ----------

async def my_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Ця команда працює в особистих повідомленнях з ботом.")
        return
    u = update.effective_user
    lines = [
        "🗂 <b>Ваші дані в боті</b>",
        "",
        f"Telegram ID: {u.id}",
        f"Ім'я в Telegram: {h(u.full_name)}" + (f" (@{h(u.username)})" if u.username else ""),
    ]
    if not db.enabled():
        lines.append("\nБаза даних не підключена — інших даних бот не зберігає.")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    d = await db.export_user_data(u.id)
    user = d["user"] or {}
    if user.get("consent_at"):
        lines.append(f"Згода на обробку даних: {fmt_date(user['consent_at'].date())} "
                     f"(редакція {h(user.get('consent_version'))})")
    else:
        lines.append("Згоду на обробку даних ще не надано.")
    lines.append("Розсилки: " + ("вимкнені" if user.get("news_off") else "увімкнені"))

    if d["bookings"]:
        lines.append(f"\n<b>Записи ({len(d['bookings'])}):</b>")
        for b in d["bookings"][-10:]:
            lines.append(f"• {fmt_date(b['day'])} {h(b['slot_time'])} — {h(b['direction'])}, "
                         f"{h(b['name'])}, {h(b['phone'])}"
                         + (f", вік {b['age']}" if b.get("age") else "") + f" [{h(b['status'])}]")
    if d["payments"]:
        lines.append(f"\n<b>Квитанції про оплату ({len(d['payments'])}):</b>")
        for p in d["payments"][-10:]:
            lines.append(f"• {fmt_date(p['created_at'].date())} — {h(p['student'])} [{h(p['status'])}]")
    if d["subscriptions"]:
        lines.append(f"\n<b>Абонементи ({len(d['subscriptions'])}):</b>")
        for s in d["subscriptions"][-10:]:
            lines.append(f"• {h(s['student'])}: {h(s['title'])}, {fmt_date(s['starts'])} — {fmt_date(s['ends'])}")
    if d["birthdays"]:
        lines.append("\n<b>Дні народження:</b>")
        for b in d["birthdays"]:
            lines.append(f"• {h(b['name'])} — {b['day']:02d}.{b['month']:02d}")
    if d["warnings"]:
        total = sum(w["count"] for w in d["warnings"])
        lines.append(f"\nПопереджень антиспаму в групах: {total}")

    lines.append("\nВидалити всі дані: /delete_me · Політика: /privacy")
    text = "\n".join(lines)
    await update.message.reply_text(text[:4000], parse_mode=ParseMode.HTML)


# ---------- /delete_me ----------

async def delete_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Ця команда працює в особистих повідомленнях з ботом.")
        return
    if not db.enabled():
        await update.message.reply_text("Бот не зберігає ваших даних у базі.")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 Так, видалити все", callback_data="delme:yes"),
        InlineKeyboardButton("Ні", callback_data="delme:no"),
    ]])
    await update.message.reply_text(
        "Видалити всі ваші дані з бота і відкликати згоду?\n\n"
        "Буде видалено: записи (зокрема майбутні), квитанції, абонементи, дні народження, попередження. "
        "Це не можна скасувати. Інформацію про оплачений абонемент тренер у боті більше не побачить.",
        reply_markup=kb,
    )


async def delete_me_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data != "delme:yes":
        await q.edit_message_text("Нічого не видалено.")
        return
    counts = await db.delete_user_data(q.from_user.id)
    _consented.discard(q.from_user.id)
    context.user_data.clear()
    await q.edit_message_text(
        "🗑 Ваші дані видалено, згоду відкликано.\n\n"
        "Якщо знову запишетесь чи надішлете оплату, бот попросить згоду ще раз."
    )
    # Адміну — лише факт і кількість, без персональних даних
    await notify_admins(
        context.bot,
        "🗑 Один із користувачів видалив свої дані з бота "
        f"(записів: {counts.get('fct_bookings', 0)}, оплат: {counts.get('fct_payments', 0)}, "
        f"абонементів: {counts.get('fct_subscriptions', 0)}).",
    )


# ---------- /news_off, /news_on ----------

async def news_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not db.enabled():
        return
    await db.touch_user(update.effective_user)
    await db.set_news_off(update.effective_user.id, True)
    await update.message.reply_text(
        "🔕 Розсилки вимкнено. Нагадування про ваші записи й абонемент і далі приходитимуть.\n"
        "Увімкнути знову: /news_on", reply_markup=MAIN_MENU)


async def news_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not db.enabled():
        return
    await db.touch_user(update.effective_user)
    await db.set_news_off(update.effective_user.id, False)
    await update.message.reply_text("🔔 Розсилки увімкнено. Вимкнути: /news_off", reply_markup=MAIN_MENU)


def register(app):
    app.add_handler(CommandHandler("privacy", show_policy))
    app.add_handler(CallbackQueryHandler(show_policy, pattern=r"^privacy:show$"))
    app.add_handler(CommandHandler("mydata", my_data))
    app.add_handler(CommandHandler("delete_me", delete_me))
    app.add_handler(CallbackQueryHandler(delete_me_confirm, pattern=r"^delme:(yes|no)$"))
    app.add_handler(CommandHandler("news_off", news_off))
    app.add_handler(CommandHandler("news_on", news_on))
