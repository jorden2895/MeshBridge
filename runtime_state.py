from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Any


STAT_KEYS = (
    "telegram_to_mesh_success",
    "mesh_to_telegram_success",
    "discord_to_mesh_success",
    "mesh_to_discord_success",
    "telegram_to_discord_success",
    "discord_to_telegram_success",
    "unauthorized_dropped",
    "oversized_dropped",
    "decrypt_failed",
    "duplicate_packets",
    "disconnected_dropped",
    "other_dropped",
)
MAX_CHAT_MESSAGES = 200


class ChatApiError(ValueError):
    """A validated, user-safe error that may be returned by the local chat API."""


class RuntimeState:
    def __init__(
        self,
        *,
        statistics_enabled: bool = True,
        secrets_to_redact: tuple[str, ...] = (),
    ) -> None:
        self._lock = threading.RLock()
        self._statistics_enabled = statistics_enabled
        self._secrets = tuple(secret for secret in secrets_to_redact if secret)
        self._started_at = time.time()
        self._generation = time.monotonic_ns()
        self._heartbeat = self._started_at
        self._bridge = {
            "status": "stopped",
            "last_changed": self._started_at,
            "last_error": None,
        }
        self._telegram = {
            "status": "starting",
            "bot_name": None,
            "last_changed": self._started_at,
            "last_error": None,
        }
        self._discord = {
            "status": "disabled",
            "bot_name": None,
            "last_changed": self._started_at,
            "last_error": None,
        }
        self._routes: dict[str, dict[str, Any]] = {}
        self._stats = {key: 0 for key in STAT_KEYS}
        self._last_forwarded_at: float | None = None
        self._messages: deque[dict[str, Any]] = deque(maxlen=MAX_CHAT_MESSAGES)
        self._next_message_id = 1

    def _redact(self, error: str | None) -> str | None:
        if error is None:
            return None
        cleaned = str(error)
        for secret in self._secrets:
            cleaned = cleaned.replace(secret, "***")
        return cleaned[:500]

    def register_route(self, route_id: str, name: str, broker: str) -> None:
        with self._lock:
            self._routes[route_id] = {
                "name": name,
                "broker": broker,
                "mqtt_status": "starting",
                "last_changed": time.time(),
                "last_error": None,
            }

    def set_telegram(
        self,
        status: str,
        *,
        bot_name: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            self._telegram.update(
                status=status,
                last_changed=time.time(),
                last_error=self._redact(error),
            )
            if bot_name is not None:
                self._telegram["bot_name"] = bot_name

    def set_bridge(self, status: str, error: str | None = None) -> None:
        with self._lock:
            self._bridge.update(
                status=status,
                last_changed=time.time(),
                last_error=self._redact(error),
            )

    def set_mqtt(self, route_id: str, status: str, error: str | None = None) -> None:
        with self._lock:
            route = self._routes.get(route_id)
            if route is None:
                return
            route.update(
                mqtt_status=status,
                last_changed=time.time(),
                last_error=self._redact(error),
            )

    def set_discord(
        self,
        status: str,
        *,
        bot_name: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            self._discord.update(
                status=status,
                last_changed=time.time(),
                last_error=self._redact(error),
            )
            if bot_name is not None:
                self._discord["bot_name"] = bot_name

    def increment(self, key: str, amount: int = 1) -> None:
        if not self._statistics_enabled:
            return
        if key not in self._stats:
            raise KeyError(f"Unknown runtime statistic: {key}")
        with self._lock:
            self._stats[key] += amount

    def mark_forwarded(self) -> None:
        with self._lock:
            self._last_forwarded_at = time.time()

    def heartbeat(self) -> None:
        with self._lock:
            self._heartbeat = time.time()

    def record_message(
        self,
        *,
        route_id: str,
        source: str,
        sender: str,
        text: str,
        destinations: tuple[str, ...],
    ) -> dict[str, Any]:
        """Append one in-memory chat event without writing message text to disk."""
        with self._lock:
            message = {
                "id": self._next_message_id,
                "timestamp": time.time(),
                "route_id": route_id,
                "route_name": self._routes.get(route_id, {}).get("name", route_id),
                "source": source,
                "sender": str(sender)[:100],
                "text": str(text)[:4096],
                "destinations": list(destinations),
            }
            self._next_message_id += 1
            self._messages.append(message)
            return dict(message)

    def messages_after(
        self,
        after_id: int = 0,
        generation: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if generation is not None and generation != self._generation:
                after_id = 0
            messages = [dict(message) for message in self._messages if message["id"] > after_id]
            latest_id = self._messages[-1]["id"] if self._messages else 0
            return {
                "heartbeat": self._heartbeat,
                "generation": self._generation,
                "messages": messages,
                "latest_id": latest_id,
            }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "heartbeat": self._heartbeat,
                "generation": self._generation,
                "pid": os.getpid(),
                "started_at": self._started_at,
                "last_forwarded_at": self._last_forwarded_at,
                "bridge": dict(self._bridge),
                "telegram": dict(self._telegram),
                "discord": dict(self._discord),
                "routes": {
                    route_id: dict(route) for route_id, route in self._routes.items()
                },
                "statistics": dict(self._stats),
            }
