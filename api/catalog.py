"""FastAPI catalog API + Telegram bot via webhook (production-ready)."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
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
    delete_command,
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
from handlers.payment import pre_checkout_handler, successful_payment_handler
from handlers.search import handle_search_query
from handlers.start import start_command

logger = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).parent.parent / "webapp"
_PAGE_SIZE = 12

# ── Global state ──────────────────────────────────────────────────────────────
_tg_app = None
_webhook_url = ""
# Strong refs so GC never collects running handler tasks
_bg_tasks: set[asyncio.Task] = set()


def _fire(coro) -> None:
    """Schedule coroutine as background task with strong reference."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


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
    tg.add_handler(CommandHandler("borrar", delete_command))
    tg.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))
    tg.add_handler(CallbackQueryHandler(callback_handler))
    tg.add_handler(MessageHandler(
        filters.SUCCESSFUL_PAYMENT,
        successful_payment_handler,
    ))
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


async def _ensure_webhook() -> None:
    """(Re)register webhook. Called on startup and by the keep-alive check."""
    global _webhook_url
    if not settings.WEBAPP_URL or _tg_app is None:
        return
    _webhook_url = f"{settings.WEBAPP_URL.rstrip('/')}/webhook/{settings.BOT_TOKEN}"
    try:
        await _tg_app.bot.set_webhook(
            url=_webhook_url,
            drop_pending_updates=False,
            allowed_updates=["message", "callback_query", "channel_post"],
        )
        wh = await _tg_app.bot.get_webhook_info()
        logger.info("Webhook SET → %s | pending=%s | error=%s",
                     _webhook_url[:60] + "...",
                     wh.pending_update_count,
                     wh.last_error_message or "none")
    except Exception:
        logger.exception("Failed to set webhook!")


async def _webhook_keepalive() -> None:
    """Periodically verify webhook is alive; re-register if lost."""
    while True:
        await asyncio.sleep(300)  # every 5 minutes
        try:
            if _tg_app is None:
                continue
            wh = await _tg_app.bot.get_webhook_info()
            if not wh.url:
                logger.warning("Webhook URL is EMPTY — re-registering...")
                await _ensure_webhook()
            else:
                logger.info("Webhook OK | pending=%s", wh.pending_update_count)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Keepalive check failed")


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    global _tg_app

    logger.info("=== STARTUP BEGIN ===")
    await db.init_db()
    logger.info("Database ready.")

    _tg_app = _build_tg_application()
    await _tg_app.initialize()
    await _tg_app.start()
    logger.info("PTB started.")

    await _ensure_webhook()

    me = await _tg_app.bot.get_me()
    logger.info("Bot @%s online | ADMIN_IDS=%s", me.username, settings.ADMIN_IDS)
    logger.info("=== STARTUP COMPLETE ===")

    # Start keepalive loop
    keepalive_task = asyncio.create_task(_webhook_keepalive())

    yield

    # Shutdown — do NOT delete webhook so new deploy can pick up where left off
    keepalive_task.cancel()
    try:
        await keepalive_task
    except asyncio.CancelledError:
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
    """Accept update → return 200 instantly → process in background."""
    if token != settings.BOT_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    if _tg_app is None:
        return Response(content="not ready", status_code=503)
    try:
        data = await request.json()
        update = Update.de_json(data, _tg_app.bot)
        _fire(_safe_process(update))
    except Exception:
        logger.exception("Webhook parse error")
    return Response(content="ok")


async def _safe_process(update: Update) -> None:
    """Process one update. Never raises — logs everything."""
    try:
        logger.info("Processing update_id=%s type=%s",
                     update.update_id,
                     "msg" if update.message else "cb" if update.callback_query else "ch_post" if update.channel_post else "other")
        await _tg_app.process_update(update)
        logger.info("Done update_id=%s", update.update_id)
    except Exception:
        logger.exception("Handler crashed on update_id=%s", update.update_id)


# ── Health ────────────────────────────────────────────────────────────────────

