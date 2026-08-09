from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


OFFLINE_AFTER_SECONDS = 5


class StatusUnavailable(RuntimeError):
    pass


class ChatSendError(RuntimeError):
    pass


def _read_discovery(discovery_path: Path) -> tuple[int, str]:
    try:
        discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
        host = str(discovery["host"])
        port = int(discovery["port"])
        token = str(discovery["token"])
    except (FileNotFoundError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise StatusUnavailable("Bridge 未執行") from exc
    if host != "127.0.0.1" or not 1 <= port <= 65535 or not token:
        raise StatusUnavailable("狀態 API 連線資訊無效")
    return port, token


def _ensure_fresh(data: dict[str, Any], now: float | None = None) -> None:
    heartbeat = float(data.get("heartbeat", 0))
    current = time.time() if now is None else now
    if current - heartbeat > OFFLINE_AFTER_SECONDS:
        raise StatusUnavailable("Bridge heartbeat 已逾時")


def fetch_status(
    discovery_path: Path,
    *,
    timeout: float = 1.5,
    now: float | None = None,
) -> dict[str, Any]:
    port, token = _read_discovery(discovery_path)

    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        # The URL is constructed locally after enforcing loopback and a valid port.
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            data = json.load(response)
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise StatusUnavailable("Bridge 狀態 API 無法連線") from exc

    _ensure_fresh(data, now)
    return data


def fetch_messages(
    discovery_path: Path,
    *,
    after_id: int = 0,
    timeout: float = 1.5,
) -> dict[str, Any]:
    port, token = _read_discovery(discovery_path)
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/messages?after={max(0, int(after_id))}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            data = json.load(response)
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise StatusUnavailable("無法讀取聊天訊息") from exc
    _ensure_fresh(data)
    return data


def send_chat_message(
    discovery_path: Path,
    *,
    route_id: str,
    text: str,
    target: str,
    timeout: float = 15,
) -> dict[str, Any]:
    port, token = _read_discovery(discovery_path)
    body = json.dumps(
        {"route_id": route_id, "text": text, "target": target},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/send",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            error = json.load(exc).get("error", "訊息無法傳送")
        except (ValueError, json.JSONDecodeError):
            error = "訊息無法傳送"
        raise ChatSendError(str(error)) from exc
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise ChatSendError("Bridge 聊天 API 無法連線") from exc
