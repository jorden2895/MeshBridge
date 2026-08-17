from __future__ import annotations

import copy
import json
import logging
import os
import queue
import random
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox

from automation import AutomationEngine
from app_controller import AppController
from config import AppConfig, ConfigError, CURRENT_CONFIG_VERSION, MAX_ROUTES
from config_store import default_config_data
from connection_probes import check_connections
from log_buffer import InMemoryLogHandler
from mqtt_service import MAX_MESHTASTIC_PAYLOAD_BYTES
from version import __version__


logger = logging.getLogger(__name__)
DEFAULT_UI_FONT = "Microsoft JhengHei UI"
THEME_TO_UI = {"system": "系統", "light": "淺色", "dark": "深色"}
UI_TO_THEME = {value: key for key, value in THEME_TO_UI.items()}
LOG_LEVEL_TO_UI = {
    "INFO": "INFO（一般）",
    "WARNING": "WARNING（警告）",
    "DEBUG": "DEBUG（詳細）",
}
UI_TO_LOG_LEVEL = {value: key for key, value in LOG_LEVEL_TO_UI.items()}
UPDATE_MODE_TO_UI = {
    "notify": "只通知（建議，不自動下載）",
    "download": "自動下載（不自動安裝）",
    "install": "自動下載並安裝",
}
UI_TO_UPDATE_MODE = {value: key for key, value in UPDATE_MODE_TO_UI.items()}
STATUS_TO_UI = {
    "starting": "啟動中",
    "connecting": "連線中",
    "running": "運行中",
    "connected": "已連線",
    "reconnecting": "重新連線中",
    "stopping": "停止中",
    "stopped": "已停止",
    "disabled": "未啟用",
    "error": "發生錯誤",
}
STAT_GROUPS = (
    (
        "成功轉送",
        (
            ("telegram_to_mesh_success", "Telegram → Meshtastic"),
            ("mesh_to_telegram_success", "Meshtastic → Telegram"),
            ("discord_to_mesh_success", "Discord → Meshtastic"),
            ("mesh_to_discord_success", "Meshtastic → Discord"),
            ("telegram_to_discord_success", "Telegram → Discord"),
            ("discord_to_telegram_success", "Discord → Telegram"),
        ),
    ),
    (
        "未傳送或已忽略",
        (
            ("unauthorized_dropped", "未授權訊息"),
            ("oversized_dropped", "訊息過長"),
            ("decrypt_failed", "解密失敗"),
            ("duplicate_packets", "重複封包"),
            ("disconnected_dropped", "離線期間訊息"),
            ("other_dropped", "其他未傳送訊息"),
        ),
    ),
)
TARGET_TO_API = {
    "全部平台": "all",
    "Meshtastic": "meshtastic",
    "Telegram": "telegram",
    "Discord": "discord",
}
NAV_TEXT_COLOR = ("#111827", "#f9fafb")
NAV_HOVER_COLOR = ("#e2e8f0", "#263244")
NAV_SELECTED_COLOR = ("#dbeafe", "#1e40af")
APP_BACKGROUND = ("#f4f7fb", "#0b1220")
CARD_BACKGROUND = ("#ffffff", "#172033")
BORDER_COLOR = ("#d7dee9", "#334155")
MUTED_TEXT_COLOR = ("#5b6472", "#aab4c3")
PRIMARY_COLOR = ("#2563eb", "#3b82f6")
PRIMARY_HOVER_COLOR = ("#1d4ed8", "#2563eb")
SECONDARY_COLOR = ("#e9eef5", "#273449")
SECONDARY_HOVER_COLOR = ("#d7e0eb", "#34445c")
SECONDARY_TEXT_COLOR = ("#172033", "#f8fafc")
DANGER_COLOR = ("#dc2626", "#ef4444")
DANGER_HOVER_COLOR = ("#b91c1c", "#dc2626")
STATUS_COLORS = {
    "running": ("#15803d", "#4ade80"),
    "connected": ("#15803d", "#4ade80"),
    "starting": ("#b45309", "#fbbf24"),
    "connecting": ("#b45309", "#fbbf24"),
    "reconnecting": ("#b45309", "#fbbf24"),
    "stopping": ("#b45309", "#fbbf24"),
    "error": ("#b91c1c", "#f87171"),
    "stopped": MUTED_TEXT_COLOR,
    "disabled": MUTED_TEXT_COLOR,
}


def apply_modern_theme() -> None:
    theme = ctk.ThemeManager.theme
    theme["CTk"]["fg_color"] = APP_BACKGROUND
    theme["CTkToplevel"]["fg_color"] = APP_BACKGROUND
    theme["CTkFrame"].update(corner_radius=12, fg_color=CARD_BACKGROUND, border_color=BORDER_COLOR)
    theme["CTkButton"].update(
        corner_radius=8,
        fg_color=PRIMARY_COLOR,
        hover_color=PRIMARY_HOVER_COLOR,
        text_color=("#ffffff", "#ffffff"),
    )
    theme["CTkEntry"].update(
        corner_radius=8,
        border_width=1,
        fg_color=CARD_BACKGROUND,
        border_color=BORDER_COLOR,
    )
    theme["CTkTextbox"].update(
        corner_radius=10,
        border_width=1,
        fg_color=CARD_BACKGROUND,
        border_color=BORDER_COLOR,
    )
    theme["CTkOptionMenu"].update(
        corner_radius=8,
        fg_color=SECONDARY_COLOR,
        button_color=("#dbe3ee", "#34445c"),
        button_hover_color=SECONDARY_HOVER_COLOR,
        text_color=SECONDARY_TEXT_COLOR,
    )
    theme["DropdownMenu"].update(
        fg_color=CARD_BACKGROUND,
        hover_color=SECONDARY_HOVER_COLOR,
        text_color=SECONDARY_TEXT_COLOR,
    )
    theme["CTkScrollableFrame"]["label_fg_color"] = SECONDARY_COLOR


def secondary_button_style() -> dict[str, object]:
    return {
        "fg_color": SECONDARY_COLOR,
        "hover_color": SECONDARY_HOVER_COLOR,
        "text_color": SECONDARY_TEXT_COLOR,
        "border_color": BORDER_COLOR,
        "border_width": 1,
    }


def danger_button_style() -> dict[str, object]:
    return {"fg_color": DANGER_COLOR, "hover_color": DANGER_HOVER_COLOR, "text_color": "#ffffff"}


def normalize_log_level(level: object) -> str:
    normalized = str(level).strip().upper()
    return normalized if normalized in LOG_LEVEL_TO_UI else "WARNING" if normalized in {"ERROR", "CRITICAL"} else "INFO"


def translate_status(status: object) -> str:
    return STATUS_TO_UI.get(str(status).strip().lower(), "未知")


def format_statistics(statistics: dict[str, Any], enabled: bool = True) -> str:
    if not enabled:
        return "執行統計未啟用"
    sections = []
    for title, items in STAT_GROUPS:
        lines = [title]
        lines.extend(f"  {label}：{int(statistics.get(key, 0))}" for key, label in items)
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def friendly_config_error(error: object) -> str:
    message = str(error)
    replacements = {
        "logging_level": "日誌層級（logging_level）",
        "mqtt.broker": "伺服器位址（mqtt.broker）",
        "mqtt.port": "連接埠（mqtt.port）",
        "mqtt.root_topic": "根主題（mqtt.root_topic）",
        "telegram.bot_token": "Telegram 機器人權杖（telegram.bot_token）",
        "discord.bot_token": "Discord 機器人權杖（discord.bot_token）",
        "node.id": "節點識別碼（node.id）",
        "channel_key": "頻道金鑰（channel_key）",
        "target_chat_id": "Telegram 聊天室識別碼（target_chat_id）",
        "topic_id": "Telegram 主題識別碼（topic_id）",
        "discord_channel_id": "Discord 頻道識別碼（discord_channel_id）",
    }
    for original, friendly in replacements.items():
        message = message.replace(original, friendly)
    return message


