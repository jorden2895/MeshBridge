from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass

from telegram import Bot

from config import DEFAULT_BRIDGE_UI_DISPLAY_NAME, RouteConfig
from mqtt_service import MAX_MESHTASTIC_PAYLOAD_BYTES, MqttService, MqttServiceError
from runtime_state import ChatApiError, RuntimeState


logger = logging.getLogger(__name__)
MAX_DISCORD_MESSAGE_CHARS = 2000


@dataclass(frozen=True)
class RouteBinding:
    route: RouteConfig
    mqtt_service: MqttService


async def forward_to_telegram(
    bot: Bot,
    message_text: str,
    chat_id: int,
    topic_id: int | None = None,
) -> None:
    """Send one routed text message through the shared Telegram bot."""
    await bot.send_message(
        chat_id=chat_id,
        text=message_text,
        message_thread_id=topic_id,
    )
    logger.info("Forwarded a routed text message to the configured Telegram chat.")


class LocalChatDispatcher:
    """Send UI chat commands from the local API thread to active routes."""

    def __init__(
        self,
        bindings: tuple[RouteBinding, ...],
        runtime_state: RuntimeState,
        display_name: str = DEFAULT_BRIDGE_UI_DISPLAY_NAME,
    ) -> None:
        self.bindings = {binding.mqtt_service.route_id: binding for binding in bindings}
        self.runtime_state = runtime_state
        self.display_name = display_name
        self._lock = threading.Lock()
        self._telegram_loop = None
        self._telegram_bot = None
        self._discord_sender: Callable[[str, str], Future] | None = None

    def bind_telegram(self, loop, bot: Bot) -> None:
        with self._lock:
            self._telegram_loop = loop
            self._telegram_bot = bot

    def clear_telegram(self) -> None:
        with self._lock:
            self._telegram_loop = None
            self._telegram_bot = None

    # Compatibility aliases for the v1.5 dispatcher interface.
    bind = bind_telegram
    clear = clear_telegram

    def bind_discord(self, sender: Callable[[str, str], Future]) -> None:
        with self._lock:
            self._discord_sender = sender

    def clear_discord(self) -> None:
        with self._lock:
            self._discord_sender = None

    def __call__(self, request: dict) -> dict:
        route_id = str(request.get("route_id", ""))
        target = str(request.get("target", "both")).lower()
        text = str(request.get("text", "")).strip()
        if route_id not in self.bindings:
            raise ChatApiError("找不到指定路由")
        if target not in {"meshtastic", "telegram", "discord", "both", "all"}:
            raise ChatApiError("發送目標無效")
        if not text:
            raise ChatApiError("訊息不可空白")
        if len(text) > 4000:
            raise ChatApiError("訊息過長")

        formatted = f"[{self.display_name}]: {text}"
        if (
            target in {"meshtastic", "both", "all"}
            and len(formatted.encode("utf-8")) > MAX_MESHTASTIC_PAYLOAD_BYTES
        ):
            raise ChatApiError(
                f"包含 UI 標記後超過 Meshtastic {MAX_MESHTASTIC_PAYLOAD_BYTES} bytes 上限"
            )
        if target in {"discord", "all"} and len(formatted) > MAX_DISCORD_MESSAGE_CHARS:
            raise ChatApiError(
                f"包含 UI 標記後超過 Discord {MAX_DISCORD_MESSAGE_CHARS} 字元上限"
            )

        binding = self.bindings[route_id]
        sent: list[str] = []
        errors: dict[str, str] = {}
        if target in {"meshtastic", "both", "all"}:
            try:
                if binding.mqtt_service.send_message(formatted):
                    sent.append("meshtastic")
                else:
                    errors["meshtastic"] = "訊息超過 Meshtastic 長度限制"
            except Exception:
                logger.exception("Bridge UI failed to send a message to Meshtastic.")
                errors["meshtastic"] = "Meshtastic 傳送失敗"

        if target in {"telegram", "both", "all"}:
            with self._lock:
                loop = self._telegram_loop
                bot = self._telegram_bot
            if loop is None or loop.is_closed() or bot is None:
                errors["telegram"] = "Telegram 尚未就緒"
            else:
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        forward_to_telegram(
                            bot,
                            formatted,
                            binding.route.target_chat_id,
                            binding.route.topic_id,
                        ),
                        loop,
                    )
                    future.result(timeout=15)
                    sent.append("telegram")
                except Exception:
                    logger.exception("Bridge UI failed to send a message to Telegram.")
                    errors["telegram"] = "Telegram 傳送失敗"

        if target in {"discord", "all"}:
            channel_id = binding.route.discord_channel_id
            with self._lock:
                discord_sender = self._discord_sender
            if channel_id is None:
                errors["discord"] = "此路由未設定 Discord 頻道"
            elif discord_sender is None:
                errors["discord"] = "Discord 尚未就緒"
            else:
                try:
                    discord_sender(channel_id, formatted).result(timeout=15)
                    sent.append("discord")
                except Exception:
                    logger.exception("Bridge UI failed to send a message to Discord.")
                    errors["discord"] = "Discord 傳送失敗"

        if not sent:
            raise ChatApiError("；".join(errors.values()) or "訊息無法傳送")
        self.runtime_state.record_message(
            route_id=route_id,
            source="bridge_ui",
            sender=self.display_name,
            text=text,
            destinations=tuple(sent),
        )
        return {"sent": sent, "errors": errors}


