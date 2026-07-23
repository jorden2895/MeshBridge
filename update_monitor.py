from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from update_service import (
    download_portable_release,
    fetch_latest_release,
    is_newer,
    record_check,
    schedule_portable_install,
    should_check,
)


logger = logging.getLogger(__name__)


class UpdateMonitor:
    def __init__(
        self,
        *,
        current_version: str,
        mode: str,
        interval_hours: int,
        application_directory: Path,
        notify: Callable[[str, str], None],
        stop_application: Callable[[], None],
    ) -> None:
        self.current_version = current_version
        self.mode = mode
        self.interval_hours = interval_hours
        self.application_directory = application_directory
        self.notify = notify
        self.stop_application = stop_application
        self.state_path = application_directory / ".meshtelegram-update-state.json"
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="update-monitor",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if should_check(self.state_path, self.interval_hours):
                self._check_once()
            self._stop_event.wait(min(self.interval_hours * 3600, 3600))

    def _check_once(self) -> None:
        try:
            release = fetch_latest_release()
            record_check(self.state_path)
            if not is_newer(release, self.current_version):
                return
            logger.info("發現新的正式 Release：%s", release.version)
            if self.mode == "notify":
                self.notify(
                    "MeshTelegram Bridge 更新",
                    f"發現正式版本 {release.version}，請開啟設定工具查看。",
                )
                return
            files = download_portable_release(release, self.application_directory)
            if self.mode == "download":
                self.notify(
                    "MeshTelegram Bridge 更新",
                    f"{release.version} 已完成驗證並下載至 .update 資料夾。",
                )
                return
            schedule_portable_install(files, self.application_directory)
            self.notify(
                "MeshTelegram Bridge 更新",
                f"{release.version} 已下載，Bridge 將重新啟動以完成更新。",
            )
            self.stop_application()
        except Exception:
            logger.exception("自動更新檢查失敗；將於下個檢查週期重試。")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
