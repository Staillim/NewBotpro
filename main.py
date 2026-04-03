"""CineStelar Premium Bot – Entry point."""

import asyncio
import logging
import os
import sys

import uvicorn
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from api.catalog import app as fastapi_app

from config.settings import settings
from database.db_manager import init_db
from handlers.start import start_command
from handlers.callbacks import callback_handler
from handlers.search import handle_search_query
from handlers.admin import (
    admin_menu,
    stats_command,
    activate_plan_command,
    cancel_plan_command,
    ban_command,
    unban_command,
    index_command,
    index_manual_command,
    index_series_command,
    index_episodes_command,
)
from handlers.broadcast import broadcast_command

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


async def post_init(application):
    """Run after bot initialization."""
    await init_db()
    logger.info("Database initialized.")
    me = await application.bot.get_me()
    logger.info("Bot started: @%s (%s)", me.username, me.id)


def _build_application():
    """Build and configure the Telegram bot application."""
    app = (
        ApplicationBuilder()
        .token(settings.BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # ── User commands (only /start) ───────────────────────────────────────
    app.add_handler(CommandHandler("start", start_command))

    # ── Admin commands ────────────────────────────────────────────────────
    app.add_handler(CommandHandler("admin", admin_menu))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("activar", activate_plan_command))
    app.add_handler(CommandHandler("cancelar", cancel_plan_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("indexar", index_command))
    app.add_handler(CommandHandler("indexar_manual", index_manual_command))
    app.add_handler(CommandHandler("indexar_serie", index_series_command))
    app.add_handler(CommandHandler("indexar_episodios", index_episodes_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))

    # ── Callback queries (all inline buttons) ─────────────────────────────
    app.add_handler(CallbackQueryHandler(callback_handler))

    # ── Text messages (search input) ──────────────────────────────────────
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        handle_search_query,
    ))

    return app


async def _run_bot(stop_event: asyncio.Event):
    """Run the Telegram bot until stop_event is set."""
    tg_app = _build_application()
    async with tg_app:
        await tg_app.start()
        await tg_app.updater.start_polling(drop_pending_updates=True)
        logger.info("Bot polling started.")
        await stop_event.wait()
        logger.info("Bot shutting down…")
        await tg_app.updater.stop()
        await tg_app.stop()


async def _run_api(stop_event: asyncio.Event):
    """Run the FastAPI catalog server.  Sets stop_event when it exits."""
    port = int(os.getenv("PORT", 8000))
    config = uvicorn.Config(
        fastapi_app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    logger.info("Catalog API starting on port %d…", port)
    await server.serve()
    # When uvicorn stops (SIGTERM / SIGINT), signal the bot to stop too
    stop_event.set()


async def main():
    if not settings.BOT_TOKEN:
        logger.error("BOT_TOKEN not set in .env")
        sys.exit(1)

    stop_event = asyncio.Event()
    await asyncio.gather(
        _run_bot(stop_event),
        _run_api(stop_event),
    )


if __name__ == "__main__":
    asyncio.run(main())
