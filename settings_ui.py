from __future__ import annotations

import asyncio
import json
import os
import random
import tempfile
import tkinter as tk
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

import paho.mqtt.client as mqtt
import discord
from telegram import Bot

from app_paths import application_dir
from config import AppConfig, ConfigError
from status_client import (
    ChatSendError,
    StatusUnavailable,
    fetch_messages,
    fetch_status,
    send_chat_message,
)
from update_service import (
    UpdateError,
    download_portable_release,
    fetch_latest_release,
    is_newer,
    record_check,
    schedule_portable_install,
    should_check,
)
from version import __version__


PROJECT_DIR = application_dir()
CONFIG_PATH = PROJECT_DIR / "config.json"
EXAMPLE_PATH = PROJECT_DIR / "config.json.example"
CONNECTION_TIMEOUT_SECONDS = 10
STATUS_DISCOVERY_PATH = PROJECT_DIR / ".meshtelegram-status.json"
UPDATE_STATE_PATH = PROJECT_DIR / ".meshtelegram-update-state.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "logging_level": "INFO",
    "telegram": {
        "bot_token": "YOUR_TELEGRAM_BOT_TOKEN_HERE",
        "target_chat_id": "YOUR_TARGET_CHAT_ID_HERE",
    },
    "discord": {"enabled": False, "bot_token": ""},
    "mqtt": {
        "broker": "mqtt.meshtastic.org",
        "port": 1883,
        "username": "meshdev",
        "password": "large4cats",
        "root_topic": "msh/US/2/e/",
        "channel_name": "LongFast",
        "channel_key": "1PG7OiApB1nwvP+rz05pAQ==",
    },
    "node": {
        "id": 2882392497,
        "long_name": "MeshTelegram Bridge",
        "short_name": "TGBT",
    },
    "bridge_ui": {"display_name": "Bridge UI"},
    "features": {
        "statistics_enabled": True,
        "multi_route_enabled": False,
        "status_api": {"enabled": True},
        "tray": {"enabled": False, "show_console": True, "autostart": False},
        "updates": {"enabled": False, "mode": "notify", "interval_hours": 24},
    },
}

FIELD_GROUPS = (
    (
        "Telegram 設定",
        (
            ("telegram.bot_token", "機器人權杖", True),
        ),
    ),
    (
        "Discord 設定",
        (
            ("discord.bot_token", "機器人權杖", True),
        ),
    ),
    (
        "MQTT 設定",
        (
            ("mqtt.broker", "伺服器", False),
            ("mqtt.port", "連接埠", False),
            ("mqtt.username", "使用者名稱", False),
            ("mqtt.password", "密碼", True),
            ("mqtt.root_topic", "根主題", False),
        ),
    ),
    (
        "Meshtastic 虛擬節點",
        (
            ("node.id", "節點 ID", False),
            ("node.long_name", "完整名稱", False),
            ("node.short_name", "簡短名稱", False),
        ),
    ),
    (
        "介面設定",
        (
            ("bridge_ui.display_name", "Bridge UI 顯示名稱", False),
        ),
    ),
)


@dataclass(frozen=True)
class ConnectionTestResult:
    service: str
    succeeded: bool
    message: str


def _safe_error_message(exc: Exception, secrets: tuple[str, ...]) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    for secret in secrets:
        if secret:
            message = message.replace(secret, "***")
    return message


def probe_telegram(config: AppConfig) -> str:
    async def get_identity() -> str:
        async with Bot(config.telegram.bot_token) as bot:
            user = await bot.get_me()
            return f"@{user.username}" if user.username else user.full_name

    return asyncio.run(get_identity())


def probe_mqtt(config: AppConfig, timeout: float = CONNECTION_TIMEOUT_SECONDS) -> str:
    connected = threading.Event()
    outcome: dict[str, str] = {}
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if config.mqtt.username:
        client.username_pw_set(config.mqtt.username, config.mqtt.password or None)

    def on_connect(client, userdata, flags, reason_code, properties) -> None:
        if reason_code == 0:
            outcome["success"] = f"{config.mqtt.broker}:{config.mqtt.port}"
        else:
            outcome["error"] = f"Broker 拒絕連線：{reason_code}"
        connected.set()

    def on_connect_fail(client, userdata) -> None:
        outcome["error"] = "無法連線至 Broker"
        connected.set()

    client.on_connect = on_connect
    client.on_connect_fail = on_connect_fail
    try:
        client.connect(config.mqtt.broker, config.mqtt.port, 60)
        client.loop_start()
        if not connected.wait(timeout):
            raise TimeoutError("連線逾時")
        if "error" in outcome:
            raise ConnectionError(outcome["error"])
        return outcome["success"]
    finally:
        if client.is_connected():
            client.disconnect()
        client.loop_stop()


def probe_discord(config: AppConfig) -> str:
    async def get_identity() -> str:
        client = discord.Client(intents=discord.Intents.none())
        try:
            await client.login(config.discord.bot_token)
            return str(client.user)
        finally:
            await client.close()

    return asyncio.run(get_identity())


