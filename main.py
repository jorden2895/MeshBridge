# MeshTelegram Bridge entry point

import argparse
import ctypes
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app_paths import application_dir
from config import ConfigError, load_config
from mqtt_service import MqttService, MqttServiceError
from runtime_state import RuntimeState, StatusApiServer
from telegram_bridge import RouteBinding, create_application, start_bot
from tray_service import (
    TrayService,
    minimize_console_close_to_tray,
    set_console_visible,
    sync_autostart,
)
from update_monitor import UpdateMonitor
from version import __version__


logger = logging.getLogger(__name__)
LOG_FILENAME = "MeshTelegramBridge.log"
LOG_MAX_BYTES = 1_048_576
LOG_BACKUP_COUNT = 5
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, secrets: tuple[str, ...] = ()) -> None:
        super().__init__(fmt)
        self.secrets = tuple(secret for secret in secrets if secret)

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        for secret in self.secrets:
            rendered = rendered.replace(secret, "***")
        return rendered


def show_error_dialog(title: str, message: str) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)


def show_notification(title: str, message: str) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x40)


def setup_logging(
    level_name: str,
    log_directory: Path | None = None,
    *,
    secrets: tuple[str, ...] = (),
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
    parser = argparse.ArgumentParser(description="MeshTelegram Bridge")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    actual_argv = sys.argv[1:] if argv is None else argv
    if "--version" in actual_argv:
        set_console_visible(True)
    build_parser().parse_args(actual_argv)
    status_server = None
    tray_service = None
    update_monitor = None
    try:
        config = load_config(application_dir() / "config.json")
        set_console_visible(
            not config.features.tray.enabled or config.features.tray.show_console
        )
        secrets = (
            config.telegram.bot_token,
            config.mqtt.password,
            *(route.channel_key.hex() for route in config.routes),
        )
        setup_logging(config.logging_level, secrets=secrets)
        logger.info("正在啟動 MeshTelegram Bridge…")
        logger.info("版本：%s", __version__)

        runtime_state = RuntimeState(
            statistics_enabled=config.features.statistics_enabled,
            secrets_to_redact=secrets,
        )
        bindings = tuple(
            RouteBinding(
                route,
                MqttService(
                    config,
                    route=route,
                    runtime_state=runtime_state,
                    route_id=f"route-{index}",
                ),
            )
            for index, route in enumerate(config.active_routes, start=1)
        )
        telegram_app = create_application(
            config.telegram.bot_token,
            bindings,
            runtime_state=runtime_state,
            ui_display_name=config.bridge_ui.display_name,
        )
        request_stop = telegram_app.bot_data["request_stop"]
        if config.features.status.enabled:
            status_server = StatusApiServer(
                runtime_state,
                application_dir() / ".meshtelegram-status.json",
                send_callback=telegram_app.bot_data["chat_dispatcher"],
            )
            status_server.start()
        sync_autostart(config.features.tray.autostart)
        if config.features.tray.enabled:
            tray_service = TrayService(request_stop)
            tray_service.start()
            if config.features.tray.show_console:
                minimize_console_close_to_tray()
        if config.features.updates.enabled:
            notifier = (
                tray_service.notify if tray_service is not None else show_notification
            )
            update_monitor = UpdateMonitor(
                current_version=__version__,
                mode=config.features.updates.mode,
                interval_hours=config.features.updates.interval_hours,
                application_directory=application_dir(),
                notify=notifier,
                stop_application=request_stop,
            )
            update_monitor.start()
        start_bot(telegram_app)
        return 0
    except ConfigError as exc:
        logger.error("設定錯誤：%s", exc)
        logger.error("請使用 MeshTelegramBridgeSettings.exe 檢查並儲存設定。")
        show_error_dialog(
            "MeshTelegram Bridge 設定錯誤",
            f"{exc}\n\n請使用 MeshTelegramBridgeSettings.exe 檢查並儲存設定。",
        )
        return 2
    except MqttServiceError as exc:
        logger.error("MQTT 啟動失敗：%s", exc)
        show_error_dialog("MeshTelegram Bridge 啟動失敗", f"MQTT 啟動失敗：{exc}")
        return 3
    except KeyboardInterrupt:
        logger.info("收到停止訊號，正在關閉程式。")
        return 0
    except Exception:
        logger.exception("MeshTelegram Bridge 因未預期的錯誤而停止。")
        show_error_dialog(
            "MeshTelegram Bridge 已停止",
            "程式發生未預期的錯誤。請開啟 MeshTelegramBridge.log 查看詳細資訊。",
        )
        return 1
    finally:
        if update_monitor is not None:
            update_monitor.stop()
        if tray_service is not None:
            tray_service.stop()
        if status_server is not None:
            status_server.stop()


if __name__ == "__main__":
    raise SystemExit(main())
