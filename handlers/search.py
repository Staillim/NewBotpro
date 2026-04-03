"""Handler: Search – inline search for movies, series, and anime."""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters

from database import db_manager as db
from database.models import ContentType

logger = logging.getLogger(__name__)

# Conversation states
WAITING_QUERY = 0


async def search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiate search – ask user what to search."""
    query = update.callback_query
    if query:
        await query.answer()

    # Store search category
    cat = None
    if query and query.data:
        parts = query.data.split(":")
        if len(parts) > 1 and parts[1] in ("movies", "series", "anime"):
            cat = parts[1]

    context.user_data["search_category"] = cat

    labels = {
        "movies": "🎬 películas",
        "series": "📺 series",
        "anime": "🎌 anime",
        None: "todo el catálogo",
    }
    label = labels.get(cat, "todo el catálogo")

    text = f"🔍 *Buscando en {label}*\n\nEscribe el título que deseas buscar:"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancelar", callback_data="menu:main")]
    ])

    if query:
        await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")

    context.user_data["awaiting_search"] = True


async def handle_search_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process the search query typed by user."""
    if not context.user_data.get("awaiting_search"):
        return  # Not in search mode

    context.user_data["awaiting_search"] = False
    query_text = update.message.text.strip()

    if len(query_text) < 2:
        await update.message.reply_text("⚠️ Escribe al menos 2 caracteres para buscar.")
        return

    user_id = update.effective_user.id
    cat = context.user_data.get("search_category")

    # Search across categories
    results_movies = []
    results_series = []
    results_anime = []

    if cat is None or cat == "movies":
        results_movies = await db.search_movies(query_text, limit=5)
    if cat is None or cat == "series":
        results_series = await db.search_shows(query_text, ContentType.SERIES, limit=5)
    if cat is None or cat == "anime":
        results_anime = await db.search_shows(query_text, ContentType.ANIME, limit=5)

    total = len(results_movies) + len(results_series) + len(results_anime)

    await db.log_search(user_id, query_text, total)

    if total == 0:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Buscar de Nuevo", callback_data="search:start")],
            [InlineKeyboardButton("🏠 Menú Principal", callback_data="menu:main")],
        ])
        await update.message.reply_text(
            f"😔 No se encontraron resultados para *\"{query_text}\"*\n\n"
            f"Intenta con otro título.",
            reply_markup=kb,
            parse_mode="Markdown",
        )
        return

    text = f"🔍 Resultados para *\"{query_text}\"* ({total})\n\n"
    buttons = []

    if results_movies:
        text += "🎬 *Películas:*\n"
        for m in results_movies:
            star = f"⭐{m.vote_average:.1f}" if m.vote_average else ""
            year = f"({m.year})" if m.year else ""
            text += f"  • {m.title} {year} {star}\n"
            buttons.append([InlineKeyboardButton(
                f"🎬 {m.title} {year}", callback_data=f"movie:{m.id}"
            )])
        text += "\n"

    if results_series:
        text += "📺 *Series:*\n"
        for s in results_series:
            star = f"⭐{s.vote_average:.1f}" if s.vote_average else ""
            year = f"({s.year})" if s.year else ""
            text += f"  • {s.name} {year} {star}\n"
            buttons.append([InlineKeyboardButton(
                f"📺 {s.name} {year}", callback_data=f"show:{s.id}"
            )])
        text += "\n"

    if results_anime:
        text += "🎌 *Anime:*\n"
        for a in results_anime:
            star = f"⭐{a.vote_average:.1f}" if a.vote_average else ""
            year = f"({a.year})" if a.year else ""
            text += f"  • {a.name} {year} {star}\n"
            buttons.append([InlineKeyboardButton(
                f"🎌 {a.name} {year}", callback_data=f"show:{a.id}"
            )])
        text += "\n"

    buttons.append([InlineKeyboardButton("🔍 Nueva Búsqueda", callback_data="search:start")])
    buttons.append([InlineKeyboardButton("🏠 Menú Principal", callback_data="menu:main")])

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )
