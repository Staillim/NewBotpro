"""Handler: Catalog browsing – movies, series, anime with pagination."""

import logging
import math

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config.settings import settings
from database import db_manager as db
from database.models import ContentType

logger = logging.getLogger(__name__)
PAGE_SIZE = settings.CATALOG_PAGE_SIZE


# ── Category list (movies page) ──────────────────────────────────────────────

async def show_movies_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    query = update.callback_query

    movies, total = await db.get_movies_page(page, PAGE_SIZE)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))

    if not movies:
        await query.answer("No hay películas disponibles aún.", show_alert=True)
        return

    text = f"🎬 *Películas* — Página {page + 1}/{total_pages}\n\n"
    buttons = []
    for m in movies:
        star = f"⭐{m.vote_average:.1f}" if m.vote_average else ""
        year = f"({m.year})" if m.year else ""
        text += f"• {m.title} {year} {star}\n"
        buttons.append([
            InlineKeyboardButton(
                f"🎬 {m.title} {year}",
                callback_data=f"movie:{m.id}"
            )
        ])

    # Pagination
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"cat:movies:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"cat:movies:{page + 1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("🔍 Buscar Película", callback_data="search:movies")])
    buttons.append([InlineKeyboardButton("🏠 Menú Principal", callback_data="menu:main")])

    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
    )


# ── Category list (series/anime page) ────────────────────────────────────────

async def show_shows_page(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           content_type: ContentType, page: int = 0):
    query = update.callback_query

    shows, total = await db.get_shows_page(content_type, page, PAGE_SIZE)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    cat_key = "series" if content_type == ContentType.SERIES else "anime"
    emoji = "📺" if content_type == ContentType.SERIES else "🎌"
    label = "Series" if content_type == ContentType.SERIES else "Anime"

    if not shows:
        await query.answer(f"No hay {label.lower()} disponibles aún.", show_alert=True)
        return

    text = f"{emoji} *{label}* — Página {page + 1}/{total_pages}\n\n"
    buttons = []
    for s in shows:
        star = f"⭐{s.vote_average:.1f}" if s.vote_average else ""
        year = f"({s.year})" if s.year else ""
        text += f"• {s.name} {year} {star}\n"
        buttons.append([
            InlineKeyboardButton(
                f"{emoji} {s.name} {year}",
                callback_data=f"show:{s.id}"
            )
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"cat:{cat_key}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"cat:{cat_key}:{page + 1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(f"🔍 Buscar {label}", callback_data=f"search:{cat_key}")])
    buttons.append([InlineKeyboardButton("🏠 Menú Principal", callback_data="menu:main")])

    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
    )


# ── Movie detail ──────────────────────────────────────────────────────────────

async def show_movie_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, movie_id: int):
    query = update.callback_query

    movie = await db.get_movie(movie_id)
    if not movie:
        await query.answer("Película no encontrada.", show_alert=True)
        return

    star = f"⭐ {movie.vote_average:.1f}/10" if movie.vote_average else ""
    year = f"({movie.year})" if movie.year else ""
    genres = f"🏷️ {movie.genres}" if movie.genres else ""
    runtime = f"⏱️ {movie.runtime} min" if movie.runtime else ""
    overview = movie.overview[:300] + "..." if movie.overview and len(movie.overview) > 300 else (movie.overview or "")

    text = (
        f"🎬 *{movie.title}* {year}\n"
        f"{star}\n"
        f"{genres}\n"
        f"{runtime}\n\n"
        f"📝 {overview}"
    )

    buttons = []
    buttons.append([InlineKeyboardButton("▶️ Ver Película", callback_data=f"watch:movie:{movie.id}")])
    buttons.append([InlineKeyboardButton("💾 Guardar en Dispositivo", callback_data=f"download:movie:{movie.id}")])
    buttons.append([
        InlineKeyboardButton("⭐ Favorito", callback_data=f"fav:add:movie:{movie.id}"),
        InlineKeyboardButton("🔙 Volver", callback_data="cat:movies:0"),
    ])

    if movie.poster_url:
        try:
            await query.message.delete()
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=movie.poster_url,
                caption=text,
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="Markdown",
            )
            return
        except Exception:
            pass

    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
    )


# ── Show detail (series/anime) ───────────────────────────────────────────────

