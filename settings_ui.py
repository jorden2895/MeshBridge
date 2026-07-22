from __future__ import annotations

import asyncio
import json
import os
import random
import tempfile
import tkinter as tk
import threading
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

import paho.mqtt.client as mqtt
from telegram import Bot

from app_paths import application_dir
from config import AppConfig, ConfigError
from version import __version__


PROJECT_DIR = application_dir()
CONFIG_PATH = PROJECT_DIR / "config.json"
EXAMPLE_PATH = PROJECT_DIR / "config.json.example"
CONNECTION_TIMEOUT_SECONDS = 10

DEFAULT_CONFIG: dict[str, Any] = {
    "logging_level": "INFO",
    "telegram": {
        "bot_token": "YOUR_TELEGRAM_BOT_TOKEN_HERE",
        "target_chat_id": "YOUR_TARGET_CHAT_ID_HERE",
    },
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
}

FIELD_GROUPS = (
    (
        "Telegram 設定",
        (
            ("telegram.bot_token", "機器人權杖", True),
            ("telegram.target_chat_id", "目標聊天室 ID", False),
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
            ("mqtt.channel_name", "頻道名稱", False),
            ("mqtt.channel_key", "頻道金鑰", True),
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
    client.username_pw_set(config.mqtt.username, config.mqtt.password)

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


def check_connections(
    config: AppConfig,
    telegram_probe=probe_telegram,
    mqtt_probe=probe_mqtt,
) -> list[ConnectionTestResult]:
    results: list[ConnectionTestResult] = []
    secrets = (
        config.telegram.bot_token,
        config.mqtt.password,
        config.mqtt.channel_key.hex(),
    )
    for service, probe in (("Telegram", telegram_probe), ("MQTT", mqtt_probe)):
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
            values[path] = str(data.get(section, {}).get(key, ""))
    return values


def require_object_config(data: Any, source_name: str) -> dict[str, Any]:
    if isinstance(data, dict):
        return data
    raise ConfigError(f"{source_name} 的格式錯誤：最外層必須是 JSON 物件。")


def build_config(values: dict[str, str]) -> dict[str, Any]:
    """Build and validate the JSON-compatible configuration from UI strings."""
    raw: dict[str, Any] = {
        "logging_level": values["logging_level"],
        "telegram": {
            "bot_token": values["telegram.bot_token"].strip(),
            "target_chat_id": values["telegram.target_chat_id"].strip(),
        },
        "mqtt": {
            "broker": values["mqtt.broker"].strip(),
            "port": values["mqtt.port"].strip(),
            "username": values["mqtt.username"],
            "password": values["mqtt.password"],
            "root_topic": values["mqtt.root_topic"].strip(),
            "channel_name": values["mqtt.channel_name"].strip(),
            "channel_key": values["mqtt.channel_key"].strip(),
        },
        "node": {
            "id": values["node.id"].strip(),
            "long_name": values["node.long_name"].strip(),
            "short_name": values["node.short_name"].strip(),
        },
    }
    validated = AppConfig.from_dict(raw)

    # Store numeric fields as JSON numbers and normalized non-secret text values.
    raw["logging_level"] = validated.logging_level
    raw["telegram"]["target_chat_id"] = validated.telegram.target_chat_id
    raw["mqtt"]["port"] = validated.mqtt.port
    raw["mqtt"]["root_topic"] = validated.mqtt.root_topic
    raw["node"]["id"] = validated.node.node_id
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
        self.geometry("760x720")
        self.minsize(620, 600)

        self.variables: dict[str, tk.StringVar] = {
            "logging_level": tk.StringVar(value="INFO")
        }
        self.secret_entries: list[ttk.Entry] = []
        self.show_secrets = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="就緒")

        self._build_ui()
        self.load()

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
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
                if secret:
                    self.secret_entries.append(entry)
                if path == "node.id":
                    ttk.Button(frame, text="隨機產生", command=self.generate_node_id).grid(
                        row=field_row, column=2, padx=(8, 0)
                    )
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
        ttk.Button(controls, text="開啟日誌資料夾", command=self.open_log_folder).pack(
            side="right", padx=(8, 0)
        )

        ttk.Separator(outer).grid(row=row + 1, column=0, sticky="ew", pady=(6, 8))
        ttk.Label(outer, textvariable=self.status).grid(row=row + 2, column=0, sticky="w")

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
        self.status.set("正在測試 Telegram 與 MQTT 連線…")

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
            self.status.set("Telegram 與 MQTT 連線測試成功")
            messagebox.showinfo("連線測試完成", details, parent=self)
        else:
            self.status.set("部分連線測試失敗")
            messagebox.showerror("連線測試完成", details, parent=self)


def main() -> None:
    SettingsEditor().mainloop()


if __name__ == "__main__":
    main()