def check_connections(
    config: AppConfig,
    telegram_probe=probe_telegram,
    mqtt_probe=probe_mqtt,
    discord_probe=probe_discord,
) -> list[ConnectionTestResult]:
    results: list[ConnectionTestResult] = []
    secrets = (
        config.telegram.bot_token,
        config.discord.bot_token,
        config.mqtt.password,
        config.mqtt.channel_key.hex(),
    )
    probes = [("Telegram", telegram_probe), ("MQTT", mqtt_probe)]
    if config.discord.enabled:
        probes.append(("Discord", discord_probe))
    for service, probe in probes:
        try:
            detail = probe(config)
            results.append(ConnectionTestResult(service, True, f"連線成功：{detail}"))
        except Exception as exc:
            results.append(
                ConnectionTestResult(
                    service,
                    False,
                    f"連線失敗：{_safe_error_message(exc, secrets)}",
                )
            )
    return results


def flatten_config(data: dict[str, Any]) -> dict[str, str]:
    values = {"logging_level": str(data.get("logging_level", "INFO"))}
    for _, fields in FIELD_GROUPS:
        for path, _, _ in fields:
            section, key = path.split(".", 1)
            default = DEFAULT_CONFIG.get(section, {}).get(key, "")
            values[path] = str(data.get(section, {}).get(key, default))
    features = data.get("features", {})
    discord = data.get("discord", {})
    status = features.get("status_api", {})
    tray = features.get("tray", {})
    updates = features.get("updates", {})
    values.update(
        {
            "features.statistics_enabled": str(
                features.get("statistics_enabled", True)
            ).lower(),
            "discord.enabled": str(discord.get("enabled", False)).lower(),
            "features.multi_route_enabled": str(
                features.get("multi_route_enabled", False)
            ).lower(),
            "features.status_api.enabled": str(status.get("enabled", True)).lower(),
            "features.tray.enabled": str(tray.get("enabled", False)).lower(),
            "features.tray.show_console": str(
                tray.get("show_console", True)
            ).lower(),
            "features.tray.autostart": str(tray.get("autostart", False)).lower(),
            "features.updates.enabled": str(updates.get("enabled", False)).lower(),
            "features.updates.mode": str(updates.get("mode", "notify")),
            "features.updates.interval_hours": str(
                updates.get("interval_hours", 24)
            ),
        }
    )
    routes = data.get("routes")
    if not isinstance(routes, list):
        routes = [
            {
                "name": "預設路由",
                "enabled": True,
                "channel_name": data.get("mqtt", {}).get("channel_name", ""),
                "channel_key": data.get("mqtt", {}).get("channel_key", ""),
                "target_chat_id": data.get("telegram", {}).get("target_chat_id", ""),
                "topic_id": "",
                "discord_channel_id": "",
            }
        ]
    for index in range(5):
        route = routes[index] if index < len(routes) and isinstance(routes[index], dict) else {}
        for key, default in (
            ("name", ""),
            ("enabled", True),
            ("channel_name", ""),
            ("channel_key", ""),
            ("target_chat_id", ""),
            ("topic_id", ""),
            ("discord_channel_id", ""),
        ):
            value = route.get(key, default)
            values[f"routes.{index}.{key}"] = (
                str(value).lower() if key == "enabled" else str(value if value is not None else "")
            )
    # Compatibility keys keep older callers and config files round-trippable.
    values["telegram.target_chat_id"] = values["routes.0.target_chat_id"]
    values["mqtt.channel_name"] = values["routes.0.channel_name"]
    values["mqtt.channel_key"] = values["routes.0.channel_key"]
    return values


def require_object_config(data: Any, source_name: str) -> dict[str, Any]:
    if isinstance(data, dict):
        return data
    raise ConfigError(f"{source_name} 的格式錯誤：最外層必須是 JSON 物件。")


