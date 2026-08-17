from __future__ import annotations

import base64
import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from config import AppConfig, ConfigError, CURRENT_CONFIG_VERSION


def default_config_data() -> dict[str, Any]:
    return {
        "config_version": CURRENT_CONFIG_VERSION,
        "logging_level": "INFO",
        "appearance": {"theme": "system"},
        "telegram": {"bot_token": ""},
        "discord": {"bot_token": ""},
        "mqtt": {
            "broker": "mqtt.meshtastic.org",
            "port": 1883,
            "username": "meshdev",
            "password": "large4cats",
            "root_topic": "msh/US/2/e/",
        },
        "node": {
            "id": 2882392497,
            "long_name": "MeshBridge",
            "short_name": "MBRG",
        },
        "bridge_ui": {"display_name": "Bridge UI"},
        "routes": [
            {
                "name": "預設路由",
                "enabled": True,
                "telegram_enabled": True,
                "discord_enabled": False,
                "eew_enabled": False,
                "channel_name": "LongFast",
                "channel_key": "AQ==",
                "target_chat_id": None,
                "topic_id": None,
                "discord_channel_id": None,
            }
        ],
        "features": {
            "statistics_enabled": True,
            "autostart": False,
            "updates": {"enabled": False, "mode": "notify", "interval_hours": 24},
        },
        "automations": {
            "keyword_rules": [],
            "eew": {"dedupe_seconds": 60},
            "schedules": [],
        },
    }


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"找不到設定檔：{path}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path} 必須使用 UTF-8 編碼") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path} 的 JSON 格式無效：第 {exc.lineno} 行，{exc.msg}"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"無法讀取設定檔 {path}：{exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError("設定檔最外層必須是 JSON 物件")
    return value


def save_config_atomic(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".config-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(data, temporary, ensure_ascii=False, indent=4)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def migrate_v2_to_v3(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a validated v3-shaped copy without mutating the source object."""
    migrated = copy.deepcopy(raw)
    for section in ("telegram", "discord", "mqtt", "features"):
        value = migrated.get(section)
        if value is not None and not isinstance(value, dict):
            raise ConfigError(f"「{section}」必須是 JSON 物件")
    telegram = migrated.setdefault("telegram", {})
    discord = migrated.setdefault("discord", {})
    mqtt = migrated.setdefault("mqtt", {})
    features = migrated.setdefault("features", {})
    tray = features.get("tray") if isinstance(features.get("tray"), dict) else {}

    routes = migrated.get("routes")
    if not isinstance(routes, list):
        routes = [
            {
                "name": "預設路由",
                "enabled": True,
                "telegram_enabled": True,
                "discord_enabled": False,
                "channel_name": mqtt.get("channel_name", ""),
                "channel_key": mqtt.get("channel_key", ""),
                "target_chat_id": telegram.get("target_chat_id"),
                "topic_id": None,
                "discord_channel_id": None,
            }
        ]
    legacy_discord_enabled = bool(discord.get("enabled", False))
    for route in routes:
        if not isinstance(route, dict):
            continue
        route.setdefault(
            "telegram_enabled", route.get("target_chat_id") not in (None, "")
        )
        route.setdefault(
            "discord_enabled",
            legacy_discord_enabled
            and route.get("discord_channel_id") not in (None, ""),
        )

    telegram.pop("target_chat_id", None)
    discord.pop("enabled", None)
    mqtt.pop("channel_name", None)
    mqtt.pop("channel_key", None)
    features.pop("multi_route_enabled", None)
    features.pop("status_api", None)
    features.pop("tray", None)
    features["autostart"] = bool(tray.get("autostart", False))

    migrated["routes"] = routes
    migrated["appearance"] = {"theme": "system"}
    migrated["config_version"] = 3
    AppConfig.from_dict(migrated)
    return migrated


def migrate_v3_to_v4(raw: dict[str, Any]) -> dict[str, Any]:
    """Add disabled automation defaults without changing existing bridge behavior."""
    migrated = copy.deepcopy(raw)
    migrated.setdefault(
        "automations",
        {
            "keyword_rules": [],
            "eew": {"enabled": False, "routes": [], "dedupe_seconds": 60},
            "schedules": [],
        },
    )
    migrated["config_version"] = 4
    AppConfig.from_dict(migrated)
    return migrated


def migrate_v4_to_v5(raw: dict[str, Any]) -> dict[str, Any]:
    """Move EEW destinations onto individual routes."""
    migrated = copy.deepcopy(raw)
    automations = migrated.setdefault("automations", {})
    eew = automations.get("eew", {})
    if not isinstance(eew, dict):
        raise ConfigError("「automations.eew」必須是 JSON 物件")
    raw_routes = eew.get("routes", [])
    if bool(eew.get("enabled", False)) and not isinstance(raw_routes, list):
        raise ConfigError("「automations.eew.routes」必須是路由名稱陣列")
    enabled_routes = (
        {str(name).strip().casefold() for name in raw_routes}
        if bool(eew.get("enabled", False)) and isinstance(raw_routes, list)
        else set()
    )
    routes = migrated.get("routes", [])
    if isinstance(routes, list):
        known_routes = {
            str(route.get("name", "")).strip().casefold()
            for route in routes
            if isinstance(route, dict)
        }
        unknown_routes = enabled_routes - known_routes
        if unknown_routes:
            raise ConfigError("「automations.eew.routes」包含不存在的路由")
        for route in routes:
            if isinstance(route, dict):
                route["eew_enabled"] = (
                    str(route.get("name", "")).strip().casefold() in enabled_routes
                )
    automations["eew"] = {"dedupe_seconds": eew.get("dedupe_seconds", 60)}
    migrated["config_version"] = CURRENT_CONFIG_VERSION
    AppConfig.from_dict(migrated)
    return migrated


def load_config_data(path: Path, *, migrate: bool = True) -> tuple[dict[str, Any], bool]:
    raw = _read_object(path)
    try:
        version = int(raw.get("config_version", 2))
    except (TypeError, ValueError) as exc:
        raise ConfigError("「config_version」必須是整數") from exc
    if version == CURRENT_CONFIG_VERSION:
        AppConfig.from_dict(raw)
        return raw, False
    if version not in {2, 3, 4}:
        AppConfig.from_dict(raw)
        raise ConfigError(f"不支援的設定檔版本：{version}")
    if not migrate:
        AppConfig.from_dict(raw)
        return raw, False

    migrated_v3 = migrate_v2_to_v3(raw) if version == 2 else copy.deepcopy(raw)
    migrated_v4 = migrate_v3_to_v4(migrated_v3) if version in {2, 3} else migrated_v3
    migrated = migrate_v4_to_v5(migrated_v4)
    backup = path.with_name(f"config.v{version}.backup.json")
    if not backup.exists():
        save_config_atomic(raw, backup)
    save_config_atomic(migrated, path)
    return migrated, True


def config_to_dict(config: AppConfig) -> dict[str, Any]:
    return {
        "config_version": CURRENT_CONFIG_VERSION,
        "logging_level": config.logging_level,
        "appearance": {"theme": config.appearance.theme},
        "telegram": {"bot_token": config.telegram.bot_token},
        "discord": {"bot_token": config.discord.bot_token},
        "mqtt": {
            "broker": config.mqtt.broker,
            "port": config.mqtt.port,
            "username": config.mqtt.username,
            "password": config.mqtt.password,
            "root_topic": config.mqtt.root_topic,
        },
        "node": {
            "id": config.node.node_id,
            "long_name": config.node.long_name,
            "short_name": config.node.short_name,
        },
        "bridge_ui": {"display_name": config.bridge_ui.display_name},
        "routes": [
            {
                "name": route.name,
                "enabled": route.enabled,
                "telegram_enabled": route.telegram_enabled,
                "discord_enabled": route.discord_enabled,
                "eew_enabled": route.eew_enabled,
                "channel_name": route.channel_name,
                "channel_key": base64.b64encode(route.channel_key).decode("ascii"),
                "target_chat_id": route.target_chat_id,
                "topic_id": route.topic_id,
                "discord_channel_id": route.discord_channel_id,
            }
            for route in config.routes
        ],
        "features": {
            "statistics_enabled": config.features.statistics_enabled,
            "autostart": config.features.autostart,
            "updates": {
                "enabled": config.features.updates.enabled,
                "mode": config.features.updates.mode,
                "interval_hours": config.features.updates.interval_hours,
            },
        },
        "automations": {
            "keyword_rules": [
                {
                    "name": rule.name,
                    "enabled": rule.enabled,
                    "routes": list(rule.routes),
                    "match": rule.match,
                    "keyword": rule.keyword,
                    "response": rule.response,
                }
                for rule in config.automations.keyword_rules
            ],
            "eew": {
                "dedupe_seconds": config.automations.eew.dedupe_seconds,
            },
            "schedules": [
                {
                    "name": schedule.name,
                    "enabled": schedule.enabled,
                    "routes": list(schedule.routes),
                    "cron": schedule.cron,
                    "message": schedule.message,
                }
                for schedule in config.automations.schedules
            ],
        },
    }
