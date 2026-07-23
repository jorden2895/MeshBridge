from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


STAT_KEYS = (
    "telegram_to_mesh_success",
    "mesh_to_telegram_success",
    "unauthorized_dropped",
    "oversized_dropped",
    "decrypt_failed",
    "duplicate_packets",
    "disconnected_dropped",
    "other_dropped",
)


def _atomic_json_write(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


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
        self._heartbeat = self._started_at
        self._telegram = {
            "status": "starting",
            "bot_name": None,
            "last_changed": self._started_at,
            "last_error": None,
        }
        self._routes: dict[str, dict[str, Any]] = {}
        self._stats = {key: 0 for key in STAT_KEYS}
        self._last_forwarded_at: float | None = None

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

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "heartbeat": self._heartbeat,
                "pid": os.getpid(),
                "started_at": self._started_at,
                "last_forwarded_at": self._last_forwarded_at,
                "telegram": dict(self._telegram),
                "routes": {
                    route_id: dict(route) for route_id, route in self._routes.items()
                },
                "statistics": dict(self._stats),
            }


@dataclass(frozen=True)
class StatusApiLocation:
    host: str
    port: int
    token: str
    pid: int


class StatusApiServer:
    def __init__(self, state: RuntimeState, discovery_path: Path) -> None:
        self.state = state
        self.discovery_path = discovery_path
        self.token = secrets.token_urlsafe(32)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> StatusApiLocation:
        state = self.state
        token = self.token

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
                if self.path != "/status":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if self.headers.get("Authorization") != f"Bearer {token}":
                    self.send_error(HTTPStatus.UNAUTHORIZED)
                    return
                payload = json.dumps(
                    state.snapshot(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        port = int(self._server.server_address[1])
        location = StatusApiLocation("127.0.0.1", port, self.token, os.getpid())
        _atomic_json_write(
            self.discovery_path,
            {
                "host": location.host,
                "port": location.port,
                "token": location.token,
                "pid": location.pid,
                "created_at": time.time(),
            },
        )
        try:
            os.chmod(self.discovery_path, 0o600)
        except OSError:
            pass
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="status-api",
            daemon=True,
        )
        self._thread.start()
        return location

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        try:
            self.discovery_path.unlink(missing_ok=True)
        except OSError:
            pass
