# MeshTelegram Bridge Telegram service

from __future__ import annotations

import asyncio
import logging
import sys
import warnings
from functools import partial

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.warnings import PTBUserWarning

from mqtt_service import MqttService, MqttServiceError


logger = logging.getLogger(__name__)


async def forward_to_telegram(bot: Bot, message_text: str, chat_id: int) -> None:
    """Forward one Meshtastic message through the application's shared bot."""
    await bot.send_message(chat_id=chat_id, text=message_text)
    logger.info("Forwarded Meshtastic message to Telegram chat %s", chat_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Respond to the /start command."""
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    await message.reply_html(f"Hi {user.mention_html()}! I am MeshTelegram Bridge.")
    logger.info("User %s (%s) started the bot.", user.id, user.username)
    if update.effective_chat is not None:
        await message.reply_text(
            f"Your Chat ID is: {update.effective_chat.id}. Configure this in config.json."
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Respond to the /help command."""
    if update.effective_message is not None:
        await update.effective_message.reply_text(
            "I forward messages between the configured Meshtastic channel and Telegram chat."
        )


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    mqtt_service: MqttService,
) -> None:
    """Forward an authorized Telegram text message to Meshtastic."""
    chat = update.effective_chat
    message = update.effective_message
    user = update.effective_user
    target_chat_id = context.bot_data["target_chat_id"]

    if chat is None or message is None or user is None or not message.text:
        return
    if chat.id != target_chat_id:
        await message.reply_text("This chat is not authorized to use this bot.")
        logger.warning("Rejected message from unauthorized chat ID %s", chat.id)
        return

    formatted_text = f"[TG:{user.id}]: {message.text}"
    logger.info("Forwarding Telegram message from user %s to Meshtastic.", user.id)
    try:
        await asyncio.to_thread(mqtt_service.send_message, formatted_text)
    except MqttServiceError as exc:
        logger.error("Failed to forward Telegram message to Meshtastic: %s", exc)
        await message.reply_text("Message could not be sent to Meshtastic. Please try again later.")


def create_application(
    bot_token: str,
    target_chat_id: int,
    mqtt_service: MqttService,
) -> Application:
    """Create a Telegram application coupled to the MQTT service lifecycle."""
    telegram_loop = None

    async def post_init(application: Application) -> None:
        nonlocal telegram_loop
        telegram_loop = asyncio.get_running_loop()

        def forward_from_mqtt(message_text: str) -> None:
            if telegram_loop is None or telegram_loop.is_closed():
                logger.error("Telegram event loop is unavailable; dropping Meshtastic message.")
                return
            future = asyncio.run_coroutine_threadsafe(
                forward_to_telegram(application.bot, message_text, target_chat_id),
                telegram_loop,
            )

            def log_result(completed) -> None:
                try:
                    completed.result()
                except Exception:
                    logger.exception("Failed to forward Meshtastic message to Telegram.")

            future.add_done_callback(log_result)

        mqtt_service.set_telegram_callback(forward_from_mqtt)
        await asyncio.to_thread(mqtt_service.start)
        logger.info("Telegram and MQTT services are ready.")

    async def post_shutdown(application: Application) -> None:
        await asyncio.to_thread(mqtt_service.stop)

    with warnings.catch_warnings():
        if getattr(sys, "frozen", False):
            warnings.filterwarnings(
                "ignore",
                message=r"`Application` instances should be built via the `ApplicationBuilder`\.",
                category=PTBUserWarning,
            )
        application = (
            Application.builder()
            .token(bot_token)
            .post_init(post_init)
            .post_shutdown(post_shutdown)
            .build()
        )
    application.bot_data["target_chat_id"] = target_chat_id
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            partial(handle_message, mqtt_service=mqtt_service),
        )
    )
    return application


def start_bot(application: Application) -> None:
    """Run the Telegram polling loop until shutdown."""
    logger.info("Starting Telegram bot polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)
