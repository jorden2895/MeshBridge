from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from croniter import croniter

from meshtastic_codec import normalize_channel_key


MAX_ROUTES = 20
CURRENT_CONFIG_VERSION = 5
DEFAULT_UPDATE_INTERVAL_HOURS = 24
DEFAULT_BRIDGE_UI_DISPLAY_NAME = "Bridge UI"
MAX_AUTOMATION_ITEMS = 50
MAX_AUTOMATION_MESSAGE_BYTES = 233


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
    bot_token: str = ""
    target_chat_id: int | None = None
    enabled: bool = False


@dataclass(frozen=True)
class DiscordConfig:
    enabled: bool = False
    bot_token: str = ""


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
class AppearanceConfig:
    theme: str = "system"


@dataclass(frozen=True)
class RouteConfig:
    name: str
    enabled: bool
    channel_name: str
    channel_key: bytes
    target_chat_id: int | None
    topic_id: int | None = None
    discord_channel_id: str | None = None
    telegram_enabled: bool = True
    discord_enabled: bool = False
    eew_enabled: bool = False


@dataclass(frozen=True)
class UpdateConfig:
    enabled: bool = False
    mode: str = "notify"
    interval_hours: int = DEFAULT_UPDATE_INTERVAL_HOURS


@dataclass(frozen=True)
class FeatureConfig:
    statistics_enabled: bool
    autostart: bool
    updates: UpdateConfig


@dataclass(frozen=True)
class KeywordRuleConfig:
    name: str
    enabled: bool
    routes: tuple[str, ...]
    match: str
    keyword: str
    response: str


@dataclass(frozen=True)
class EewConfig:
    enabled: bool = False
    routes: tuple[str, ...] = ()
    dedupe_seconds: int = 60


@dataclass(frozen=True)
class ScheduleConfig:
    name: str
    enabled: bool
    routes: tuple[str, ...]
    cron: str
    message: str


@dataclass(frozen=True)
class AutomationConfig:
    keyword_rules: tuple[KeywordRuleConfig, ...] = ()
    eew: EewConfig = EewConfig()
    schedules: tuple[ScheduleConfig, ...] = ()


