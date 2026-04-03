"""Handler: /start – onboarding, verification, and main menu."""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
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
    # Catalog WebApp button – only shown when WEBAPP_URL is configured
    if settings.WEBAPP_URL:
        buttons.append([
            InlineKeyboardButton(
                "🌐 Catálogo Web",
                web_app=WebAppInfo(url=settings.WEBAPP_URL),
            )
        ])
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

    # Parse deeplink args
    referred_by = None
    catalog_deeplink = None
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            try:
                referred_by = int(arg[4:])
                if referred_by == user.id:
                    referred_by = None  # prevent self-referral
            except ValueError:
                pass
        elif arg.startswith("watch_movie_") or arg.startswith("watch_show_"):
            catalog_deeplink = arg

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

    # Admins always get full access — no plan required
    is_admin = settings.is_admin(user.id)

    # Check subscription
    is_active, plan = await db.check_subscription(user.id)

    # ── Handle catalog deeplinks from WebApp ──
    if catalog_deeplink:
        await _handle_catalog_deeplink(update, catalog_deeplink, is_active or is_admin, plan)
        return

    if is_active or is_admin:
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


# ── Catalog deeplink handler ─────────────────────────────────────────────────

async def _handle_catalog_deeplink(
    update: Update,
    arg: str,
    is_active: bool,
    plan,
):
    """Handle watch_movie_ID and watch_show_ID deeplinks from the WebApp."""
    if not is_active:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("💫 Lite — $3/mes", callback_data="plans:lite"),
            InlineKeyboardButton("👑 Pro — $5/mes", callback_data="plans:pro"),
        ]])
        await update.message.reply_text(
            NOT_SUBSCRIBED_TEXT, reply_markup=kb, parse_mode="Markdown"
        )
        return

    protect = plan != PlanType.PRO

    if arg.startswith("watch_movie_"):
        try:
            movie_id = int(arg.split("_")[-1])
        except ValueError:
            return
        movie = await db.get_movie(movie_id)
        if not movie:
            await update.message.reply_text("⚠️ Película no encontrada.")
            return
        caption = f"🎬 *{movie.title}*"
        if movie.year:
            caption += f"  ({movie.year})"
        if movie.vote_average:
            caption += f"\n⭐ {movie.vote_average:.1f}"
        if movie.overview:
            caption += f"\n\n{movie.overview[:300]}…"
        await update.message.reply_video(
            movie.file_id,
            caption=caption,
            parse_mode="Markdown",
            protect_content=protect,
        )

    elif arg.startswith("watch_show_"):
        try:
            show_id = int(arg.split("_")[-1])
        except ValueError:
            return
        show = await db.get_show(show_id)
        if not show:
            await update.message.reply_text("⚠️ Serie no encontrada.")
            return
        seasons = show.number_of_seasons or "?"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"📂 Ver temporadas ({seasons})",
                callback_data=f"show:{show_id}",
            )
        ]])
        caption = f"{'🎌' if show.content_type and 'anime' in show.content_type.value else '📺'} *{show.name}*"
        if show.year:
            caption += f"  ({show.year})"
        if show.vote_average:
            caption += f"\n⭐ {show.vote_average:.1f}"
        await update.message.reply_text(
            caption, reply_markup=kb, parse_mode="Markdown"
        )