async def show_show_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, show_id: int):
    """Skip description page — go straight to episode list (or compact season selector)."""
    query = update.callback_query

    show = await db.get_show(show_id)
    if not show:
        await query.answer("No encontrado.", show_alert=True)
        return

    seasons = await db.get_seasons(show_id)
    if not seasons:
        await query.answer("Esta serie aún no tiene episodios.", show_alert=True)
        return

    # Single season → go directly to episode list
    if len(seasons) == 1:
        await show_season(update, context, show_id, seasons[0], page=0)
        return

    # Multiple seasons → compact list (no poster/description, just season buttons)
    emoji = "📺" if show.content_type == ContentType.SERIES else "🎌"
    year = f" ({show.year})" if show.year else ""
    text = f"{emoji} *{show.name}{year}*\n\nSelecciona una temporada:"

    buttons = []
    for s in seasons:
        eps = await db.get_episodes(show_id, s)
        ep_count = f"  ({len(eps)} ep.)" if eps else ""
        buttons.append([
            InlineKeyboardButton(f"📁 Temporada {s}{ep_count}", callback_data=f"season:{show_id}:{s}:0")
        ])

    cat_key = "series" if show.content_type == ContentType.SERIES else "anime"
    buttons.append([InlineKeyboardButton("🔙 Volver", callback_data=f"cat:{cat_key}:0")])

    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
    )


# ── Season episodes list ─────────────────────────────────────────────────────

EP_PAGE_SIZE = 20

async def show_season(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       show_id: int, season: int, page: int = 0):
    query = update.callback_query

    show = await db.get_show(show_id)
    episodes = await db.get_episodes(show_id, season)

    if not episodes:
        await query.answer("No hay episodios disponibles.", show_alert=True)
        return

    total = len(episodes)
    total_pages = max(1, math.ceil(total / EP_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    page_eps = episodes[page * EP_PAGE_SIZE : (page + 1) * EP_PAGE_SIZE]

    emoji = "📺" if show and show.content_type == ContentType.SERIES else "🎌"
    title = show.name if show else "Serie"
    year = f" ({show.year})" if show and show.year else ""

    page_info = f"  —  {page + 1}/{total_pages}" if total_pages > 1 else ""
    text = f"{emoji} *{title}{year}*  — T{season}{page_info}\n\n"

    buttons = []
    for ep in page_eps:
        ep_title = ep.title or f"Episodio {ep.episode_number}"
        label = f"▶️ {ep.episode_number}. {ep_title[:40]}"
        buttons.append([
            InlineKeyboardButton(label, callback_data=f"watch:ep:{ep.id}")
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"season:{show_id}:{season}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"season:{show_id}:{season}:{page + 1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("🔙 Volver", callback_data=f"show:{show_id}")])

    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
    )


# ── Watch / Download ─────────────────────────────────────────────────────────

async def watch_movie(update: Update, context: ContextTypes.DEFAULT_TYPE, movie_id: int):
    query = update.callback_query

    movie = await db.get_movie(movie_id)
    if not movie:
        await query.answer("Película no encontrada.", show_alert=True)
        return

    user_id = query.from_user.id
    is_active, _ = await db.check_subscription(user_id)

    if not is_active:
        await query.answer()
        title = movie.title or "Película"
        caption = (
            f"🎬 *{title}*\n\n"
            f"_Necesitas un plan o ver un anuncio para continuar._"
        )
        buttons = [
            [InlineKeyboardButton("📺 Ver con anuncio", callback_data=f"watch_ad:movie:{movie.id}")],
            [InlineKeyboardButton("💎 Adquirir plan", callback_data="plans:show")],
        ]
        if movie.poster_url:
            try:
                await query.message.delete()
                await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=movie.poster_url,
                    caption=caption,
                    reply_markup=InlineKeyboardMarkup(buttons),
                    parse_mode="Markdown",
                )
                return
            except Exception:
                pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=caption,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )
        return

    await query.answer("📤 Enviando película...")
    try:
        await context.bot.send_video(
            chat_id=query.message.chat_id,
            video=movie.file_id,
            caption=f"🎬 *{movie.title}* ({movie.year or ''})\n\n_TodoCineHD_",
            parse_mode="Markdown",
        )
        await db.log_activity(query.from_user.id, "watch_movie", movie.id, "movie")
    except Exception:
        try:
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=movie.file_id,
                caption=f"🎬 *{movie.title}* ({movie.year or ''})\n\n_TodoCineHD_",
                parse_mode="Markdown",
            )
            await db.log_activity(query.from_user.id, "watch_movie", movie.id, "movie")
        except Exception as e2:
            logger.error("Failed to send movie %s: %s", movie_id, e2)
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="❌ Error al enviar la película. Intenta de nuevo más tarde.",
            )


async def watch_episode(update: Update, context: ContextTypes.DEFAULT_TYPE, episode_id: int):
    query = update.callback_query

    ep = await db.get_episode(episode_id)
    if not ep:
        await query.answer("Episodio no encontrado.", show_alert=True)
        return

    show = await db.get_show(ep.tv_show_id)
    show_name = show.name if show else "Serie"
    ep_title = ep.title or f"Episodio {ep.episode_number}"

    user_id = query.from_user.id
    is_active, _ = await db.check_subscription(user_id)

    if not is_active:
        await query.answer()
        ep_label = f"T{ep.season_number}E{ep.episode_number}: {ep_title}"
        caption = (
            f"📺 *{show_name}*\n{ep_label}\n\n"
            f"_Necesitas un plan o ver un anuncio para continuar._"
        )
        buttons = [
            [InlineKeyboardButton("📺 Ver con anuncio", callback_data=f"watch_ad:ep:{ep.id}")],
            [InlineKeyboardButton("💎 Adquirir plan", callback_data="plans:show")],
        ]
        poster_url = show.poster_url if show else None
        if poster_url:
            try:
                await query.message.delete()
                await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=poster_url,
                    caption=caption,
                    reply_markup=InlineKeyboardMarkup(buttons),
                    parse_mode="Markdown",
                )
                return
            except Exception:
                pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=caption,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )
        return

    await query.answer("📤 Enviando episodio...")
    try:
        await context.bot.send_video(
            chat_id=query.message.chat_id,
            video=ep.file_id,
            caption=(
                f"📺 *{show_name}*\n"
                f"T{ep.season_number}E{ep.episode_number}: {ep_title}\n\n"
                f"_TodoCineHD_"
            ),
            parse_mode="Markdown",
        )
        content_type = "anime" if (show and show.content_type == ContentType.ANIME) else "series"
        await db.log_activity(query.from_user.id, "watch_episode", ep.id, content_type)
    except Exception:
        # Fallback: try sending as document (file_id might be a document)
        try:
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=ep.file_id,
                caption=(
                    f"📺 *{show_name}*\n"
                    f"T{ep.season_number}E{ep.episode_number}: {ep_title}\n\n"
                    f"_TodoCineHD_"
                ),
                parse_mode="Markdown",
            )
            await db.log_activity(query.from_user.id, "watch_episode", ep.id, "series")
        except Exception as e2:
            logger.error("Failed to send episode %s: %s", episode_id, e2)
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="❌ Error al enviar el episodio. Intenta de nuevo más tarde.",
            )


async def download_movie(update: Update, context: ContextTypes.DEFAULT_TYPE, movie_id: int):
    query = update.callback_query

    movie = await db.get_movie(movie_id)
    if not movie:
        await query.answer("Película no encontrada.", show_alert=True)
        return

    await query.answer("💾 Enviando para guardar...")
    try:
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=movie.file_id,
            caption=f"💾 *{movie.title}* ({movie.year or ''})\n\n_Guardado con CineStelar Pro_",
            parse_mode="Markdown",
        )
        await db.log_activity(query.from_user.id, "download_movie", movie.id, "movie")
    except Exception as e:
        logger.error("Failed to send document %s: %s", movie_id, e)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="❌ Error al enviar el archivo. Intenta de nuevo.",
        )


# ── Favorites ─────────────────────────────────────────────────────────────────

async def show_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    favs = await db.get_favorites(query.from_user.id)
    if not favs:
        await query.answer("No tienes favoritos aún.", show_alert=True)
        return

    text = "⭐ *Mis Favoritos*\n\n"
    buttons = []
    for f in favs[:20]:
        if f.content_type == ContentType.MOVIE:
            movie = await db.get_movie(f.content_id)
            if movie:
                text += f"🎬 {movie.title}\n"
                buttons.append([InlineKeyboardButton(
                    f"🎬 {movie.title}", callback_data=f"movie:{movie.id}"
                )])
        else:
            show = await db.get_show(f.content_id)
            if show:
                emoji = "📺" if show.content_type == ContentType.SERIES else "🎌"
                text += f"{emoji} {show.name}\n"
                buttons.append([InlineKeyboardButton(
                    f"{emoji} {show.name}", callback_data=f"show:{show.id}"
                )])

    buttons.append([InlineKeyboardButton("🏠 Menú Principal", callback_data="menu:main")])

    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
    )


async def toggle_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           action: str, content_type_str: str, content_id: int):
    query = update.callback_query
    ct_map = {"movie": ContentType.MOVIE, "series": ContentType.SERIES, "anime": ContentType.ANIME}
    ct = ct_map.get(content_type_str, ContentType.MOVIE)

    if action == "add":
        await db.add_favorite(query.from_user.id, ct, content_id)
        await query.answer("⭐ Agregado a favoritos")
    else:
        await db.remove_favorite(query.from_user.id, ct, content_id)
        await query.answer("❌ Eliminado de favoritos")
