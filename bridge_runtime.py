from __future__ import annotations

import logging
import base64
import threading
import time
from functools import partial
from typing import Callable

from bridge_router import BridgeRouter, RouteBinding
from config import AppConfig
from discord_bridge import DiscordBridge
from mqtt_service import MqttService
from runtime_state import RuntimeState
from telegram_bridge import create_application, start_bot


logger = logging.getLogger(__name__)
RuntimeListener = Callable[[dict], None]


class BridgeRuntimeError(RuntimeError):
    pass


class BridgeRuntime:
    """Own all bridge adapters and expose one restartable lifecycle to the GUI."""

    def __init__(self, config: AppConfig, listener: RuntimeListener | None = None) -> None:
        self.config = config
        self.listener = listener
        self.state = self._new_state(config)
        self.router: BridgeRouter | None = None
        self.bindings: tuple[RouteBinding, ...] = ()
        self.discord_service: DiscordBridge | None = None
        self.telegram_app = None
        self._telegram_thread: threading.Thread | None = None
        self._telegram_error: Exception | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._running = False

    @staticmethod
    def _new_state(config: AppConfig) -> RuntimeState:
        secrets = (
            config.telegram.bot_token,
            config.discord.bot_token,
            config.mqtt.password,
            *(base64.b64encode(route.channel_key).decode("ascii") for route in config.routes),
        )
        return RuntimeState(
            statistics_enabled=config.features.statistics_enabled,
            secrets_to_redact=secrets,
        )

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def snapshot(self) -> dict:
        return self.state.snapshot()

    def messages_after(self, after_id: int = 0) -> dict:
        return self.state.messages_after(after_id)

    def send(self, payload: dict) -> dict:
        router = self.router
        if not self.running or router is None:
            raise BridgeRuntimeError("Bridge 尚未啟動")
        return router(payload)

    def _notify(self) -> None:
        if self.listener is not None:
            try:
                self.listener(self.snapshot())
            except Exception:
                logger.exception("Runtime listener failed.")

    def _wait_telegram_ready(self, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.state.snapshot()["telegram"]["status"]
            if status == "connected":
                return
            if self._telegram_error is not None:
                raise BridgeRuntimeError(
                    f"Telegram Bot 啟動失敗：{self._telegram_error}"
                ) from self._telegram_error
            if self._telegram_thread is not None and not self._telegram_thread.is_alive():
                raise BridgeRuntimeError("Telegram Bot 在完成啟動前停止")
            time.sleep(0.1)
        raise BridgeRuntimeError("Telegram Bot 連線逾時")

    def _start_telegram(self) -> None:
        self.state.set_telegram("starting")
        self.telegram_app = create_application(
            self.config.telegram.bot_token,
            self.bindings,
            runtime_state=self.state,
            ui_display_name=self.config.bridge_ui.display_name,
            router=self.router,
        )

        def run() -> None:
            try:
                start_bot(self.telegram_app)
            except Exception as exc:
                self._telegram_error = exc
                self.state.set_telegram("error", error=str(exc))
                logger.exception("Telegram Bot 已停止。")
            finally:
                self._notify()

        self._telegram_thread = threading.Thread(
            target=run,
            name="telegram-bridge",
            daemon=True,
        )
        self._telegram_thread.start()
        self._wait_telegram_ready()

    def _start_discord(self) -> None:
        if not self.config.discord.enabled:
            self.state.set_discord("disabled")
            return
        self.state.set_discord("starting")
        channel_routes = {
            route.discord_channel_id: binding.mqtt_service.route_id
            for route, binding in zip(self.config.active_routes, self.bindings)
            if route.discord_enabled and route.discord_channel_id is not None
        }

        def update(status: str, bot_name: str | None, error: str | None) -> None:
            self.state.set_discord(status, bot_name=bot_name, error=error)
            self._notify()

        self.discord_service = DiscordBridge(
            self.config.discord.bot_token,
            channel_routes,
            self.router.forward_discord,
            on_status=update,
        )
        self.router.bind_discord(self.discord_service.schedule_text)
        self.discord_service.start()

    def _fatal_mqtt(self, error: str) -> None:
        logger.error("MQTT 發生致命錯誤：%s", error)
        self.state.set_bridge("error", error)
        self._notify()
        if self.running:
            threading.Thread(target=self.stop, name="fatal-stop", daemon=True).start()

    def _start_mqtt(self) -> None:
        started: list[RouteBinding] = []
        try:
            for binding in self.bindings:
                binding.mqtt_service.set_message_callback(
                    partial(self.router.forward_meshtastic, binding.mqtt_service.route_id)
                )
                binding.mqtt_service.set_fatal_callback(self._fatal_mqtt)
                binding.mqtt_service.start()
                started.append(binding)
        except Exception:
            for binding in reversed(started):
                binding.mqtt_service.stop()
            raise

    def _heartbeat(self) -> None:
        while not self._stop_event.wait(1):
            self.state.heartbeat()
            self._notify()

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._stop_event.clear()
            self._telegram_error = None
            self.state = self._new_state(self.config)
            self.state.set_bridge("starting")
            self.bindings = tuple(
                RouteBinding(
                    route,
                    MqttService(
                        self.config,
                        route=route,
                        runtime_state=self.state,
                        route_id=f"route-{index}",
                    ),
                )
                for index, route in enumerate(self.config.active_routes, start=1)
            )
            self.router = BridgeRouter(
                self.bindings,
                self.state,
                self.config.bridge_ui.display_name,
            )
        try:
            if self.config.telegram.enabled:
                self._start_telegram()
            else:
                self.state.set_telegram("disabled")
            self._start_discord()
            self._start_mqtt()
            with self._lock:
                self._running = True
            self.state.set_bridge("running")
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat,
                name="bridge-heartbeat",
                daemon=True,
            )
            self._heartbeat_thread.start()
            logger.info("MeshBridge 已就緒，可以開始轉發訊息。")
            self._notify()
        except Exception as exc:
            self.state.set_bridge("error", str(exc))
            self._notify()
            self.stop()
            raise BridgeRuntimeError(str(exc)) from exc

    def stop(self) -> None:
        with self._lock:
            resources_exist = bool(
                self.bindings or self.telegram_app is not None or self.discord_service is not None
            )
            if not self._running and not resources_exist:
                self.state.set_bridge("stopped")
                return
            self._running = False
            self._stop_event.set()
            self.state.set_bridge("stopping")
        if self.discord_service is not None:
            if self.router is not None:
                self.router.clear_discord()
            try:
                self.discord_service.stop()
            except Exception:
                logger.exception("關閉 Discord Bot 時發生錯誤。")
            self.discord_service = None
        if self.telegram_app is not None:
            try:
                self.telegram_app.bot_data["request_stop"]()
            except Exception:
                logger.exception("要求 Telegram Bot 停止時發生錯誤。")
            if self._telegram_thread is not None:
                self._telegram_thread.join(timeout=15)
            self.telegram_app = None
            self._telegram_thread = None
        for binding in reversed(self.bindings):
            try:
                binding.mqtt_service.stop()
            except Exception:
                logger.exception("關閉 MQTT 路由時發生錯誤。")
        if self._heartbeat_thread is not None and self._heartbeat_thread is not threading.current_thread():
            self._heartbeat_thread.join(timeout=2)
        self._heartbeat_thread = None
        self.bindings = ()
        self.router = None
        self.state.set_bridge("stopped")
        self._notify()

    def restart(self, config: AppConfig | None = None) -> None:
        self.stop()
        if config is not None:
            self.config = config
        self.start()
