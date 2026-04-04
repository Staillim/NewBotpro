"""Central callback query router."""

import logging
import urllib.parse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ContextTypes

from config.settings import settings
from database.models import ContentType
from handlers.start import main_menu_keyboard, WELCOME_TEXT, verify_callback
from handlers.catalog import (
    show_movies_page,
    show_shows_page,
    show_movie_detail,
    show_show_detail,
    show_season,
    watch_movie,
    watch_episode,
    download_movie,
    show_favorites,
    toggle_favorite,
)
from handlers.subscription import show_plans, select_plan, show_account
from handlers.payment import send_invoice_lite, send_invoice_pro, send_invoice_lite_15d, send_invoice_lite_6m, send_invoice_lite_1y
from handlers.admin import (
    stats_command,
    activate_plan_start,
    index_command,
    handle_series_selection,
    handle_delete_callback,
    send_admin_panel,
    show_content_menu,
    show_content_list,
)
from database import db_manager as db

logger = logging.getLogger(__name__)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route all callback queries to appropriate handlers."""
    query = update.callback_query
    data = query.data

    if not data:
        await query.answer()
        return

    parts = data.split(":")

    try:
        # ── Navigation ────────────────────────────────────────────
        if data == "menu:main":
            await query.answer()
            await query.edit_message_text(
                WELCOME_TEXT,
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown",
            )

        # ── Verification ──────────────────────────────────────────
        elif data == "verify:check":
            await verify_callback(update, context)

        # ── Catalog: Movies ───────────────────────────────────────
        elif data.startswith("cat:movies:"):
            page = int(parts[2])
            await query.answer()
            await show_movies_page(update, context, page)

        # ── Catalog: Series ───────────────────────────────────────
        elif data.startswith("cat:series:"):
            page = int(parts[2])
            await query.answer()
            await show_shows_page(update, context, ContentType.SERIES, page)

        # ── Catalog: Anime ────────────────────────────────────────
        elif data.startswith("cat:anime:"):
            page = int(parts[2])
            await query.answer()
            await show_shows_page(update, context, ContentType.ANIME, page)

        # ── Movie Detail ──────────────────────────────────────────
        elif data.startswith("movie:"):
            movie_id = int(parts[1])
            await query.answer()
            await show_movie_detail(update, context, movie_id)

        # ── Show Detail (Series/Anime) ────────────────────────────
        elif data.startswith("show:"):
            show_id = int(parts[1])
            await query.answer()
            await show_show_detail(update, context, show_id)

        # ── Season Episodes ───────────────────────────────────────
        elif data.startswith("season:"):
            show_id = int(parts[1])
            season = int(parts[2])
            page = int(parts[3]) if len(parts) > 3 else 0
            await query.answer()
            await show_season(update, context, show_id, season, page)

        # ── Watch ─────────────────────────────────────────────────
        elif data.startswith("watch:movie:"):
            movie_id = int(parts[2])
            await watch_movie(update, context, movie_id)

        elif data.startswith("watch:ep:"):
            episode_id = int(parts[2])
            await watch_episode(update, context, episode_id)

        # ── Watch Ad (open Mini App after subscription check fails) ───
        elif data.startswith("watch_ad:"):
            # watch_ad:movie:42  /  watch_ad:ep:42
            content_kind = parts[1]   # "movie" or "ep"
            content_id   = int(parts[2])

            if content_kind == "movie":
                item = await db.get_movie(content_id)
                title  = item.title     if item else "Película"
                poster = item.poster_url if item else ""
            else:
                ep     = await db.get_episode(content_id)
                show   = await db.get_show(ep.tv_show_id) if ep else None
                ep_num = ep.episode_number if ep else 0
                ep_ttl = (ep.title or f"Episodio {ep_num}") if ep else "Episodio"
                title  = f"{show.name} — {ep_ttl}" if show else ep_ttl
                poster = (show.poster_url or "") if show else ""

            base_url = settings.WEBAPP_URL.rstrip("/")
            qs = urllib.parse.urlencode({
                "user_id":      query.from_user.id,
                "content_id":   content_id,
                "content_type": content_kind,
                "title":        title,
                "poster":       poster,
            })
            webapp_url = f"{base_url}/ad?{qs}"

            btn = InlineKeyboardButton(
                "📺 Ver anuncio ahora",
                web_app=WebAppInfo(url=webapp_url),
            )
            await query.answer()
            await query.message.edit_reply_markup(InlineKeyboardMarkup([[btn]]))

        # ── Download ──────────────────────────────────────────────
        elif data.startswith("download:movie:"):
            movie_id = int(parts[2])
            await download_movie(update, context, movie_id)

        # ── Search ────────────────────────────────────────────────
        elif data.startswith("search:"):
            from handlers.search import search_start
            await search_start(update, context)

        # ── Favorites ─────────────────────────────────────────────
        elif data == "favorites:list":
            await query.answer()
            await show_favorites(update, context)

        elif data.startswith("fav:"):
            # fav:add:movie:123 or fav:remove:series:456
            action = parts[1]
            ct_str = parts[2]
            content_id = int(parts[3])
            await toggle_favorite(update, context, action, ct_str, content_id)

        # ── Plans ─────────────────────────────────────────────────
        elif data == "plans:show":
            await show_plans(update, context)

        elif data.startswith("plans:"):
            plan_key = parts[1]
            if plan_key in ("lite", "pro"):
                await select_plan(update, context, plan_key)

        # ── Payment (Telegram Stars) ───────────────────────────────
        elif data == "payment:lite":
            await send_invoice_lite(update, context)
        elif data == "payment:lite_15d":
            await send_invoice_lite_15d(update, context)

        elif data == "payment:lite_6m":
            await send_invoice_lite_6m(update, context)

        elif data == "payment:lite_1y":
            await send_invoice_lite_1y(update, context)
        elif data == "payment:pro":
            await send_invoice_pro(update, context)

        # ── Account ───────────────────────────────────────────────
        elif data == "account:info":
            await show_account(update, context)

        # ── Admin ─────────────────────────────────────────────────
        elif data.startswith("admin:") and settings.is_admin(query.from_user.id):

            if data == "admin:broadcast":
                await query.answer("Usa /broadcast <mensaje> en el chat privado.", show_alert=True)

            else:
                await query.answer()

                if data == "admin:home":
                    await send_admin_panel(query, context)

                elif data == "admin:stats":
                    await stats_command(update, context)

                elif data == "admin:activate":
                    await activate_plan_start(update, context)

                elif data == "admin:index":
                    await index_command(update, context)

                elif data == "admin:users":
                    try:
                        total = await db.get_total_users()
                        active = await db.get_active_subscribers()
                    except Exception:
                        total = active = "?"
                    back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Panel principal", callback_data="admin:home")]])
                    await query.edit_message_text(
                        f"👥 *Gestión de Usuarios*\n\n"
                        f"Total: `{total}`\n"
                        f"Suscriptores activos: `{active}`\n\n"
                        f"*Comandos disponibles:*\n"
                        f"`/activar <id> <lite|pro> [días]`\n"
                        f"`/cancelar <id>`\n"
                        f"`/ban <id>`\n"
                        f"`/unban <id>`",
                        reply_markup=back,
                        parse_mode="Markdown",
                    )

                elif data == "admin:content":
                    await show_content_menu(query, context)

                elif data.startswith("admin:content:"):
                    # admin:content:<kind>:<page>
                    _, _, kind, page_str = data.split(":", 3)
                    await show_content_list(query, context, kind, int(page_str))

                elif data.startswith("admin:del:"):
                    await handle_delete_callback(update, context, parts[2:])

                elif data.startswith("admin:select_series:"):
                    idx = int(parts[2])
                    await handle_series_selection(update, context, idx)

                else:
                    logger.warning("Unhandled admin callback: %s", data)

        elif data.startswith("admin:"):
            await query.answer("🚫 Sin permisos.", show_alert=True)

        else:
            await query.answer()
            logger.warning("Unhandled callback: %s", data)

    except Exception as e:
        logger.error("Callback error for '%s': %s", data, e, exc_info=True)
        try:
            await query.answer("❌ Error interno. Intenta de nuevo.", show_alert=True)
        except Exception:
            pass
