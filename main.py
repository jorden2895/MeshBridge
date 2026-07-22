# MeshTelegram Bridge entry point

import argparse
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app_paths import application_dir
from config import ConfigError, load_config
from mqtt_service import MqttService
from telegram_bridge import create_application, start_bot
from version import __version__


logger = logging.getLogger(__name__)
LOG_FILENAME = "MeshTelegramBridge.log"
LOG_MAX_BYTES = 1_048_576
LOG_BACKUP_COUNT = 5
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def setup_logging(level_name: str, log_directory: Path | None = None) -> Path | None:
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level_name))
    formatter = logging.Formatter(LOG_FORMAT)

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
    build_parser().parse_args(argv)
    try:
        config = load_config(application_dir() / "config.json")
        setup_logging(config.logging_level)
        logger.info("正在啟動 MeshTelegram Bridge…")
        logger.info("版本：%s", __version__)

        mqtt_service = MqttService(config)
        telegram_app = create_application(
            config.telegram.bot_token,
            config.telegram.target_chat_id,
            mqtt_service,
        )
        start_bot(telegram_app)
        return 0
    except ConfigError as exc:
        logger.error("設定錯誤：%s", exc)
        logger.error("請使用 MeshTelegramBridgeSettings.exe 檢查並儲存設定。")
        return 2
    except KeyboardInterrupt:
        logger.info("收到停止訊號，正在關閉程式。")
        return 0
    except Exception:
        logger.exception("MeshTelegram Bridge 因未預期的錯誤而停止。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
