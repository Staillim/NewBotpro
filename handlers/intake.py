"""Handler: Real-time intake channel processing.

Flow:
  - Video/doc sent to intake channel → auto-indexed as movie
  - "serie: Nombre"                  → opens a series session
  - "anime: Nombre"                  → opens an anime session
  - videos sent while session open   → indexed as episodes (auto S01E01, E02…)
  - "final"                          → closes session, reports count

Architecture:
  ALL intake channel posts (commands + videos) go through a single serial
  PriorityQueue keyed by message_id.  The worker processes them one at a time,
  in the exact order they were sent.  This eliminates every race condition that
  arises from concurrent_updates=True.
"""

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import RetryAfter, TimedOut
from telegram.ext import ContextTypes

from config.settings import settings
from database import db_manager as db
from database.models import ContentType
from utils import tmdb_api
from utils.title_cleaner import clean_title, extract_episode_info, extract_year

logger = logging.getLogger(__name__)

# ── Session state (one active show at a time per process) ─────────────────────
_active_session: dict | None = None

# ── Single serial queue for ALL intake channel posts ──────────────────────────
# Items: (message_id, update, context)  — processed one at a time in order.
_intake_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
_intake_worker_task: asyncio.Task | None = None

# Delay between consecutive channel sends (flood-limit protection)
_SEND_DELAY = 2.0

# ── Pending-movie state (TMDB not found) ──────────────────────────────────────
# keyed by intake message_id
_pending_movies: dict[int, dict] = {}
# admin_chat_id → pending movie msg_id (waiting for admin to type a new name)
_awaiting_rename: dict[int, int] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_file_id(msg: Message) -> str | None:
    """Return file_id if the message contains a video or video-document."""
    if msg.video:
        return msg.video.file_id
    if msg.document and (msg.document.mime_type or "").startswith("video/"):
        return msg.document.file_id
    return None


async def _notify(context, text: str) -> None:
    """Send a status message to the first admin's private chat."""
    if not settings.ADMIN_IDS:
        logger.warning("_notify: no ADMIN_IDS configured")
        return
    try:
        await context.bot.send_message(
            chat_id=settings.ADMIN_IDS[0],
            text=text,
            parse_mode="Markdown",
        )
    except Exception:
        # Markdown parse failed — retry as plain text
        try:
            await context.bot.send_message(
                chat_id=settings.ADMIN_IDS[0],
                text=text.replace("*", "").replace("`", "").replace("_", " "),
            )
        except Exception as exc2:
            logger.warning("Admin notification failed: %s", exc2)


async def _notify_groups(context, title: str, year: str | None,
                         poster_url: str | None, deeplink: str,
                         emoji: str = "🎬") -> None:
    """Send new-content notification to every registered group."""
    groups = await db.get_active_groups()
    if not groups:
        return

    year_str = f" ({year})" if year else ""
    text = f"{emoji} *¡Nuevo contenido disponible!*\n\n*{title}{year_str}*\n\n👉 Ver ahora"
    url = f"https://t.me/{settings.BOT_USERNAME}?start={deeplink}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"{emoji} Ver ahora", url=url)]])

    for chat_id in groups:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=kb,
                parse_mode="Markdown",
            )
            await asyncio.sleep(0.3)  # small delay between groups
        except RetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
        except Exception as exc:
            logger.warning("Group notify failed for %s: %s", chat_id, exc)


# ── Intake worker ─────────────────────────────────────────────────────────────

def _ensure_intake_worker(context) -> None:
    global _intake_worker_task
    if _intake_worker_task is None or _intake_worker_task.done():
        _intake_worker_task = asyncio.create_task(_intake_worker())


async def _intake_worker() -> None:
    """Drain _intake_queue serially.  One item at a time, in message_id order."""
    while True:
        _msg_id, update, context = await _intake_queue.get()
        try:
            await _process_intake_post(update, context)
        except Exception as exc:
            logger.error("Intake worker unhandled error: %s", exc, exc_info=True)
        finally:
            _intake_queue.task_done()


# ── Main channel-post handler ─────────────────────────────────────────────────