class BridgeRouter(LocalChatDispatcher):
    """Route text between platform adapters without owning their lifecycle."""

    def forward_meshtastic(self, route_id: str, message_text: str) -> None:
        binding = self.bindings.get(route_id)
        if binding is None:
            logger.error("Cannot route Meshtastic message for unknown route %s.", route_id)
            self.runtime_state.increment("other_dropped")
            return
        with self._lock:
            loop = self._telegram_loop
            bot = self._telegram_bot
        if loop is None or loop.is_closed() or bot is None:
            logger.error("Telegram event loop is unavailable; dropping Meshtastic message.")
            self.runtime_state.increment("other_dropped")
        else:
            future = asyncio.run_coroutine_threadsafe(
                forward_to_telegram(
                    bot,
                    message_text,
                    binding.route.target_chat_id,
                    binding.route.topic_id,
                ),
                loop,
            )

            def record_result(completed) -> None:
                try:
                    completed.result()
                    self.runtime_state.increment("mesh_to_telegram_success")
                    self.runtime_state.mark_forwarded()
                except Exception as exc:
                    logger.exception("Failed to forward Meshtastic message to Telegram.")
                    self.runtime_state.set_telegram("error", error=str(exc))
                    self.runtime_state.increment("other_dropped")

            future.add_done_callback(record_result)

        channel_id = binding.route.discord_channel_id
        with self._lock:
            discord_sender = self._discord_sender
        if channel_id is not None and discord_sender is not None:
            try:
                discord_future = discord_sender(channel_id, message_text)

                def record_discord_result(completed) -> None:
                    try:
                        completed.result()
                        self.runtime_state.increment("mesh_to_discord_success")
                        self.runtime_state.mark_forwarded()
                    except Exception as exc:
                        logger.exception("Failed to forward Meshtastic message to Discord.")
                        self.runtime_state.set_discord("error", error=str(exc))
                        self.runtime_state.increment("other_dropped")

                discord_future.add_done_callback(record_discord_result)
            except Exception as exc:
                logger.exception("Discord is unavailable; dropping routed message.")
                self.runtime_state.set_discord("error", error=str(exc))
                self.runtime_state.increment("other_dropped")

    async def forward_telegram(
        self,
        binding: RouteBinding,
        *,
        user_id: int,
        username: str | None,
        text: str,
    ) -> bool:
        formatted_text = f"[TG:{user_id}]: {text}"
        sent = False
        destinations: list[str] = []
        mqtt_error: MqttServiceError | None = None
        try:
            try:
                sent = await asyncio.to_thread(binding.mqtt_service.send_message, formatted_text)
                if sent:
                    destinations.append("meshtastic")
                    self.runtime_state.increment("telegram_to_mesh_success")
            except MqttServiceError as exc:
                mqtt_error = exc

            channel_id = binding.route.discord_channel_id
            with self._lock:
                discord_sender = self._discord_sender
            if channel_id is not None and discord_sender is not None:
                try:
                    await asyncio.wrap_future(discord_sender(channel_id, formatted_text))
                    destinations.append("discord")
                    self.runtime_state.increment("telegram_to_discord_success")
                except Exception as exc:
                    logger.exception("Failed to forward Telegram message to Discord.")
                    self.runtime_state.set_discord("error", error=str(exc))
                    self.runtime_state.increment("other_dropped")
            if destinations:
                self.runtime_state.mark_forwarded()
            if mqtt_error is not None:
                raise mqtt_error
            return sent
        finally:
            self.runtime_state.record_message(
                route_id=binding.mqtt_service.route_id,
                source="telegram",
                sender=f"@{username}" if username else f"TG:{user_id}",
                text=text,
                destinations=tuple(destinations),
            )

    async def forward_discord(
        self,
        route_id: str,
        user_id: int,
        username: str,
        text: str,
    ) -> None:
        binding = self.bindings.get(route_id)
        if binding is None:
            self.runtime_state.increment("other_dropped")
            return
        identity = f"@{username}" if username else str(user_id)
        formatted_text = f"[DC:{identity}]: {text}"
        destinations: list[str] = []
        try:
            if await asyncio.to_thread(binding.mqtt_service.send_message, formatted_text):
                destinations.append("meshtastic")
                self.runtime_state.increment("discord_to_mesh_success")
        except MqttServiceError:
            logger.exception("Failed to forward Discord message to Meshtastic.")
            self.runtime_state.increment("other_dropped")

        with self._lock:
            loop = self._telegram_loop
            bot = self._telegram_bot
        if loop is not None and not loop.is_closed() and bot is not None:
            try:
                telegram_future = asyncio.run_coroutine_threadsafe(
                    forward_to_telegram(
                        bot,
                        formatted_text,
                        binding.route.target_chat_id,
                        binding.route.topic_id,
                    ),
                    loop,
                )
                await asyncio.wrap_future(telegram_future)
                destinations.append("telegram")
                self.runtime_state.increment("discord_to_telegram_success")
            except Exception:
                logger.exception("Failed to forward Discord message to Telegram.")
                self.runtime_state.increment("other_dropped")
        if destinations:
            self.runtime_state.mark_forwarded()
        self.runtime_state.record_message(
            route_id=route_id,
            source="discord",
            sender=identity,
            text=text,
            destinations=tuple(destinations),
        )
