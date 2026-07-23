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


def fetch_status(
    discovery_path: Path,
    *,
    timeout: float = 1.5,
    now: float | None = None,
) -> dict[str, Any]:
    try:
        discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
        host = str(discovery["host"])
        port = int(discovery["port"])
        token = str(discovery["token"])
    except (FileNotFoundError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise StatusUnavailable("Bridge 未執行") from exc

    if host != "127.0.0.1":
        raise StatusUnavailable("狀態 API 位址無效")
    if not 1 <= port <= 65535 or not token:
        raise StatusUnavailable("狀態 API 連線資訊無效")

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

    heartbeat = float(data.get("heartbeat", 0))
    current = time.time() if now is None else now
    if current - heartbeat > OFFLINE_AFTER_SECONDS:
        raise StatusUnavailable("Bridge heartbeat 已逾時")
    return data