async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point: enqueue every intake channel post for serial processing."""
    post = update.channel_post
    if not post:
        return
    if post.chat.id != settings.INTAKE_CHANNEL_ID:
        return
    _ensure_intake_worker(context)
    await _intake_queue.put((post.message_id, update, context))


async def _process_intake_post(update: Update, context) -> None:
    """Process one intake post — always runs inside the serial worker."""
    global _active_session

    post = update.channel_post
    text = (post.text or post.caption or "").strip()
    tl = text.lower()

    # ── serie: NAME ───────────────────────────────────────────────────────────
    if tl.startswith("serie:"):
        name = text[6:].strip()
        if not name:
            await _notify(context, "❌ Falta el nombre.\nEjemplo: `serie: Breaking Bad`")
            return
        await _start_show_session(name, ContentType.SERIES, context)
        return

    # ── anime: NAME ───────────────────────────────────────────────────────────
    if tl.startswith("anime:"):
        name = text[6:].strip()
        if not name:
            await _notify(context, "❌ Falta el nombre.\nEjemplo: `anime: Naruto`")
            return
        await _start_show_session(name, ContentType.ANIME, context)
        return

    # ── final ─────────────────────────────────────────────────────────────────
    if tl == "final":
        if not _active_session:
            await _notify(context, "⚠️ No hay ninguna sesión activa.")
            return
        session = _active_session
        _active_session = None
        show = session["show"]
        count = session["episode_count"]
        emoji = "🎌" if show.content_type == ContentType.ANIME else "📺"
        await _notify(
            context,
            f"✅ *Sesión finalizada*\n\n"
            f"{emoji} *{show.name}*\n"
            f"📦 {count} episodio(s) indexado(s)",
        )
        if count > 0:
            await db.publish_show(show.id)
            content_type_str = "anime" if show.content_type == ContentType.ANIME else "series"
            await _notify_groups(
                context,
                title=show.name,
                year=show.year,
                poster_url=show.poster_url,
                deeplink=f"watch_{content_type_str}_{show.id}",
                emoji=emoji,
            )
        return

    # ── Video file ────────────────────────────────────────────────────────────
    file_id = _extract_file_id(post)
    if not file_id:
        return

    if _active_session:
        await _do_add_episode(file_id, post, context)
        await asyncio.sleep(1.0)  # pace episode sends
    else:
        await _do_index_movie(file_id, post, context)
        await asyncio.sleep(_SEND_DELAY)  # pace movie sends


# ── Session management ────────────────────────────────────────────────────────


async def _start_show_session(
    name: str, content_type: ContentType, context
) -> None:
    """Look up or create the show and open a new indexing session."""
    global _active_session

    # Save previous session — only close it AFTER the new one succeeds
    previous_session = _active_session

    emoji = "🎌" if content_type == ContentType.ANIME else "📺"
    await _notify(context, f"🔍 Buscando *{name}* en base de datos y TMDB…")
    try:
        await _do_start_show_session(name, content_type, emoji, context)
    except Exception as exc:
        logger.error("_start_show_session error: %s", exc, exc_info=True)
        # Restore the previous session so episodes keep going there
        _active_session = previous_session
        await _notify(context, f"❌ Error al crear la sesión para '{name}': {type(exc).__name__}")
        return

    # New session created successfully — now close the old one if it existed
    if previous_session:
        show = previous_session["show"]
        count = previous_session["episode_count"]
        e = "🎌" if show.content_type == ContentType.ANIME else "📺"
        await _notify(
            context,
            f"⚠️ Sesión anterior cerrada automáticamente.\n"
            f"{e} *{show.name}* — {count} episodio(s) indexado(s).",
        )
        if count > 0:
            await db.publish_show(show.id)
            ct_str = "anime" if show.content_type == ContentType.ANIME else "series"
            await _notify_groups(
                context,
                title=show.name,
                year=show.year,
                poster_url=show.poster_url,
                deeplink=f"watch_{ct_str}_{show.id}",
                emoji=e,
            )


async def _do_start_show_session(
    name: str, content_type: ContentType, emoji: str, context
) -> None:
    global _active_session
    existing = await db.search_shows(name, content_type, limit=1, published_only=False)
    if existing:
        show = existing[0]
        # Verify the show actually still exists in DB (extra safety check)
        confirmed = await db.get_show(show.id)
        if not confirmed:
            existing = []  # Treat as not found
    if existing:
        show = existing[0]
        # If the show is missing key metadata, try to enrich it from TMDB now
        needs_enrich = not show.poster_url or not show.overview or not show.tmdb_id
        if needs_enrich:
            try:
                tmdb_results = await tmdb_api.search_tv(name)
                if tmdb_results:
                    td = tmdb_results[0]
                    updates = {}
                    if not show.tmdb_id and td.get("tmdb_id"):
                        updates["tmdb_id"] = td["tmdb_id"]
                    if not show.poster_url and td.get("poster_url"):
                        updates["poster_url"] = td["poster_url"]
                    if not show.backdrop_url and td.get("backdrop_url"):
                        updates["backdrop_url"] = td["backdrop_url"]
                    if not show.overview and td.get("overview"):
                        updates["overview"] = td["overview"]
                    if not show.vote_average and td.get("vote_average"):
                        updates["vote_average"] = td["vote_average"]
                    if not show.genres and td.get("genres"):
                        updates["genres"] = td["genres"]
                    if not show.year and td.get("year"):
                        updates["year"] = td["year"]
                    if not show.number_of_seasons and td.get("number_of_seasons"):
                        updates["number_of_seasons"] = td["number_of_seasons"]
                    if not show.status and td.get("status"):
                        updates["status"] = td["status"]
                    if updates:
                        await db.update_show_metadata(show.id, **updates)
                        # Refresh local object so the session uses updated data
                        for k, v in updates.items():
                            setattr(show, k, v)
                        logger.info("Enriched show %s with TMDB data: %s", show.name, list(updates))
            except Exception as exc:
                logger.warning("TMDB enrich failed for '%s': %s", name, exc)
        await _notify(
            context,
            f"{emoji} *{show.name}* encontrada en DB.\n"
            f"Envía los episodios y escribe `final` cuando termines.",
        )
    else:
        # Search TMDB
        tmdb_data: dict = {}
        try:
            results = await tmdb_api.search_tv(name)
            if results:
                tmdb_data = results[0]
                if content_type == ContentType.ANIME and tmdb_data.get("tmdb_id"):
                    is_anime = await tmdb_api.is_anime(tmdb_data["tmdb_id"])
                    if is_anime:
                        content_type = ContentType.ANIME
        except Exception as exc:
            logger.warning("TMDB search failed for '%s': %s", name, exc)

        show = await db.add_tv_show(
            name=tmdb_data.get("name", name),
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
        await _notify(
            context,
            f"{emoji} *{show.name}* creada correctamente.\n"
            f"Envía los episodios y escribe `final` cuando termines.",
        )

    _active_session = {
        "show": show,
        "episode_count": 0,
        "next_episode": 1,
        "season": 1,
    }

    # If show already has episodes, continue numbering from where it left off
    last_ep = await db.get_last_episode_number(show.id, season=1)
    if last_ep > 0:
        _active_session["next_episode"] = last_ep + 1
        await _notify(
            context,
            f"📌 Continuando desde el episodio {last_ep + 1}.",
        )


# ── Movie auto-index ──────────────────────────────────────────────────────────


async def _do_add_episode(file_id: str, post: Message, context) -> None:
    """Internal: assign episode number, send to channel, and save to DB."""
    global _active_session
    if not _active_session:
        return

    session = _active_session
    show = session["show"]
    caption = (post.caption or "").strip()

    # Always auto-increment during intake sessions
    ep_info = {
        "season": session["season"],
        "episode": session["next_episode"],
    }

    dest_channel = (
        settings.ANIME_CHANNEL_ID
        if show.content_type == ContentType.ANIME
        else settings.SERIES_CHANNEL_ID
    )
    emoji = "🎌" if show.content_type == ContentType.ANIME else "📺"

    channel_msg_id = None
    dist_caption = caption if caption else f"{emoji} {show.name} — T{ep_info['season']:02d}E{ep_info['episode']:02d}"

    for attempt in range(3):
        try:
            sent = await context.bot.send_video(
                chat_id=dest_channel,
                video=file_id,
                caption=dist_caption,
            )
            channel_msg_id = sent.message_id
            break
        except RetryAfter as exc:
            wait = exc.retry_after + 1
            logger.warning("Flood control (episode): retrying in %ss", wait)
            await asyncio.sleep(wait)
        except TimedOut:
            logger.warning("TimedOut sending episode to channel (attempt %d)", attempt + 1)
            await asyncio.sleep(5)
        except Exception as exc:
            logger.error("Failed to distribute episode: %s", exc)
            break

    # Use the original caption as episode title so users see exactly
    # what was sent, regardless of file naming format.
    ep_title = caption if caption else None

    # Fetch extra metadata from TMDB (best-effort, title NOT overridden)
    ep_meta: dict = {}
    if show.tmdb_id:
        try:
            ep_meta = await tmdb_api.get_episode_details(
                show.tmdb_id, ep_info["season"], ep_info["episode"]
            ) or {}
        except Exception:
            pass

    await db.add_episode(
        tv_show_id=show.id,
        file_id=file_id,
        message_id=post.message_id,
        channel_message_id=channel_msg_id,
        season_number=ep_info["season"],
        episode_number=ep_info["episode"],
        title=ep_title,
        overview=ep_meta.get("overview"),
        air_date=ep_meta.get("air_date"),
        runtime=ep_meta.get("runtime"),
        still_path=ep_meta.get("still_path"),
        raw_caption=caption,
    )

    session["episode_count"] += 1
    session["next_episode"] = ep_info["episode"] + 1

    logger.info(
        "Episode indexed: %s S%02dE%02d (msg_id=%s)",
        show.name, ep_info["season"], ep_info["episode"], post.message_id,
    )


# ── Movie auto-index ──────────────────────────────────────────────────────────

async def _publish_movie(
    file_id: str,
    orig_msg_id: int,
    caption: str,
    tmdb_data: dict,
    fallback_title: str,
    year: str | None,
    context,
) -> None:
    """Send video to distribution channel and persist to DB."""
    title = tmdb_data.get("title", fallback_title or "Sin título")
    year_val = tmdb_data.get("year", year)

    # Check first if this file_id or tmdb_id is already in DB
    # to avoid re-distributing the video if admin re-forwards
    tmdb_id_check = tmdb_data.get("tmdb_id")
    pre_existing = None
    if tmdb_id_check:
        pre_existing = await db.get_movie_by_tmdb(tmdb_id_check)
    if pre_existing is None:
        pre_existing = await db.get_movie_by_file(file_id)

    if pre_existing:
        await _notify(context, f"♻️ *{title}* ya estaba indexada, omitiendo reenvío al canal.")
        logger.info("Movie already indexed (id=%s), skipping channel send: %s", pre_existing.id, title)
        return

    channel_msg_id = None
    caption_text = f"🎬 {title} ({year_val})" if year_val else f"🎬 {title}"

    for attempt in range(3):
        try:
            sent = await context.bot.send_video(
                chat_id=settings.MOVIES_CHANNEL_ID,
                video=file_id,
                caption=caption_text,
            )
            channel_msg_id = sent.message_id
            break
        except RetryAfter as exc:
            wait = exc.retry_after + 1
            logger.warning("Flood control: retrying in %ss (attempt %d)", wait, attempt + 1)
            await asyncio.sleep(wait)
        except TimedOut:
            logger.warning("TimedOut sending movie to channel (attempt %d)", attempt + 1)
            await asyncio.sleep(5)
        except Exception as exc:
            logger.error("Failed to distribute movie '%s': %s", title, exc)
            break

    movie, created = await db.add_movie(
        file_id=file_id,
        message_id=orig_msg_id,
        channel_message_id=channel_msg_id,
        title=title,
        original_title=tmdb_data.get("original_title"),
        year=year_val,
        overview=tmdb_data.get("overview"),
        poster_url=tmdb_data.get("poster_url"),
        backdrop_url=tmdb_data.get("backdrop_url"),
        vote_average=tmdb_data.get("vote_average"),
        runtime=tmdb_data.get("runtime"),
        genres=tmdb_data.get("genres"),
        tmdb_id=tmdb_data.get("tmdb_id"),
        raw_caption=caption,
    )

    await _notify(context, f"✅ *{title}* {'(' + str(year_val) + ')' if year_val else ''} indexada.")
    logger.info("Movie indexed: %s (msg_id=%s)", title, orig_msg_id)

    if created and movie:
        await _notify_groups(
            context,
            title=movie.title,
            year=movie.year,
            poster_url=movie.poster_url,
            deeplink=f"watch_movie_{movie.id}",
            emoji="🎬",
        )


async def _do_index_movie(file_id: str, post: Message, context) -> None:
    """Internal: perform TMDB lookup, channel send, and DB insert for one movie."""
    caption = (post.caption or "").strip()
    clean = clean_title(caption)
    year = extract_year(caption)

    # Search TMDB (small delay avoids hammering the API back-to-back)
    tmdb_data: dict = {}
    tmdb_request_failed = False
    if clean:
        try:
            results = await tmdb_api.search_movie(clean, year)
            if results:
                tmdb_data = results[0]
        except RuntimeError as exc:
            # Network/timeout failure — index without metadata, don't send to pending
            logger.warning("TMDB unavailable for '%s': %s", clean, exc)
            tmdb_request_failed = True
        except Exception as exc:
            logger.warning("TMDB movie search failed for '%s': %s", clean, exc)
            tmdb_request_failed = True

    # ── Not found in TMDB → ask admin to skip or retry with another name ──────
    # Only enter this flow when TMDB responded but returned no results
    if not tmdb_data and not tmdb_request_failed:
        msg_key = post.message_id
        _pending_movies[msg_key] = {
            "file_id": file_id,
            "msg_id": msg_key,
            "caption": caption,
            "clean": clean,
            "year": year,
        }
        searched = clean or caption[:80] or "(sin título)"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⏭️ Omitir", callback_data=f"skip_movie:{msg_key}"),
            InlineKeyboardButton("🔍 Otro nombre", callback_data=f"rename_movie:{msg_key}"),
        ]])
        if settings.ADMIN_IDS:
            await context.bot.send_message(
                chat_id=settings.ADMIN_IDS[0],
                text=(
                    f"❓ *Película no encontrada en TMDB*\n\n"
                    f"Título buscado: `{searched}`\n\n"
                    f"¿Qué hacemos con este video?"
                ),
                parse_mode="Markdown",
                reply_markup=kb,
            )
        return

    await _publish_movie(
        file_id, post.message_id, caption, tmdb_data,
        clean or caption[:100], year, context,
    )


# ── Indexing-error public handlers ───────────────────────────────────────────

async def handle_intake_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle admin inline-keyboard decisions on unresolved movies (skip / rename)."""
    query = update.callback_query
    await query.answer()

    parts = (query.data or "").split(":", 1)
    if len(parts) != 2:
        return
    action, msg_id_str = parts
    try:
        msg_id = int(msg_id_str)
    except ValueError:
        return

    if action == "skip_movie":
        _pending_movies.pop(msg_id, None)
        await query.edit_message_text("⏭️ Película omitida.")
        return

    if action == "rename_movie":
        if msg_id not in _pending_movies:
            await query.edit_message_text("⚠️ Esta película ya fue procesada.")
            return
        admin_id = query.from_user.id
        _awaiting_rename[admin_id] = msg_id
        await query.edit_message_text(
            "✏️ Escribe el nombre con el que quieres buscar la película en TMDB:"
        )