class MeshBridgeWindow(ctk.CTk):
    def __init__(
        self,
        controller: AppController,
        raw_config: dict[str, Any] | None,
        load_error: str | None,
        log_handler: InMemoryLogHandler,
        *,
        start_hidden: bool = False,
        on_exit: Callable[[], None] | None = None,
    ) -> None:
        self.raw = copy.deepcopy(raw_config or default_config_data())
        theme = str(self.raw.get("appearance", {}).get("theme", "system"))
        ctk.set_appearance_mode(theme)
        ctk.set_default_color_theme("blue")
        apply_modern_theme()
        ctk.ThemeManager.theme["CTkFont"]["family"] = DEFAULT_UI_FONT
        super().__init__()
        self.controller = controller
        self.log_handler = log_handler
        self.on_exit = on_exit
        self.load_error = load_error
        self.title(f"MeshBridge v{__version__}")
        self.geometry("1120x760")
        self.minsize(920, 650)
        self.protocol("WM_DELETE_WINDOW", self.hide_window)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.pages: dict[str, ctk.CTkFrame] = {}
        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        self._route_index = 0
        self._route_vars: dict[str, Any] = {}
        self._settings_vars: dict[str, Any] = {}
        self._keyword_rows: list[dict[str, Any]] = []
        self._schedule_rows: list[dict[str, Any]] = []
        self._last_message_id = 0
        self._chat_generation: int | None = None
        self._last_log_sequence = 0
        self._operation_running = False
        self._tested_config: str | None = None
        self._build_sidebar()
        self._build_pages()
        self.show_page("設定" if raw_config is None else "儀表板")
        self.after(200, self._poll_events)
        self.after(500, self._refresh_runtime)
        self.after(700, self._refresh_chat)
        self.after(700, self._refresh_logs)
        if start_hidden:
            self.withdraw()
        elif raw_config is None:
            self.after(300, self._show_setup_notice)

    def _build_sidebar(self) -> None:
        sidebar = ctk.CTkFrame(
            self,
            width=190,
            corner_radius=0,
            fg_color=(("#e8edf4", "#111827")),
        )
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_rowconfigure(7, weight=1)
        ctk.CTkLabel(
            sidebar,
            text="MeshBridge",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).grid(row=0, column=0, padx=20, pady=(24, 22))
        for row, name in enumerate(("儀表板", "聊天", "路由", "自動化", "設定", "日誌"), start=1):
            button = ctk.CTkButton(
                sidebar,
                text=name,
                anchor="w",
                height=42,
                corner_radius=8,
                fg_color="transparent",
                hover_color=NAV_HOVER_COLOR,
                text_color=NAV_TEXT_COLOR,
                command=lambda selected=name: self.show_page(selected),
            )
            button.grid(row=row, column=0, padx=12, pady=4, sticky="ew")
            self.nav_buttons[name] = button
        self.sidebar_state = ctk.CTkLabel(
            sidebar,
            text="橋接服務：已停止",
            anchor="w",
            justify="left",
            text_color=NAV_TEXT_COLOR,
        )
        self.sidebar_state.grid(row=8, column=0, padx=18, pady=(8, 18), sticky="ew")

    def _page(self, name: str) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        page.grid(row=0, column=1, sticky="nsew")
        self.pages[name] = page
        return page

    def _title(self, page, text: str, subtitle: str = "") -> None:
        ctk.CTkLabel(page, text=text, font=ctk.CTkFont(size=25, weight="bold")).pack(
            anchor="w", padx=24, pady=(22, 2)
        )
        if subtitle:
            ctk.CTkLabel(page, text=subtitle, text_color=MUTED_TEXT_COLOR).pack(
                anchor="w", padx=24, pady=(0, 16)
            )

    def _secret_entry(self, parent, variable: tk.StringVar) -> ctk.CTkEntry:
        container = ctk.CTkFrame(parent, fg_color="transparent")
        container.pack(side="left", fill="x", expand=True)
        entry = ctk.CTkEntry(container, textvariable=variable, show="•")
        entry.pack(side="left", fill="x", expand=True)
        button = ctk.CTkButton(container, text="顯示", width=64, **secondary_button_style())

        def toggle() -> None:
            hidden = bool(entry.cget("show"))
            entry.configure(show="" if hidden else "•")
            button.configure(text="隱藏" if hidden else "顯示")

        button.configure(command=toggle)
        button.pack(side="left", padx=(8, 0))
        return entry

    def _build_pages(self) -> None:
        self._build_dashboard(self._page("儀表板"))
        self._build_chat(self._page("聊天"))
        self._build_routes(self._page("路由"))
        self._build_automation(self._page("自動化"))
        self._build_settings(self._page("設定"))
        self._build_logs(self._page("日誌"))

    def show_page(self, name: str) -> None:
        page = self.pages.get(name)
        if page is None:
            return
        page.tkraise()
        for key, button in self.nav_buttons.items():
            button.configure(
                fg_color=NAV_SELECTED_COLOR if key == name else "transparent",
                text_color=NAV_TEXT_COLOR,
            )

    def show_window(self, page: str = "儀表板") -> None:
        self.after(0, lambda: self._show_window_on_ui(page))

    def _show_window_on_ui(self, page: str) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()
        self.show_page(page)

    def hide_window(self) -> None:
        self.withdraw()

    def _show_setup_notice(self) -> None:
        reason = f"\n\n原因：{self.load_error}" if self.load_error else ""
        messagebox.showinfo(
            "MeshBridge 初始設定",
            "尚未找到可用設定。請依序填寫連線、平台與路由資料，然後按「儲存並套用」。"
            + reason,
            parent=self,
        )

    def _build_dashboard(self, page: ctk.CTkFrame) -> None:
        self._title(page, "狀態儀表板", "檢視橋接服務與所有平台、路由的即時狀態")
        controls = ctk.CTkFrame(page, fg_color="transparent")
        controls.pack(fill="x", padx=24, pady=(0, 12))
        self.start_button = ctk.CTkButton(controls, text="啟動", command=self.controller.start_async)
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button = ctk.CTkButton(controls, text="停止", command=self.controller.stop_async, **secondary_button_style())
        self.stop_button.pack(side="left", padx=8)
        self.restart_button = ctk.CTkButton(controls, text="重新啟動", command=self.controller.restart_async, **secondary_button_style())
        self.restart_button.pack(side="left", padx=8)
        self.operation_label = ctk.CTkLabel(controls, text="")
        self.operation_label.pack(side="right")

        cards = ctk.CTkFrame(page, fg_color="transparent")
        cards.pack(fill="x", padx=18)
        for column in range(4):
            cards.grid_columnconfigure(column, weight=1)
        self.status_labels: dict[str, ctk.CTkLabel] = {}
        for column, (key, title) in enumerate(
            (("bridge", "橋接服務"), ("mqtt", "Meshtastic 路由"), ("telegram", "Telegram"), ("discord", "Discord"))
        ):
            card = ctk.CTkFrame(cards)
            card.grid(row=0, column=column, padx=6, sticky="ew")
            ctk.CTkLabel(card, text=title, font=ctk.CTkFont(weight="bold")).pack(
                anchor="w", padx=14, pady=(12, 3)
            )
            label = ctk.CTkLabel(card, text="等待資料", anchor="w")
            label.pack(anchor="w", padx=14, pady=(0, 12))
            self.status_labels[key] = label

        content = ctk.CTkFrame(page, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=24, pady=16)
        content.grid_columnconfigure((0, 1), weight=1)
        content.grid_rowconfigure(0, weight=1)
        route_card = ctk.CTkFrame(content)
        route_card.grid(row=0, column=0, padx=(0, 8), sticky="nsew")
        ctk.CTkLabel(route_card, text="路由狀態", font=ctk.CTkFont(size=17, weight="bold")).pack(
            anchor="w", padx=15, pady=12
        )
        self.dashboard_routes = ctk.CTkTextbox(route_card, state="disabled")
        self.dashboard_routes.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        stat_card = ctk.CTkFrame(content)
        stat_card.grid(row=0, column=1, padx=(8, 0), sticky="nsew")
        ctk.CTkLabel(stat_card, text="本次執行統計", font=ctk.CTkFont(size=17, weight="bold")).pack(
            anchor="w", padx=15, pady=12
        )
        self.dashboard_stats = ctk.CTkTextbox(stat_card, state="disabled")
        self.dashboard_stats.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def _build_chat(self, page: ctk.CTkFrame) -> None:
        self._title(page, "聊天", "監看所有啟用路由並傳送純文字訊息")
        options = ctk.CTkFrame(page, fg_color="transparent")
        options.pack(fill="x", padx=24, pady=(0, 10))
        self.chat_route = tk.StringVar(value="")
        self.chat_target = tk.StringVar(value="全部平台")
        self.chat_route_menu = ctk.CTkOptionMenu(options, variable=self.chat_route, values=["尚無路由"])
        self.chat_route_menu.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.chat_target_menu = ctk.CTkOptionMenu(
            options,
            variable=self.chat_target,
            values=list(TARGET_TO_API),
            command=lambda _: self._update_chat_limit(),
        )
        self.chat_target_menu.pack(side="left", padx=8)
        self.chat_status = ctk.CTkLabel(options, text="橋接服務尚未啟動")
        self.chat_status.pack(side="right", padx=(8, 0))
        self.chat_history = ctk.CTkTextbox(page, state="disabled", wrap="word")
        self.chat_history.pack(fill="both", expand=True, padx=24, pady=(0, 10))
        composer = ctk.CTkFrame(page, fg_color="transparent")
        composer.pack(fill="x", padx=24, pady=(0, 20))
        self.chat_input = ctk.CTkTextbox(composer, height=90)
        self.chat_input.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.chat_input.bind("<KeyRelease>", lambda _: self._update_chat_limit())
        self.chat_input.bind("<Control-Return>", self._chat_shortcut)
        side = ctk.CTkFrame(composer, fg_color="transparent")
        side.pack(side="right", fill="y")
        self.chat_limit = ctk.CTkLabel(side, text="0 bytes")
        self.chat_limit.pack(anchor="e")
        self.chat_send = ctk.CTkButton(side, text="發送", command=self._send_chat, state="disabled")
        self.chat_send.pack(side="bottom")

    def _build_routes(self, page: ctk.CTkFrame) -> None:
        self._title(page, "路由", "新增、排序及設定最多 20 條 Meshtastic 路由")
        body = ctk.CTkFrame(page, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24, pady=(0, 18))
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)
        left = ctk.CTkFrame(body, width=240)
        left.grid(row=0, column=0, padx=(0, 12), sticky="nsew")
        self.route_list = ctk.CTkScrollableFrame(left, label_text="路由清單")
        self.route_list.pack(fill="both", expand=True, padx=8, pady=8)
        route_controls = ctk.CTkFrame(left, fg_color="transparent")
        route_controls.pack(fill="x", padx=8, pady=(0, 8))
        for text, command in (
            ("新增", self._add_route), ("刪除", self._delete_route),
            ("上移", lambda: self._move_route(-1)), ("下移", lambda: self._move_route(1)),
        ):
            style = danger_button_style() if text == "刪除" else secondary_button_style()
            ctk.CTkButton(route_controls, text=text, width=50, command=command, **style).pack(
                side="left", padx=2, expand=True
            )
        editor = ctk.CTkScrollableFrame(body, label_text="路由內容")
        editor.grid(row=0, column=1, sticky="nsew")
        self._route_vars = {
            "enabled": tk.BooleanVar(),
            "telegram_enabled": tk.BooleanVar(),
            "discord_enabled": tk.BooleanVar(),
            "eew_enabled": tk.BooleanVar(),
        }
        ctk.CTkSwitch(editor, text="啟用此路由", variable=self._route_vars["enabled"]).pack(anchor="w", padx=12, pady=5)
        platform = ctk.CTkFrame(editor, fg_color="transparent")
        platform.pack(fill="x", padx=8)
        ctk.CTkSwitch(platform, text="Telegram", variable=self._route_vars["telegram_enabled"]).pack(side="left", padx=4)
        ctk.CTkSwitch(platform, text="Discord", variable=self._route_vars["discord_enabled"]).pack(side="left", padx=12)
        eew_row = ctk.CTkFrame(editor, fg_color="transparent")
        eew_row.pack(fill="x", padx=8, pady=(8, 2))
        ctk.CTkSwitch(
            eew_row,
            text="啟用 EEW 自動發訊",
            variable=self._route_vars["eew_enabled"],
        ).pack(side="left", padx=4)
        ctk.CTkLabel(
            editor,
            text="收到地牛速報後會傳送到此路由的所有已啟用平台；請使用地牛 Wake Up! 的測試發送功能驗證。",
            text_color=MUTED_TEXT_COLOR,
            font=ctk.CTkFont(size=12),
        ).pack(anchor="w", padx=12, pady=(0, 4))
        for key, label, secret, help_text in (
            ("name", "路由名稱", False, "用來在儀表板與聊天頁辨識這條路由。"),
            ("channel_name", "Meshtastic 頻道名稱（Channel name）", False, "必須與 Meshtastic 裝置使用的頻道名稱相同。"),
            ("channel_key", "頻道金鑰（Channel key）", True, "貼上 Meshtastic 頻道的 Base64 金鑰。"),
            ("target_chat_id", "Telegram 聊天室識別碼（Chat ID）", False, "啟用 Telegram 時必填。"),
            ("topic_id", "Telegram 主題識別碼（Topic ID，可留空）", False, "只有使用 Telegram 群組主題時才需要。"),
            ("discord_channel_id", "Discord 頻道識別碼（Channel ID，可留空）", False, "啟用 Discord 時必填。"),
        ):
            ctk.CTkLabel(editor, text=label).pack(anchor="w", padx=12, pady=(10, 2))
            ctk.CTkLabel(
                editor,
                text=help_text,
                text_color=MUTED_TEXT_COLOR,
                font=ctk.CTkFont(size=12),
            ).pack(anchor="w", padx=12, pady=(0, 3))
            variable = tk.StringVar()
            self._route_vars[key] = variable
            if secret:
                secret_row = ctk.CTkFrame(editor, fg_color="transparent")
                secret_row.pack(fill="x", padx=12)
                self._secret_entry(secret_row, variable)
            else:
                ctk.CTkEntry(editor, textvariable=variable).pack(fill="x", padx=12)
        ctk.CTkLabel(
            editor,
            text="儲存後會套用全部設定，並安全重新啟動橋接服務。",
            text_color=MUTED_TEXT_COLOR,
            font=ctk.CTkFont(size=12),
        ).pack(anchor="e", padx=12, pady=(12, 2))
        ctk.CTkButton(editor, text="儲存並套用", command=self._apply_settings).pack(
            anchor="e", padx=12, pady=18
        )
        self._rebuild_route_list()
        self._load_route(0)

    def _build_settings(self, page: ctk.CTkFrame) -> None:
        self._title(page, "設定", "儲存後會安全重新啟動橋接服務；失敗時自動還原")
        scroll = ctk.CTkScrollableFrame(page)
        scroll.pack(fill="both", expand=True, padx=24, pady=(0, 12))
        fields = (
            ("bridge_ui.display_name", "一般", "聊天顯示名稱", self.raw.get("bridge_ui", {}).get("display_name", "Bridge UI"), False),
            ("mqtt.broker", "MQTT", "伺服器位址（Broker）", self.raw.get("mqtt", {}).get("broker", ""), False),
            ("mqtt.port", "MQTT", "連接埠（Port）", self.raw.get("mqtt", {}).get("port", 1883), False),
            ("mqtt.username", "MQTT", "使用者名稱（Username）", self.raw.get("mqtt", {}).get("username", ""), False),
            ("mqtt.password", "MQTT", "密碼", self.raw.get("mqtt", {}).get("password", ""), True),
            ("mqtt.root_topic", "MQTT", "根主題（Root topic）", self.raw.get("mqtt", {}).get("root_topic", ""), False),
            ("telegram.bot_token", "平台", "Telegram 機器人權杖（Bot Token）", self.raw.get("telegram", {}).get("bot_token", ""), True),
            ("discord.bot_token", "平台", "Discord 機器人權杖（Bot Token）", self.raw.get("discord", {}).get("bot_token", ""), True),
            ("node.id", "虛擬節點", "節點識別碼（Node ID）", self.raw.get("node", {}).get("id", ""), False),
            ("node.long_name", "虛擬節點", "完整名稱", self.raw.get("node", {}).get("long_name", ""), False),
            ("node.short_name", "虛擬節點", "簡短名稱", self.raw.get("node", {}).get("short_name", ""), False),
        )
        groups: dict[str, ctk.CTkFrame] = {}
        group_descriptions = {
            "一般": "設定聊天中顯示的名稱與程式記錄的詳細程度。",
            "MQTT": "連接 Meshtastic 訊息伺服器所需的資料。",
            "平台": "只有路由啟用對應平台時才需要填寫權杖。",
            "虛擬節點": "MeshBridge 在 Meshtastic 網路中顯示的節點資訊。",
        }

        def get_group(name: str) -> ctk.CTkFrame:
            if name not in groups:
                groups[name] = ctk.CTkFrame(scroll)
                groups[name].pack(fill="x", padx=6, pady=7)
                ctk.CTkLabel(groups[name], text=name, font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w", padx=12, pady=(10, 2))
                ctk.CTkLabel(
                    groups[name],
                    text=group_descriptions[name],
                    text_color=MUTED_TEXT_COLOR,
                ).pack(anchor="w", padx=12, pady=(0, 6))
            return groups[name]

        general = get_group("一般")
        logging_row = ctk.CTkFrame(general, fg_color="transparent")
        logging_row.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(logging_row, text="日誌層級", width=180, anchor="w").pack(side="left")
        raw_log_level = normalize_log_level(self.raw.get("logging_level", "INFO"))
        self._settings_vars["logging_level"] = tk.StringVar(value=LOG_LEVEL_TO_UI[raw_log_level])
        ctk.CTkOptionMenu(
            logging_row,
            variable=self._settings_vars["logging_level"],
            values=list(LOG_LEVEL_TO_UI.values()),
            width=340,
        ).pack(side="left", fill="x", expand=True)

        for path, group, label, value, secret in fields:
            row = ctk.CTkFrame(get_group(group), fg_color="transparent")
            row.pack(fill="x", padx=12, pady=4)
            ctk.CTkLabel(row, text=label, width=180, anchor="w").pack(side="left")
            variable = tk.StringVar(value=str(value if value is not None else ""))
            self._settings_vars[path] = variable
            if secret:
                self._secret_entry(row, variable)
            else:
                ctk.CTkEntry(row, textvariable=variable).pack(side="left", fill="x", expand=True)
            if path == "node.id":
                ctk.CTkButton(row, text="隨機產生", width=90, command=self._random_node_id).pack(side="left", padx=(8, 0))

        preferences = ctk.CTkFrame(scroll)
        preferences.pack(fill="x", padx=6, pady=7)
        ctk.CTkLabel(preferences, text="介面與背景功能", font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w", padx=12, pady=(10, 6))
        ctk.CTkLabel(
            preferences,
            text="選擇外觀、開機啟動與自動更新行為。",
            text_color=MUTED_TEXT_COLOR,
        ).pack(anchor="w", padx=12, pady=(0, 6))
        self.theme_var = tk.StringVar(value=THEME_TO_UI.get(self.raw.get("appearance", {}).get("theme", "system"), "系統"))
        theme_row = ctk.CTkFrame(preferences, fg_color="transparent")
        theme_row.pack(fill="x", padx=12, pady=5)
        ctk.CTkLabel(theme_row, text="介面主題", width=120, anchor="w").pack(side="left")
        ctk.CTkOptionMenu(theme_row, variable=self.theme_var, values=list(UI_TO_THEME), command=self._change_theme).pack(side="left")
        features = self.raw.get("features", {})
        updates = features.get("updates", {})
        self.stats_var = tk.BooleanVar(value=bool(features.get("statistics_enabled", True)))
        self.autostart_var = tk.BooleanVar(value=bool(features.get("autostart", False)))
        self.update_var = tk.BooleanVar(value=bool(updates.get("enabled", False)))
        for text, variable in (
            ("啟用執行統計", self.stats_var),
            ("登入 Windows 後自動啟動", self.autostart_var),
            ("啟用更新檢查", self.update_var),
        ):
            ctk.CTkSwitch(preferences, text=text, variable=variable).pack(anchor="w", padx=12, pady=5)
        update_row = ctk.CTkFrame(preferences, fg_color="transparent")
        update_row.pack(fill="x", padx=12, pady=(5, 12))
        update_code = str(updates.get("mode", "notify"))
        self.update_mode = tk.StringVar(value=UPDATE_MODE_TO_UI.get(update_code, UPDATE_MODE_TO_UI["notify"]))
        self.update_interval = tk.StringVar(value=str(updates.get("interval_hours", 24)))
        ctk.CTkLabel(update_row, text="更新方式").pack(side="left")
        ctk.CTkOptionMenu(update_row, variable=self.update_mode, values=list(UPDATE_MODE_TO_UI.values()), width=230).pack(side="left", padx=8)
        ctk.CTkLabel(update_row, text="間隔（小時）").pack(side="left", padx=(16, 4))
        ctk.CTkEntry(update_row, textvariable=self.update_interval, width=80).pack(side="left")
        ctk.CTkButton(
            update_row,
            text="立即檢查更新",
            width=120,
            command=self.controller.check_updates_async,
        ).pack(side="right")

        actions = ctk.CTkFrame(page, fg_color="transparent")
        actions.pack(fill="x", padx=24, pady=(0, 18))
        self.settings_status = ctk.CTkLabel(actions, text="填寫設定 → 測試連線 → 儲存並啟動")
        self.settings_status.pack(side="left")
        self.apply_button = ctk.CTkButton(actions, text="儲存並套用", command=self._apply_settings)
        self.apply_button.pack(side="right", padx=5)
        self.test_button = ctk.CTkButton(actions, text="測試連線", command=self._test_connections, **secondary_button_style())
        self.test_button.pack(side="right", padx=5)
        self.validate_button = ctk.CTkButton(actions, text="檢查設定", command=self._validate_form, **secondary_button_style())
        self.validate_button.pack(side="right", padx=5)

    @staticmethod
    def _automation_routes(value: object) -> str:
        return "、".join(str(item) for item in value) if isinstance(value, list) else ""

    @staticmethod
    def _parse_route_names(value: str) -> list[str]:
        normalized = value.replace("，", ",").replace("、", ",")
        return [item.strip() for item in normalized.split(",") if item.strip()]

    def _labeled_entry(self, parent, label: str, variable: tk.StringVar) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=3)
        ctk.CTkLabel(row, text=label, width=110, anchor="w").pack(side="left")
        ctk.CTkEntry(row, textvariable=variable).pack(side="left", fill="x", expand=True)

    def _build_automation(self, page: ctk.CTkFrame) -> None:
        self._title(page, "自動化", "關鍵字回應與 Windows 本機時區 Cron 排程")
        scroll = ctk.CTkScrollableFrame(page)
        scroll.pack(fill="both", expand=True, padx=24, pady=(0, 12))
        automations = self.raw.setdefault("automations", default_config_data()["automations"])
        keyword = ctk.CTkFrame(scroll)
        keyword.pack(fill="x", padx=6, pady=7)
        ctk.CTkLabel(keyword, text="關鍵字自動回應", font=ctk.CTkFont(size=17, weight="bold")).pack(anchor="w", padx=12, pady=(10, 2))
        ctk.CTkLabel(keyword, text="同時命中只執行第一條；適用路由可從下拉清單複選。", text_color=MUTED_TEXT_COLOR).pack(anchor="w", padx=12, pady=(0, 6))
        self.keyword_container = ctk.CTkFrame(keyword, fg_color="transparent")
        self.keyword_container.pack(fill="x")
        for item in automations.get("keyword_rules", []):
            self._add_keyword_rule(item)
        ctk.CTkButton(keyword, text="新增關鍵字規則", command=self._add_keyword_rule).pack(anchor="e", padx=12, pady=10)

        schedule = ctk.CTkFrame(scroll)
        schedule.pack(fill="x", padx=6, pady=7)
        ctk.CTkLabel(schedule, text="Cron 排程訊息", font=ctk.CTkFont(size=17, weight="bold")).pack(anchor="w", padx=12, pady=(10, 2))
        ctk.CTkLabel(schedule, text="五欄：分鐘 小時 日期 月份 星期；例如 0 9 * * 1-5。錯過時段不補送。", text_color=MUTED_TEXT_COLOR).pack(anchor="w", padx=12, pady=(0, 6))
        self.schedule_container = ctk.CTkFrame(schedule, fg_color="transparent")
        self.schedule_container.pack(fill="x")
        for item in automations.get("schedules", []):
            self._add_schedule(item)
        ctk.CTkButton(schedule, text="新增排程", command=self._add_schedule).pack(anchor="e", padx=12, pady=10)

        actions = ctk.CTkFrame(page, fg_color="transparent")
        actions.pack(fill="x", padx=24, pady=(0, 18))
        self.automation_status = ctk.CTkLabel(
            actions,
            text="修改後請儲存並套用，完成後自動化規則才會生效。",
            text_color=MUTED_TEXT_COLOR,
        )
        self.automation_status.pack(side="left")
        self.automation_apply_button = ctk.CTkButton(
            actions,
            text="儲存並套用",
            command=self._apply_settings,
        )
        self.automation_apply_button.pack(side="right")

    def _add_keyword_rule(self, initial: dict[str, Any] | None = None) -> None:
        default_route = next(
            (route.get("name", "") for route in self._routes() if route.get("enabled", True)),
            self._routes()[0].get("name", "") if self._routes() else "",
        )
        item = initial or {"name": f"規則 {len(self._keyword_rows) + 1}", "enabled": True, "routes": [default_route] if default_route else [], "match": "exact", "keyword": "", "response": ""}
        card = ctk.CTkFrame(self.keyword_container)
        card.pack(fill="x", padx=10, pady=5)
        row: dict[str, Any] = {
            "frame": card,
            "enabled": tk.BooleanVar(value=bool(item.get("enabled", True))),
            "name": tk.StringVar(value=str(item.get("name", ""))),
            "routes": tk.StringVar(value=self._automation_routes(item.get("routes", []))),
            "match": tk.StringVar(value="完全相符" if item.get("match", "exact") == "exact" else "包含"),
            "keyword": tk.StringVar(value=str(item.get("keyword", ""))),
            "response": tk.StringVar(value=str(item.get("response", ""))),
        }
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(7, 2))
        ctk.CTkSwitch(top, text="啟用", variable=row["enabled"]).pack(side="left")
        ctk.CTkEntry(top, textvariable=row["name"], width=180).pack(side="left", padx=8)
        ctk.CTkOptionMenu(top, variable=row["match"], values=["完全相符", "包含"], width=110).pack(side="left")
        ctk.CTkButton(top, text="刪除", width=65, command=lambda: self._delete_automation_row(self._keyword_rows, row), **danger_button_style()).pack(side="right")
        route_line = ctk.CTkFrame(card, fg_color="transparent")
        route_line.pack(fill="x", padx=10, pady=3)
        ctk.CTkLabel(route_line, text="適用路由", width=110, anchor="w").pack(side="left")
        row["route_button"] = ctk.CTkButton(
            route_line,
            text="",
            anchor="w",
            command=lambda: self._choose_keyword_routes(row),
            **secondary_button_style(),
        )
        row["route_button"].pack(side="left", fill="x", expand=True)
        row["routes"].trace_add("write", lambda *_args: self._update_keyword_route_summary(row))
        self._update_keyword_route_summary(row)
        for label, key in (("關鍵字", "keyword"), ("回應文字", "response")):
            self._labeled_entry(card, label, row[key])
        self._keyword_rows.append(row)

    def _update_keyword_route_summary(self, row: dict[str, Any]) -> None:
        selected = self._parse_route_names(row["routes"].get())
        if not selected:
            text = "未選擇路由 ▼"
        elif len(selected) == 1:
            text = f"{selected[0]} ▼"
        else:
            text = f"已選 {len(selected)} 個路由 ▼"
        row["route_button"].configure(text=text)

    def _choose_keyword_routes(self, row: dict[str, Any]) -> None:
        dialog = ctk.CTkToplevel(self)
        dialog.title("選擇適用路由")
        dialog.geometry("420x460")
        dialog.minsize(360, 320)
        dialog.transient(self)
        dialog.configure(fg_color=APP_BACKGROUND)
        ctk.CTkLabel(dialog, text="選擇適用路由", font=ctk.CTkFont(size=20, weight="bold")).pack(anchor="w", padx=20, pady=(18, 2))
        ctk.CTkLabel(dialog, text="可同時選擇多條路由，只有按下套用才會儲存變更。", text_color=MUTED_TEXT_COLOR).pack(anchor="w", padx=20, pady=(0, 12))
        choices = ctk.CTkScrollableFrame(dialog, fg_color=CARD_BACKGROUND, border_width=1, border_color=BORDER_COLOR)
        choices.pack(fill="both", expand=True, padx=18, pady=0)
        selected = {name.casefold() for name in self._parse_route_names(row["routes"].get())}
        variables: list[tuple[str, tk.BooleanVar]] = []
        for route in self._routes():
            name = str(route.get("name", "")).strip()
            variable = tk.BooleanVar(value=name.casefold() in selected)
            variables.append((name, variable))
            ctk.CTkCheckBox(choices, text=name, variable=variable).pack(anchor="w", padx=10, pady=7)
        controls = ctk.CTkFrame(dialog, fg_color=CARD_BACKGROUND, border_width=1, border_color=BORDER_COLOR)
        controls.pack(fill="x", padx=18, pady=(12, 18))

        def apply_selection() -> None:
            row["routes"].set("、".join(name for name, variable in variables if variable.get()))
            dialog.destroy()

        ctk.CTkButton(controls, text="取消", width=96, command=dialog.destroy, **secondary_button_style()).pack(side="right", padx=(8, 12), pady=12)
        ctk.CTkButton(controls, text="套用", width=96, command=apply_selection).pack(side="right", pady=12)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        def present_dialog() -> None:
            self.update_idletasks()
            dialog.update_idletasks()
            width = max(420, dialog.winfo_width())
            height = max(460, dialog.winfo_height())
            x = self.winfo_rootx() + max(0, (self.winfo_width() - width) // 2)
            y = self.winfo_rooty() + max(0, (self.winfo_height() - height) // 2)
            dialog.geometry(f"{width}x{height}+{x}+{y}")
            dialog.grab_set()
            dialog.focus_force()

        dialog.after(100, present_dialog)

    def _add_schedule(self, initial: dict[str, Any] | None = None) -> None:
        item = initial or {"name": f"排程 {len(self._schedule_rows) + 1}", "enabled": True, "routes": [], "cron": "0 9 * * 1-5", "message": ""}
        card = ctk.CTkFrame(self.schedule_container)
        card.pack(fill="x", padx=10, pady=5)
        row: dict[str, Any] = {
            "frame": card,
            "enabled": tk.BooleanVar(value=bool(item.get("enabled", True))),
            "name": tk.StringVar(value=str(item.get("name", ""))),
            "routes": tk.StringVar(value=self._automation_routes(item.get("routes", []))),
            "cron": tk.StringVar(value=str(item.get("cron", ""))),
            "message": tk.StringVar(value=str(item.get("message", ""))),
            "next": tk.StringVar(value=""),
        }
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(7, 2))
        ctk.CTkSwitch(top, text="啟用", variable=row["enabled"]).pack(side="left")
        ctk.CTkEntry(top, textvariable=row["name"], width=220).pack(side="left", padx=8)
        ctk.CTkButton(top, text="刪除", width=65, command=lambda: self._delete_automation_row(self._schedule_rows, row), **danger_button_style()).pack(side="right")
        for label, key in (("目標路由", "routes"), ("Cron", "cron"), ("訊息", "message")):
            self._labeled_entry(card, label, row[key])
        ctk.CTkLabel(card, textvariable=row["next"], text_color=MUTED_TEXT_COLOR).pack(anchor="w", padx=120, pady=(0, 6))
        row["cron"].trace_add("write", lambda *_args: self._update_schedule_preview(row))
        self._update_schedule_preview(row)
        self._schedule_rows.append(row)

    @staticmethod
    def _update_schedule_preview(row: dict[str, Any]) -> None:
        try:
            expression = row["cron"].get().strip()
            if len(expression.split()) != 5:
                raise ValueError
            next_run = AutomationEngine.next_run(expression)
            row["next"].set(f"下一次：{next_run.strftime('%Y-%m-%d %H:%M %z')}")
        except Exception:
            row["next"].set("下一次：Cron 格式無效")

    @staticmethod
    def _delete_automation_row(collection: list[dict[str, Any]], row: dict[str, Any]) -> None:
        row["frame"].destroy()
        collection.remove(row)

    def _collect_automations(self) -> dict[str, Any]:
        return {
            "keyword_rules": [{"name": row["name"].get().strip(), "enabled": bool(row["enabled"].get()), "routes": self._parse_route_names(row["routes"].get()), "match": "exact" if row["match"].get() == "完全相符" else "contains", "keyword": row["keyword"].get().strip(), "response": row["response"].get().strip()} for row in self._keyword_rows],
            "eew": {"dedupe_seconds": 60},
            "schedules": [{"name": row["name"].get().strip(), "enabled": bool(row["enabled"].get()), "routes": self._parse_route_names(row["routes"].get()), "cron": row["cron"].get().strip(), "message": row["message"].get().strip()} for row in self._schedule_rows],
        }

    def _build_logs(self, page: ctk.CTkFrame) -> None:
        self._title(page, "日誌", "僅顯示經敏感資訊遮蔽的近期程式紀錄")
        controls = ctk.CTkFrame(page, fg_color="transparent")
        controls.pack(fill="x", padx=24, pady=(0, 10))
        self.log_level = tk.StringVar(value=LOG_LEVEL_TO_UI["INFO"])
        ctk.CTkOptionMenu(
            controls,
            variable=self.log_level,
            values=list(LOG_LEVEL_TO_UI.values()),
            command=lambda _: self._reset_logs(),
        ).pack(side="left")
        ctk.CTkButton(controls, text="清空畫面", command=self._clear_logs, width=100, **secondary_button_style()).pack(side="left", padx=8)
        ctk.CTkButton(controls, text="開啟日誌資料夾", command=self._open_log_folder, width=130, **secondary_button_style()).pack(side="right")
        self.log_text = ctk.CTkTextbox(page, state="disabled", wrap="none")
        self.log_text.pack(fill="both", expand=True, padx=24, pady=(0, 20))

    def _replace_text(self, widget: ctk.CTkTextbox, value: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", value)
        widget.configure(state="disabled")

    def _refresh_runtime(self) -> None:
        snapshot = self.controller.snapshot()
        status = self.controller.service_status
        self.start_button.configure(state="normal" if self.controller.can_start() else "disabled")
        self.stop_button.configure(state="normal" if self.controller.can_stop() else "disabled")
        self.restart_button.configure(state="normal" if self.controller.can_restart() else "disabled")
        if snapshot is None:
            self.sidebar_state.configure(text="橋接服務：尚未設定")
            for label in self.status_labels.values():
                label.configure(text="尚未啟動", text_color=MUTED_TEXT_COLOR)
        else:
            bridge = snapshot.get("bridge", {})
            bridge_status = translate_status(bridge.get("status", "stopped"))
            status_color = STATUS_COLORS.get(status, MUTED_TEXT_COLOR)
            self.sidebar_state.configure(text=f"橋接服務：{bridge_status}", text_color=status_color)
            self.status_labels["bridge"].configure(text=bridge_status, text_color=status_color)
            routes = snapshot.get("routes", {})
            connected = sum(1 for route in routes.values() if route.get("mqtt_status") == "connected")
            mqtt_status = "connected" if routes and connected == len(routes) else "connecting" if routes else "disabled"
            telegram_status = str(snapshot.get("telegram", {}).get("status", "disabled"))
            discord_status = str(snapshot.get("discord", {}).get("status", "disabled"))
            self.status_labels["mqtt"].configure(text=f"{connected}/{len(routes)} 已連線", text_color=STATUS_COLORS.get(mqtt_status, MUTED_TEXT_COLOR))
            self.status_labels["telegram"].configure(text=translate_status(telegram_status), text_color=STATUS_COLORS.get(telegram_status, MUTED_TEXT_COLOR))
            self.status_labels["discord"].configure(text=translate_status(discord_status), text_color=STATUS_COLORS.get(discord_status, MUTED_TEXT_COLOR))
            route_lines = [
                f"{route.get('name', route_id)}\n  連線狀態：{translate_status(route.get('mqtt_status'))}\n  伺服器（Broker）：{route.get('broker', '')}"
                for route_id, route in routes.items()
            ]
            self._replace_text(self.dashboard_routes, "\n\n".join(route_lines) or "尚無啟用路由")
            stats = snapshot.get("statistics", {})
            statistics_enabled = bool(
                self.controller.config
                and self.controller.config.features.statistics_enabled
            )
            self._replace_text(
                self.dashboard_stats,
                format_statistics(stats, statistics_enabled),
            )
            names = [route.get("name", route_id) for route_id, route in routes.items()]
            if names:
                current = self.chat_route.get()
                self.chat_route_menu.configure(values=names)
                if current not in names:
                    self.chat_route.set(names[0])
        self._update_chat_limit()
        self.after(1000, self._refresh_runtime)

    def _poll_events(self) -> None:
        while True:
            try:
                kind, payload = self.controller.events.get_nowait()
            except queue.Empty:
                break
            if kind == "notice":
                self.settings_status.configure(text=str(payload))
            elif kind == "exit_for_update":
                self.request_exit()
            elif kind == "activate":
                self._show_window_on_ui(str(payload))
            elif kind == "external_command":
                parts = str(payload).split("|", 2)
                if len(parts) == 3 and parts[0] == "eew":
                    self.controller.send_eew_async(parts[1], parts[2])
            elif kind == "request_exit":
                self.request_exit()
            elif kind == "shutdown_complete":
                self.destroy()
                return
            elif kind == "chat_result":
                self._chat_sent(payload)
                self._update_chat_limit()
            elif kind == "chat_error":
                messagebox.showerror("傳送失敗", str(payload), parent=self)
                self._update_chat_limit()
            elif kind == "connection_results":
                results, fingerprint = payload
                self._show_connection_results(results, fingerprint)
            elif kind == "operation":
                self._operation_running = bool(payload.get("running"))
                if self._operation_running:
                    self.operation_label.configure(text="正在處理…")
                    self.automation_status.configure(text="正在儲存並套用設定…")
                    for button in (self.validate_button, self.test_button, self.apply_button, self.automation_apply_button):
                        button.configure(state="disabled")
                else:
                    for button in (self.validate_button, self.test_button, self.apply_button, self.automation_apply_button):
                        button.configure(state="normal")
                    if payload.get("ok"):
                        text = payload.get("result") or "操作完成"
                        self.operation_label.configure(text=str(text))
                        self.settings_status.configure(text=str(text))
                        self.automation_status.configure(text=str(text))
                    else:
                        error = payload.get("error", "未知錯誤")
                        self.operation_label.configure(text="操作失敗")
                        self.automation_status.configure(text="儲存或套用失敗")
                        messagebox.showerror(
                            "MeshBridge 操作失敗",
                            friendly_config_error(error),
                            parent=self,
                        )
        self.after(200, self._poll_events)

    def _refresh_chat(self) -> None:
        result = self.controller.messages_after(
            self._last_message_id,
            self._chat_generation,
        )
        generation = result.get("generation")
        if generation != self._chat_generation:
            self._chat_generation = generation
            self._last_message_id = 0
        messages = result.get("messages", [])
        if messages:
            self.chat_history.configure(state="normal")
            for item in messages:
                stamp = datetime.fromtimestamp(item.get("timestamp", 0)).strftime("%H:%M:%S")
                destinations = ", ".join(item.get("destinations", [])) or "未轉送"
                self.chat_history.insert(
                    "end",
                    f"[{stamp}] {item.get('route_name')} · {item.get('sender')} → {destinations}\n{item.get('text', '')}\n\n",
                )
            self.chat_history.see("end")
            self.chat_history.configure(state="disabled")
            self._last_message_id = int(result.get("latest_id", self._last_message_id))
        service_status = self.controller.service_status
        self.chat_status.configure(
            text=f"橋接服務：{translate_status(service_status)}",
            text_color=STATUS_COLORS.get(service_status, MUTED_TEXT_COLOR),
        )
        self.after(800, self._refresh_chat)

    def _route_id_for_name(self, name: str) -> str | None:
        snapshot = self.controller.snapshot() or {}
        for route_id, route in snapshot.get("routes", {}).items():
            if route.get("name", route_id) == name:
                return route_id
        return None

    def _update_chat_limit(self) -> None:
        text = self.chat_input.get("1.0", "end-1c").strip() if hasattr(self, "chat_input") else ""
        target = self.chat_target.get() if hasattr(self, "chat_target") else "全部平台"
        display_name = self.raw.get("bridge_ui", {}).get("display_name", "Bridge UI")
        formatted = f"[{display_name}]: {text}"
        if target in {"全部平台", "Meshtastic"}:
            count = len(formatted.encode("utf-8"))
            limit = MAX_MESHTASTIC_PAYLOAD_BYTES
            unit = "bytes（含名稱）"
        elif target == "Discord":
            count, limit, unit = len(formatted), 2000, "字元（含名稱）"
        else:
            count, limit, unit = len(text), 4000, "字元"
        self.chat_limit.configure(text=f"{count}/{limit} {unit}")
        valid = bool(text) and count <= limit and self.controller.running and bool(self._route_id_for_name(self.chat_route.get()))
        self.chat_send.configure(state="normal" if valid else "disabled")

    def _chat_shortcut(self, event=None):
        if str(self.chat_send.cget("state")) != "disabled":
            self._send_chat()
        return "break"

    def _send_chat(self) -> None:
        route_id = self._route_id_for_name(self.chat_route.get())
        text = self.chat_input.get("1.0", "end-1c").strip()
        target = TARGET_TO_API[self.chat_target.get()]
        if not route_id or not text:
            return
        self.chat_send.configure(state="disabled")

        def worker() -> None:
            try:
                result = self.controller.send(
                    {"route_id": route_id, "text": text, "target": target}
                )
                self.controller.events.put(("chat_result", result))
            except Exception as exc:
                self.controller.events.put(("chat_error", str(exc)))

        threading.Thread(target=worker, name="chat-send", daemon=True).start()

    def _chat_sent(self, result: dict) -> None:
        self.chat_input.delete("1.0", "end")
        sent = "、".join(result.get("sent", []))
        self.chat_status.configure(text=f"已傳送至：{sent}" if sent else "未傳送")

    def _routes(self) -> list[dict[str, Any]]:
        routes = self.raw.setdefault("routes", [])
        return routes if isinstance(routes, list) else []

    def _rebuild_route_list(self) -> None:
        for child in self.route_list.winfo_children():
            child.destroy()
        for index, route in enumerate(self._routes()):
            marker = "●" if route.get("enabled", True) else "○"
            ctk.CTkButton(
                self.route_list,
                text=f"{marker} {route.get('name') or f'路由 {index + 1}'}",
                anchor="w",
                fg_color=NAV_SELECTED_COLOR if index == self._route_index else "transparent",
                hover_color=NAV_HOVER_COLOR,
                text_color=NAV_TEXT_COLOR,
                command=lambda selected=index: self._select_route(selected),
            ).pack(fill="x", pady=3)

    def _select_route(self, index: int) -> None:
        self._commit_route(show_errors=False)
        self._route_index = index
        self._load_route(index)
        self._rebuild_route_list()

    def _load_route(self, index: int) -> None:
        routes = self._routes()
        if not routes:
            return
        self._route_index = max(0, min(index, len(routes) - 1))
        route = routes[self._route_index]
        defaults = {"enabled": True, "telegram_enabled": True, "discord_enabled": False, "eew_enabled": False}
        for key, default in defaults.items():
            self._route_vars[key].set(bool(route.get(key, default)))
        for key in ("name", "channel_name", "channel_key", "target_chat_id", "topic_id", "discord_channel_id"):
            value = route.get(key, "")
            self._route_vars[key].set("" if value is None else str(value))

    def _commit_route(self, show_errors: bool = True) -> bool:
        routes = self._routes()
        if not routes or self._route_index >= len(routes):
            return False
        name = self._route_vars["name"].get().strip()
        if not name:
            if show_errors:
                messagebox.showerror("路由設定", "路由名稱不可空白", parent=self)
            return False
        route = routes[self._route_index]
        old_name = str(route.get("name", ""))
        for key in ("enabled", "telegram_enabled", "discord_enabled", "eew_enabled"):
            route[key] = bool(self._route_vars[key].get())
        for key in ("name", "channel_name", "channel_key"):
            route[key] = self._route_vars[key].get().strip()
        for key in ("target_chat_id", "topic_id", "discord_channel_id"):
            route[key] = self._route_vars[key].get().strip() or None
        if old_name and old_name.casefold() != name.casefold():
            for row in (*self._keyword_rows, *self._schedule_rows):
                selected = self._parse_route_names(row["routes"].get())
                row["routes"].set("、".join(name if item.casefold() == old_name.casefold() else item for item in selected))
        self._rebuild_route_list()
        return True

    def _add_route(self) -> None:
        self._commit_route(show_errors=False)
        routes = self._routes()
        if len(routes) >= MAX_ROUTES:
            messagebox.showinfo("路由上限", f"最多只能建立 {MAX_ROUTES} 條路由。", parent=self)
            return
        template = copy.deepcopy(routes[-1] if routes else default_config_data()["routes"][0])
        template.update(name=f"路由 {len(routes) + 1}", enabled=True, eew_enabled=False)
        routes.append(template)
        self._route_index = len(routes) - 1
        self._load_route(self._route_index)
        self._rebuild_route_list()

    def _delete_route(self) -> None:
        routes = self._routes()
        if len(routes) <= 1:
            messagebox.showinfo("路由設定", "至少必須保留一條路由。", parent=self)
            return
        deleted_name = str(routes[self._route_index].get("name", ""))
        routes.pop(self._route_index)
        for row in (*self._keyword_rows, *self._schedule_rows):
            selected = self._parse_route_names(row["routes"].get())
            row["routes"].set("、".join(
                item for item in selected if item.casefold() != deleted_name.casefold()
            ))
        self._route_index = min(self._route_index, len(routes) - 1)
        self._load_route(self._route_index)
        self._rebuild_route_list()

    def _move_route(self, offset: int) -> None:
        self._commit_route(show_errors=False)
        routes = self._routes()
        target = self._route_index + offset
        if not 0 <= target < len(routes):
            return
        routes[self._route_index], routes[target] = routes[target], routes[self._route_index]
        self._route_index = target
        self._load_route(target)
        self._rebuild_route_list()

    def _random_node_id(self) -> None:
        self._settings_vars["node.id"].set(str(random.randint(1, 0xFFFFFFFF)))

    def _change_theme(self, selected: str) -> None:
        ctk.set_appearance_mode(UI_TO_THEME.get(selected, "system"))

    def _collect_form(self) -> dict[str, Any]:
        if not self._commit_route():
            raise ConfigError("請先完成目前路由設定")
        raw = copy.deepcopy(self.raw)
        raw["config_version"] = CURRENT_CONFIG_VERSION
        raw["logging_level"] = UI_TO_LOG_LEVEL[self._settings_vars["logging_level"].get()]
        raw["appearance"] = {"theme": UI_TO_THEME.get(self.theme_var.get(), "system")}
        raw["bridge_ui"] = {"display_name": self._settings_vars["bridge_ui.display_name"].get().strip()}
        raw["telegram"] = {"bot_token": self._settings_vars["telegram.bot_token"].get().strip()}
        raw["discord"] = {"bot_token": self._settings_vars["discord.bot_token"].get().strip()}
        raw["mqtt"] = {
            "broker": self._settings_vars["mqtt.broker"].get().strip(),
            "port": self._settings_vars["mqtt.port"].get().strip(),
            "username": self._settings_vars["mqtt.username"].get(),
            "password": self._settings_vars["mqtt.password"].get(),
            "root_topic": self._settings_vars["mqtt.root_topic"].get().strip(),
        }
        raw["node"] = {
            "id": self._settings_vars["node.id"].get().strip(),
            "long_name": self._settings_vars["node.long_name"].get().strip(),
            "short_name": self._settings_vars["node.short_name"].get().strip(),
        }
        raw["features"] = {
            "statistics_enabled": bool(self.stats_var.get()),
            "autostart": bool(self.autostart_var.get()),
            "updates": {
                "enabled": bool(self.update_var.get()),
                "mode": UI_TO_UPDATE_MODE[self.update_mode.get()],
                "interval_hours": self.update_interval.get().strip(),
            },
        }
        raw["routes"] = copy.deepcopy(self._routes())
        raw["automations"] = self._collect_automations()
        return raw

    def _validate_form(self) -> bool:
        try:
            AppConfig.from_dict(self._collect_form())
        except (ConfigError, ValueError) as exc:
            self.settings_status.configure(text="驗證失敗")
            messagebox.showerror("設定內容不正確", friendly_config_error(exc), parent=self)
            return False
        self.settings_status.configure(text="所有設定均有效")
        messagebox.showinfo("驗證完成", "所有設定均有效。", parent=self)
        return True

    def _apply_settings(self) -> None:
        try:
            raw = self._collect_form()
            AppConfig.from_dict(raw)
        except (ConfigError, ValueError) as exc:
            messagebox.showerror("設定內容不正確", friendly_config_error(exc), parent=self)
            return
        fingerprint = json.dumps(raw, ensure_ascii=False, sort_keys=True)
        if self.controller.config is None and self._tested_config != fingerprint:
            messagebox.showerror(
                "尚未完成連線測試",
                "首次設定必須先完成「測試連線」，確認成功後才能啟動 Bridge。",
                parent=self,
            )
            return
        self.raw = copy.deepcopy(raw)
        self.controller.apply_async(raw)

    def _test_connections(self) -> None:
        try:
            config = AppConfig.from_dict(self._collect_form())
        except ConfigError as exc:
            messagebox.showerror("設定內容不正確", friendly_config_error(exc), parent=self)
            return
        self.settings_status.configure(text="正在測試連線…")
        fingerprint = json.dumps(self._collect_form(), ensure_ascii=False, sort_keys=True)

        def worker() -> None:
            results = check_connections(config)
            self.controller.events.put(("connection_results", (results, fingerprint)))

        threading.Thread(target=worker, name="connection-test", daemon=True).start()

    def _show_connection_results(self, results, fingerprint: str) -> None:
        lines = [f"{result.service}：{result.message}" for result in results]
        succeeded = all(result.succeeded for result in results)
        self._tested_config = fingerprint if succeeded else None
        self.settings_status.configure(text="所有連線測試成功" if succeeded else "部分連線測試失敗")
        (messagebox.showinfo if succeeded else messagebox.showwarning)(
            "連線測試", "\n".join(lines), parent=self
        )

    def _refresh_logs(self) -> None:
        selected_level = UI_TO_LOG_LEVEL[self.log_level.get()]
        entries = self.log_handler.entries_after(self._last_log_sequence, selected_level)
        if entries:
            self.log_text.configure(state="normal")
            for entry in entries:
                self.log_text.insert("end", entry.message + "\n")
                self._last_log_sequence = max(self._last_log_sequence, entry.sequence)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.after(600, self._refresh_logs)

    def _reset_logs(self) -> None:
        self._last_log_sequence = 0
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _clear_logs(self) -> None:
        self.log_handler.clear()
        self._reset_logs()

    def _open_log_folder(self) -> None:
        path = self.controller.config_path.parent
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]

    def request_exit(self) -> None:
        if self.on_exit is not None:
            self.on_exit()
        else:
            self.destroy()