@dataclass(frozen=True)
class AppConfig:
    logging_level: str
    telegram: TelegramConfig
    discord: DiscordConfig
    mqtt: MqttConfig
    node: NodeConfig
    bridge_ui: BridgeUiConfig
    routes: tuple[RouteConfig, ...]
    features: FeatureConfig
    automations: AutomationConfig = AutomationConfig()
    config_version: int = CURRENT_CONFIG_VERSION
    appearance: AppearanceConfig = AppearanceConfig()

    @property
    def active_routes(self) -> tuple[RouteConfig, ...]:
        active = tuple(route for route in self.routes if route.enabled)
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

        telegram_raw = _object(raw, "telegram", required=False)
        discord_raw = _object(raw, "discord", required=False)
        mqtt_raw = _object(raw, "mqtt")
        node_raw = _object(raw, "node")
        bridge_ui_raw = _object(raw, "bridge_ui", required=False)
        appearance_raw = _object(raw, "appearance", required=False)

        config_version = _integer(raw.get("config_version", 2), "config_version")
        if config_version not in {2, 3, 4, CURRENT_CONFIG_VERSION}:
            raise ConfigError(f"不支援的設定版本：{config_version}")
        theme = str(appearance_raw.get("theme", "system")).strip().lower()
        if theme not in {"system", "light", "dark"}:
            raise ConfigError("「appearance.theme」必須是 system、light 或 dark")

        bot_token = str(telegram_raw.get("bot_token", "")).strip()
        legacy_target = telegram_raw.get("target_chat_id")
        discord_enabled = _bool(discord_raw.get("enabled"), "discord.enabled", False)
        discord_bot_token = str(discord_raw.get("bot_token", "")).strip()
        if config_version < CURRENT_CONFIG_VERSION and discord_enabled and not discord_bot_token:
            raise ConfigError("啟用 Discord 時，「discord.bot_token」為必填欄位")

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
                    telegram_enabled=True,
                    discord_enabled=False,
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
                discord_channel_value = route_raw.get("discord_channel_id")
                discord_channel_id = (
                    None
                    if discord_channel_value in (None, "")
                    else str(discord_channel_value).strip()
                )
                if discord_channel_id is not None and (
                    not discord_channel_id.isascii()
                    or not discord_channel_id.isdecimal()
                    or int(discord_channel_id) <= 0
                ):
                    raise ConfigError(f"「{path}.discord_channel_id」必須是正整數字串")
                target_value = route_raw.get("target_chat_id")
                target_chat_id = (
                    None
                    if target_value in (None, "")
                    else _integer(target_value, f"{path}.target_chat_id")
                )
                telegram_enabled = _bool(
                    route_raw.get("telegram_enabled"),
                    f"{path}.telegram_enabled",
                    target_chat_id is not None,
                )
                discord_route_enabled = _bool(
                    route_raw.get("discord_enabled"),
                    f"{path}.discord_enabled",
                    discord_enabled and discord_channel_id is not None,
                )
                if not telegram_enabled and not discord_route_enabled:
                    raise ConfigError(f"「{path}」至少必須啟用 Telegram 或 Discord")
                if telegram_enabled and target_chat_id is None:
                    raise ConfigError(f"啟用 Telegram 時，「{path}.target_chat_id」為必填欄位")
                if discord_route_enabled and discord_channel_id is None:
                    raise ConfigError(f"啟用 Discord 時，「{path}.discord_channel_id」為必填欄位")
                parsed_routes.append(
                    RouteConfig(
                        name=name,
                        enabled=_bool(route_raw.get("enabled"), f"{path}.enabled", True),
                        channel_name=channel_name,
                        channel_key=channel_key,
                        target_chat_id=target_chat_id,
                        topic_id=topic_id,
                        discord_channel_id=discord_channel_id,
                        telegram_enabled=telegram_enabled,
                        discord_enabled=discord_route_enabled,
                        eew_enabled=_bool(
                            route_raw.get("eew_enabled"),
                            f"{path}.eew_enabled",
                            False,
                        ),
                    )
                )
            routes = tuple(parsed_routes)
            channel_endpoints: set[str] = set()
            telegram_endpoints: set[tuple[int, int | None]] = set()
            discord_endpoints: set[str] = set()
            route_names: set[str] = set()
            for route in routes:
                normalized_name = route.name.casefold()
                telegram_endpoint = (route.target_chat_id, route.topic_id)
                if normalized_name in route_names:
                    raise ConfigError("每組路由名稱必須不同")
                if route.channel_name.casefold() in channel_endpoints:
                    raise ConfigError("每組路由的 Meshtastic 頻道必須不同")
                if route.telegram_enabled and telegram_endpoint in telegram_endpoints:
                    raise ConfigError("每組路由的 Telegram 聊天室／主題組合必須不同")
                if (
                    route.discord_channel_id is not None
                    and route.discord_channel_id in discord_endpoints
                ):
                    raise ConfigError("每組路由的 Discord 頻道必須不同")
                route_names.add(normalized_name)
                channel_endpoints.add(route.channel_name.casefold())
                if route.telegram_enabled:
                    telegram_endpoints.add(telegram_endpoint)
                if route.discord_channel_id is not None:
                    discord_endpoints.add(route.discord_channel_id)

        effective_routes = routes
        telegram_enabled = any(
            route.enabled and route.telegram_enabled for route in effective_routes
        )
        discord_enabled = any(
            route.enabled and route.discord_enabled for route in effective_routes
        )
        if _bool(discord_raw.get("enabled"), "discord.enabled", False) and not discord_enabled:
            raise ConfigError("啟用 Discord 時，至少一組啟用路由必須設定 Discord 頻道 ID")
        if telegram_enabled and not bot_token:
            raise ConfigError("至少一組路由啟用 Telegram 時，「telegram.bot_token」為必填欄位")
        if discord_enabled and not discord_bot_token:
            raise ConfigError("至少一組路由啟用 Discord 時，「discord.bot_token」為必填欄位")

        first_route = routes[0]
        features_raw = _object(raw, "features", required=False)
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

        autostart_value = features_raw.get("autostart", tray_raw.get("autostart"))
        features = FeatureConfig(
            statistics_enabled=_bool(
                features_raw.get("statistics_enabled"),
                "features.statistics_enabled",
                True,
            ),
            autostart=_bool(
                autostart_value,
                "features.autostart",
                False,
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

        route_lookup = {route.name.casefold(): route.name for route in routes}

        def automation_routes(value: Any, path: str) -> tuple[str, ...]:
            if not isinstance(value, list) or not value:
                raise ConfigError(f"「{path}」必須至少選擇一組路由")
            selected: list[str] = []
            seen: set[str] = set()
            for item in value:
                normalized = str(item).strip().casefold()
                if normalized not in route_lookup:
                    raise ConfigError(f"「{path}」包含不存在的路由：{item}")
                if normalized not in seen:
                    selected.append(route_lookup[normalized])
                    seen.add(normalized)
            return tuple(selected)

        def automation_text(value: Any, path: str) -> str:
            text = str(value or "").strip()
            if not text:
                raise ConfigError(f"「{path}」不可空白")
            if len(text.encode("utf-8")) > MAX_AUTOMATION_MESSAGE_BYTES:
                raise ConfigError(
                    f"「{path}」不可超過 {MAX_AUTOMATION_MESSAGE_BYTES} UTF-8 bytes"
                )
            return text

        automations_raw = _object(raw, "automations", required=False)
        keyword_rules_raw = automations_raw.get("keyword_rules", [])
        if not isinstance(keyword_rules_raw, list) or len(keyword_rules_raw) > MAX_AUTOMATION_ITEMS:
            raise ConfigError(f"「automations.keyword_rules」最多只能有 {MAX_AUTOMATION_ITEMS} 項")
        keyword_rules: list[KeywordRuleConfig] = []
        for index, item in enumerate(keyword_rules_raw):
            path = f"automations.keyword_rules[{index}]"
            if not isinstance(item, dict):
                raise ConfigError(f"「{path}」必須是 JSON 物件")
            match = str(item.get("match", "exact")).strip().lower()
            if match not in {"exact", "contains"}:
                raise ConfigError(f"「{path}.match」必須是 exact 或 contains")
            keyword = str(item.get("keyword", "")).strip()
            if not keyword or len(keyword) > 100:
                raise ConfigError(f"「{path}.keyword」必須包含 1 到 100 個字元")
            keyword_rules.append(
                KeywordRuleConfig(
                    name=_required_text(item, "name", path),
                    enabled=_bool(item.get("enabled"), f"{path}.enabled", True),
                    routes=automation_routes(item.get("routes"), f"{path}.routes"),
                    match=match,
                    keyword=keyword,
                    response=automation_text(item.get("response"), f"{path}.response"),
                )
            )

        eew_raw = automations_raw.get("eew", {})
        if not isinstance(eew_raw, dict):
            raise ConfigError("「automations.eew」必須是 JSON 物件")
        dedupe_seconds = _integer(eew_raw.get("dedupe_seconds", 60), "automations.eew.dedupe_seconds")
        if not 0 <= dedupe_seconds <= 3600:
            raise ConfigError("「automations.eew.dedupe_seconds」必須介於 0 到 3600")

        schedules_raw = automations_raw.get("schedules", [])
        if not isinstance(schedules_raw, list) or len(schedules_raw) > MAX_AUTOMATION_ITEMS:
            raise ConfigError(f"「automations.schedules」最多只能有 {MAX_AUTOMATION_ITEMS} 項")
        schedules: list[ScheduleConfig] = []
        for index, item in enumerate(schedules_raw):
            path = f"automations.schedules[{index}]"
            if not isinstance(item, dict):
                raise ConfigError(f"「{path}」必須是 JSON 物件")
            expression = str(item.get("cron", "")).strip()
            if len(expression.split()) != 5 or not croniter.is_valid(expression, strict=True):
                raise ConfigError(f"「{path}.cron」必須是有效的五欄 Cron 表達式")
            schedules.append(
                ScheduleConfig(
                    name=_required_text(item, "name", path),
                    enabled=_bool(item.get("enabled"), f"{path}.enabled", True),
                    routes=automation_routes(item.get("routes"), f"{path}.routes"),
                    cron=expression,
                    message=automation_text(item.get("message"), f"{path}.message"),
                )
            )

        automations = AutomationConfig(
            keyword_rules=tuple(keyword_rules),
            eew=EewConfig(
                enabled=any(route.enabled and route.eew_enabled for route in routes),
                routes=tuple(
                    route.name for route in routes if route.enabled and route.eew_enabled
                ),
                dedupe_seconds=dedupe_seconds,
            ),
            schedules=tuple(schedules),
        )

        # Accessing active_routes here turns an all-disabled route set into a config error.
        config = cls(
            logging_level=logging_level,
            telegram=TelegramConfig(
                bot_token=bot_token,
                target_chat_id=next(
                    (
                        route.target_chat_id
                        for route in effective_routes
                        if route.enabled and route.telegram_enabled
                    ),
                    None,
                ),
                enabled=telegram_enabled,
            ),
            discord=DiscordConfig(
                enabled=discord_enabled,
                bot_token=discord_bot_token,
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
            automations=automations,
            config_version=CURRENT_CONFIG_VERSION,
            appearance=AppearanceConfig(theme=theme),
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
