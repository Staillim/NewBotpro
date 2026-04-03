"""Handler: Telegram Stars payments for Lite / Pro plans."""

import logging

from telegram import LabeledPrice, Update
from telegram.ext import ContextTypes

from config.settings import settings
from database import db_manager as db
from database.models import PlanType

logger = logging.getLogger(__name__)

# Payload prefixes stored in the invoice so successful_payment knows what to activate
_PAYLOAD_LITE = "plan_lite_30d"
_PAYLOAD_PRO  = "plan_pro_30d"


async def send_invoice_lite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a Telegram Stars invoice for the Lite plan."""
    query = update.callback_query
    chat_id = query.message.chat_id if query else update.effective_chat.id
    if query:
        await query.answer()

    await context.bot.send_invoice(
        chat_id=chat_id,
        title="💫 Plan Lite — 30 días",
        description=(
            "✅ Catálogo completo (Películas, Series, Anime)\n"
            "✅ Streaming sin anuncios\n"
            "✅ Búsqueda inteligente\n"
            "❌ Sin descarga de contenido"
        ),
        payload=_PAYLOAD_LITE,
        currency="XTR",           # Telegram Stars — no provider token needed
        prices=[LabeledPrice("Plan Lite 30 días", settings.PLAN_LITE_STARS)],
    )


async def send_invoice_pro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a Telegram Stars invoice for the Pro plan."""
    query = update.callback_query
    chat_id = query.message.chat_id if query else update.effective_chat.id
    if query:
        await query.answer()

    await context.bot.send_invoice(
        chat_id=chat_id,
        title="👑 Plan Pro — 30 días",
        description=(
            "✅ Todo lo del Plan Lite\n"
            "✅ Streaming sin anuncios\n"
            "✅ Guardar contenido en tu dispositivo\n"
            "✅ Acceso prioritario a estrenos"
        ),
        payload=_PAYLOAD_PRO,
        currency="XTR",
        prices=[LabeledPrice("Plan Pro 30 días", settings.PLAN_PRO_STARS)],
    )


async def pre_checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approve all pre-checkout queries (Telegram requires a response within 10s)."""
    query = update.pre_checkout_query
    if query.invoice_payload not in (_PAYLOAD_LITE, _PAYLOAD_PRO):
        await query.answer(ok=False, error_message="Pago no reconocido.")
        return
    await query.answer(ok=True)


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Activate plan automatically after Stars payment is confirmed."""
    payment = update.message.successful_payment
    user_id = update.effective_user.id
    payload = payment.invoice_payload

    if payload == _PAYLOAD_LITE:
        plan = PlanType.LITE
        label = "💫 Plan Lite"
    elif payload == _PAYLOAD_PRO:
        plan = PlanType.PRO
        label = "👑 Plan Pro"
    else:
        logger.warning("Unknown payment payload: %s from user %s", payload, user_id)
        return

    try:
        await db.activate_plan(
            user_id=user_id,
            plan=plan,
            days=settings.PLAN_DURATION_DAYS,
            payment_ref=f"stars:{payment.telegram_payment_charge_id}",
        )
        logger.info("Plan %s activated for user %s (charge %s)",
                    plan, user_id, payment.telegram_payment_charge_id)

        await update.message.reply_text(
            f"✅ *¡Pago confirmado!*\n\n"
            f"Tu {label} está activo por *{settings.PLAN_DURATION_DAYS} días*.\n\n"
            f"Ya puedes disfrutar el catálogo completo sin anuncios. 🎬",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("Failed to activate plan for user %s: %s", user_id, e)
        await update.message.reply_text(
            "⚠️ Pago recibido pero hubo un error activando tu plan. "
            "Contacta al soporte con este código: "
            f"`{payment.telegram_payment_charge_id}`",
            parse_mode="Markdown",
        )
