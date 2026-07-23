from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import winreg
from pathlib import Path
from typing import Callable

from app_paths import application_dir


logger = logging.getLogger(__name__)
AUTOSTART_NAME = "MeshTelegram Bridge"
_WINDOW_PROCEDURES: list[object] = []


def _settings_command() -> list[str]:
    directory = application_dir()
    executable = directory / "MeshTelegramBridgeSettings.exe"
    if executable.exists():
        command = [str(executable)]
    else:
        command = [sys.executable, str(directory / "settings_ui.py")]
    return command


def open_settings() -> None:
    subprocess.Popen(
        _settings_command(),
        cwd=application_dir(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


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


def minimize_console_close_to_tray() -> None:
    """Turn the console close button into a hide-to-tray action."""
    if os.name != "nt":
        return
    window = ctypes.windll.kernel32.GetConsoleWindow()
    if not window:
        return
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t,
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_size_t,
        ctypes.c_ssize_t,
    )
    user32.SetWindowLongPtrW.restype = ctypes.c_void_p
    user32.SetWindowLongPtrW.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
    )
    user32.GetWindowLongPtrW.restype = ctypes.c_void_p
    user32.GetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int)
    user32.CallWindowProcW.restype = ctypes.c_ssize_t
    old_procedure = user32.GetWindowLongPtrW(window, -4)

    @callback_type
    def window_procedure(hwnd, message, wparam, lparam):
        if message == 0x0010:  # WM_CLOSE
            user32.ShowWindow(hwnd, 0)
            return 0
        return user32.CallWindowProcW(
            old_procedure,
            hwnd,
            message,
            wparam,
            lparam,
        )

    user32.SetWindowLongPtrW(
        window,
        -4,
        ctypes.cast(window_procedure, ctypes.c_void_p),
    )
    _WINDOW_PROCEDURES.append(window_procedure)


def sync_autostart(enabled: bool) -> None:
    if os.name != "nt":
        return
    if getattr(sys, "frozen", False):
        command = f'"{sys.executable}"'
    else:
        command = f'"{sys.executable}" "{application_dir() / "main.py"}"'
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        key_path,
        0,
        winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE,
    ) as key:
        if enabled:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass


class TrayService:
    def __init__(self, stop_callback: Callable[[], None]) -> None:
        self.stop_callback = stop_callback
        self._icon = None

    def start(self) -> None:
        import pystray
        from PIL import Image, ImageDraw

        image = Image.new("RGBA", (64, 64), "#1469b8")
        draw = ImageDraw.Draw(image)
        draw.ellipse((8, 8, 56, 56), outline="white", width=5)
        draw.line((18, 39, 31, 25, 45, 40), fill="white", width=5)

        def show_settings(icon=None, item=None) -> None:
            open_settings()

        def exit_bridge(icon=None, item=None) -> None:
            self.stop_callback()

        self._icon = pystray.Icon(
            "MeshTelegramBridge",
            image,
            "MeshTelegram Bridge",
            menu=pystray.Menu(
                pystray.MenuItem("設定", show_settings, default=True),
                pystray.MenuItem("結束", exit_bridge),
            ),
        )
        self._icon.run_detached()

    def stop(self) -> None:
        if self._icon is not None:
            self._icon.stop()
            self._icon = None

    def notify(self, title: str, message: str) -> None:
        if self._icon is not None:
            self._icon.notify(message, title)
