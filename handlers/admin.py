"""Handler: Admin commands – indexing, user management, stats, plan activation."""

import asyncio
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config.settings import settings
from database import db_manager as db
from database.models import ContentType, PlanType
from utils import tmdb_api
from utils.title_cleaner import clean_title, extract_year, extract_episode_info
from utils.content_classifier import classify

logger = logging.getLogger(__name__)


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


# ── Admin Menu ────────────────────────────────────────────────────────────────

@admin_only
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show admin panel."""
    buttons = [
        [
            InlineKeyboardButton("📥 Indexar Canal", callback_data="admin:index"),
            InlineKeyboardButton("📊 Estadísticas", callback_data="admin:stats"),
        ],
        [
            InlineKeyboardButton("👥 Usuarios", callback_data="admin:users"),
            InlineKeyboardButton("💎 Activar Plan", callback_data="admin:activate"),
        ],
        [
            InlineKeyboardButton("📢 Broadcast", callback_data="admin:broadcast"),
        ],
    ]
    await update.message.reply_text(
        "🛠️ *Panel de Administración*",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


# ── Stats ─────────────────────────────────────────────────────────────────────

@admin_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_users = await db.get_total_users()
    active_subs = await db.get_active_subscribers()
    total_movies = await db.get_total_movies()
    total_series = await db.get_total_shows(ContentType.SERIES)
    total_anime = await db.get_total_shows(ContentType.ANIME)

    text = (
        "📊 *Estadísticas del Bot*\n\n"
        f"👥 Usuarios totales: {total_users}\n"
        f"💎 Suscriptores activos: {active_subs}\n\n"
        f"🎬 Películas: {total_movies}\n"
        f"📺 Series: {total_series}\n"
        f"🎌 Anime: {total_anime}\n"
        f"📦 Total contenido: {total_movies + total_series + total_anime}\n"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")


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

    status_msg = await msg.reply_text("📥 Iniciando indexación del canal de intake...")

    last_indexed = await db.get_config("last_indexed_message", "0")
    last_id = int(last_indexed)

    intake_channel = settings.INTAKE_CHANNEL_ID
    if not intake_channel:
        await status_msg.edit_text("❌ INTAKE_CHANNEL_ID no configurado.")
        return

    indexed_movies = 0
    indexed_episodes = 0
    errors = 0
    current_msg_id = last_id + 1

    # We'll scan forward from last indexed message
    consecutive_not_found = 0
    max_consecutive = 50  # stop after 50 consecutive empty IDs

    while consecutive_not_found < max_consecutive:
        try:
            fwd = await context.bot.forward_message(
                chat_id=update.effective_user.id,
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
                # Delete the forwarded non-video message
                try:
                    await fwd.delete()
                except Exception:
                    pass
                current_msg_id += 1
                continue

            # Delete forwarded message from admin chat
            try:
                await fwd.delete()
            except Exception:
                pass

            # Classify content
            content_type = await classify(caption)
            clean = clean_title(caption)
            year = extract_year(caption)

            if content_type == ContentType.MOVIE:
                # Search TMDb for metadata
                tmdb_data = {}
                if clean:
                    results = await tmdb_api.search_movie(clean, year)
                    if results:
                        tmdb_data = results[0]

                # Distribute to movies channel
                channel_msg_id = None
                try:
                    sent = await context.bot.send_video(
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
                # Series or Anime – need show context
                ep_info = extract_episode_info(caption)
                if not ep_info:
                    ep_info = {"season": 1, "episode": 1}

                # Try to find or create the show
                show_title = clean
                existing_shows = await db.search_shows(show_title, content_type, limit=1)

                if existing_shows:
                    show = existing_shows[0]
                else:
                    # Search TMDb for series info
                    tmdb_data = {}
                    if show_title:
                        tv_results = await tmdb_api.search_tv(show_title)
                        if tv_results:
                            tmdb_data = tv_results[0]
                            # Recheck anime classification with TMDb
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

                # Distribute to appropriate channel
                dest_channel = (
                    settings.ANIME_CHANNEL_ID
                    if content_type == ContentType.ANIME
                    else settings.SERIES_CHANNEL_ID
                )
                channel_msg_id = None
                emoji = "🎌" if content_type == ContentType.ANIME else "📺"
                try:
                    sent = await context.bot.send_video(
                        chat_id=dest_channel,
                        video=file_id,
                        caption=f"{emoji} {show.name} — T{ep_info['season']}E{ep_info['episode']}",
                    )
                    channel_msg_id = sent.message_id
                except Exception as e:
                    logger.error("Failed to distribute episode: %s", e)

                # Get episode metadata from TMDb
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

        # Update progress every 10 items
        total = indexed_movies + indexed_episodes
        if total > 0 and total % 10 == 0:
            try:
                await status_msg.edit_text(
                    f"📥 Indexando... {total} procesados\n"
                    f"🎬 Películas: {indexed_movies}\n"
                    f"📺📌 Series/Anime: {indexed_episodes}\n"
                    f"❌ Errores: {errors}"
                )
            except Exception:
                pass

        # Rate limiting
        await asyncio.sleep(1.5)

    # Save last indexed
    await db.set_config("last_indexed_message", str(current_msg_id - consecutive_not_found - 1))

    await status_msg.edit_text(
        f"✅ *Indexación completada*\n\n"
        f"🎬 Películas nuevas: {indexed_movies}\n"
        f"📺🎌 Episodios nuevos: {indexed_episodes}\n"
        f"❌ Errores: {errors}\n"
        f"📍 Último mensaje: {current_msg_id - consecutive_not_found - 1}",
        parse_mode="Markdown",
    )


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
