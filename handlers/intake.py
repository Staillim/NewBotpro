"""Handler: Real-time intake channel processing.

Flow:
  - Video/doc sent to intake channel → auto-indexed as movie
  - "serie: Nombre"                  → opens a series session
  - "anime: Nombre"                  → opens an anime session
  - videos sent while session open   → indexed as episodes (auto S01E01, E02…)
  - "final"                          → closes session, reports count
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

# Semaphore: process max 1 movie at a time to avoid Telegram/TMDB flood
_index_lock = asyncio.Semaphore(1)

# Delay between consecutive sends to the distribution channel (seconds)
_SEND_DELAY = 2.0


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
    except Exception as exc:
        logger.warning("Admin notification failed: %s", exc)


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
            if poster_url:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=poster_url,
                    caption=text,
                    reply_markup=kb,
                    parse_mode="Markdown",
                )
            else:
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


# ── Main channel-post handler ─────────────────────────────────────────────────

async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point for every post in the intake channel."""
    global _active_session

    post = update.channel_post
    if not post:
        return

    # Only react to the configured intake channel
    if post.chat.id != settings.INTAKE_CHANNEL_ID:
        return

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
        # Publish and notify groups only when episodes were added
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
        return  # text not matching any command — ignore

    if _active_session:
        await _add_episode(file_id, post, context)
    else:
        await _index_movie(file_id, post, context)


# ── Session management ────────────────────────────────────────────────────────

async def _auto_close_session(context) -> None:
    """Close the active session silently (used when a new session starts without `final`)."""
    global _active_session
    if not _active_session:
        return
    session = _active_session
    _active_session = None
    show = session["show"]
    count = session["episode_count"]
    emoji = "🎌" if show.content_type == ContentType.ANIME else "📺"
    await _notify(
        context,
        f"⚠️ Sesión anterior cerrada automáticamente.\n"
        f"{emoji} *{show.name}* — {count} episodio(s) indexado(s).",
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


async def _start_show_session(
    name: str, content_type: ContentType, context
) -> None:
    """Look up or create the show and open a new indexing session."""
    global _active_session

    # Auto-close any lingering session before starting a new one
    if _active_session:
        await _auto_close_session(context)

    emoji = "🎌" if content_type == ContentType.ANIME else "📺"
    await _notify(context, f"🔍 Buscando *{name}* en base de datos y TMDB…")
    try:
        await _do_start_show_session(name, content_type, emoji, context)
    except Exception as exc:
        logger.error("_start_show_session error: %s", exc, exc_info=True)
        await _notify(context, f"❌ Error al crear la sesión: {exc}")


async def _do_start_show_session(
    name: str, content_type: ContentType, emoji: str, context
) -> None:
    global _active_session
    existing = await db.search_shows(name, content_type, limit=1, published_only=False)
    if existing:
        show = existing[0]
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


async def _add_episode(file_id: str, post: Message, context) -> None:
    """Index a video as the next episode in the active session — serialized to keep order."""
    async with _index_lock:
        await _do_add_episode(file_id, post, context)
        await asyncio.sleep(1.0)  # brief pause to keep channel order


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

async def _index_movie(file_id: str, post: Message, context) -> None:
    """Auto-index a video as a movie — serialized to avoid flood limits."""
    async with _index_lock:
        await _do_index_movie(file_id, post, context)
        # Pause between consecutive movies to respect Telegram's rate limit
        await asyncio.sleep(_SEND_DELAY)


async def _do_index_movie(file_id: str, post: Message, context) -> None:
    """Internal: perform TMDB lookup, channel send, and DB insert for one movie."""
    caption = (post.caption or "").strip()
    clean = clean_title(caption)
    year = extract_year(caption)

    # Search TMDB (small delay avoids hammering the API back-to-back)
    tmdb_data: dict = {}
    if clean:
        try:
            results = await tmdb_api.search_movie(clean, year)
            if results:
                tmdb_data = results[0]
        except Exception as exc:
            logger.warning("TMDB movie search failed for '%s': %s", clean, exc)

    title = tmdb_data.get("title", clean or caption[:100] or "Sin título")
    year_val = tmdb_data.get("year", year)

    channel_msg_id = None
    caption_text = f"🎬 {title} ({year_val})" if year_val else f"🎬 {title}"

    # Send to distribution channel with retry on flood control
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

    await db.add_movie(
        file_id=file_id,
        message_id=post.message_id,
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
    logger.info("Movie indexed: %s (msg_id=%s)", title, post.message_id)

    # Notify all registered groups
    movie = (await db.search_movies(title, limit=1) or [None])[0]
    if movie:
        await _notify_groups(
            context,
            title=movie.title,
            year=movie.year,
            poster_url=movie.poster_url,
            deeplink=f"watch_movie_{movie.id}",
            emoji="🎬",
        )
