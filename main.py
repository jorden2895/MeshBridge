# MeshBridge entry point

import argparse
import base64
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app_controller import AppController
from app_paths import application_dir
from app_ui import MeshBridgeWindow
from log_buffer import InMemoryLogHandler
from single_instance import SingleInstance
from tray_service import (
    TrayService,
    set_console_visible,
    sync_autostart,
)
from version import __version__


logger = logging.getLogger(__name__)
LOG_FILENAME = "MeshBridge.log"
LOG_MAX_BYTES = 1_048_576
LOG_BACKUP_COUNT = 5
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, secrets: tuple[str, ...] = ()) -> None:
        super().__init__(fmt)
        self._lock = threading.RLock()
        self.secrets = tuple(secret for secret in secrets if secret)

    def update_secrets(self, secrets: tuple[str, ...]) -> None:
        with self._lock:
            self.secrets = tuple(secret for secret in secrets if secret)

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        with self._lock:
            secrets = self.secrets
        for secret in secrets:
            rendered = rendered.replace(secret, "***")
        return rendered


def config_secrets(config) -> tuple[str, ...]:
    return (
        config.telegram.bot_token,
        config.discord.bot_token,
        config.mqtt.password,
        *(base64.b64encode(route.channel_key).decode("ascii") for route in config.routes),
    )


def update_logging_config(config) -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, config.logging_level))
    secrets = config_secrets(config)
    for handler in root_logger.handlers:
        formatter = handler.formatter
        if isinstance(formatter, RedactingFormatter):
            formatter.update_secrets(secrets)


def setup_logging(
    level_name: str,
    log_directory: Path | None = None,
    *,
    secrets: tuple[str, ...] = (),
    memory_handler: InMemoryLogHandler | None = None,
) -> Path | None:
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level_name))
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    formatter = RedactingFormatter(LOG_FORMAT, secrets)

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        handler.close()

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    if memory_handler is not None:
        memory_handler.setFormatter(formatter)
        root_logger.addHandler(memory_handler)

    log_path = (log_directory or application_dir()) / LOG_FILENAME
    try:
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    except OSError as exc:
        logger.warning("無法建立日誌檔案 %s：%s；將繼續使用終端日誌。", log_path, exc)
        log_path = None

    logger.info("日誌層級：%s", level_name)
    return log_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MeshBridge")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--autostart", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    actual_argv = sys.argv[1:] if argv is None else argv
    if "--version" in actual_argv:
        set_console_visible(True)
    args = build_parser().parse_args(actual_argv)
    set_console_visible(False)
    controller = AppController(application_dir() / "config.json")

    def activate(command: str) -> None:
        page = "設定" if command == "show:settings" else "儀表板"
        controller.events.put(("activate", page))

    instance = SingleInstance(activate)
    if not instance.acquire("show:dashboard"):
        return 0

    raw, load_error = controller.load()
    config = controller.config
    secrets = () if config is None else config_secrets(config)
    memory_handler = InMemoryLogHandler()
    setup_logging(config.logging_level if config else "INFO", secrets=secrets, memory_handler=memory_handler)
    controller.configure_logging_hook(update_logging_config)
    logger.info("正在啟動 MeshBridge v%s…", __version__)
    if config is not None:
        try:
            sync_autostart(config.features.autostart)
        except OSError:
            logger.exception("無法同步 Windows 開機自動啟動設定。")
    tray_service: TrayService | None = None
    shutting_down = threading.Event()

    def exit_application() -> None:
        if shutting_down.is_set():
            return
        shutting_down.set()

        def worker() -> None:
            controller.shutdown()
            if tray_service is not None:
                tray_service.stop()
            instance.close()
            controller.events.put(("shutdown_complete", None))

        threading.Thread(target=worker, name="application-shutdown", daemon=True).start()

    window = MeshBridgeWindow(
        controller,
        raw,
        load_error,
        memory_handler,
        start_hidden=bool(args.autostart and raw is not None),
        on_exit=exit_application,
    )
    tray_service = TrayService(
        lambda: controller.events.put(("request_exit", None)),
        show_callback=lambda: controller.events.put(("activate", "儀表板")),
        settings_callback=lambda: controller.events.put(("activate", "設定")),
        start_callback=controller.start_async,
        stop_callback=controller.stop_async,
        restart_callback=controller.restart_async,
        status_callback=lambda: f"橋接服務：{'運行中' if controller.running else '已停止'}",
        running_callback=lambda: controller.running,
    )
    tray_service.start()
    controller.configure_update_hooks(
        tray_service.notify,
        lambda: controller.events.put(("request_exit", None)),
    )
    if raw is not None:
        controller.start_async()
    try:
        window.mainloop()
    finally:
        if not shutting_down.is_set():
            controller.shutdown()
            tray_service.stop()
            instance.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
