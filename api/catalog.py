"""FastAPI catalog API + Telegram bot via webhook (no polling conflict)."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config.settings import settings
from database import db_manager as db
from database.models import ContentType
from handlers.admin import (
    activate_plan_command,
    admin_menu,
    ban_command,
    cancel_plan_command,
    index_command,
    index_episodes_command,
    index_manual_command,
    index_series_command,
    stats_command,
    unban_command,
)
from handlers.broadcast import broadcast_command
from handlers.callbacks import callback_handler
from handlers.search import handle_search_query
from handlers.start import start_command

logger = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).parent.parent / "webapp"
_PAGE_SIZE = 12

# Global bot application instance shared between lifespan and webhook endpoint
_tg_app = None


def _build_tg_application():
    # updater=None = webhook mode, no background polling thread
    tg = ApplicationBuilder().token(settings.BOT_TOKEN).updater(None).build()
    tg.add_handler(CommandHandler("start", start_command))
    tg.add_handler(CommandHandler("admin", admin_menu))
    tg.add_handler(CommandHandler("stats", stats_command))
    tg.add_handler(CommandHandler("activar", activate_plan_command))
    tg.add_handler(CommandHandler("cancelar", cancel_plan_command))
    tg.add_handler(CommandHandler("ban", ban_command))
    tg.add_handler(CommandHandler("unban", unban_command))
    tg.add_handler(CommandHandler("indexar", index_command))
    tg.add_handler(CommandHandler("indexar_manual", index_manual_command))
    tg.add_handler(CommandHandler("indexar_serie", index_series_command))
    tg.add_handler(CommandHandler("indexar_episodios", index_episodes_command))
    tg.add_handler(CommandHandler("broadcast", broadcast_command))
    tg.add_handler(CallbackQueryHandler(callback_handler))
    tg.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        handle_search_query,
    ))
    tg.add_error_handler(_ptb_error_handler)
    return tg


async def _ptb_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log ALL exceptions raised in handlers so they are visible in Render logs."""
    logger.error("PTB handler error", exc_info=context.error)


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    """Startup: init DB + start bot webhook. Shutdown: clean up."""
    global _tg_app

    # 1. Create all tables
    logger.info("Initializing database...")
    await db.init_db()
    logger.info("Database initialized.")

    # 2. Build and start bot application
    _tg_app = _build_tg_application()
    await _tg_app.initialize()
    await _tg_app.start()

    # 3. Register webhook with Telegram
    if settings.WEBAPP_URL:
        webhook_url = f"{settings.WEBAPP_URL.rstrip('/')}/webhook/{settings.BOT_TOKEN}"
        await _tg_app.bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True,
        )
        me = await _tg_app.bot.get_me()
        logger.info("Webhook set: %s  (@%s)", webhook_url, me.username)
    else:
        logger.warning("WEBAPP_URL not set - webhook NOT registered. Bot won't receive messages.")

    yield  # ─── app is running ───

    # Shutdown
    if settings.WEBAPP_URL:
        await _tg_app.bot.delete_webhook()
    await _tg_app.stop()
    await _tg_app.shutdown()
    logger.info("Bot stopped.")


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="CineStelar", docs_url=None, redoc_url=None, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── Telegram webhook endpoint ─────────────────────────────────────────────────

@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != settings.BOT_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    update = Update.de_json(data, _tg_app.bot)
    # Fire-and-forget: process in background, return 200 immediately to Telegram
    asyncio.create_task(_tg_app.process_update(update))
    return Response(content="ok")


# ── Health check (Render sends HEAD /) ────────────────────────────────────────

@app.head("/")
async def health_head():
    return Response()


# ── Config ────────────────────────────────────────────────────────────────────

@app.get("/api/config")
async def get_config():
    return {"bot_username": settings.BOT_USERNAME}


# ── Movies ────────────────────────────────────────────────────────────────────

@app.get("/api/movies")
async def get_movies(
    page: int = Query(0, ge=0),
    search: str = Query("", max_length=100),
):
    if search.strip():
        items = await db.search_movies(search.strip(), limit=20)
        return {"items": [_movie(m) for m in items], "total": len(items), "pages": 1, "page": 0}
    movies, total = await db.get_movies_page(page, page_size=_PAGE_SIZE)
    return {
        "items": [_movie(m) for m in movies],
        "total": total,
        "pages": max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE),
        "page": page,
    }


# ── Series ────────────────────────────────────────────────────────────────────

@app.get("/api/series")
async def get_series(
    page: int = Query(0, ge=0),
    search: str = Query("", max_length=100),
):
    if search.strip():
        items = await db.search_shows(search.strip(), content_type=ContentType.SERIES, limit=20)
        return {"items": [_show(s) for s in items], "total": len(items), "pages": 1, "page": 0}
    shows, total = await db.get_shows_page(ContentType.SERIES, page, page_size=_PAGE_SIZE)
    return {
        "items": [_show(s) for s in shows],
        "total": total,
        "pages": max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE),
        "page": page,
    }


# ── Anime ─────────────────────────────────────────────────────────────────────

@app.get("/api/anime")
async def get_anime(
    page: int = Query(0, ge=0),
    search: str = Query("", max_length=100),
):
    if search.strip():
        items = await db.search_shows(search.strip(), content_type=ContentType.ANIME, limit=20)
        return {"items": [_show(s) for s in items], "total": len(items), "pages": 1, "page": 0}
    shows, total = await db.get_shows_page(ContentType.ANIME, page, page_size=_PAGE_SIZE)
    return {
        "items": [_show(s) for s in shows],
        "total": total,
        "pages": max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE),
        "page": page,
    }


# ── Detail ────────────────────────────────────────────────────────────────────

@app.get("/api/movie/{movie_id}")
async def movie_detail(movie_id: int):
    m = await db.get_movie(movie_id)
    if not m:
        raise HTTPException(status_code=404, detail="Not found")
    return _movie(m, detail=True)


@app.get("/api/show/{show_id}")
async def show_detail(show_id: int):
    s = await db.get_show(show_id)
    if not s:
        raise HTTPException(status_code=404, detail="Not found")
    return _show(s, detail=True)


# ── Serializers ───────────────────────────────────────────────────────────────

def _movie(m, detail: bool = False) -> dict:
    d = {
        "id": m.id,
        "type": "movie",
        "title": m.title,
        "year": m.year,
        "vote_average": m.vote_average,
        "poster_url": m.poster_url,
        "genres": m.genres,
    }
    if detail:
        d.update({
            "overview": m.overview,
            "backdrop_url": m.backdrop_url,
            "runtime": m.runtime,
            "original_title": m.original_title,
        })
    return d


def _show(s, detail: bool = False) -> dict:
    d = {
        "id": s.id,
        "type": s.content_type.value if s.content_type else "series",
        "title": s.name,
        "year": s.year,
        "vote_average": s.vote_average,
        "poster_url": s.poster_url,
        "genres": s.genres,
        "seasons": s.number_of_seasons,
    }
    if detail:
        d.update({
            "overview": s.overview,
            "backdrop_url": s.backdrop_url,
            "original_title": s.original_name,
        })
    return d


# ── Serve static WebApp - must be the last route ──────────────────────────────

@app.get("/")
async def index():
    index_file = WEBAPP_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="WebApp not built")
    return FileResponse(str(index_file))
