"""Handler: Group Search – auto-detects content requests in group chats.

REQUIRES: BotFather → /mybots → Bot → Bot Settings → Group Privacy → Turn OFF
"""

import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config.settings import settings
from database import db_manager as db
from database.models import ContentType

logger = logging.getLogger(__name__)

# ── Patterns ──────────────────────────────────────────────────────────────────

_SEARCH_KEYWORDS = re.compile(
    r"\b(alguien tiene|tienen|busco|busca|buscando|hay|tienes|saben donde|"
    r"d[oó]nde puedo|puedo ver|quiero ver|me pasan|me manden|me pueden pasar|"
    r"alguien sabe|saben de|me recomienda|recomienda[n]?|"
    r"someone has|looking for|does anyone have|can i watch|want to watch)\b",
    re.IGNORECASE,
)

_YEAR_PATTERN = re.compile(r"\b(19|20)\d{2}\b")

_EPISODE_PATTERN = re.compile(
    r"\b(s\d{1,2}e\d{1,2}|t\d+\s*cap\s*\d+|temporada\s*\d+|season\s*\d+|ep\.?\s*\d+)\b",
    re.IGNORECASE,
)

_CASUAL_ONLY = re.compile(
    r"^(hola|hey|buenas|buenos d[ií]as|buenas noches|buenas tardes|"
    r"gracias|de nada|por favor|ok|okay|si|no|jaja|jajaja|lol|xd|"
    r"[\U00010000-\U0010ffff\u2600-\u27BF]+)\s*$",
    re.IGNORECASE,
)

_MIN_LENGTH = 4
_MAX_LENGTH = 120


def _is_potential_search(text: str) -> tuple[bool, float]:
    """Return (is_search, confidence 0.0–1.0)."""
    text = text.strip()

    if len(text) < _MIN_LENGTH or len(text) > _MAX_LENGTH:
        return False, 0.0

    if _CASUAL_ONLY.match(text):
        return False, 0.0

    if "http" in text or "www." in text or "t.me" in text:
        return False, 0.0

    score = 0.0

    if _SEARCH_KEYWORDS.search(text):
        score += 0.45

    if _YEAR_PATTERN.search(text):
        score += 0.15

    if _EPISODE_PATTERN.search(text):
        score += 0.20

    # Title-like capitalized words
    cap_words = re.findall(r"\b[A-ZÁÉÍÓÚÑÜ][a-záéíóúñü]{2,}\b", text)
    if len(cap_words) >= 2:
        score += 0.20

    # Short direct title (3-6 words, no keyword but has caps) — bare title typed
    words = text.split()
    if 2 <= len(words) <= 5 and not _SEARCH_KEYWORDS.search(text) and score == 0.0:
        if sum(1 for w in words if w and w[0].isupper()) >= 2:
            score += 0.35

    return score >= 0.40, score


def _clean_query(text: str) -> str:
    """Strip search phrases, leaving the title."""
    text = _SEARCH_KEYWORDS.sub("", text)
    text = re.sub(r"[¿?¡!,;:]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ── Main handler ──────────────────────────────────────────────────────────────

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Respond to suspected content requests in group/supergroup chats."""
    message = update.message
    if not message or not message.text:
        return

    user = update.effective_user
    if not user or user.is_bot:
        return

    # Auto-register this group silently on every message
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        await db.register_group(chat.id, chat.title)

    text = message.text.strip()
    is_search, score = _is_potential_search(text)
    if not is_search:
        return

    query_text = _clean_query(text)
    if len(query_text) < 2:
        query_text = text  # fallback: use original if clean strips too much

    # Search catalog
    results_movies = await db.search_movies(query_text, limit=3)
    results_series = await db.search_shows(query_text, ContentType.SERIES, limit=3)
    results_anime = await db.search_shows(query_text, ContentType.ANIME, limit=3)

    total = len(results_movies) + len(results_series) + len(results_anime)
    if total == 0:
        return  # nothing found → stay silent

    # Add result-presence bonus and re-check threshold
    final_score = score + 0.30
    if final_score < 0.70:
        return

    bot_username = settings.BOT_USERNAME
    lines = [f'🎬 *Encontré esto para: "{query_text}"*\n']
    buttons: list[list[InlineKeyboardButton]] = []

    if results_movies:
        lines.append("📽️ *Películas:*")
        for m in results_movies:
            star = f" ⭐{m.vote_average:.1f}" if m.vote_average else ""
            year = f" ({m.year})" if m.year else ""
            lines.append(f"  • {m.title}{year}{star}")
            url = f"https://t.me/{bot_username}?start=watch_movie_{m.id}"
            buttons.append([InlineKeyboardButton(f"🎬 {m.title}{year}", url=url)])

    if results_series:
        lines.append("\n📺 *Series:*")
        for s in results_series:
            star = f" ⭐{s.vote_average:.1f}" if s.vote_average else ""
            year = f" ({s.year})" if s.year else ""
            lines.append(f"  • {s.name}{year}{star}")
            url = f"https://t.me/{bot_username}?start=watch_series_{s.id}"
            buttons.append([InlineKeyboardButton(f"📺 {s.name}{year}", url=url)])

    if results_anime:
        lines.append("\n🎌 *Anime:*")
        for a in results_anime:
            star = f" ⭐{a.vote_average:.1f}" if a.vote_average else ""
            year = f" ({a.year})" if a.year else ""
            lines.append(f"  • {a.name}{year}{star}")
            url = f"https://t.me/{bot_username}?start=watch_anime_{a.id}"
            buttons.append([InlineKeyboardButton(f"🎌 {a.name}{year}", url=url)])

    try:
        await message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )
    except Exception:
        logger.warning("group_search: failed to send reply", exc_info=True)


# ── Group membership tracking ─────────────────────────────────────────────────

async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Track when bot is added or removed from a group."""
    event = update.my_chat_member
    if not event:
        return

    chat = event.chat
    if chat.type not in ("group", "supergroup"):
        return

    new_status = event.new_chat_member.status  # "member", "administrator", "left", "kicked"

    if new_status in ("member", "administrator"):
        await db.register_group(chat.id, chat.title)
        logger.info("Bot added to group: %s (%s)", chat.title, chat.id)
    elif new_status in ("left", "kicked", "restricted"):
        await db.remove_group(chat.id)
        logger.info("Bot removed from group: %s (%s)", chat.title, chat.id)