async def handle_admin_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Intercept admin private messages when they are providing a new search name.
    Returns True if message was consumed (rename flow), False otherwise.
    """
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or user_id not in settings.ADMIN_IDS:
        return False
    if user_id not in _awaiting_rename:
        return False

    msg_id = _awaiting_rename.pop(user_id)
    pending = _pending_movies.pop(msg_id, None)
    if not pending:
        await update.message.reply_text("⚠️ La película ya fue procesada o fue omitida.")
        return True

    new_name = update.message.text.strip()
    await update.message.reply_text(f"🔍 Buscando *{new_name}* en TMDB…", parse_mode="Markdown")

    tmdb_data: dict = {}
    try:
        results = await tmdb_api.search_movie(new_name, pending["year"])
        if results:
            tmdb_data = results[0]
    except Exception as exc:
        logger.warning("TMDB rename search failed for '%s': %s", new_name, exc)

    if not tmdb_data:
        await update.message.reply_text(
            f"⚠️ *{new_name}* tampoco fue encontrada en TMDB.\n"
            f"Indexando con ese nombre sin metadata.",
            parse_mode="Markdown",
        )

    await _publish_movie(
        pending["file_id"],
        pending["msg_id"],
        pending["caption"],
        tmdb_data,
        new_name,
        pending["year"],
        context,
    )
    return True
