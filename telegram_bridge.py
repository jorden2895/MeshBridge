# MeshTelegram Bridge Telegram service

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import warnings
from dataclasses import dataclass
from functools import partial
from typing import Iterable

from telegram import Bot, Update
from telegram.error import Conflict
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.warnings import PTBUserWarning

from config import RouteConfig
from mqtt_service import MqttService, MqttServiceError
from runtime_state import RuntimeState


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RouteBinding:
    route: RouteConfig
    mqtt_service: MqttService


class ThreadSafeApplicationStop:
    """Schedule Application.stop_running on its owning asyncio event loop."""

    def __init__(self, stop_callback) -> None:
        self._stop_callback = stop_callback
        self._lock = threading.Lock()
        self._loop = None
        self._pending = False

    def bind_loop(self, loop) -> None:
        with self._lock:
            self._loop = loop
            pending = self._pending
            self._pending = False
        if pending:
            loop.call_soon(self._stop_callback)

    def __call__(self) -> None:
        with self._lock:
            loop = self._loop
            if loop is None or loop.is_closed():
                self._pending = True
                return
        loop.call_soon_threadsafe(self._stop_callback)

    def clear_loop(self) -> None:
        with self._lock:
            self._loop = None
            self._pending = False


async def forward_to_telegram(
    bot: Bot,
    message_text: str,
    chat_id: int,
    topic_id: int | None = None,
) -> None:
    """Forward one Meshtastic message through the application's shared bot."""
    await bot.send_message(
        chat_id=chat_id,
        text=message_text,
        message_thread_id=topic_id,
    )
    logger.info("Forwarded Meshtastic message to the configured Telegram chat.")


def is_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    if chat is None:
        return False
    return any(binding.route.target_chat_id == chat.id for binding in _bindings(context))


def _bindings(context: ContextTypes.DEFAULT_TYPE) -> tuple[RouteBinding, ...]:
    bindings = context.bot_data.get("route_bindings")
    if bindings:
        return tuple(bindings)
    service = context.bot_data.get("mqtt_service")
    target_chat_id = context.bot_data.get("target_chat_id")
    if service is None or target_chat_id is None:
        return ()
    route = getattr(service, "route", None)
    if route is None:
        route = RouteConfig("預設路由", True, "", "", target_chat_id, None)
    return (RouteBinding(route, service),)


