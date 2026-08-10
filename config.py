from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meshtastic_codec import normalize_channel_key


MAX_ROUTES = 5
DEFAULT_STATUS_API_ENABLED = True
DEFAULT_UPDATE_INTERVAL_HOURS = 24
DEFAULT_BRIDGE_UI_DISPLAY_NAME = "Bridge UI"


class ConfigError(ValueError):
    """Raised when config.json contains an invalid or missing value."""


def _required(section: dict[str, Any], key: str, path: str) -> Any:
    value = section.get(key)
    if value is None or value == "":
        raise ConfigError(f"「{path}.{key}」為必填欄位")
    return value


def _required_text(section: dict[str, Any], key: str, path: str) -> str:
    value = str(_required(section, key, path)).strip()
    if not value:
        raise ConfigError(f"「{path}.{key}」不可只包含空白")
    return value


def _object(raw: dict[str, Any], key: str, *, required: bool = True) -> dict[str, Any]:
    value = raw.get(key)
    if value is None and not required:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"「{key}」必須是 JSON 物件")
    return value


def _bool(value: Any, path: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ConfigError(f"「{path}」必須是 true 或 false")


def _integer(value: Any, path: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"「{path}」必須是整數") from exc


def _validate_topic_segment(value: str, path: str) -> str:
    value = value.strip().strip("/")
    if not value:
        raise ConfigError(f"「{path}」不可為空白")
    if "+" in value or "#" in value:
        raise ConfigError(f"「{path}」不可包含 MQTT wildcard「+」或「#」")
    return value


def _validate_root_topic(value: str) -> str:
    value = value.strip().strip("/")
    if not value:
        raise ConfigError("「mqtt.root_topic」不可為空白")
    if "+" in value or "#" in value:
        raise ConfigError("「mqtt.root_topic」不可包含 MQTT wildcard「+」或「#」")
    return value + "/"


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
class BridgeUiConfig:
    display_name: str = DEFAULT_BRIDGE_UI_DISPLAY_NAME


@dataclass(frozen=True)
class RouteConfig:
    name: str
    enabled: bool
    channel_name: str
    channel_key: bytes
    target_chat_id: int
    topic_id: int | None = None


@dataclass(frozen=True)
class StatusConfig:
    enabled: bool = DEFAULT_STATUS_API_ENABLED


@dataclass(frozen=True)
class TrayConfig:
    enabled: bool = False
    show_console: bool = True
    autostart: bool = False


@dataclass(frozen=True)
class UpdateConfig:
    enabled: bool = False
    mode: str = "notify"
    interval_hours: int = DEFAULT_UPDATE_INTERVAL_HOURS


@dataclass(frozen=True)
class FeatureConfig:
    statistics_enabled: bool
    multi_route_enabled: bool
    status: StatusConfig
    tray: TrayConfig
    updates: UpdateConfig


@dataclass(frozen=True)
class AppConfig:
    logging_level: str
    telegram: TelegramConfig
    mqtt: MqttConfig
    node: NodeConfig
    bridge_ui: BridgeUiConfig
    routes: tuple[RouteConfig, ...]
    features: FeatureConfig

    @property
    def active_routes(self) -> tuple[RouteConfig, ...]:
        candidates = self.routes if self.features.multi_route_enabled else self.routes[:1]
        active = tuple(route for route in candidates if route.enabled)
        if not active:
            raise ConfigError("至少必須啟用一組路由")
        return active

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AppConfig":
        if not isinstance(raw, dict):
            raise ConfigError("設定檔最外層必須是 JSON 物件")

        logging_level = str(raw.get("logging_level", "INFO")).strip().upper()
        if logging_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError("「logging_level」必須是 DEBUG、INFO、WARNING、ERROR 或 CRITICAL")

        telegram_raw = _object(raw, "telegram")
        mqtt_raw = _object(raw, "mqtt")
        node_raw = _object(raw, "node")
        bridge_ui_raw = _object(raw, "bridge_ui", required=False)

        bot_token = _required_text(telegram_raw, "bot_token", "telegram")
        legacy_target = telegram_raw.get("target_chat_id")

        broker = _required_text(mqtt_raw, "broker", "mqtt")
        port = _integer(_required(mqtt_raw, "port", "mqtt"), "mqtt.port")
        if not 1 <= port <= 65535:
            raise ConfigError("「mqtt.port」必須介於 1 到 65535 之間")
        root_topic = _validate_root_topic(_required_text(mqtt_raw, "root_topic", "mqtt"))

        try:
            node_id = _integer(_required(node_raw, "id", "node"), "node.id")
        except ConfigError as exc:
            raise ConfigError("「node.id」必須是 32 位元無號整數") from exc
        if not 1 <= node_id <= 0xFFFFFFFF:
            raise ConfigError("「node.id」必須介於 1 到 4294967295 之間")

        long_name = _required_text(node_raw, "long_name", "node")
        short_name = _required_text(node_raw, "short_name", "node")
        if len(long_name) > 40:
            raise ConfigError("「node.long_name」最多只能有 40 個字元")
        if len(short_name) > 4:
            raise ConfigError("「node.short_name」最多只能有 4 個字元")

        display_name = str(
            bridge_ui_raw.get("display_name", DEFAULT_BRIDGE_UI_DISPLAY_NAME)
        ).strip()
        if not 1 <= len(display_name) <= 32:
            raise ConfigError("「bridge_ui.display_name」必須包含 1 到 32 個字元")
        if any(ord(character) < 32 or ord(character) == 127 for character in display_name):
            raise ConfigError("「bridge_ui.display_name」不可包含換行或控制字元")

        routes_raw = raw.get("routes")
        if routes_raw is None:
            if legacy_target is None:
                raise ConfigError("「telegram.target_chat_id」為必填欄位")
            channel_name = _validate_topic_segment(
                _required_text(mqtt_raw, "channel_name", "mqtt"), "mqtt.channel_name"
            )
            try:
                channel_key = normalize_channel_key(
                    str(_required(mqtt_raw, "channel_key", "mqtt"))
                )
            except ValueError as exc:
                raise ConfigError(f"「mqtt.channel_key」無效：{exc}") from exc
            routes = (
                RouteConfig(
                    name="預設路由",
                    enabled=True,
                    channel_name=channel_name,
                    channel_key=channel_key,
                    target_chat_id=_integer(legacy_target, "telegram.target_chat_id"),
                ),
            )
        else:
            if not isinstance(routes_raw, list):
                raise ConfigError("「routes」必須是 JSON 陣列")
            if not 1 <= len(routes_raw) <= MAX_ROUTES:
                raise ConfigError(f"「routes」必須包含 1 到 {MAX_ROUTES} 組路由")
            parsed_routes: list[RouteConfig] = []
            for index, route_raw in enumerate(routes_raw):
                path = f"routes[{index}]"
                if not isinstance(route_raw, dict):
                    raise ConfigError(f"「{path}」必須是 JSON 物件")
                name = _required_text(route_raw, "name", path)
                channel_name = _validate_topic_segment(
                    _required_text(route_raw, "channel_name", path),
                    f"{path}.channel_name",
                )
                try:
                    channel_key = normalize_channel_key(
                        str(_required(route_raw, "channel_key", path))
                    )
                except ValueError as exc:
                    raise ConfigError(f"「{path}.channel_key」無效：{exc}") from exc
                topic_value = route_raw.get("topic_id")
                topic_id = (
                    None
                    if topic_value in (None, "")
                    else _integer(topic_value, f"{path}.topic_id")
                )
                parsed_routes.append(
                    RouteConfig(
                        name=name,
                        enabled=_bool(route_raw.get("enabled"), f"{path}.enabled", True),
                        channel_name=channel_name,
                        channel_key=channel_key,
                        target_chat_id=_integer(
                            _required(route_raw, "target_chat_id", path),
                            f"{path}.target_chat_id",
                        ),
                        topic_id=topic_id,
                    )
                )
            routes = tuple(parsed_routes)
            channel_endpoints: set[str] = set()
            telegram_endpoints: set[tuple[int, int | None]] = set()
            route_names: set[str] = set()
            for route in routes:
                normalized_name = route.name.casefold()
                telegram_endpoint = (route.target_chat_id, route.topic_id)
                if normalized_name in route_names:
                    raise ConfigError("每組路由名稱必須不同")
                if route.channel_name.casefold() in channel_endpoints:
                    raise ConfigError("每組路由的 Meshtastic 頻道必須不同")
                if telegram_endpoint in telegram_endpoints:
                    raise ConfigError("每組路由的 Telegram 聊天室／主題組合必須不同")
                route_names.add(normalized_name)
                channel_endpoints.add(route.channel_name.casefold())
                telegram_endpoints.add(telegram_endpoint)

        first_route = routes[0]
        features_raw = _object(raw, "features", required=False)
        status_raw = _object(features_raw, "status_api", required=False)
        tray_raw = _object(features_raw, "tray", required=False)
        updates_raw = _object(features_raw, "updates", required=False)

        update_mode = str(updates_raw.get("mode", "notify")).strip().lower()
        if update_mode not in {"notify", "download", "install"}:
            raise ConfigError("「features.updates.mode」必須是 notify、download 或 install")
        interval_hours = _integer(
            updates_raw.get("interval_hours", DEFAULT_UPDATE_INTERVAL_HOURS),
            "features.updates.interval_hours",
        )
        if not 1 <= interval_hours <= 720:
            raise ConfigError("「features.updates.interval_hours」必須介於 1 到 720")

        features = FeatureConfig(
            statistics_enabled=_bool(
                features_raw.get("statistics_enabled"),
                "features.statistics_enabled",
                True,
            ),
            multi_route_enabled=_bool(
                features_raw.get("multi_route_enabled"),
                "features.multi_route_enabled",
                False,
            ),
            status=StatusConfig(
                enabled=_bool(
                    status_raw.get("enabled"),
                    "features.status_api.enabled",
                    DEFAULT_STATUS_API_ENABLED,
                )
            ),
            tray=TrayConfig(
                enabled=_bool(tray_raw.get("enabled"), "features.tray.enabled", False),
                show_console=_bool(
                    tray_raw.get("show_console"),
                    "features.tray.show_console",
                    True,
                ),
                autostart=_bool(
                    tray_raw.get("autostart"),
                    "features.tray.autostart",
                    False,
                ),
            ),
            updates=UpdateConfig(
                enabled=_bool(
                    updates_raw.get("enabled"),
                    "features.updates.enabled",
                    False,
                ),
                mode=update_mode,
                interval_hours=interval_hours,
            ),
        )

        # Accessing active_routes here turns an all-disabled route set into a config error.
        config = cls(
            logging_level=logging_level,
            telegram=TelegramConfig(
                bot_token=bot_token,
                target_chat_id=first_route.target_chat_id,
            ),
            mqtt=MqttConfig(
                broker=broker,
                port=port,
                username=str(mqtt_raw.get("username", "")),
                password=str(mqtt_raw.get("password", "")),
                root_topic=root_topic,
                channel_name=first_route.channel_name,
                channel_key=first_route.channel_key,
            ),
            node=NodeConfig(node_id=node_id, long_name=long_name, short_name=short_name),
            bridge_ui=BridgeUiConfig(display_name=display_name),
            routes=routes,
            features=features,
        )
        config.active_routes
        return config


def load_config(path: str | Path = "config.json") -> AppConfig:
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"找不到設定檔：{config_path}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{config_path} 必須使用 UTF-8 編碼") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{config_path} 的 JSON 格式無效：第 {exc.lineno} 行，{exc.msg}") from exc
    except OSError as exc:
        raise ConfigError(f"無法讀取設定檔 {config_path}：{exc}") from exc
    return AppConfig.from_dict(raw)