@app.head("/")
async def health_head():
    return Response()

@app.get("/health")
async def health_check():
    ready = _tg_app is not None
    info = {"status": "ok" if ready else "starting", "tasks": len(_bg_tasks)}
    if ready:
        try:
            wh = await _tg_app.bot.get_webhook_info()
            info["webhook_set"] = bool(wh.url)
            info["pending"] = wh.pending_update_count
            info["last_error"] = wh.last_error_message
        except Exception:
            info["webhook_set"] = "error"
    return JSONResponse(info)


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


# ── Ad system ─────────────────────────────────────────────────────────────────

@app.get("/ad")
async def serve_ad_viewer():
    ad_file = WEBAPP_DIR / "ad_viewer.html"
    if not ad_file.exists():
        raise HTTPException(status_code=404, detail="Ad viewer not found")
    return FileResponse(str(ad_file))


@app.get("/api/ad-config")
async def ad_config():
    return {"zone_id": settings.LIBTL_ZONE_ID}


class AdCompletedPayload(BaseModel):
    user_id: int
    content_id: int
    content_type: str  # "movie" or "ep"


@app.post("/api/ad-completed")
async def ad_completed(payload: AdCompletedPayload):
    if _tg_app is None:
        raise HTTPException(status_code=503, detail="Bot not ready")

    if payload.content_type == "movie":
        movie = await db.get_movie(payload.content_id)
        if not movie:
            raise HTTPException(status_code=404, detail="Not found")

        async def _send_movie():
            caption = f"🎬 *{movie.title}* ({movie.year or ''})\n\n_TodoCineHD_"
            try:
                await _tg_app.bot.send_video(
                    chat_id=payload.user_id,
                    video=movie.file_id,
                    caption=caption,
                    parse_mode="Markdown",
                )
                await db.log_activity(payload.user_id, "watch_movie_ad", movie.id, "movie")
            except Exception:
                try:
                    await _tg_app.bot.send_document(
                        chat_id=payload.user_id,
                        document=movie.file_id,
                        caption=caption,
                        parse_mode="Markdown",
                    )
                    await db.log_activity(payload.user_id, "watch_movie_ad", movie.id, "movie")
                except Exception as e2:
                    logger.error("ad_completed: failed to send movie %s to %s: %s",
                                 payload.content_id, payload.user_id, e2)

        _fire(_send_movie())

    elif payload.content_type == "ep":
        ep = await db.get_episode(payload.content_id)
        if not ep:
            raise HTTPException(status_code=404, detail="Not found")
        show = await db.get_show(ep.tv_show_id)
        show_name = show.name if show else "Serie"
        ep_title = ep.title or f"Episodio {ep.episode_number}"

        async def _send_ep():
            caption = (
                f"📺 *{show_name}*\n"
                f"T{ep.season_number}E{ep.episode_number}: {ep_title}\n\n"
                f"_TodoCineHD_"
            )
            try:
                await _tg_app.bot.send_video(
                    chat_id=payload.user_id,
                    video=ep.file_id,
                    caption=caption,
                    parse_mode="Markdown",
                )
                await db.log_activity(payload.user_id, "watch_episode_ad", ep.id, "series")
            except Exception:
                try:
                    await _tg_app.bot.send_document(
                        chat_id=payload.user_id,
                        document=ep.file_id,
                        caption=caption,
                        parse_mode="Markdown",
                    )
                    await db.log_activity(payload.user_id, "watch_episode_ad", ep.id, "series")
                except Exception as e2:
                    logger.error("ad_completed: failed to send ep %s to %s: %s",
                                 payload.content_id, payload.user_id, e2)

        _fire(_send_ep())

    else:
        raise HTTPException(status_code=400, detail="Invalid content_type")

    return {"status": "ok"}


# ── Serve static WebApp - must be the last route ──────────────────────────────

@app.get("/")
async def index():
    index_file = WEBAPP_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="WebApp not built")
    return FileResponse(str(index_file))
