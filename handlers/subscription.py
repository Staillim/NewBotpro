"""Handler: Subscription plans and account management."""

import logging
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config.settings import settings
from database import db_manager as db
from database.models import PlanType, SubStatus

logger = logging.getLogger(__name__)


PLANS_TEXT = """
💎 *Planes CineStelar Premium*

Elige la duración que prefieras:

━━━━━━━━━━━━━━━━━━━━━━

⚡ *Plan Lite 15 días* — {lite_15d_stars} ⭐
└ Ideal para descubrir el servicio

💫 *Plan Lite 30 días* — {lite_stars} ⭐ / mes
├ ✅ Catálogo completo (Pelis, Series, Anime)
├ ✅ Streaming sin anuncios
├ ✅ Búsqueda inteligente
└ ❌ No puedes guardar contenido

🗓️ *Plan Lite 6 meses* — {lite_6m_stars} ⭐
└ 🔥 ¡Ahorra {lite_6m_savings} ⭐ vs pago mensual! (≈23%)

🏆 *Plan Lite 1 año* — {lite_1y_stars} ⭐
└ 🔥 ¡Ahorra {lite_1y_savings} ⭐ vs pago mensual! (≈40%)

━━━━━━━━━━━━━━━━━━━━━━

👑 *Plan Pro 30 días* — {pro_stars} ⭐ / mes
├ ✅ Todo lo del Plan Lite
├ ✅ Guardar contenido en tu dispositivo
├ ✅ Acceso prioritario a estrenos
└ ✅ Soporte prioritario

━━━━━━━━━━━━━━━━━━━━━━

⭐ El pago se realiza con *Telegram Stars* directamente desde la app.
"""


async def show_plans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display subscription plans."""
    query = update.callback_query
    if query:
        await query.answer()

    text = PLANS_TEXT.format(
        lite_stars=settings.PLAN_LITE_STARS,
        pro_stars=settings.PLAN_PRO_STARS,
        lite_15d_stars=settings.PLAN_LITE_15D_STARS,
        lite_6m_stars=settings.PLAN_LITE_6M_STARS,
        lite_1y_stars=settings.PLAN_LITE_1Y_STARS,
        lite_6m_savings=settings.PLAN_LITE_STARS * 6 - settings.PLAN_LITE_6M_STARS,
        lite_1y_savings=settings.PLAN_LITE_STARS * 12 - settings.PLAN_LITE_1Y_STARS,
    )

    buttons = [
        [
            InlineKeyboardButton(f"⚡ 15d — {settings.PLAN_LITE_15D_STARS} ⭐", callback_data="payment:lite_15d"),
            InlineKeyboardButton(f"💫 30d — {settings.PLAN_LITE_STARS} ⭐",    callback_data="payment:lite"),
        ],
        [
            InlineKeyboardButton(f"🗓️ 6m — {settings.PLAN_LITE_6M_STARS} ⭐",  callback_data="payment:lite_6m"),
            InlineKeyboardButton(f"🏆 1año — {settings.PLAN_LITE_1Y_STARS} ⭐", callback_data="payment:lite_1y"),
        ],
        [
            InlineKeyboardButton(f"👑 Pro 30d — {settings.PLAN_PRO_STARS} ⭐",  callback_data="payment:pro"),
        ],
        [InlineKeyboardButton("🏠 Menú Principal", callback_data="menu:main")],
    ]

    if query:
        # If the message has a photo (poster), we can't edit_message_text —
        # delete it and send a new text message instead.
        if query.message.photo or query.message.document:
            try:
                await query.message.delete()
            except Exception:
                pass
            sent = await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="Markdown",
            )
        else:
            try:
                await query.edit_message_text(
                    text,
                    reply_markup=InlineKeyboardMarkup(buttons),
                    parse_mode="Markdown",
                )
            except Exception:
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=text,
                    reply_markup=InlineKeyboardMarkup(buttons),
                    parse_mode="Markdown",
                )
    else:
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )


async def select_plan(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_key: str):
    """Handle plan selection — show payment instructions."""
    query = update.callback_query
    await query.answer()

    plan_label = "Lite 💫" if plan_key == "lite" else "Pro 👑"
    price = "$3 USD" if plan_key == "lite" else "$5 USD"
    features = (
        "• Catálogo completo\n• Streaming ilimitado\n• Sin anuncios"
        if plan_key == "lite"
        else "• Todo lo del Lite\n• Guardar contenido\n• Soporte prioritario"
    )

    # Build admin contact for payment
    admin_links = ""
    for admin_id in settings.ADMIN_IDS[:2]:
        admin_links += f"👉 [Contactar Admin](tg://user?id={admin_id})\n"

    text = (
        f"💳 *Suscripción al Plan {plan_label}*\n\n"
        f"💰 Precio: *{price}/mes*\n\n"
        f"Incluye:\n{features}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📩 *¿Cómo pagar?*\n\n"
        f"1️⃣ Realiza el pago de {price}\n"
        f"2️⃣ Envía el comprobante al admin\n"
        f"3️⃣ Tu plan se activará al instante\n\n"
        f"{admin_links}\n"
        f"_Tu plan se activa inmediatamente después de la verificación del pago._"
    )

    buttons = [
        [InlineKeyboardButton("🔙 Ver Planes", callback_data="plans:show")],
        [InlineKeyboardButton("🏠 Menú Principal", callback_data="menu:main")],
    ]

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def show_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user account info."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    user = await db.get_user(user_id)
    if not user:
        await query.answer("Error al obtener tu cuenta.", show_alert=True)
        return

    is_active, plan = await db.check_subscription(user_id)
    plan_label = {
        PlanType.LITE: "💫 Lite",
        PlanType.PRO: "👑 Pro",
        PlanType.NONE: "❌ Sin plan",
    }.get(plan, "❌ Sin plan")

    status_label = "✅ Activo" if is_active else "❌ Inactivo"
    expires = ""
    if user.plan_expires_at:
        exp_date = user.plan_expires_at
        if hasattr(exp_date, 'strftime'):
            expires = f"\n📅 Vence: {exp_date.strftime('%d/%m/%Y')}"

    joined = user.joined_at.strftime('%d/%m/%Y') if user.joined_at else "N/A"

    text = (
        f"👤 *Mi Cuenta*\n\n"
        f"🆔 ID: `{user.user_id}`\n"
        f"📛 Nombre: {user.first_name or 'N/A'}\n"
        f"👤 Username: @{user.username or 'N/A'}\n"
        f"📅 Registro: {joined}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💎 Plan: {plan_label}\n"
        f"📊 Estado: {status_label}{expires}\n"
    )

    buttons = []
    if not is_active:
        buttons.append([InlineKeyboardButton("💎 Ver Planes", callback_data="plans:show")])
    buttons.append([InlineKeyboardButton("🏠 Menú Principal", callback_data="menu:main")])

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )
