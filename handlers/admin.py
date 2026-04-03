"""Handler: Admin commands – indexing, user management, stats, plan activation."""

import asyncio
import logging
from typing import Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message, Bot
from telegram.ext import ContextTypes

from config.settings import settings
from database import db_manager as db
from database.models import ContentType, PlanType
from utils import tmdb_api
from utils.title_cleaner import clean_title, extract_year, extract_episode_info
from utils.content_classifier import classify

logger = logging.getLogger(__name__)

# Keeps references to background tasks so GC doesn't cancel them
_active_tasks: set[asyncio.Task] = set()


def _bg_task(coro: Any) -> asyncio.Task:
    """Schedule a coroutine as a background task, keeping a strong reference."""
    task = asyncio.create_task(coro)
    _active_tasks.add(task)
    task.add_done_callback(_active_tasks.discard)
    return task


def admin_only(func):
    """Decorator to restrict handler to admins."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if not settings.is_admin(user_id):
            if update.message:
                await update.message.reply_text("🚫 No tienes permisos de administrador.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


# ── Admin Panel ───────────────────────────────────────────────────────────────

async def send_admin_panel(msg_or_query, context: ContextTypes.DEFAULT_TYPE):
    """Build and send the main admin panel with live stats."""
    try:
        total_users = await db.get_total_users()
        active_subs = await db.get_active_subscribers()
        total_movies = await db.get_total_movies()
        total_series = await db.get_total_shows(ContentType.SERIES)
        total_anime = await db.get_total_shows(ContentType.ANIME)
    except Exception:
        total_users = active_subs = total_movies = total_series = total_anime = "?"

    text = (
        "🛠️ *Panel de Administración — TodoCineHD*\n\n"
        "📊 *Estadísticas rápidas*\n"
        f"👥 Usuarios: `{total_users}`\n"
        f"💎 Suscriptores activos: `{active_subs}`\n\n"
        "🎬 *Contenido*\n"
        f"🎬 Películas: `{total_movies}`\n"
        f"📺 Series: `{total_series}`\n"
        f"🎌 Anime: `{total_anime}`\n"
        f"📦 Total: `{sum(x for x in [total_movies, total_series, total_anime] if isinstance(x, int))}`"
    )

    buttons = [
        [
            InlineKeyboardButton("📊 Estadísticas", callback_data="admin:stats"),
            InlineKeyboardButton("👥 Usuarios", callback_data="admin:users"),
        ],
        [
            InlineKeyboardButton("🎬 Administrar Contenido", callback_data="admin:content"),
        ],
        [
            InlineKeyboardButton("📥 Indexar Canal", callback_data="admin:index"),
            InlineKeyboardButton("📢 Broadcast", callback_data="admin:broadcast"),
        ],
        [
            InlineKeyboardButton("💎 Activar Plan", callback_data="admin:activate"),
        ],
    ]
    kb = InlineKeyboardMarkup(buttons)

    # msg_or_query can be a Message or a CallbackQuery
    if hasattr(msg_or_query, "edit_message_text"):
        await msg_or_query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await msg_or_query.reply_text(text, reply_markup=kb, parse_mode="Markdown")


@admin_only
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show admin panel (command version)."""
    msg = update.message
    await send_admin_panel(msg, context)


# ── Stats ─────────────────────────────────────────────────────────────────────

@admin_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_users = await db.get_total_users()
    active_subs = await db.get_active_subscribers()
    total_movies = await db.get_total_movies()
    total_series = await db.get_total_shows(ContentType.SERIES)
    total_anime = await db.get_total_shows(ContentType.ANIME)

    text = (
        "📊 *Estadísticas detalladas*\n\n"
        f"👥 Usuarios totales: `{total_users}`\n"
        f"💎 Suscriptores activos: `{active_subs}`\n\n"
        f"🎬 Películas: `{total_movies}`\n"
        f"📺 Series: `{total_series}`\n"
        f"🎌 Anime: `{total_anime}`\n"
        f"📦 Total contenido: `{total_movies + total_series + total_anime}`\n"
    )
    back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Panel principal", callback_data="admin:home")]])

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=back, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=back, parse_mode="Markdown")


# ── Content management ────────────────────────────────────────────────────────

async def show_content_menu(query, context: ContextTypes.DEFAULT_TYPE):
    """Show content management menu."""
    total_movies = await db.get_total_movies()
    total_series = await db.get_total_shows(ContentType.SERIES)
    total_anime = await db.get_total_shows(ContentType.ANIME)

    text = (
        "🎬 *Administrar Contenido*\n\n"
        f"🎬 Películas: `{total_movies}`\n"
        f"📺 Series: `{total_series}`\n"
        f"🎌 Anime: `{total_anime}`\n\n"
        "Selecciona una categoría para ver y gestionar el contenido:"
    )
    buttons = [
        [InlineKeyboardButton(f"🎬 Películas ({total_movies})", callback_data="admin:content:movies:0")],
        [InlineKeyboardButton(f"📺 Series ({total_series})", callback_data="admin:content:series:0")],
        [InlineKeyboardButton(f"🎌 Anime ({total_anime})", callback_data="admin:content:anime:0")],
        [InlineKeyboardButton("🔙 Panel principal", callback_data="admin:home")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")


async def show_content_list(query, context: ContextTypes.DEFAULT_TYPE, kind: str, page: int):
    """Browse movies/series/anime with delete buttons."""
    PAGE = 6
    if kind == "movies":
        items, total = await db.get_movies_page(page, PAGE)
        emoji = "🎬"
        label = "Películas"
        cat_back = "admin:content"
        def item_cb(m): return f"admin:del:movie:{m.id}"
        def item_label(m): return f"🎬 {m.title} ({m.year or '?'})"
    else:
        ct = ContentType.SERIES if kind == "series" else ContentType.ANIME
        items, total = await db.get_shows_page(ct, page, PAGE)
        emoji = "📺" if kind == "series" else "🎌"
        label = "Series" if kind == "series" else "Anime"
        cat_back = "admin:content"
        def item_cb(s): return f"admin:del:show:{s.id}"
        def item_label(s): return f"{emoji} {s.name} ({s.year or '?'})"

    import math
    total_pages = max(1, math.ceil(total / PAGE))
    page = max(0, min(page, total_pages - 1))

    text = f"{emoji} *{label}* — Página {page + 1}/{total_pages}  ({total} total)\n\nToca un título para borrarlo:"

    buttons = []
    for item in items:
        buttons.append([InlineKeyboardButton(
            f"🗑️ {item_label(item)}",
            callback_data=item_cb(item),
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"admin:content:{kind}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"admin:content:{kind}:{page + 1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("🔙 Volver", callback_data=cat_back)])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")


# ── Activate Plan (admin manually activates for a user) ──────────────────────

@admin_only
async def activate_plan_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt admin for user ID and plan."""
    text = (
        "💎 *Activar Plan de Usuario*\n\n"
        "Envía el comando así:\n"
        "`/activar <user_id> <lite|pro> [días]`\n\n"
        "Ejemplo: `/activar 123456789 pro 30`"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")


@admin_only
async def activate_plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /activar command."""
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Uso: `/activar <user_id> <lite|pro> [días]`",
            parse_mode="Markdown",
        )
        return

    try:
        target_user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ ID de usuario inválido.")
        return

    plan_key = args[1].lower()
    if plan_key not in ("lite", "pro"):
        await update.message.reply_text("❌ Plan debe ser `lite` o `pro`.", parse_mode="Markdown")
        return

    days = 30
    if len(args) > 2:
        try:
            days = int(args[2])
        except ValueError:
            pass

    plan = PlanType.LITE if plan_key == "lite" else PlanType.PRO
    sub = await db.activate_plan(target_user_id, plan, days, payment_ref=f"admin:{update.effective_user.id}")

    plan_label = "💫 Lite" if plan == PlanType.LITE else "👑 Pro"
    await update.message.reply_text(
        f"✅ Plan {plan_label} activado para `{target_user_id}` por {days} días.",
        parse_mode="Markdown",
    )

    # Notify the user
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=(
                f"🎉 *¡Tu plan ha sido activado!*\n\n"
                f"💎 Plan: {plan_label}\n"
                f"📅 Duración: {days} días\n\n"
                f"Disfruta de CineStelar Premium. Escribe /start para comenzar."
            ),
            parse_mode="Markdown",
        )
    except Exception:
        pass


# ── Cancel Plan ───────────────────────────────────────────────────────────────

@admin_only
async def cancel_plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cancelar <user_id>."""
    args = context.args
    if not args:
        await update.message.reply_text("Uso: `/cancelar <user_id>`", parse_mode="Markdown")
        return

    try:
        target_user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ ID de usuario inválido.")
        return

    await db.cancel_plan(target_user_id)
    await update.message.reply_text(f"✅ Plan cancelado para `{target_user_id}`.", parse_mode="Markdown")


# ── Ban/Unban ─────────────────────────────────────────────────────────────────

@admin_only
async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Uso: `/ban <user_id>`", parse_mode="Markdown")
        return
    try:
        uid = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ ID inválido.")
        return
    await db.set_user_banned(uid, True)
    await update.message.reply_text(f"🚫 Usuario `{uid}` baneado.", parse_mode="Markdown")


@admin_only
async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Uso: `/unban <user_id>`", parse_mode="Markdown")
        return
    try:
        uid = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ ID inválido.")
        return
    await db.set_user_banned(uid, False)
    await update.message.reply_text(f"✅ Usuario `{uid}` desbaneado.", parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
#  INDEXER — Scans intake channel and distributes to Movies/Series/Anime
# ══════════════════════════════════════════════════════════════════════════════

@admin_only
async def index_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Scan the intake channel and auto-distribute content."""
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg:
        return

    if update.callback_query:
        await update.callback_query.answer()

    intake_channel = settings.INTAKE_CHANNEL_ID
    if not intake_channel:
        await msg.reply_text("❌ INTAKE_CHANNEL_ID no configurado.")
        return

    last_id = int(await db.get_config("last_indexed_message", "0"))

    status_msg = await msg.reply_text(
        "📥 Indexación iniciada en background...\n"
        "Puedes seguir usando el bot con normalidad."
    )

    # Run the heavy loop in background — handler returns here, webhook sends 200 fast.
    _bg_task(_run_index_loop(context.bot, update.effective_user.id, status_msg, last_id))


async def _run_index_loop(
    bot: Bot, admin_user_id: int, status_msg: Message, last_id: int
) -> None:
    """Scan intake channel and distribute content. Runs as a background task."""
    intake_channel = settings.INTAKE_CHANNEL_ID
    indexed_movies = 0
    indexed_episodes = 0
    errors = 0
    current_msg_id = last_id + 1

    consecutive_not_found = 0
    max_consecutive = 50  # stop after 50 consecutive empty IDs

    while consecutive_not_found < max_consecutive:
        try:
            fwd = await bot.forward_message(
                chat_id=admin_user_id,
                from_chat_id=intake_channel,
                message_id=current_msg_id,
                disable_notification=True,
            )
        except Exception:
            consecutive_not_found += 1
            current_msg_id += 1
            continue

        consecutive_not_found = 0

        # Process the forwarded message
        try:
            file_id = None
            caption = ""

            if fwd.video:
                file_id = fwd.video.file_id
                caption = fwd.caption or fwd.video.file_name or ""
            elif fwd.document:
                mime = fwd.document.mime_type or ""
                if mime.startswith("video/"):
                    file_id = fwd.document.file_id
                    caption = fwd.caption or fwd.document.file_name or ""

            if not file_id:
                try:
                    await fwd.delete()
                except Exception:
                    pass
                current_msg_id += 1
                continue

            try:
                await fwd.delete()
            except Exception:
                pass

            # Classify content
            content_type = await classify(caption)
            clean = clean_title(caption)
            year = extract_year(caption)

            if content_type == ContentType.MOVIE:
                tmdb_data = {}
                if clean:
                    results = await tmdb_api.search_movie(clean, year)
                    if results:
                        tmdb_data = results[0]

                channel_msg_id = None
                try:
                    sent = await bot.send_video(
                        chat_id=settings.MOVIES_CHANNEL_ID,
                        video=file_id,
                        caption=f"🎬 {tmdb_data.get('title', clean)} ({tmdb_data.get('year', year or '')})",
                    )
                    channel_msg_id = sent.message_id
                except Exception as e:
                    logger.error("Failed to distribute movie: %s", e)

                await db.add_movie(
                    file_id=file_id,
                    message_id=current_msg_id,
                    channel_message_id=channel_msg_id,
                    title=tmdb_data.get("title", clean or caption[:100]),
                    original_title=tmdb_data.get("original_title"),
                    year=tmdb_data.get("year", year),
                    overview=tmdb_data.get("overview"),
                    poster_url=tmdb_data.get("poster_url"),
                    backdrop_url=tmdb_data.get("backdrop_url"),
                    vote_average=tmdb_data.get("vote_average"),
                    runtime=tmdb_data.get("runtime"),
                    genres=tmdb_data.get("genres"),
                    tmdb_id=tmdb_data.get("tmdb_id"),
                    raw_caption=caption,
                )
                indexed_movies += 1

            else:
                ep_info = extract_episode_info(caption)
                if not ep_info:
                    ep_info = {"season": 1, "episode": 1}

                show_title = clean
                existing_shows = await db.search_shows(show_title, content_type, limit=1)

                if existing_shows:
                    show = existing_shows[0]
                else:
                    tmdb_data = {}
                    if show_title:
                        tv_results = await tmdb_api.search_tv(show_title)
                        if tv_results:
                            tmdb_data = tv_results[0]
                            if tmdb_data.get("tmdb_id"):
                                is_anime = await tmdb_api.is_anime(tmdb_data["tmdb_id"])
                                if is_anime:
                                    content_type = ContentType.ANIME

                    show = await db.add_tv_show(
                        name=tmdb_data.get("name", show_title or caption[:100]),
                        original_name=tmdb_data.get("original_name"),
                        content_type=content_type,
                        tmdb_id=tmdb_data.get("tmdb_id"),
                        year=tmdb_data.get("year"),
                        overview=tmdb_data.get("overview"),
                        poster_url=tmdb_data.get("poster_url"),
                        backdrop_url=tmdb_data.get("backdrop_url"),
                        vote_average=tmdb_data.get("vote_average"),
                        genres=tmdb_data.get("genres"),
                        number_of_seasons=tmdb_data.get("number_of_seasons"),
                        status=tmdb_data.get("status"),
                    )

                dest_channel = (
                    settings.ANIME_CHANNEL_ID
                    if content_type == ContentType.ANIME
                    else settings.SERIES_CHANNEL_ID
                )
                channel_msg_id = None
                emoji = "🎌" if content_type == ContentType.ANIME else "📺"
                try:
                    sent = await bot.send_video(
                        chat_id=dest_channel,
                        video=file_id,
                        caption=f"{emoji} {show.name} — T{ep_info['season']}E{ep_info['episode']}",
                    )
                    channel_msg_id = sent.message_id
                except Exception as e:
                    logger.error("Failed to distribute episode: %s", e)

                ep_meta = {}
                if show.tmdb_id:
                    ep_meta = await tmdb_api.get_episode_details(
                        show.tmdb_id, ep_info["season"], ep_info["episode"]
                    ) or {}

                await db.add_episode(
                    tv_show_id=show.id,
                    file_id=file_id,
                    message_id=current_msg_id,
                    channel_message_id=channel_msg_id,
                    season_number=ep_info["season"],
                    episode_number=ep_info["episode"],
                    title=ep_meta.get("title"),
                    overview=ep_meta.get("overview"),
                    air_date=ep_meta.get("air_date"),
                    runtime=ep_meta.get("runtime"),
                    still_path=ep_meta.get("still_path"),
                    raw_caption=caption,
                )
                indexed_episodes += 1

        except Exception as e:
            logger.error("Error indexing message %s: %s", current_msg_id, e)
            errors += 1

        current_msg_id += 1

        total = indexed_movies + indexed_episodes
        if total > 0 and total % 10 == 0:
            try:
                await status_msg.edit_text(
                    f"📥 Indexando... {total} procesados\n"
                    f"🎬 Películas: {indexed_movies}\n"
                    f"📺🎌 Series/Anime: {indexed_episodes}\n"
                    f"❌ Errores: {errors}"
                )
            except Exception:
                pass

        await asyncio.sleep(1.5)

    await db.set_config("last_indexed_message", str(current_msg_id - consecutive_not_found - 1))

    try:
        await status_msg.edit_text(
            f"✅ *Indexación completada*\n\n"
            f"🎬 Películas nuevas: {indexed_movies}\n"
            f"📺🎌 Episodios nuevos: {indexed_episodes}\n"
            f"❌ Errores: {errors}\n"
            f"📍 Último mensaje: {current_msg_id - consecutive_not_found - 1}",
            parse_mode="Markdown",
        )
    except Exception:
        pass


# ── Manual index single message ──────────────────────────────────────────────

@admin_only
async def index_manual_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /indexar_manual <message_id> <movie|series|anime>."""
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Uso: `/indexar_manual <message_id> <movie|series|anime>`",
            parse_mode="Markdown",
        )
        return

    try:
        msg_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ ID de mensaje inválido.")
        return

    type_arg = args[1].lower()
    type_map = {"movie": ContentType.MOVIE, "series": ContentType.SERIES, "anime": ContentType.ANIME}
    content_type = type_map.get(type_arg)
    if not content_type:
        await update.message.reply_text("❌ Tipo debe ser `movie`, `series` o `anime`.", parse_mode="Markdown")
        return

    try:
        fwd = await context.bot.forward_message(
            chat_id=update.effective_user.id,
            from_chat_id=settings.INTAKE_CHANNEL_ID,
            message_id=msg_id,
            disable_notification=True,
        )
    except Exception as e:
        await update.message.reply_text(f"❌ No se pudo obtener el mensaje: {e}")
        return

    file_id = None
    caption = ""
    if fwd.video:
        file_id = fwd.video.file_id
        caption = fwd.caption or ""
    elif fwd.document:
        file_id = fwd.document.file_id
        caption = fwd.caption or ""

    try:
        await fwd.delete()
    except Exception:
        pass

    if not file_id:
        await update.message.reply_text("❌ El mensaje no contiene video.")
        return

    clean = clean_title(caption)
    year = extract_year(caption)

    if content_type == ContentType.MOVIE:
        tmdb_data = {}
        if clean:
            results = await tmdb_api.search_movie(clean, year)
            if results:
                tmdb_data = results[0]

        channel_msg_id = None
        try:
            sent = await context.bot.send_video(
                chat_id=settings.MOVIES_CHANNEL_ID,
                video=file_id,
                caption=f"🎬 {tmdb_data.get('title', clean)}",
            )
            channel_msg_id = sent.message_id
        except Exception:
            pass

        await db.add_movie(
            file_id=file_id,
            message_id=msg_id,
            channel_message_id=channel_msg_id,
            title=tmdb_data.get("title", clean or caption[:100]),
            original_title=tmdb_data.get("original_title"),
            year=tmdb_data.get("year", year),
            overview=tmdb_data.get("overview"),
            poster_url=tmdb_data.get("poster_url"),
            backdrop_url=tmdb_data.get("backdrop_url"),
            vote_average=tmdb_data.get("vote_average"),
            runtime=tmdb_data.get("runtime"),
            genres=tmdb_data.get("genres"),
            tmdb_id=tmdb_data.get("tmdb_id"),
            raw_caption=caption,
        )
        await update.message.reply_text(f"✅ Película indexada: {tmdb_data.get('title', clean)}")
    else:
        await update.message.reply_text("✅ Usa `/indexar_serie` para indexar series/anime.", parse_mode="Markdown")


# ── Index Series ──────────────────────────────────────────────────────────────

@admin_only
async def index_series_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /indexar_serie <nombre> <series|anime>.
    
    Usage: /indexar_serie Breaking Bad series
    """
    args = context.args
    if not args:
        await update.message.reply_text(
            "Uso: `/indexar_serie <nombre> [series|anime]`",
            parse_mode="Markdown",
        )
        return

    # Last arg might be content type
    content_type = ContentType.SERIES
    show_name_parts = list(args)

    if show_name_parts[-1].lower() in ("anime", "series"):
        ct_str = show_name_parts.pop().lower()
        content_type = ContentType.ANIME if ct_str == "anime" else ContentType.SERIES

    show_name = " ".join(show_name_parts)

    # Search TMDb
    tv_results = await tmdb_api.search_tv(show_name)
    if not tv_results:
        await update.message.reply_text(f"❌ No se encontró *\"{show_name}\"* en TMDb.", parse_mode="Markdown")
        return

    # Show options
    buttons = []
    for i, r in enumerate(tv_results[:5]):
        star = f"⭐{r.get('vote_average', 0):.1f}"
        label = f"{r['name']} ({r.get('year', '?')}) {star}"
        # Store in context for callback
        context.user_data[f"tmdb_series_{i}"] = r
        context.user_data["series_content_type"] = content_type.value
        buttons.append([InlineKeyboardButton(label, callback_data=f"admin:select_series:{i}")])

    buttons.append([InlineKeyboardButton("❌ Cancelar", callback_data="menu:main")])

    await update.message.reply_text(
        f"🔍 Resultados para *\"{show_name}\"*:\n\nSelecciona la correcta:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def handle_series_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, idx: int):
    """Admin selected a TMDb series result. Create the show record."""
    query = update.callback_query
    await query.answer()

    tmdb_data = context.user_data.get(f"tmdb_series_{idx}")
    if not tmdb_data:
        await query.edit_message_text("❌ Datos expirados. Usa /indexar_serie de nuevo.")
        return

    ct_str = context.user_data.get("series_content_type", "series")
    content_type = ContentType.ANIME if ct_str == "anime" else ContentType.SERIES

    # Check TMDb for anime reclassification
    tmdb_id = tmdb_data.get("tmdb_id")
    if tmdb_id:
        is_anime = await tmdb_api.is_anime(tmdb_id)
        if is_anime:
            content_type = ContentType.ANIME

    show = await db.add_tv_show(
        name=tmdb_data.get("name"),
        original_name=tmdb_data.get("original_name"),
        content_type=content_type,
        tmdb_id=tmdb_id,
        year=tmdb_data.get("year"),
        overview=tmdb_data.get("overview"),
        poster_url=tmdb_data.get("poster_url"),
        backdrop_url=tmdb_data.get("backdrop_url"),
        vote_average=tmdb_data.get("vote_average"),
        genres=tmdb_data.get("genres"),
        number_of_seasons=tmdb_data.get("number_of_seasons"),
        status=tmdb_data.get("status"),
    )

    emoji = "🎌" if content_type == ContentType.ANIME else "📺"
    await query.edit_message_text(
        f"✅ {emoji} *{show.name}* creada en la base de datos.\n\n"
        f"Ahora puedes indexar episodios con:\n"
        f"`/indexar_episodios {show.id} <msg_inicio> <msg_fin>`",
        parse_mode="Markdown",
    )


# ── Index Episodes Range ─────────────────────────────────────────────────────

@admin_only
async def index_episodes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /indexar_episodios <show_id> <msg_start> <msg_end>."""
    args = context.args
    if not args or len(args) < 3:
        await update.message.reply_text(
            "Uso: `/indexar_episodios <show_id> <msg_inicio> <msg_fin>`",
            parse_mode="Markdown",
        )
        return

    try:
        show_id = int(args[0])
        msg_start = int(args[1])
        msg_end = int(args[2])
    except ValueError:
        await update.message.reply_text("❌ Todos los parámetros deben ser números.")
        return

    show = await db.get_show(show_id)
    if not show:
        await update.message.reply_text("❌ Serie no encontrada.")
        return

    status_msg = await update.message.reply_text(f"📥 Indexando episodios de *{show.name}*...", parse_mode="Markdown")

    dest_channel = (
        settings.ANIME_CHANNEL_ID
        if show.content_type == ContentType.ANIME
        else settings.SERIES_CHANNEL_ID
    )
    emoji = "🎌" if show.content_type == ContentType.ANIME else "📺"

    indexed = 0
    errors = 0

    for msg_id in range(msg_start, msg_end + 1):
        try:
            fwd = await context.bot.forward_message(
                chat_id=update.effective_user.id,
                from_chat_id=settings.INTAKE_CHANNEL_ID,
                message_id=msg_id,
                disable_notification=True,
            )
        except Exception:
            continue

        file_id = None
        caption = ""
        if fwd.video:
            file_id = fwd.video.file_id
            caption = fwd.caption or fwd.video.file_name or ""
        elif fwd.document:
            mime = fwd.document.mime_type or ""
            if mime.startswith("video/"):
                file_id = fwd.document.file_id
                caption = fwd.caption or fwd.document.file_name or ""

        try:
            await fwd.delete()
        except Exception:
            pass

        if not file_id:
            continue

        ep_info = extract_episode_info(caption) or {"season": 1, "episode": indexed + 1}

        # Get TMDb episode metadata
        ep_meta = {}
        if show.tmdb_id:
            ep_meta = await tmdb_api.get_episode_details(
                show.tmdb_id, ep_info["season"], ep_info["episode"]
            ) or {}

        # Distribute to channel
        channel_msg_id = None
        try:
            sent = await context.bot.send_video(
                chat_id=dest_channel,
                video=file_id,
                caption=f"{emoji} {show.name} — T{ep_info['season']}E{ep_info['episode']}",
            )
            channel_msg_id = sent.message_id
        except Exception as e:
            logger.error("Distribute episode error: %s", e)

        try:
            await db.add_episode(
                tv_show_id=show.id,
                file_id=file_id,
                message_id=msg_id,
                channel_message_id=channel_msg_id,
                season_number=ep_info["season"],
                episode_number=ep_info["episode"],
                title=ep_meta.get("title"),
                overview=ep_meta.get("overview"),
                air_date=ep_meta.get("air_date"),
                runtime=ep_meta.get("runtime"),
                still_path=ep_meta.get("still_path"),
                raw_caption=caption,
            )
            indexed += 1
        except Exception as e:
            logger.error("DB error indexing episode: %s", e)
            errors += 1

        if indexed % 5 == 0:
            try:
                await status_msg.edit_text(
                    f"📥 Indexando *{show.name}*... {indexed} episodios",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

        await asyncio.sleep(1.5)

    await status_msg.edit_text(
        f"✅ *Indexación de {show.name} completada*\n\n"
        f"{emoji} Episodios: {indexed}\n"
        f"❌ Errores: {errors}",
        parse_mode="Markdown",
    )


# ── Delete movie / show ───────────────────────────────────────────────────────

@admin_only
async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /borrar — show usage or search for content to delete.

    Usage:
      /borrar pelicula <ID o nombre>
      /borrar serie <ID o nombre>
    """
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "🗑️ *Borrar contenido*\n\n"
            "Uso:\n"
            "`/borrar pelicula <ID o nombre>`\n"
            "`/borrar serie <ID o nombre>`\n\n"
            "Ejemplos:\n"
            "`/borrar pelicula 42`\n"
            "`/borrar pelicula Avengers`\n"
            "`/borrar serie 7`\n"
            "`/borrar serie Breaking Bad`",
            parse_mode="Markdown",
        )
        return

    kind = args[0].lower()
    query = " ".join(args[1:])

    if kind not in ("pelicula", "película", "serie", "anime"):
        await update.message.reply_text(
            "❌ Tipo inválido. Usa `pelicula`, `serie` o `anime`.",
            parse_mode="Markdown",
        )
        return

    is_movie = kind in ("pelicula", "película")

    # If query is a number, delete by ID directly
    if query.isdigit():
        item_id = int(query)
        if is_movie:
            movie = await db.get_movie(item_id)
            if not movie:
                await update.message.reply_text(f"❌ No encontré ninguna película con ID `{item_id}`.", parse_mode="Markdown")
                return
            # Confirm step via inline button
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"🗑️ Confirmar: borrar «{movie.title}»", callback_data=f"admin:del:movie:{item_id}"),
                InlineKeyboardButton("❌ Cancelar", callback_data="admin:del:cancel"),
            ]])
            await update.message.reply_text(
                f"¿Confirmar borrado de la película *{movie.title}* (ID {item_id})?",
                reply_markup=kb, parse_mode="Markdown",
            )
        else:
            show = await db.get_show(item_id)
            if not show:
                await update.message.reply_text(f"❌ No encontré ninguna serie con ID `{item_id}`.", parse_mode="Markdown")
                return
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"🗑️ Confirmar: borrar «{show.name}»", callback_data=f"admin:del:show:{item_id}"),
                InlineKeyboardButton("❌ Cancelar", callback_data="admin:del:cancel"),
            ]])
            await update.message.reply_text(
                f"¿Confirmar borrado de *{show.name}* y todos sus episodios (ID {item_id})?",
                reply_markup=kb, parse_mode="Markdown",
            )
        return

    # Search by name and show results
    if is_movie:
        results = await db.search_movies(query)
        if not results:
            await update.message.reply_text(f"❌ No encontré películas con ese nombre.")
            return
        buttons = [
            [InlineKeyboardButton(
                f"🎬 {m.title} ({m.year or '?'}) — ID {m.id}",
                callback_data=f"admin:del:movie:{m.id}",
            )]
            for m in results[:8]
        ]
        buttons.append([InlineKeyboardButton("❌ Cancelar", callback_data="admin:del:cancel")])
        await update.message.reply_text(
            "Selecciona la película a borrar:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    else:
        ct = ContentType.ANIME if kind == "anime" else ContentType.SERIES
        results = await db.search_shows(query, limit=8)
        if not results:
            await update.message.reply_text(f"❌ No encontré series con ese nombre.")
            return
        buttons = [
            [InlineKeyboardButton(
                f"{'🎌' if s.content_type == ContentType.ANIME else '📺'} {s.name} ({s.year or '?'}) — ID {s.id}",
                callback_data=f"admin:del:show:{s.id}",
            )]
            for s in results
        ]
        buttons.append([InlineKeyboardButton("❌ Cancelar", callback_data="admin:del:cancel")])
        await update.message.reply_text(
            "Selecciona la serie a borrar:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def handle_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, parts: list[str]):
    """Handle admin:del:movie/show:<id> and admin:del:cancel callbacks."""
    query = update.callback_query

    if parts[0] == "cancel":
        await query.edit_message_text(
            "❌ Borrado cancelado.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Administrar Contenido", callback_data="admin:content")
            ]]),
        )
        return

    if len(parts) < 2:
        await query.edit_message_text("❌ Datos inválidos.")
        return

    kind, item_id_str = parts[0], parts[1]
    try:
        item_id = int(item_id_str)
    except ValueError:
        await query.edit_message_text("❌ ID inválido.")
        return

    back_kind = "movies" if kind == "movie" else "series"
    back_btn = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Volver al listado", callback_data=f"admin:content:{back_kind}:0"),
        InlineKeyboardButton("🏠 Panel", callback_data="admin:home"),
    ]])

    if kind == "movie":
        movie = await db.get_movie(item_id)
        name = movie.title if movie else str(item_id)
        deleted = await db.delete_movie(item_id)
        if deleted:
            await query.edit_message_text(f"✅ Película *{name}* borrada.", reply_markup=back_btn, parse_mode="Markdown")
        else:
            await query.edit_message_text(f"❌ No se encontró la película con ID {item_id}.", reply_markup=back_btn)
    elif kind == "show":
        show = await db.get_show(item_id)
        name = show.name if show else str(item_id)
        back_kind = "anime" if (show and show.content_type == ContentType.ANIME) else "series"
        back_btn = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Volver al listado", callback_data=f"admin:content:{back_kind}:0"),
            InlineKeyboardButton("🏠 Panel", callback_data="admin:home"),
        ]])
        deleted = await db.delete_show(item_id)
        if deleted:
            await query.edit_message_text(f"✅ *{name}* y todos sus episodios borrados.", reply_markup=back_btn, parse_mode="Markdown")
        else:
            await query.edit_message_text(f"❌ No se encontró la serie con ID {item_id}.", reply_markup=back_btn)
    else:
        await query.edit_message_text("❌ Tipo desconocido.")


