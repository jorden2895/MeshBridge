from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meshtastic_codec import normalize_channel_key


class ConfigError(ValueError):
    """Raised when config.json contains an invalid or missing value."""


def _required(section: dict[str, Any], key: str, path: str) -> Any:
    value = section.get(key)
    if value is None or value == "":
        raise ConfigError(f"「{path}.{key}」為必填欄位")
    return value


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    target_chat_id: int


@dataclass(frozen=True)
class MqttConfig:
    broker: str
    port: int
    username: str
    password: str
    root_topic: str
    channel_name: str
    channel_key: bytes


@dataclass(frozen=True)
class NodeConfig:
    node_id: int
    long_name: str
    short_name: str


@dataclass(frozen=True)
class AppConfig:
    logging_level: str
    telegram: TelegramConfig
    mqtt: MqttConfig
    node: NodeConfig

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AppConfig":
        if not isinstance(raw, dict):
            raise ConfigError("設定檔最外層必須是 JSON 物件")

        logging_level = str(raw.get("logging_level", "INFO")).upper()
        if logging_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError("「logging_level」必須是 DEBUG、INFO、WARNING、ERROR 或 CRITICAL")

        telegram_raw = raw.get("telegram")
        mqtt_raw = raw.get("mqtt")
        node_raw = raw.get("node")
        for name, value in (("telegram", telegram_raw), ("mqtt", mqtt_raw), ("node", node_raw)):
            if not isinstance(value, dict):
                raise ConfigError(f"「{name}」必須是 JSON 物件")

        bot_token = str(_required(telegram_raw, "bot_token", "telegram")).strip()
        try:
            target_chat_id = int(_required(telegram_raw, "target_chat_id", "telegram"))
        except (TypeError, ValueError) as exc:
            raise ConfigError("「telegram.target_chat_id」必須是整數") from exc

        broker = str(_required(mqtt_raw, "broker", "mqtt")).strip()
        try:
            port = int(_required(mqtt_raw, "port", "mqtt"))
        except (TypeError, ValueError) as exc:
            raise ConfigError("「mqtt.port」必須是整數") from exc
        if not 1 <= port <= 65535:
            raise ConfigError("「mqtt.port」必須介於 1 到 65535 之間")

        root_topic = str(_required(mqtt_raw, "root_topic", "mqtt")).strip().rstrip("/") + "/"
        channel_name = str(_required(mqtt_raw, "channel_name", "mqtt")).strip()
        try:
            channel_key = normalize_channel_key(str(_required(mqtt_raw, "channel_key", "mqtt")))
        except ValueError as exc:
            raise ConfigError(f"「mqtt.channel_key」無效：{exc}") from exc

        try:
            node_id = int(_required(node_raw, "id", "node"))
        except (TypeError, ValueError) as exc:
            raise ConfigError("「node.id」必須是 32 位元無號整數") from exc
        if not 1 <= node_id <= 0xFFFFFFFF:
            raise ConfigError("「node.id」必須介於 1 到 4294967295 之間")

        long_name = str(_required(node_raw, "long_name", "node")).strip()
        short_name = str(_required(node_raw, "short_name", "node")).strip()
        if len(long_name) > 40:
            raise ConfigError("「node.long_name」最多只能有 40 個字元")
        if len(short_name) > 4:
            raise ConfigError("「node.short_name」最多只能有 4 個字元")

        return cls(
            logging_level=logging_level,
            telegram=TelegramConfig(bot_token=bot_token, target_chat_id=target_chat_id),
            mqtt=MqttConfig(
                broker=broker,
                port=port,
                username=str(mqtt_raw.get("username", "")),
                password=str(mqtt_raw.get("password", "")),
                root_topic=root_topic,
                channel_name=channel_name,
                channel_key=channel_key,
            ),
            node=NodeConfig(node_id=node_id, long_name=long_name, short_name=short_name),
        )


def load_config(path: str | Path = "config.json") -> AppConfig:
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"找不到設定檔：{config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{config_path} 的 JSON 格式無效：第 {exc.lineno} 行，{exc.msg}") from exc
    return AppConfig.from_dict(raw)
