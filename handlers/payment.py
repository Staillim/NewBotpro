"""Handler: Telegram Stars payments for Lite / Pro plans."""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.ext import ContextTypes

from config.settings import settings
from database import db_manager as db
from database.models import PlanType

logger = logging.getLogger(__name__)

# Payload prefixes stored in the invoice so successful_payment knows what to activate
_PAYLOAD_LITE = "plan_lite_30d"
_PAYLOAD_PRO  = "plan_pro_30d"
_PAYLOAD_DONATE = "donate_stars"


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
    payload = query.invoice_payload
    logger.info("PRE CHECKOUT received — payload=%s user=%s", payload, query.from_user.id)
    if payload not in (_PAYLOAD_LITE, _PAYLOAD_PRO) and not payload.startswith(_PAYLOAD_DONATE):
        logger.warning("PRE CHECKOUT REJECTED — unknown payload: %s", payload)
        await query.answer(ok=False, error_message="Pago no reconocido.")
        return
    logger.info("PRE CHECKOUT OK — approved")
    await query.answer(ok=True)


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Activate plan automatically after Stars payment is confirmed."""
    payment = update.message.successful_payment
    user_id = update.effective_user.id
    payload = payment.invoice_payload
    logger.info("PAGO CONFIRMADO — user=%s payload=%s amount=%s charge=%s",
                user_id, payload, payment.total_amount, payment.telegram_payment_charge_id)

    # ── Donation ──
    if payload.startswith(_PAYLOAD_DONATE):
        user = update.effective_user
        uname = f"@{user.username}" if user.username else user.first_name
        stars = payment.total_amount
        await update.message.reply_text(
            f"❤️ *¡Gracias por tu donación de {stars} ⭐!*\n\n"
            "Tu apoyo nos ayuda a mantener CineStelar funcionando. 🎬",
            parse_mode="Markdown",
        )
        for admin_id in settings.ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"☕ *Nueva donación*\n\n"
                        f"👤 {uname} (`{user_id}`)\n"
                        f"⭐ {stars} estrellas\n"
                        f"💳 `{payment.telegram_payment_charge_id}`"
                    ),
                    parse_mode="Markdown",
                )
            except Exception:
                pass
        return

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

        # Notify admin
        user = update.effective_user
        uname = f"@{user.username}" if user.username else user.first_name
        for admin_id in settings.ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"💰 *Nuevo plan adquirido*\n\n"
                        f"👤 {uname} (`{user_id}`)\n"
                        f"📦 {label}\n"
                        f"💳 `{payment.telegram_payment_charge_id}`"
                    ),
                    parse_mode="Markdown",
                )
            except Exception as notify_exc:
                logger.warning("Admin notify failed: %s", notify_exc)

        # Notify groups
        groups = await db.get_active_groups()
        display_name = f"@{user.username}" if user.username else user.first_name
        group_text = (
            f"🎉 *¡Nuevo miembro Premium!*\n\n"
            f"👤 {display_name} acaba de adquirir el {label}.\n\n"
            f"¡Gracias por apoyar a CineStelar! Ahora puedes disfrutar de todo el "
            f"catálogo sin anuncios, series, anime y mucho más. 🎬🍿"
        )
        group_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "💎 Adquirir un plan",
                url=f"https://t.me/{settings.BOT_USERNAME}?start=plans",
            )
        ]])
        for chat_id in groups:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=group_text,
                    parse_mode="Markdown",
                    reply_markup=group_kb,
                )
            except Exception as g_exc:
                logger.warning("Group plan notify failed for %s: %s", chat_id, g_exc)

    except Exception as e:
        logger.error("Failed to activate plan for user %s: %s", user_id, e)
        await update.message.reply_text(
            "⚠️ Pago recibido pero hubo un error activando tu plan. "
            "Contacta al soporte con este código: "
            f"`{payment.telegram_payment_charge_id}`",
            parse_mode="Markdown",
        )


# ── Donations ─────────────────────────────────────────────────────────────────

DONATE_AMOUNTS = [5, 10, 25, 50, 100]


async def donate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show donation options with preset Star amounts."""
    buttons = [
        [InlineKeyboardButton(f"⭐ {amt} estrellas", callback_data=f"donate:{amt}")]
        for amt in DONATE_AMOUNTS
    ]
    await update.message.reply_text(
        "☕ *¡Apoya a CineStelar!*\n\n"
        "Tu donación nos ayuda a mantener el catálogo, "
        "los servidores y seguir agregando contenido.\n\n"
        "Elige la cantidad de estrellas que deseas donar:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def send_donate_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a Stars invoice for a donation amount."""
    query = update.callback_query
    await query.answer()

    amount = int(query.data.split(":")[1])
    if amount not in DONATE_AMOUNTS:
        return

    await context.bot.send_invoice(
        chat_id=query.message.chat_id,
        title=f"☕ Donación — {amount} ⭐",
        description="¡Gracias por apoyar a CineStelar! Tu donación nos ayuda a crecer.",
        payload=f"{_PAYLOAD_DONATE}_{amount}",
        currency="XTR",
        prices=[LabeledPrice(f"Donación {amount} estrellas", amount)],
    )
