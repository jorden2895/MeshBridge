from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Future

import discord


logger = logging.getLogger(__name__)
DiscordTextHandler = Callable[[str, int, str, str], Awaitable[None]]
DiscordStatusHandler = Callable[[str, str | None, str | None], None]


class DiscordServiceError(RuntimeError):
    """Raised when the Discord adapter cannot start or send text."""


class DiscordBridge:
    """Own a Discord client on a dedicated asyncio thread."""

    def __init__(
        self,
        bot_token: str,
        channel_routes: dict[str, str],
        on_text: DiscordTextHandler,
        *,
        startup_timeout: float = 20.0,
        on_status: DiscordStatusHandler | None = None,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        self.client = discord.Client(intents=intents)
        self.bot_token = bot_token
        self.channel_routes = dict(channel_routes)
        self.on_text = on_text
        self.startup_timeout = startup_timeout
        self.on_status = on_status
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._startup_error: Exception | None = None
        self._install_events()

    def _install_events(self) -> None:
        @self.client.event
        async def on_ready() -> None:
            identity = self.client.user
            logger.info("Discord Bot 啟動成功：%s", identity)
            if self.on_status is not None:
                self.on_status("connected", str(identity), None)
            self._ready.set()

        @self.client.event
        async def on_disconnect() -> None:
            if self.on_status is not None and not self._stopped.is_set():
                self.on_status("reconnecting", None, "Discord 連線中斷，正在重新連線")

        @self.client.event
        async def on_resumed() -> None:
            if self.on_status is not None:
                self.on_status("connected", str(self.client.user), None)

        @self.client.event
        async def on_message(message: discord.Message) -> None:
            if message.author.bot or not message.content:
                return
            route_id = self.channel_routes.get(str(message.channel.id))
            if route_id is None:
                return
            await self.on_text(
                route_id,
                message.author.id,
                message.author.name,
                message.content,
            )

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            await self.client.start(self.bot_token, reconnect=True)
        except Exception as exc:
            self._startup_error = exc
            if self.on_status is not None:
                self.on_status("error", None, str(exc))
            self._ready.set()
            if not self._stopped.is_set():
                logger.exception("Discord Bot 已停止。")
        finally:
            self._stopped.set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self._run()),
            name="discord-bridge",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(self.startup_timeout):
            self.stop()
            raise DiscordServiceError("Discord Bot 連線逾時")
        if self._startup_error is not None:
            error = self._startup_error
            self.stop()
            raise DiscordServiceError(f"Discord Bot 啟動失敗：{error}") from error

    async def _send_text(self, channel_id: str, text: str) -> None:
        channel = self.client.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.client.fetch_channel(int(channel_id))
            except Exception as exc:
                raise DiscordServiceError(f"找不到 Discord 頻道 {channel_id}") from exc
        if not hasattr(channel, "send"):
            raise DiscordServiceError(f"Discord 頻道 {channel_id} 不支援文字訊息")
        await channel.send(text)

    def schedule_text(self, channel_id: str, text: str) -> Future:
        loop = self._loop
        if loop is None or loop.is_closed() or not self._ready.is_set():
            raise DiscordServiceError("Discord 尚未就緒")
        return asyncio.run_coroutine_threadsafe(self._send_text(channel_id, text), loop)

    def send_text(self, channel_id: str, text: str) -> None:
        future = self.schedule_text(channel_id, text)
        try:
            future.result(timeout=15)
        except Exception as exc:
            future.cancel()
            if isinstance(exc, DiscordServiceError):
                raise
            raise DiscordServiceError(f"Discord 訊息傳送失敗：{exc}") from exc

    def stop(self) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed() and not self.client.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(self.client.close(), loop).result(timeout=10)
            except Exception:
                logger.exception("關閉 Discord Bot 時發生錯誤。")
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
