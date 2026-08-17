from __future__ import annotations

import ctypes
import logging
import os
import sys
import winreg
from pathlib import Path
from typing import Callable

from app_paths import application_dir


logger = logging.getLogger(__name__)
AUTOSTART_NAME = "MeshBridge"
LEGACY_AUTOSTART_NAME = "MeshTelegram Bridge"


def set_console_visible(visible: bool) -> None:
    if os.name != "nt":
        return
    window = ctypes.windll.kernel32.GetConsoleWindow()
    if visible and not window:
        if ctypes.windll.kernel32.AllocConsole():
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
            window = ctypes.windll.kernel32.GetConsoleWindow()
    if window:
        ctypes.windll.user32.ShowWindow(window, 5 if visible else 0)


def sync_autostart(enabled: bool) -> None:
    if os.name != "nt":
        return
    if getattr(sys, "frozen", False):
        command = f'"{sys.executable}" --autostart'
    else:
        command = f'"{sys.executable}" "{application_dir() / "main.py"}" --autostart'
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        key_path,
        0,
        winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE,
    ) as key:
        try:
            winreg.DeleteValue(key, LEGACY_AUTOSTART_NAME)
        except FileNotFoundError:
            pass
        if enabled:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass


class TrayService:
    def __init__(
        self,
        exit_callback: Callable[[], None],
        *,
        show_callback: Callable[[], None] | None = None,
        settings_callback: Callable[[], None] | None = None,
        start_callback: Callable[[], None] | None = None,
        stop_callback: Callable[[], None] | None = None,
        restart_callback: Callable[[], None] | None = None,
        status_callback: Callable[[], str] | None = None,
        can_start_callback: Callable[[], bool] | None = None,
        can_stop_callback: Callable[[], bool] | None = None,
        can_restart_callback: Callable[[], bool] | None = None,
    ) -> None:
        self.exit_callback = exit_callback
        self.show_callback = show_callback or (lambda: None)
        self.settings_callback = settings_callback or self.show_callback
        self.start_callback = start_callback or (lambda: None)
        self.stop_callback = stop_callback or exit_callback
        self.restart_callback = restart_callback or (lambda: None)
        self.status_callback = status_callback or (lambda: "橋接服務：狀態未知")
        self.can_start_callback = can_start_callback or (lambda: True)
        self.can_stop_callback = can_stop_callback or (lambda: False)
        self.can_restart_callback = can_restart_callback or (lambda: False)
        self._icon = None

    def start(self) -> None:
        import pystray
        from PIL import Image, ImageDraw

        image = Image.new("RGBA", (64, 64), "#1469b8")
        draw = ImageDraw.Draw(image)
        draw.ellipse((8, 8, 56, 56), outline="white", width=5)
        draw.line((18, 39, 31, 25, 45, 40), fill="white", width=5)

        def invoke(callback):
            def wrapped(icon=None, item=None) -> None:
                callback()
            return wrapped

        def exit_bridge(icon=None, item=None) -> None:
            self.exit_callback()

        self._icon = pystray.Icon(
            "MeshBridge",
            image,
            "MeshBridge",
            menu=pystray.Menu(
                pystray.MenuItem(lambda item: self.status_callback(), None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("顯示 MeshBridge", invoke(self.show_callback)),
                pystray.MenuItem("設定", invoke(self.settings_callback), default=True),
                pystray.MenuItem(
                    "啟動橋接服務",
                    invoke(self.start_callback),
                    enabled=lambda item: self.can_start_callback(),
                ),
                pystray.MenuItem(
                    "停止橋接服務",
                    invoke(self.stop_callback),
                    enabled=lambda item: self.can_stop_callback(),
                ),
                pystray.MenuItem(
                    "重新啟動橋接服務",
                    invoke(self.restart_callback),
                    enabled=lambda item: self.can_restart_callback(),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("結束", exit_bridge),
            ),
        )
        self._icon.run_detached()

    def stop(self) -> None:
        if self._icon is not None:
            self._icon.stop()
            self._icon = None

    def refresh_menu(self) -> None:
        icon = self._icon
        if icon is not None:
            try:
                icon.update_menu()
            except Exception:
                logger.exception("Unable to refresh tray menu.")

    def notify(self, title: str, message: str) -> None:
        if self._icon is not None:
            self._icon.notify(message, title)
