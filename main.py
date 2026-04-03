"""CineStelar Premium Bot – Entry point."""

import logging
import sys

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

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


def main():
    if not settings.BOT_TOKEN:
        logger.error("BOT_TOKEN not set in .env")
        sys.exit(1)

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

    # ── Start polling ─────────────────────────────────────────────────────
    logger.info("Starting bot polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
