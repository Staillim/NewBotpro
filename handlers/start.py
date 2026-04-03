"""Handler: /start – onboarding, verification, and main menu."""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config.settings import settings
from database import db_manager as db
from database.models import PlanType

logger = logging.getLogger(__name__)


# ── Main Menu Keyboard ───────────────────────────────────────────────────────

def main_menu_keyboard(has_plan: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("🎬 Películas", callback_data="cat:movies:0"),
            InlineKeyboardButton("📺 Series", callback_data="cat:series:0"),
        ],
        [
            InlineKeyboardButton("🎌 Anime", callback_data="cat:anime:0"),
            InlineKeyboardButton("🔍 Buscar", callback_data="search:start"),
        ],
        [
            InlineKeyboardButton("⭐ Mis Favoritos", callback_data="favorites:list"),
        ],
    ]
    if has_plan:
        buttons.append([
            InlineKeyboardButton("👤 Mi Cuenta", callback_data="account:info"),
        ])
    else:
        buttons.append([
            InlineKeyboardButton("💎 Planes Premium", callback_data="plans:show"),
        ])
    return InlineKeyboardMarkup(buttons)


WELCOME_TEXT = """
🎬 *Bienvenido a CineStelar Premium*

Tu plataforma de entretenimiento en Telegram.

📽️ *Películas* — Miles de títulos disponibles
📺 *Series* — Temporadas completas
🎌 *Anime* — Lo mejor del anime

Navega el catálogo y disfruta tu contenido favorito al instante.
"""

NOT_SUBSCRIBED_TEXT = """
⚠️ *No tienes un plan activo*

Para acceder al catálogo necesitas una suscripción:

💫 *Plan Lite* — $3/mes
├ Acceso completo al catálogo
├ Streaming ilimitado
└ Sin anuncios

👑 *Plan Pro* — $5/mes
├ Todo lo del plan Lite
├ Guardar contenido en tu dispositivo
└ Acceso prioritario a estrenos

Selecciona un plan para comenzar 👇
"""

VERIFY_TEXT = """
⚠️ *Verificación requerida*

Para usar el bot, únete a nuestro canal oficial:

👉 {channel}

Después presiona el botón de verificación.
"""


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user = update.effective_user
    if not user:
        return

    # Parse referral
    referred_by = None
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            try:
                referred_by = int(arg[4:])
                if referred_by == user.id:
                    referred_by = None  # prevent self-referral
            except ValueError:
                pass

    # Register/update user
    db_user = await db.get_or_create_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        language_code=user.language_code,
        referred_by=referred_by,
    )

    if db_user.banned:
        await update.message.reply_text("🚫 Tu cuenta ha sido suspendida.")
        return

    # Check channel membership
    if settings.VERIFICATION_CHANNEL_ID:
        try:
            member = await context.bot.get_chat_member(
                settings.VERIFICATION_CHANNEL_ID, user.id
            )
            if member.status in ("left", "kicked"):
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "📢 Unirse al Canal",
                        url=f"https://t.me/{settings.VERIFICATION_CHANNEL_USERNAME.lstrip('@')}"
                    )],
                    [InlineKeyboardButton("✅ Ya me uní", callback_data="verify:check")],
                ])
                await update.message.reply_text(
                    VERIFY_TEXT.format(channel=settings.VERIFICATION_CHANNEL_USERNAME),
                    reply_markup=kb,
                    parse_mode="Markdown",
                )
                return
            else:
                if not db_user.verified:
                    await db.set_user_verified(user.id)
        except Exception as e:
            logger.warning("Verification check failed: %s", e)

    # Check subscription
    is_active, plan = await db.check_subscription(user.id)

    if is_active:
        await update.message.reply_text(
            WELCOME_TEXT,
            reply_markup=main_menu_keyboard(has_plan=True),
            parse_mode="Markdown",
        )
    else:
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💫 Lite — $3/mes", callback_data="plans:lite"),
                InlineKeyboardButton("👑 Pro — $5/mes", callback_data="plans:pro"),
            ],
        ])
        await update.message.reply_text(
            NOT_SUBSCRIBED_TEXT,
            reply_markup=kb,
            parse_mode="Markdown",
        )


async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle verification button press."""
    query = update.callback_query
    await query.answer()
    user = query.from_user

    try:
        member = await context.bot.get_chat_member(
            settings.VERIFICATION_CHANNEL_ID, user.id
        )
        if member.status in ("left", "kicked"):
            await query.answer("❌ Aún no te has unido al canal.", show_alert=True)
            return
    except Exception:
        pass

    await db.set_user_verified(user.id)

    is_active, plan = await db.check_subscription(user.id)
    if is_active:
        await query.edit_message_text(
            WELCOME_TEXT,
            reply_markup=main_menu_keyboard(has_plan=True),
            parse_mode="Markdown",
        )
    else:
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💫 Lite — $3/mes", callback_data="plans:lite"),
                InlineKeyboardButton("👑 Pro — $5/mes", callback_data="plans:pro"),
            ],
        ])
        await query.edit_message_text(
            NOT_SUBSCRIBED_TEXT,
            reply_markup=kb,
            parse_mode="Markdown",
        )
