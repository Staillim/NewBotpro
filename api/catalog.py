"""FastAPI catalog API + Telegram bot via webhook (production-ready)."""

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
from handlers.intake import handle_channel_post
from handlers.search import handle_search_query
from handlers.start import start_command

logger = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).parent.parent / "webapp"
_PAGE_SIZE = 12

# ── Global state ──────────────────────────────────────────────────────────────
_tg_app = None


def _build_tg_application():
    tg = (
        ApplicationBuilder()
        .token(settings.BOT_TOKEN)
        .updater(None)
        .concurrent_updates(True)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(15)
        .build()
    )
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
    tg.add_handler(MessageHandler(
        filters.UpdateType.CHANNEL_POST,
        handle_channel_post,
    ))
    tg.add_error_handler(_ptb_error_handler)
    return tg


async def _ptb_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("PTB handler error: %s", context.error, exc_info=context.error)


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    global _tg_app

    logger.info("Initializing database...")
    await db.init_db()
    logger.info("Database ready.")

    _tg_app = _build_tg_application()
    await _tg_app.initialize()
    await _tg_app.start()
    logger.info("PTB Application started.")

    if settings.WEBAPP_URL:
        webhook_url = f"{settings.WEBAPP_URL.rstrip('/')}/webhook/{settings.BOT_TOKEN}"
        await _tg_app.bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query", "channel_post"],
        )
        me = await _tg_app.bot.get_me()
        wh_info = await _tg_app.bot.get_webhook_info()
        logger.info("Bot @%s online | Webhook: %s", me.username, webhook_url)
        logger.info("Pending: %s | Last error: %s",
                     wh_info.pending_update_count, wh_info.last_error_message or "none")
        logger.info("ADMIN_IDS: %s", settings.ADMIN_IDS)
    else:
        logger.error("WEBAPP_URL not set! Bot cannot receive messages.")

    yield

    if settings.WEBAPP_URL:
        try:
            await _tg_app.bot.delete_webhook()
        except Exception:
            pass
    await _tg_app.stop()
    await _tg_app.shutdown()
    logger.info("Bot stopped.")


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="CineStelar", docs_url=None, redoc_url=None, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "HEAD"],
    allow_headers=["*"],
)


# ── Webhook ───────────────────────────────────────────────────────────────────

@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    """Receive update and process it inline. Telegram allows up to 60s."""
    if token != settings.BOT_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    if _tg_app is None:
        return Response(content="not ready", status_code=503)
    try:
        data = await request.json()
        update = Update.de_json(data, _tg_app.bot)
        await _tg_app.process_update(update)
    except Exception:
        logger.exception("Webhook error")
    return Response(content="ok")


# ── Health ────────────────────────────────────────────────────────────────────

@app.head("/")
async def health_head():
    return Response()

@app.get("/health")
async def health_check():
    return {"status": "ok" if _tg_app else "starting"}


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