def build_config(values: dict[str, str]) -> dict[str, Any]:
    """Build and validate the JSON-compatible configuration from UI strings."""
    def checked(path: str, default: str) -> bool:
        return values.get(path, default).strip().lower() == "true"

    raw: dict[str, Any] = {
        "logging_level": values["logging_level"],
        "telegram": {
            "bot_token": values["telegram.bot_token"].strip(),
            "target_chat_id": values.get(
                "telegram.target_chat_id",
                values.get("routes.0.target_chat_id", ""),
            ).strip(),
        },
        "discord": {
            "enabled": checked("discord.enabled", "false"),
            "bot_token": values.get("discord.bot_token", "").strip(),
        },
        "mqtt": {
            "broker": values["mqtt.broker"].strip(),
            "port": values["mqtt.port"].strip(),
            "username": values["mqtt.username"],
            "password": values["mqtt.password"],
            "root_topic": values["mqtt.root_topic"].strip(),
            "channel_name": values.get(
                "mqtt.channel_name",
                values.get("routes.0.channel_name", ""),
            ).strip(),
            "channel_key": values.get(
                "mqtt.channel_key",
                values.get("routes.0.channel_key", ""),
            ).strip(),
        },
        "node": {
            "id": values["node.id"].strip(),
            "long_name": values["node.long_name"].strip(),
            "short_name": values["node.short_name"].strip(),
        },
        "bridge_ui": {
            "display_name": values.get("bridge_ui.display_name", "Bridge UI").strip(),
        },
        "features": {
            "statistics_enabled": checked("features.statistics_enabled", "true"),
            "multi_route_enabled": checked("features.multi_route_enabled", "false"),
            "status_api": {
                "enabled": checked("features.status_api.enabled", "true")
            },
            "tray": {
                "enabled": checked("features.tray.enabled", "false"),
                "show_console": checked("features.tray.show_console", "true"),
                "autostart": checked("features.tray.autostart", "false"),
            },
            "updates": {
                "enabled": checked("features.updates.enabled", "false"),
                "mode": values.get("features.updates.mode", "notify"),
                "interval_hours": values.get(
                    "features.updates.interval_hours", "24"
                ),
            },
        },
    }
    routes = []
    for index in range(5):
        prefix = f"routes.{index}."
        if not values.get(prefix + "name", "").strip():
            continue
        routes.append(
            {
                "name": values[prefix + "name"].strip(),
                "enabled": checked(prefix + "enabled", "true"),
                "channel_name": values[prefix + "channel_name"].strip(),
                "channel_key": values[prefix + "channel_key"].strip(),
                "target_chat_id": values[prefix + "target_chat_id"].strip(),
                "topic_id": values.get(prefix + "topic_id", "").strip() or None,
                "discord_channel_id": values.get(
                    prefix + "discord_channel_id", ""
                ).strip() or None,
            }
        )
    if routes:
        raw["routes"] = routes
    validated = AppConfig.from_dict(raw)

    # Store numeric fields as JSON numbers and normalized non-secret text values.
    raw["logging_level"] = validated.logging_level
    raw["telegram"]["target_chat_id"] = validated.telegram.target_chat_id
    raw["discord"] = {
        "enabled": validated.discord.enabled,
        "bot_token": validated.discord.bot_token,
    }
    raw["mqtt"]["port"] = validated.mqtt.port
    raw["mqtt"]["root_topic"] = validated.mqtt.root_topic
    raw["node"]["id"] = validated.node.node_id
    raw["bridge_ui"]["display_name"] = validated.bridge_ui.display_name
    raw["features"]["statistics_enabled"] = validated.features.statistics_enabled
    raw["features"]["multi_route_enabled"] = validated.features.multi_route_enabled
    raw["features"]["status_api"]["enabled"] = validated.features.status.enabled
    raw["features"]["tray"] = {
        "enabled": validated.features.tray.enabled,
        "show_console": validated.features.tray.show_console,
        "autostart": validated.features.tray.autostart,
    }
    raw["features"]["updates"] = {
        "enabled": validated.features.updates.enabled,
        "mode": validated.features.updates.mode,
        "interval_hours": validated.features.updates.interval_hours,
    }
    if "routes" in raw:
        raw["routes"] = [
            {
                "name": route.name,
                "enabled": route.enabled,
                "channel_name": route.channel_name,
                "channel_key": raw["routes"][index]["channel_key"],
                "target_chat_id": route.target_chat_id,
                "topic_id": route.topic_id,
                "discord_channel_id": route.discord_channel_id,
            }
            for index, route in enumerate(validated.routes)
        ]
    return raw


def save_config_atomic(data: dict[str, Any], path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".config-", suffix=".tmp", delete=False
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


class SettingsEditor(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"MeshTelegram Bridge 設定工具 v{__version__}")
        self.geometry("900x850")
        self.minsize(760, 700)

        self.variables: dict[str, tk.StringVar] = {
            "logging_level": tk.StringVar(value="INFO")
        }
        self.secret_entries: list[ttk.Entry] = []
        self.show_secrets = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="就緒")
        self.chat_status = tk.StringVar(value="正在連線 Bridge…")
        self.chat_route = tk.StringVar()
        self.chat_target = tk.StringVar(value="全部平台")
        self.chat_byte_count = tk.StringVar(value="0 bytes")
        self._chat_route_ids: dict[str, str] = {}
        self._last_chat_message_id = 0
        self._chat_polling = False
        self._chat_connected = False
        self._chat_feedback_until = 0.0

        self._build_ui()
        self.load()
        self.after(500, self._refresh_runtime_status)
        self.after(700, self._poll_chat_messages)
        self.after(1000, self._maybe_check_updates)

    def _build_ui(self) -> None:
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)
        chat_page = ttk.Frame(notebook)
        settings_page = ttk.Frame(notebook)
        notebook.add(chat_page, text="聊天")
        notebook.add(settings_page, text="設定")
        self._build_chat_ui(chat_page)

        container = ttk.Frame(settings_page)
        container.pack(fill="both", expand=True)
        canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        outer = ttk.Frame(canvas, padding=14)
        window_id = canvas.create_window((0, 0), window=outer, anchor="nw")

        def update_scroll_region(event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def fill_width(event) -> None:
            canvas.itemconfigure(window_id, width=event.width)

        outer.bind("<Configure>", update_scroll_region)
        canvas.bind("<Configure>", fill_width)
        canvas.bind_all(
            "<MouseWheel>",
            lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"),
        )
        outer.columnconfigure(0, weight=1)

        title = ttk.Label(
            outer,
            text=f"MeshTelegram Bridge 設定工具 v{__version__}",
            font=("Segoe UI", 16, "bold"),
        )
        title.grid(row=0, column=0, sticky="w", pady=(0, 10))

        general = ttk.LabelFrame(outer, text="一般設定", padding=10)
        general.grid(row=1, column=0, sticky="ew", pady=4)
        general.columnconfigure(1, weight=1)
        ttk.Label(general, text="記錄層級").grid(row=0, column=0, sticky="w", padx=(0, 10))
        logging_box = ttk.Combobox(
            general,
            textvariable=self.variables["logging_level"],
            values=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
            state="readonly",
        )
        logging_box.grid(row=0, column=1, sticky="ew")

        row = 2
        for group_name, fields in FIELD_GROUPS:
            frame = ttk.LabelFrame(outer, text=group_name, padding=10)
            frame.grid(row=row, column=0, sticky="ew", pady=4)
            frame.columnconfigure(1, weight=1)
            for field_row, (path, label, secret) in enumerate(fields):
                variable = tk.StringVar()
                self.variables[path] = variable
                ttk.Label(frame, text=label).grid(
                    row=field_row, column=0, sticky="w", padx=(0, 10), pady=3
                )
                entry = ttk.Entry(frame, textvariable=variable, show="•" if secret else "")
                entry.grid(row=field_row, column=1, sticky="ew", pady=3)
                if path == "bridge_ui.display_name":
                    variable.trace_add("write", lambda *_: self._update_chat_byte_count())
                if secret:
                    self.secret_entries.append(entry)
                if path == "node.id":
                    ttk.Button(frame, text="隨機產生", command=self.generate_node_id).grid(
                        row=field_row, column=2, padx=(8, 0)
                    )
            row += 1

        feature_frame = ttk.LabelFrame(outer, text="功能設定", padding=10)
        feature_frame.grid(row=row, column=0, sticky="ew", pady=4)
        feature_options = (
            ("discord.enabled", "啟用 Discord 橋接"),
            ("features.statistics_enabled", "啟用執行統計"),
            ("features.multi_route_enabled", "啟用多頻道路由"),
            ("features.status_api.enabled", "啟用本機狀態 API"),
            ("features.tray.enabled", "啟用系統匣"),
            ("features.tray.show_console", "系統匣模式仍顯示主控台"),
            ("features.tray.autostart", "登入 Windows 後自動啟動"),
            ("features.updates.enabled", "啟用更新檢查"),
        )
        for index, (path, label) in enumerate(feature_options):
            variable = tk.StringVar(value="false")
            self.variables[path] = variable
            ttk.Checkbutton(
                feature_frame,
                text=label,
                variable=variable,
                onvalue="true",
                offvalue="false",
            ).grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 24), pady=2)
        ttk.Label(feature_frame, text="更新方式").grid(row=4, column=0, sticky="w")
        self.variables["features.updates.mode"] = tk.StringVar(value="notify")
        ttk.Combobox(
            feature_frame,
            textvariable=self.variables["features.updates.mode"],
            values=("notify", "download", "install"),
            state="readonly",
            width=12,
        ).grid(row=4, column=0, sticky="e", padx=(75, 24))
        ttk.Label(feature_frame, text="檢查間隔（小時）").grid(
            row=4, column=1, sticky="w"
        )
        self.variables["features.updates.interval_hours"] = tk.StringVar(value="24")
        ttk.Entry(
            feature_frame,
            textvariable=self.variables["features.updates.interval_hours"],
            width=8,
        ).grid(row=4, column=1, sticky="e")
        row += 1

        route_frame = ttk.LabelFrame(
            outer,
            text="多頻道路由（啟用多頻道路由後生效，最多 5 組）",
            padding=8,
        )
        route_frame.grid(row=row, column=0, sticky="ew", pady=4)
        route_frame.columnconfigure(0, weight=1)
        route_tabs = ttk.Notebook(route_frame)
        route_tabs.grid(row=0, column=0, sticky="ew")
        route_fields = (
            ("name", "路由名稱", False),
            ("channel_name", "Meshtastic 頻道", False),
            ("channel_key", "頻道金鑰", True),
            ("target_chat_id", "Telegram 聊天室 ID", False),
            ("topic_id", "Telegram 主題 ID（可留空）", False),
            ("discord_channel_id", "Discord 頻道 ID（可留空）", False),
        )
        for index in range(5):
            page = ttk.Frame(route_tabs, padding=8)
            page.columnconfigure(1, weight=1)
            route_tabs.add(page, text=f"路由 {index + 1}")
            enabled_path = f"routes.{index}.enabled"
            self.variables[enabled_path] = tk.StringVar(value="true")
            ttk.Checkbutton(
                page,
                text="啟用此路由",
                variable=self.variables[enabled_path],
                onvalue="true",
                offvalue="false",
            ).grid(row=0, column=0, columnspan=2, sticky="w")
            for field_row, (key, label, secret) in enumerate(route_fields, start=1):
                path = f"routes.{index}.{key}"
                self.variables[path] = tk.StringVar()
                ttk.Label(page, text=label).grid(
                    row=field_row, column=0, sticky="w", padx=(0, 10), pady=2
                )
                entry = ttk.Entry(
                    page,
                    textvariable=self.variables[path],
                    show="•" if secret else "",
                )
                entry.grid(row=field_row, column=1, sticky="ew", pady=2)
                if secret:
                    self.secret_entries.append(entry)
        row += 1

        runtime_frame = ttk.LabelFrame(outer, text="即時執行狀態", padding=10)
        runtime_frame.grid(row=row, column=0, sticky="ew", pady=4)
        runtime_frame.columnconfigure(0, weight=1)
        self.runtime_status = tk.StringVar(value="Bridge 未執行或狀態 API 未啟用")
        ttk.Label(
            runtime_frame,
            textvariable=self.runtime_status,
            justify="left",
        ).grid(row=0, column=0, sticky="w")
        row += 1

        controls = ttk.Frame(outer)
        controls.grid(row=row, column=0, sticky="ew", pady=(10, 4))
        ttk.Checkbutton(
            controls,
            text="顯示敏感資訊",
            variable=self.show_secrets,
            command=self.toggle_secrets,
        ).pack(side="left")
        ttk.Button(controls, text="重新載入", command=self.load).pack(side="right", padx=(8, 0))
        ttk.Button(controls, text="儲存", command=self.save).pack(side="right", padx=(8, 0))
        ttk.Button(controls, text="驗證", command=self.validate).pack(side="right")
        self.test_button = ttk.Button(controls, text="測試連線", command=self.test_connections)
        self.test_button.pack(side="right", padx=(8, 0))
        self.update_button = ttk.Button(
            controls,
            text="立即檢查更新",
            command=lambda: self.check_updates(manual=True),
        )
        self.update_button.pack(side="right", padx=(8, 0))
        ttk.Button(controls, text="開啟日誌資料夾", command=self.open_log_folder).pack(
            side="right", padx=(8, 0)
        )

        ttk.Separator(outer).grid(row=row + 1, column=0, sticky="ew", pady=(6, 8))
        ttk.Label(outer, textvariable=self.status).grid(row=row + 2, column=0, sticky="w")

    def _build_chat_ui(self, parent: ttk.Frame) -> None:
        outer = ttk.Frame(parent, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        ttk.Label(
            outer,
            text=f"MeshTelegram Bridge 聊天 v{__version__}",
            font=("Segoe UI", 16, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(outer, textvariable=self.chat_status).grid(
            row=1, column=0, sticky="w", pady=(4, 8)
        )

        history_frame = ttk.Frame(outer)
        history_frame.grid(row=2, column=0, sticky="nsew")
        history_frame.columnconfigure(0, weight=1)
        history_frame.rowconfigure(0, weight=1)
        self.chat_history = tk.Text(
            history_frame,
            wrap="word",
            state="disabled",
            font=("Segoe UI", 10),
            padx=8,
            pady=8,
        )
        history_scroll = ttk.Scrollbar(
            history_frame, orient="vertical", command=self.chat_history.yview
        )
        self.chat_history.configure(yscrollcommand=history_scroll.set)
        self.chat_history.grid(row=0, column=0, sticky="nsew")
        history_scroll.grid(row=0, column=1, sticky="ns")
        self.chat_history.tag_configure("meta", foreground="#666666")
        self.chat_history.tag_configure("telegram", foreground="#1677b8")
        self.chat_history.tag_configure("meshtastic", foreground="#17823b")
        self.chat_history.tag_configure("discord", foreground="#5865f2")
        self.chat_history.tag_configure("bridge_ui", foreground="#8a4f00")

        options = ttk.Frame(outer)
        options.grid(row=3, column=0, sticky="ew", pady=(10, 6))
        options.columnconfigure(1, weight=1)
        ttk.Label(options, text="路由").grid(row=0, column=0, sticky="w")
        self.chat_route_box = ttk.Combobox(
            options,
            textvariable=self.chat_route,
            state="readonly",
            width=28,
        )
        self.chat_route_box.grid(row=0, column=1, sticky="ew", padx=(8, 18))
        self.chat_route_box.bind("<<ComboboxSelected>>", self._update_chat_byte_count)
        ttk.Label(options, text="發送到").grid(row=0, column=2, sticky="w")
        self.chat_target_box = ttk.Combobox(
            options,
            textvariable=self.chat_target,
            values=("全部平台", "Meshtastic", "Telegram", "Discord"),
            state="readonly",
            width=14,
        )
        self.chat_target_box.grid(row=0, column=3, sticky="e", padx=(8, 0))
        self.chat_target_box.bind("<<ComboboxSelected>>", self._update_chat_byte_count)

        compose = ttk.Frame(outer)
        compose.grid(row=4, column=0, sticky="ew")
        compose.columnconfigure(0, weight=1)
        self.chat_input = tk.Text(compose, height=4, wrap="word", font=("Segoe UI", 10))
        self.chat_input.grid(row=0, column=0, sticky="ew")
        self.chat_input.bind("<KeyRelease>", self._update_chat_byte_count)
        self.chat_input.bind("<Control-Return>", self._send_chat_from_shortcut)
        side = ttk.Frame(compose)
        side.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        ttk.Label(side, textvariable=self.chat_byte_count).pack(anchor="e")
        self.chat_send_button = ttk.Button(
            side,
            text="發送",
            command=self.send_chat,
            state="disabled",
        )
        self.chat_send_button.pack(side="bottom", fill="x")
        ttk.Label(
            outer,
            text="Ctrl+Enter 發送；聊天內容僅保留於 Bridge 記憶體，重啟後清空。",
            foreground="#666666",
        ).grid(row=5, column=0, sticky="w", pady=(5, 0))

    def current_values(self) -> dict[str, str]:
        return {key: variable.get() for key, variable in self.variables.items()}

    def load(self) -> None:
        try:
            if CONFIG_PATH.exists():
                source_name = CONFIG_PATH.name
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            elif EXAMPLE_PATH.exists():
                source_name = EXAMPLE_PATH.name
                data = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
            else:
                source_name = "內建預設值"
                data = DEFAULT_CONFIG
            data = require_object_config(data, source_name)
            for key, value in flatten_config(data).items():
                if key in self.variables:
                    self.variables[key].set(value)
            self.status.set(f"已載入 {source_name}")
        except (ConfigError, OSError, json.JSONDecodeError) as exc:
            messagebox.showerror("載入失敗", str(exc), parent=self)
            self.status.set("載入失敗")

    def validate(self) -> bool:
        try:
            build_config(self.current_values())
        except (ConfigError, KeyError) as exc:
            messagebox.showerror("設定內容不正確", str(exc), parent=self)
            self.status.set("驗證失敗")
            return False
        self.status.set("所有設定均有效")
        messagebox.showinfo("驗證完成", "所有設定均有效。", parent=self)
        return True

    def save(self) -> None:
        try:
            data = build_config(self.current_values())
            save_config_atomic(data)
        except (ConfigError, KeyError, OSError) as exc:
            messagebox.showerror("儲存失敗", str(exc), parent=self)
            self.status.set("儲存失敗")
            return
        self.status.set(f"已儲存 {CONFIG_PATH.name}")
        messagebox.showinfo("儲存完成", "設定已成功儲存。", parent=self)

    def toggle_secrets(self) -> None:
        show = "" if self.show_secrets.get() else "•"
        for entry in self.secret_entries:
            entry.configure(show=show)

    def generate_node_id(self) -> None:
        self.variables["node.id"].set(str(random.SystemRandom().randint(1, 0xFFFFFFFF)))
        self.status.set("已產生隨機節點 ID")

    def open_log_folder(self) -> None:
        try:
            os.startfile(PROJECT_DIR)  # type: ignore[attr-defined]
        except (AttributeError, OSError) as exc:
            messagebox.showerror("無法開啟資料夾", str(exc), parent=self)

    def test_connections(self) -> None:
        try:
            config = AppConfig.from_dict(build_config(self.current_values()))
        except (ConfigError, KeyError) as exc:
            messagebox.showerror("無法測試連線", str(exc), parent=self)
            self.status.set("設定內容不正確")
            return

        self.test_button.configure(state="disabled")
        service_names = "Telegram、MQTT 與 Discord" if config.discord.enabled else "Telegram 與 MQTT"
        self.status.set(f"正在測試 {service_names} 連線…")

        def worker() -> None:
            results = check_connections(config)
            self.after(0, self._show_connection_results, results)

        threading.Thread(target=worker, daemon=True).start()

    def _show_connection_results(self, results: list[ConnectionTestResult]) -> None:
        self.test_button.configure(state="normal")
        details = "\n".join(
            f"{result.service}：{result.message}" for result in results
        )
        if all(result.succeeded for result in results):
            self.status.set("所有連線測試成功")
            messagebox.showinfo("連線測試完成", details, parent=self)
        else:
            self.status.set("部分連線測試失敗")
            messagebox.showerror("連線測試完成", details, parent=self)

    def _refresh_runtime_status(self) -> None:
        try:
            snapshot = fetch_status(STATUS_DISCOVERY_PATH)
            telegram = snapshot.get("telegram", {})
            discord = snapshot.get("discord", {})
            routes = snapshot.get("routes", {})
            stats = snapshot.get("statistics", {})
            route_lines = [
                f"{route.get('name', route_id)}：MQTT {route.get('mqtt_status', '未知')} "
                f"({route.get('broker', '')})"
                + (
                    f"；錯誤：{route.get('last_error')}"
                    if route.get("last_error")
                    else ""
                )
                for route_id, route in routes.items()
            ]
            summary = (
                f"Telegram：{telegram.get('status', '未知')} "
                f"{telegram.get('bot_name') or ''}\n"
                f"Discord：{discord.get('status', '未啟用')} "
                f"{discord.get('bot_name') or ''}\n"
                + ("\n".join(route_lines) or "MQTT：尚無路由")
                + "\n"
                + (
                    "統計：TG→Mesh {telegram_to_mesh_success}、"
                    "Mesh→TG {mesh_to_telegram_success}、DC→Mesh {discord_to_mesh_success}、"
                    "Mesh→DC {mesh_to_discord_success}、未授權 {unauthorized_dropped}、"
                    "過長 {oversized_dropped}、解密失敗 {decrypt_failed}、"
                    "重複 {duplicate_packets}"
                ).format_map({key: stats.get(key, 0) for key in (
                    "telegram_to_mesh_success",
                    "mesh_to_telegram_success",
                    "discord_to_mesh_success",
                    "mesh_to_discord_success",
                    "unauthorized_dropped",
                    "oversized_dropped",
                    "decrypt_failed",
                    "duplicate_packets",
                )})
            )
            recent_error = telegram.get("last_error")
            recent_error = recent_error or discord.get("last_error")
            if recent_error:
                summary += f"\n最近錯誤：{recent_error}"
            self.runtime_status.set(summary)
            self._update_chat_routes(routes)
            self._chat_connected = True
            if time.monotonic() >= self._chat_feedback_until:
                self.chat_status.set("Bridge 已連線，正在監看所有啟用路由")
            self._update_chat_byte_count()
        except (StatusUnavailable, OSError, ValueError):
            self.runtime_status.set("Bridge 未執行或狀態心跳已超過 5 秒")
            self._chat_connected = False
            self.chat_status.set("Bridge 未執行或聊天 API 無法連線")
            self.chat_send_button.configure(state="disabled")
        finally:
            self.after(2000, self._refresh_runtime_status)

    def _update_chat_routes(self, routes: dict[str, dict[str, Any]]) -> None:
        current_id = self._chat_route_ids.get(self.chat_route.get())
        mapping = {
            f"{route.get('name', route_id)} ({route_id})": route_id
            for route_id, route in routes.items()
        }
        self._chat_route_ids = mapping
        values = tuple(mapping.keys())
        self.chat_route_box.configure(values=values)
        selected = next(
            (label for label, route_id in mapping.items() if route_id == current_id),
            values[0] if values else "",
        )
        self.chat_route.set(selected)

    def _poll_chat_messages(self) -> None:
        if self._chat_polling:
            self.after(1000, self._poll_chat_messages)
            return
        self._chat_polling = True
        after_id = self._last_chat_message_id

        def worker() -> None:
            try:
                result = fetch_messages(
                    STATUS_DISCOVERY_PATH,
                    after_id=after_id,
                )
                self.after(0, self._apply_chat_messages, result, None)
            except Exception as exc:
                self.after(0, self._apply_chat_messages, None, exc)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_chat_messages(self, result, error) -> None:
        self._chat_polling = False
        if error is None and result is not None:
            for message in result.get("messages", []):
                self._append_chat_message(message)
            self._last_chat_message_id = max(
                self._last_chat_message_id,
                int(result.get("latest_id", 0)),
            )
        self.after(1000, self._poll_chat_messages)

    def _append_chat_message(self, message: dict[str, Any]) -> None:
        timestamp = datetime.fromtimestamp(float(message.get("timestamp", 0))).strftime("%H:%M:%S")
        route_name = str(message.get("route_name", message.get("route_id", "")))
        source = str(message.get("source", "unknown"))
        source_names = {
            "telegram": "Telegram",
            "meshtastic": "Meshtastic",
            "discord": "Discord",
        }
        sender = str(message.get("sender", ""))
        source_name = sender if source == "bridge_ui" else source_names.get(source, source)
        sender_suffix = "" if source == "bridge_ui" else f" · {sender}"
        text = str(message.get("text", ""))
        self.chat_history.configure(state="normal")
        self.chat_history.insert(
            "end",
            f"[{timestamp}] {route_name} · {source_name}{sender_suffix}\n",
            ("meta",),
        )
        self.chat_history.insert("end", text + "\n\n", (source,))
        self.chat_history.configure(state="disabled")
        self.chat_history.see("end")

    def _update_chat_byte_count(self, event=None) -> None:
        text = self.chat_input.get("1.0", "end-1c").strip()
        uses_mesh = self.chat_target.get() in {"全部平台", "Meshtastic"}
        display_name = self.variables.get("bridge_ui.display_name")
        name = display_name.get().strip() if display_name is not None else "Bridge UI"
        payload = (f"[{name or 'Bridge UI'}]: " + text) if uses_mesh else text
        byte_count = len(payload.encode("utf-8"))
        uses_discord = self.chat_target.get() in {"全部平台", "Discord"}
        limit = 233 if uses_mesh else (2000 if uses_discord else 4000)
        self.chat_byte_count.set(f"{byte_count}/{limit} bytes")
        valid = (
            self._chat_connected
            and bool(self._chat_route_ids.get(self.chat_route.get()))
            and bool(text)
            and byte_count <= limit
        )
        self.chat_send_button.configure(state="normal" if valid else "disabled")

    def _send_chat_from_shortcut(self, event=None):
        if str(self.chat_send_button.cget("state")) != "disabled":
            self.send_chat()
        return "break"

    def send_chat(self) -> None:
        route_id = self._chat_route_ids.get(self.chat_route.get())
        text = self.chat_input.get("1.0", "end-1c").strip()
        target = {
            "全部平台": "all",
            "Meshtastic": "meshtastic",
            "Telegram": "telegram",
            "Discord": "discord",
        }.get(self.chat_target.get(), "all")
        if not route_id or not text:
            return
        self.chat_send_button.configure(state="disabled")
        self.chat_status.set("正在傳送訊息…")
        self._chat_feedback_until = time.monotonic() + 20

        def worker() -> None:
            try:
                result = send_chat_message(
                    STATUS_DISCOVERY_PATH,
                    route_id=route_id,
                    text=text,
                    target=target,
                )
                self.after(0, self._finish_chat_send, result, None)
            except Exception as exc:
                self.after(0, self._finish_chat_send, None, exc)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_chat_send(self, result, error) -> None:
        if error is not None:
            message = str(error) if isinstance(error, ChatSendError) else "訊息傳送失敗"
            self.chat_status.set(message)
            self._chat_feedback_until = time.monotonic() + 5
            messagebox.showerror("訊息傳送失敗", message, parent=self)
            self._update_chat_byte_count()
            return
        sent_names = {
            "meshtastic": "Meshtastic",
            "telegram": "Telegram",
        }
        sent = [sent_names.get(item, item) for item in result.get("sent", [])]
        errors = result.get("errors", {})
        summary = f"已傳送到：{'、'.join(sent)}"
        if errors:
            summary += "；部分失敗：" + "、".join(errors.values())
        self.chat_status.set(summary)
        self._chat_feedback_until = time.monotonic() + 5
        self.chat_input.delete("1.0", "end")
        self._update_chat_byte_count()

    def _maybe_check_updates(self) -> None:
        try:
            config = AppConfig.from_dict(build_config(self.current_values()))
        except (ConfigError, KeyError):
            return
        updates = config.features.updates
        if updates.enabled and should_check(UPDATE_STATE_PATH, updates.interval_hours):
            self.check_updates(manual=False)

    def check_updates(self, *, manual: bool) -> None:
        try:
            config = AppConfig.from_dict(build_config(self.current_values()))
        except (ConfigError, KeyError) as exc:
            if manual:
                messagebox.showerror("無法檢查更新", str(exc), parent=self)
            return
        self.update_button.configure(state="disabled")
        self.status.set("正在檢查 GitHub 正式 Release…")

        def worker() -> None:
            try:
                release = fetch_latest_release()
                record_check(UPDATE_STATE_PATH)
                outcome = ("release", release, config.features.updates.mode)
            except Exception as exc:
                outcome = ("error", exc, config.features.updates.mode)
            self.after(0, self._show_update_result, outcome, manual)

        threading.Thread(target=worker, daemon=True).start()

    def _show_update_result(self, outcome: tuple, manual: bool) -> None:
        self.update_button.configure(state="normal")
        kind, value, mode = outcome
        if kind == "error":
            self.status.set("更新檢查失敗")
            if manual:
                messagebox.showerror("更新檢查失敗", str(value), parent=self)
            return
        release = value
        try:
            newer = is_newer(release, __version__)
        except UpdateError as exc:
            self.status.set("Release 版本格式無效")
            if manual:
                messagebox.showerror("更新檢查失敗", str(exc), parent=self)
            return
        if not newer:
            self.status.set(f"目前已是最新版 v{__version__}")
            if manual:
                messagebox.showinfo(
                    "沒有可用更新",
                    f"目前已是最新版 v{__version__}。",
                    parent=self,
                )
            return
        self.status.set(f"發現新版本 {release.version}")
        if mode == "notify":
            if messagebox.askyesno(
                "發現新版本",
                f"發現正式版本 {release.version}，是否開啟 Release 頁面？",
                parent=self,
            ):
                webbrowser.open(release.page_url)
            return
        self.status.set(f"正在下載 {release.version}…")

        def download_worker() -> None:
            try:
                files = download_portable_release(release, PROJECT_DIR)
                result = ("downloaded", files, mode, release.version)
            except Exception as exc:
                result = ("download_error", exc, mode, release.version)
            self.after(0, self._finish_update_download, result)

        threading.Thread(target=download_worker, daemon=True).start()

    def _finish_update_download(self, result: tuple) -> None:
        kind, value, mode, version = result
        if kind == "download_error":
            self.status.set("更新下載失敗")
            messagebox.showerror("更新下載失敗", str(value), parent=self)
            return
        files = value
        if mode == "download":
            self.status.set(f"{version} 已下載至 .update 資料夾")
            messagebox.showinfo(
                "更新已下載",
                "兩個執行檔已完成 SHA-256 驗證並存放於 .update 資料夾。",
                parent=self,
            )
            return
        try:
            schedule_portable_install(files, PROJECT_DIR)
        except UpdateError as exc:
            messagebox.showerror("無法自動安裝", str(exc), parent=self)
            return
        messagebox.showinfo(
            "準備安裝",
            "設定工具關閉後會更新可用的執行檔；若 Bridge 正在執行，"
            "其檔案會在 Bridge 結束後完成替換。",
            parent=self,
        )
        self.destroy()


def main() -> None:
    SettingsEditor().mainloop()


if __name__ == "__main__":
    main()
