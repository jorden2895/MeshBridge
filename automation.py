from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime

from croniter import croniter

from config import AutomationConfig, EewConfig


logger = logging.getLogger(__name__)
EEW_INTENSITIES = {"1", "2", "3", "4", "5-", "5+", "6-", "6+", "7"}


def normalize_eew(intensity: str, seconds: str | int) -> tuple[str, int]:
    normalized = str(intensity).strip()
    if normalized not in EEW_INTENSITIES:
        raise ValueError("預估震度必須是 1～4、5-、5+、6-、6+ 或 7")
    if not re.fullmatch(r"\d+", str(seconds).strip()):
        raise ValueError("抵達秒數必須是非負整數")
    arrival = int(seconds)
    if arrival > 3600:
        raise ValueError("抵達秒數不可超過 3600")
    return normalized, arrival


def format_eew_message(intensity: str, seconds: str | int) -> str:
    normalized, arrival = normalize_eew(intensity, seconds)
    display = normalized.replace("-", "弱").replace("+", "強")
    return (
        f"⚠ 地震速報：預估震度{display}，約{arrival}秒後抵達。"
        "來源：地牛"
    )


class AutomationEngine:
    """Own keyword matching, EEW deduplication, and minute-based schedules."""

    def __init__(self, config: AutomationConfig, router) -> None:
        self.config = config
        self.router = router
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._eew_lock = threading.Lock()
        self._last_eew: tuple[tuple[str, int], float] | None = None

    def keyword_response(self, route_name: str, text: str) -> str | None:
        candidate = text.strip().casefold()
        route_key = route_name.casefold()
        for rule in self.config.keyword_rules:
            if not rule.enabled or route_key not in {name.casefold() for name in rule.routes}:
                continue
            keyword = rule.keyword.strip().casefold()
            matched = candidate == keyword if rule.match == "exact" else keyword in candidate
            if matched:
                logger.info("關鍵字規則已觸發：%s", rule.name)
                return rule.response
        return None

    def send_eew(self, intensity: str, seconds: str | int) -> dict:
        normalized = normalize_eew(intensity, seconds)
        eew: EewConfig = self.config.eew
        if not eew.enabled:
            raise RuntimeError("EEW 自動發訊尚未啟用")
        now = time.monotonic()
        with self._eew_lock:
            if (
                self._last_eew is not None
                and self._last_eew[0] == normalized
                and now - self._last_eew[1] < eew.dedupe_seconds
            ):
                return {"duplicate": True, "sent": [], "errors": {}}
            self._last_eew = (normalized, now)
        return self.router.send_to_routes(
            eew.routes,
            format_eew_message(*normalized),
            source="eew",
        )

    @staticmethod
    def next_run(expression: str, base: datetime | None = None) -> datetime:
        current = base or datetime.now().astimezone()
        return croniter(expression, current).get_next(datetime)

    def start(self) -> None:
        if self._thread is not None or not any(item.enabled for item in self.config.schedules):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_schedules, name="automation-cron", daemon=True)
        self._thread.start()

    def _run_schedules(self) -> None:
        enabled = [(index, item) for index, item in enumerate(self.config.schedules) if item.enabled]
        initial = datetime.now().astimezone()
        last_slots = {
            index: initial.strftime("%Y-%m-%dT%H:%M%z") for index, _item in enabled
        }
        while not self._stop.wait(1):
            now = datetime.now().astimezone()
            slot = now.strftime("%Y-%m-%dT%H:%M%z")
            for index, item in enabled:
                if last_slots[index] == slot:
                    continue
                last_slots[index] = slot
                if not croniter.match(item.cron, now):
                    continue
                try:
                    self.router.send_to_routes(item.routes, item.message, source="schedule")
                    logger.info("排程訊息已執行：%s", item.name)
                except Exception:
                    logger.exception("排程訊息發送失敗：%s", item.name)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None
