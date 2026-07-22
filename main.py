# MeshTelegram Bridge entry point

import logging

from app_paths import application_dir
from config import ConfigError, load_config
from mqtt_service import MqttService
from telegram_bridge import create_application, start_bot


logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def setup_logging(level_name: str) -> None:
    logging.getLogger().setLevel(getattr(logging, level_name))
    logger.info("日誌層級：%s", level_name)


def main() -> int:
    try:
        config = load_config(application_dir() / "config.json")
        setup_logging(config.logging_level)
        logger.info("正在啟動 MeshTelegram Bridge…")

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
