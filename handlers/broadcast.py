"""Handler: Broadcast messages to all users."""

import asyncio
import logging
from typing import Any

from telegram import Update, Message, Bot
from telegram.ext import ContextTypes

from config.settings import settings
from database import db_manager as db

logger = logging.getLogger(__name__)

# Keeps references to background tasks so GC doesn't cancel them
_active_tasks: set[asyncio.Task] = set()


def _bg_task(coro: Any) -> asyncio.Task:
    """Schedule a coroutine as a background task, keeping a strong reference."""
    task = asyncio.create_task(coro)
    _active_tasks.add(task)
    task.add_done_callback(_active_tasks.discard)
    return task


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /broadcast <message>."""
    if not settings.is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "📢 *Broadcast*\n\n"
            "Envía `/broadcast <tu mensaje>` para enviar a todos los usuarios.\n\n"
            "También puedes responder a un mensaje (foto, video, texto) con `/broadcast`.",
            parse_mode="Markdown",
        )
        context.user_data["awaiting_broadcast"] = True
        return

    text = " ".join(context.args)
    await _do_broadcast_text(update, context, text)


async def handle_broadcast_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle reply-based broadcast (for media)."""
    if not settings.is_admin(update.effective_user.id):
        return
    if not context.user_data.get("awaiting_broadcast"):
        return

    context.user_data["awaiting_broadcast"] = False
    text = update.message.text or update.message.caption or ""
    if not text:
        await update.message.reply_text("❌ No se detectó mensaje para broadcast.")
        return

    await _do_broadcast_text(update, context, text)


async def _do_broadcast_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_ids = await db.get_all_user_ids()
    total = len(user_ids)

    status_msg = await update.message.reply_text(
        f"📤 Broadcast iniciado en background para {total} usuarios...\n"
        f"Puedes seguir usando el bot normalmente."
    )

    # Run the actual send loop in background — handler returns immediately so
    # Telegram receives 200 quickly and won't re-send this webhook update.
    _bg_task(_run_broadcast_loop(status_msg, context.bot, user_ids, text))


async def _run_broadcast_loop(
    status_msg: Message, bot: Bot, user_ids: list, text: str
) -> None:
    """Send text to all users. Runs as a background task."""
    total = len(user_ids)
    sent = 0
    errors = 0

    for uid in user_ids:
        try:
            await bot.send_message(
                chat_id=uid,
                text=text,
                parse_mode="Markdown",
            )
            sent += 1
        except Exception:
            errors += 1

        if (sent + errors) % 100 == 0:
            try:
                await status_msg.edit_text(
                    f"📤 Enviando... {sent + errors}/{total}\n"
                    f"✅ {sent} | ❌ {errors}"
                )
            except Exception:
                pass

        await asyncio.sleep(0.05)

    try:
        await status_msg.edit_text(
            f"✅ *Broadcast completado*\n\n"
            f"📊 Total: {total}\n"
            f"✅ Enviados: {sent}\n"
            f"❌ Errores: {errors}",
            parse_mode="Markdown",
        )
    except Exception:
        pass
