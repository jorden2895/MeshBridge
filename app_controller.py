from __future__ import annotations

import copy
import logging
import queue
import threading
from pathlib import Path
from typing import Any

from bridge_runtime import BridgeRuntime
from config import AppConfig, ConfigError
from config_store import load_config_data, save_config_atomic
from tray_service import sync_autostart
from update_monitor import UpdateMonitor
from update_service import fetch_latest_release, is_newer
from version import __version__


logger = logging.getLogger(__name__)


class AppController:
    """Coordinate configuration, runtime workers, and GUI-safe events."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.raw_config: dict[str, Any] | None = None
        self.config: AppConfig | None = None
        self.runtime: BridgeRuntime | None = None
        self._operation_lock = threading.Lock()
        self._shutting_down = False
        self.update_monitor: UpdateMonitor | None = None
        self._notify = lambda title, message: self.events.put(("notice", f"{title}：{message}"))
        self._exit_for_update = lambda: self.events.put(("exit_for_update", None))
        self._logging_config = lambda config: None
        self._status_changed = lambda: None

    def configure_update_hooks(self, notify, exit_for_update) -> None:
        self._notify = notify
        self._exit_for_update = exit_for_update

    def configure_logging_hook(self, update_logging) -> None:
        self._logging_config = update_logging

    def configure_status_hook(self, status_changed) -> None:
        self._status_changed = status_changed

    def _emit_status_changed(self) -> None:
        try:
            self._status_changed()
        except Exception:
            logger.exception("Unable to refresh service status consumer.")

    def _stop_updates(self) -> None:
        if self.update_monitor is not None:
            self.update_monitor.stop()
            self.update_monitor = None

    def _start_updates(self) -> None:
        self._stop_updates()
        if self.config is None or not self.config.features.updates.enabled:
            return
        self.update_monitor = UpdateMonitor(
            current_version=__version__,
            mode=self.config.features.updates.mode,
            interval_hours=self.config.features.updates.interval_hours,
            application_directory=self.config_path.parent,
            notify=self._notify,
            stop_application=self._exit_for_update,
        )
        self.update_monitor.start()

    def load(self) -> tuple[dict[str, Any] | None, str | None]:
        try:
            raw, migrated = load_config_data(self.config_path)
            config = AppConfig.from_dict(raw)
        except ConfigError as exc:
            return None, str(exc)
        self.raw_config = raw
        self.config = config
        if migrated:
            self.events.put(("notice", "已備份並升級舊版設定為 v4 格式。"))
        return copy.deepcopy(raw), None

    def _runtime_listener(self, snapshot: dict) -> None:
        self.events.put(("runtime", snapshot))
        self._emit_status_changed()

    def _replace_runtime(self, config: AppConfig) -> BridgeRuntime:
        runtime = BridgeRuntime(config, self._runtime_listener)
        self.runtime = runtime
        return runtime

    @property
    def running(self) -> bool:
        return self.runtime is not None and self.runtime.running

    @property
    def operation_running(self) -> bool:
        return self._operation_lock.locked()

    @property
    def service_status(self) -> str:
        snapshot = self.snapshot()
        if snapshot is None:
            return "stopped"
        return str(snapshot.get("bridge", {}).get("status", "stopped"))

    def can_start(self) -> bool:
        return not self.operation_running and self.service_status in {"stopped", "error"}

    def can_stop(self) -> bool:
        return not self.operation_running and self.service_status in {"starting", "running", "error"}

    def can_restart(self) -> bool:
        return not self.operation_running and self.service_status in {"running", "error"}

    def snapshot(self) -> dict | None:
        return self.runtime.snapshot() if self.runtime is not None else None

    def messages_after(self, after_id: int, generation: int | None = None) -> dict:
        if self.runtime is None:
            return {"messages": [], "latest_id": after_id, "generation": generation}
        return self.runtime.messages_after(after_id, generation)

    def send(self, payload: dict) -> dict:
        if self.runtime is None:
            raise RuntimeError("Bridge 尚未啟動")
        return self.runtime.send(payload)

    def _run_operation(self, name: str, operation) -> None:
        def worker() -> None:
            if not self._operation_lock.acquire(blocking=False):
                self.events.put(("notice", "另一項 Bridge 操作正在進行中。"))
                return
            self.events.put(("operation", {"name": name, "running": True}))
            self._emit_status_changed()
            try:
                result = operation()
                self.events.put(("operation", {"name": name, "running": False, "ok": True, "result": result}))
            except Exception as exc:
                logger.exception("Bridge operation failed: %s", name)
                self.events.put(("operation", {"name": name, "running": False, "ok": False, "error": str(exc)}))
            finally:
                self._operation_lock.release()
                self._emit_status_changed()

        threading.Thread(target=worker, name=f"controller-{name}", daemon=True).start()

    def start_async(self) -> None:
        def start() -> None:
            if self.config is None:
                raise ConfigError("請先完成並儲存設定")
            if self.runtime is None:
                self._replace_runtime(self.config)
            self.runtime.start()
            self._start_updates()

        self._run_operation("start", start)

    def stop_async(self) -> None:
        def stop() -> None:
            self._stop_updates()
            if self.runtime:
                self.runtime.stop()
        self._run_operation("stop", stop)

    def restart_async(self) -> None:
        def restart() -> None:
            if self.config is None:
                raise ConfigError("請先完成並儲存設定")
            if self.runtime is None:
                self._replace_runtime(self.config).start()
            else:
                self.runtime.restart(self.config)
            self._start_updates()

        self._run_operation("restart", restart)

    def check_updates_async(self) -> None:
        def check() -> str:
            release = fetch_latest_release()
            if is_newer(release, __version__):
                self._notify(
                    "MeshBridge 更新",
                    f"發現正式版本 {release.version}。{release.page_url}",
                )
                return f"發現新版本 {release.version}"
            return "目前已是最新正式版本"

        self._run_operation("check-update", check)

    def send_eew_async(self, intensity: str, seconds: str | int) -> None:
        def send_eew() -> str:
            if self.config is None:
                raise ConfigError("尚未完成設定，無法發送 EEW")
            if self.runtime is None:
                self._replace_runtime(self.config)
            if not self.runtime.running:
                self.runtime.start()
                self._start_updates()
            result = self.runtime.send_eew(intensity, seconds)
            if result.get("duplicate"):
                logger.info("已忽略短時間內重複的 EEW。")
                return "已忽略短時間內重複的 EEW"
            logger.info(
                "EEW 已發送至 %s 個目的地；失敗 %s 個。",
                len(result.get("sent", [])),
                len(result.get("errors", {})),
            )
            return f"EEW 已發送至 {len(result.get('sent', []))} 個目的地"

        self._run_operation("eew", send_eew)

    def apply_async(self, new_raw: dict[str, Any]) -> None:
        new_raw = copy.deepcopy(new_raw)

        def apply() -> str:
            new_config = AppConfig.from_dict(new_raw)
            old_raw = copy.deepcopy(self.raw_config)
            old_config = self.config
            was_running = self.running
            if self.runtime is not None:
                self.runtime.stop()
            self._stop_updates()
            try:
                self._logging_config(new_config)
                self._replace_runtime(new_config).start()
                save_config_atomic(new_raw, self.config_path)
                sync_autostart(new_config.features.autostart)
                self.raw_config = copy.deepcopy(new_raw)
                self.config = new_config
                self._start_updates()
                return "設定已儲存並套用。"
            except Exception as exc:
                logger.exception("New configuration failed; restoring previous configuration.")
                if self.runtime is not None:
                    self.runtime.stop()
                if old_raw is not None and old_config is not None:
                    self.raw_config = old_raw
                    self.config = old_config
                    self._logging_config(old_config)
                    try:
                        save_config_atomic(old_raw, self.config_path)
                    except OSError:
                        logger.exception("無法將舊設定寫回設定檔。")
                    try:
                        sync_autostart(old_config.features.autostart)
                    except OSError:
                        logger.exception("無法還原 Windows 開機自動啟動設定。")
                    self._replace_runtime(old_config)
                    if was_running:
                        self.runtime.start()
                        self._start_updates()
                else:
                    self.raw_config = None
                    self.config = None
                    self.runtime = None
                raise RuntimeError(f"新設定無法啟動，已還原舊設定：{exc}") from exc

        self._run_operation("apply", apply)

    def shutdown(self) -> None:
        self._shutting_down = True
        self._stop_updates()
        if self.runtime is not None:
            self.runtime.stop()
