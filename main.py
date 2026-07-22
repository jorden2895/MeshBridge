# MeshTelegram Bridge entry point

import logging

from config import ConfigError, load_config
from mqtt_service import MqttService
from telegram_bridge import create_application, start_bot


logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def setup_logging(level_name: str) -> None:
    logging.getLogger().setLevel(getattr(logging, level_name))
    logger.info("Logging level set to %s", level_name)


def main() -> int:
    try:
        config = load_config()
        setup_logging(config.logging_level)
        logger.info("Starting MeshTelegram Bridge...")

        mqtt_service = MqttService(config)
        telegram_app = create_application(
            config.telegram.bot_token,
            config.telegram.target_chat_id,
            mqtt_service,
        )
        start_bot(telegram_app)
        return 0
    except ConfigError as exc:
        logger.error("Invalid configuration: %s", exc)
        return 2
    except KeyboardInterrupt:
        logger.info("Shutdown signal received.")
        return 0
    except Exception:
        logger.exception("MeshTelegram Bridge stopped because of a fatal error.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