def _select_binding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> RouteBinding | None:
    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None:
        return None
    thread_id = getattr(message, "message_thread_id", None)
    for binding in _bindings(context):
        route = binding.route
        if route.target_chat_id != chat.id:
            continue
        if route.topic_id is None and thread_id is None:
            return binding
        if route.topic_id == thread_id:
            return binding
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Respond to the /start command."""
    if not is_authorized(update, context):
        logger.warning("Ignored /start from an unauthorized Telegram chat.")
        return
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
    if is_authorized(update, context) and update.effective_message is not None:
        await update.effective_message.reply_text(
            "I forward messages between the configured Meshtastic channel and Telegram chat."
        )


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    mqtt_service: MqttService | None = None,
    runtime_state: RuntimeState | None = None,
) -> None:
    """Forward an authorized Telegram text message to Meshtastic."""
    chat = update.effective_chat
    message = update.effective_message
    user = update.effective_user

    if chat is None or message is None or user is None or not message.text:
        return
    binding = _select_binding(update, context)
    if (
        binding is None
        and mqtt_service is not None
        and chat.id == context.bot_data.get("target_chat_id")
    ):
        route = getattr(mqtt_service, "route", None)
        if route is None:
            route = RouteConfig("預設路由", True, "", b"", chat.id, None)
        binding = RouteBinding(route, mqtt_service)
    if binding is None:
        logger.warning("Ignored a message from an unauthorized Telegram chat.")
        if runtime_state is not None:
            runtime_state.increment("unauthorized_dropped")
        return

    formatted_text = f"[TG:{user.id}]: {message.text}"
    logger.info("Forwarding Telegram message from user %s to Meshtastic.", user.id)
    try:
        await asyncio.to_thread(
            (mqtt_service or binding.mqtt_service).send_message,
            formatted_text,
        )
    except MqttServiceError as exc:
        logger.error("Failed to forward Telegram message to Meshtastic: %s", exc)
        await message.reply_text("Message could not be sent to Meshtastic. Please try again later.")


def create_application(
    bot_token: str,
    target_chat_id: int | Iterable[RouteBinding],
    mqtt_service: MqttService | None = None,
    runtime_state: RuntimeState | None = None,
) -> Application:
    """Create a Telegram application coupled to the MQTT service lifecycle."""
    telegram_loop = None
    heartbeat_task = None

    if isinstance(target_chat_id, int):
        if mqtt_service is None:
            raise ValueError("mqtt_service is required for the legacy single-route interface")
        route = getattr(mqtt_service, "route", None)
        if route is None:
            route = RouteConfig("預設路由", True, "", "", target_chat_id, None)
        bindings = (RouteBinding(route, mqtt_service),)
    else:
        bindings = tuple(target_chat_id)
        if not bindings:
            raise ValueError("at least one route binding is required")

    async def post_init(application: Application) -> None:
        nonlocal telegram_loop, heartbeat_task
        telegram_loop = asyncio.get_running_loop()
        stop_request.bind_loop(telegram_loop)
        bot_name = application.bot.username or str(application.bot.id)
        logger.info("Telegram Bot 啟動成功：%s", bot_name)
        if runtime_state is not None:
            runtime_state.set_telegram("connected", bot_name=bot_name)

            async def update_heartbeat() -> None:
                while True:
                    runtime_state.heartbeat()
                    await asyncio.sleep(1)

            heartbeat_task = asyncio.create_task(update_heartbeat())

        started: list[MqttService] = []
        try:
            for binding in bindings:
                route = binding.route

                def forward_from_mqtt(
                    message_text: str,
                    *,
                    selected_route: RouteConfig = route,
                ) -> None:
                    if telegram_loop is None or telegram_loop.is_closed():
                        logger.error(
                            "Telegram event loop is unavailable; dropping Meshtastic message."
                        )
                        if runtime_state is not None:
                            runtime_state.increment("other_dropped")
                        return
                    future = asyncio.run_coroutine_threadsafe(
                        forward_to_telegram(
                            application.bot,
                            message_text,
                            selected_route.target_chat_id,
                            selected_route.topic_id,
                        ),
                        telegram_loop,
                    )

                    def log_result(completed) -> None:
                        try:
                            completed.result()
                            if runtime_state is not None:
                                runtime_state.increment("mesh_to_telegram_success")
                                runtime_state.mark_forwarded()
                        except Exception as exc:
                            logger.exception("Failed to forward Meshtastic message to Telegram.")
                            if runtime_state is not None:
                                runtime_state.set_telegram("error", error=str(exc))
                                runtime_state.increment("other_dropped")

                    future.add_done_callback(log_result)

                binding.mqtt_service.set_telegram_callback(forward_from_mqtt)

                def fatal_mqtt(error: str) -> None:
                    logger.error("MQTT 發生致命錯誤：%s", error)
                    stop_request()

                binding.mqtt_service.set_fatal_callback(fatal_mqtt)
                await asyncio.to_thread(binding.mqtt_service.start)
                started.append(binding.mqtt_service)
        except Exception:
            for service in reversed(started):
                await asyncio.to_thread(service.stop)
            raise
        logger.info("MeshTelegram Bridge 已就緒，可以開始轉發訊息。")

    async def post_shutdown(application: Application) -> None:
        stop_request.clear_loop()
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
        for binding in reversed(bindings):
            await asyncio.to_thread(binding.mqtt_service.stop)
        if runtime_state is not None:
            runtime_state.set_telegram("stopped")

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
    application.bot_data["target_chat_id"] = bindings[0].route.target_chat_id
    application.bot_data["mqtt_service"] = bindings[0].mqtt_service
    application.bot_data["route_bindings"] = bindings
    stop_request = ThreadSafeApplicationStop(application.stop_running)
    application.bot_data["request_stop"] = stop_request
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            partial(handle_message, runtime_state=runtime_state),
        )
    )

    conflict_reported = False

    async def handle_telegram_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        nonlocal conflict_reported
        if isinstance(context.error, Conflict):
            if not conflict_reported:
                logger.error(
                    "Telegram Bot 發生重複輪詢衝突：相同 Bot Token 正在其他程式或電腦執行。"
                )
                conflict_reported = True
            application.stop_running()
            if runtime_state is not None:
                runtime_state.set_telegram("error", error=str(context.error))
            return
        if runtime_state is not None:
            runtime_state.set_telegram("error", error=str(context.error))
        logger.error(
            "Telegram 更新處理失敗：%s",
            context.error,
            exc_info=(type(context.error), context.error, context.error.__traceback__),
        )

    application.add_error_handler(handle_telegram_error)
    return application


def start_bot(application: Application) -> None:
    """Run the Telegram polling loop until shutdown."""
    logger.info("正在啟動 Telegram Bot 輪詢…")
    application.run_polling(allowed_updates=Update.ALL_TYPES)
