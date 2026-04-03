"""Handler: /start – onboarding, verification, and main menu."""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ContextTypes

from config.settings import settings
from database import db_manager as db

logger = logging.getLogger(__name__)


# ── Main Menu Keyboard ───────────────────────────────────────────────────────

def main_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    if settings.WEBAPP_URL:
        buttons.append([
            InlineKeyboardButton(
                "🎥 Ver Catálogo",
                web_app=WebAppInfo(url=settings.WEBAPP_URL),
            )
        ])
    return InlineKeyboardMarkup(buttons)


WELCOME_TEXT = """
🎬 *¿Bienvenido a TodoCineHD!*

Aquí encontrarás películas, series y anime para ver desde Telegram.

📺 *¿Quieres pedir algo?*
Entra a @TodoCineHD y dínos qué quieres ver.

⬇️ Toca el botón para abrir el catálogo completo.
"""

VERIFY_TEXT = """
⚠️ *Verificación requerida*

Para usar el bot, únete a nuestro canal oficial:

👉 {channel}

Después presiona el botón de verificación.
"""


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command — shows admin panel for admins, catalog for users."""
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
        elif arg.startswith(("watch_movie_", "watch_show_", "watch_series_", "watch_anime_", "watch_ep_")):
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

    # ── Admins get the admin panel ──
    if settings.is_admin(user.id):
        from handlers.admin import send_admin_panel
        await send_admin_panel(update.message, context)
        return

    # ── Handle catalog deeplinks from WebApp ──
    if catalog_deeplink:
        await _handle_catalog_deeplink(update, catalog_deeplink)
        return

    await update.message.reply_text(
        WELCOME_TEXT,
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown",
    )


async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle verification button press (kept for old buttons)."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        WELCOME_TEXT,
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown",
    )


# ── Catalog deeplink handler ─────────────────────────────────────────────────

async def _handle_catalog_deeplink(update: Update, arg: str):
    """Handle watch_movie_ID, watch_show_ID, watch_series_ID, watch_anime_ID deeplinks."""

    if arg.startswith("watch_movie_"):
        try:
            movie_id = int(arg.split("_")[-1])
        except ValueError:
            return
        movie = await db.get_movie(movie_id)
        if not movie:
            await update.message.reply_text("⚠️ Película no encontrada.")
            return

        user_id = update.effective_user.id
        is_active, _ = await db.check_subscription(user_id)

        if not is_active:
            caption = (
                f"🎬 *{movie.title}*\n\n"
                f"_Necesitas un plan o ver un anuncio para continuar._"
            )
            buttons = [
                [InlineKeyboardButton("📺 Ver con anuncio", callback_data=f"watch_ad:movie:{movie.id}")],
                [InlineKeyboardButton("💎 Adquirir plan", callback_data="plans:show")],
            ]
            if movie.poster_url:
                try:
                    await update.message.reply_photo(
                        movie.poster_url,
                        caption=caption,
                        reply_markup=InlineKeyboardMarkup(buttons),
                        parse_mode="Markdown",
                    )
                    return
                except Exception:
                    pass
            await update.message.reply_text(
                caption,
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="Markdown",
            )
            return

        caption = f"🎬 *{movie.title}*"
        if movie.year:
            caption += f"  ({movie.year})"
        if movie.vote_average:
            caption += f"\n⭐ {movie.vote_average:.1f}"
        if movie.overview:
            caption += f"\n\n{movie.overview[:300]}…"
        try:
            await update.message.reply_video(
                movie.file_id,
                caption=caption,
                parse_mode="Markdown",
            )
        except Exception:
            try:
                await update.message.reply_document(
                    movie.file_id,
                    caption=caption,
                    parse_mode="Markdown",
                )
            except Exception:
                await update.message.reply_text("❌ Error al enviar la película.")

    elif arg.startswith(("watch_show_", "watch_series_", "watch_anime_")):
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
        emoji = "🎌" if show.content_type and "anime" in show.content_type.value else "📺"
        caption = f"{emoji} *{show.name}*"
        if show.year:
            caption += f"  ({show.year})"
        if show.vote_average:
            caption += f"\n⭐ {show.vote_average:.1f}"
        if show.poster_url:
            try:
                await update.message.reply_photo(
                    show.poster_url,
                    caption=caption,
                    reply_markup=kb,
                    parse_mode="Markdown",
                )
                return
            except Exception:
                pass
        await update.message.reply_text(
            caption, reply_markup=kb, parse_mode="Markdown"
        )

    elif arg.startswith("watch_ep_"):
        try:
            ep_id = int(arg.split("_")[-1])
        except ValueError:
            return
        ep = await db.get_episode(ep_id)
        if not ep:
            await update.message.reply_text("⚠️ Episodio no encontrado.")
            return
        show = await db.get_show(ep.tv_show_id)
        show_name = show.name if show else "Serie"
        emoji = "🎌" if show and show.content_type and "anime" in show.content_type.value else "📺"
        ep_title = ep.title or f"Episodio {ep.episode_number}"
        user_id = update.effective_user.id
        is_active, _ = await db.check_subscription(user_id)

        if not is_active:
            ep_label = f"T{ep.season_number}E{ep.episode_number}: {ep_title}"
            caption = (
                f"{emoji} *{show_name}*\n{ep_label}\n\n"
                f"_Necesitas un plan o ver un anuncio para continuar._"
            )
            buttons = [
                [InlineKeyboardButton("📺 Ver con anuncio", callback_data=f"watch_ad:ep:{ep.id}")],
                [InlineKeyboardButton("💎 Adquirir plan", callback_data="plans:show")],
            ]
            poster_url = show.poster_url if show else None
            if poster_url:
                try:
                    await update.message.reply_photo(
                        poster_url,
                        caption=caption,
                        reply_markup=InlineKeyboardMarkup(buttons),
                        parse_mode="Markdown",
                    )
                    return
                except Exception:
                    pass
            await update.message.reply_text(
                caption,
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="Markdown",
            )
            return

        caption = f"{emoji} *{show_name}*\nT{ep.season_number}E{ep.episode_number}: {ep_title}"
        try:
            await update.message.reply_video(
                ep.file_id, caption=caption, parse_mode="Markdown"
            )
        except Exception:
            try:
                await update.message.reply_document(
                    ep.file_id, caption=caption, parse_mode="Markdown"
                )
            except Exception:
                await update.message.reply_text("❌ Error al enviar el episodio.")
