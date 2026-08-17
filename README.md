# MeshBridge

MeshBridge is a portable Windows application that bridges plain-text messages among Meshtastic MQTT, Telegram, and Discord. Version 3.1 adds source-local keyword replies, Ground Cow EEW forwarding, and Windows-local Cron schedules to the single `MeshBridge.exe`.

[繁體中文說明](README.zh-TW.md)

## Features

- One Windows executable and one process
- Meshtastic ↔ Telegram ↔ Discord plain-text forwarding
- Up to 20 independently enabled routes
- Per-route Telegram and Discord destinations
- Dashboard for Bridge, platform, route, and session statistics
- Built-in chat monitor and sender (`Ctrl+Enter` to send)
- Route list with add, delete, reorder, enable, and disable controls
- Redacted in-app logs with INFO (general), WARNING, and DEBUG (detailed) filters
- System/light/dark appearance
- Always-available system tray and optional Windows sign-in startup
- Single-instance protection: starting the app again opens the existing window
- Stable-release update checks with GitHub SHA-256 verification
- Case-insensitive exact/contains keyword auto-replies to the original source
- Ground Cow (地牛 Wake Up!) EEW forwarding to every enabled platform on selected routes
- Standard five-field Cron messages using the Windows local timezone

Messages larger than the 233-byte Meshtastic payload limit are dropped without sending a notification. Non-text Discord content is ignored. Message history and statistics exist only in memory and reset when the application exits.

## Quick start

1. Download `MeshBridge.exe` from the latest GitHub Release.
2. Put it in a writable folder and run it.
3. Complete MQTT, platform, node, and route settings.
4. Use **Test connections**, then **Save and apply**.

Closing the window hides it in the system tray. Use the tray menu to show the window, open Settings, start/stop/restart the Bridge, or exit completely. Double-clicking the tray icon opens Settings; a single click intentionally does nothing.

`config.json` is stored beside the executable. Keep it private because it can contain bot tokens, MQTT credentials, and channel keys.

## Routes

Each enabled route represents one Meshtastic channel and may send to Telegram, Discord, or both. At least one destination must be enabled. Telegram-only and Discord-only configurations are supported.

Each route contains:

- A unique route name and Meshtastic channel name
- A Base64 channel key that decodes to 16, 24, or 32 bytes
- Optional Telegram chat/topic destination
- Optional Discord channel destination

## Automation

The **Routes** page enables EEW independently for each route. The **Automation** page manages keyword rules and five-field Cron schedules; keyword routes are selected from a multi-select list. After editing, use **Save and apply** at the bottom of the Automation page. Automation text is limited to 233 UTF-8 bytes for reliable Meshtastic delivery. Missed schedules are not replayed.

For Ground Cow v4.2.0, enable EEW on each intended MeshBridge route and select `MeshBridge.exe` as the linked program. The run-once option is optional. MeshBridge accepts Ground Cow's named `--local-intensity` and `--remaining-time` parameters. Use Ground Cow's built-in test sender to verify delivery. A compact legacy-compatible test is:

```powershell
.\MeshBridge.exe 5+ 20
```

For manual diagnostics, `.\MeshBridge.exe --eew 5+ 20` remains supported.

This is intended for private/internal forwarding and is not an official public earthquake warning service.

## Configuration upgrades

Version 3.1 uses `config_version: 5`. EEW destinations from v4 are migrated to per-route switches. A previous configuration is backed up with its version number before migration, and writes remain atomic.

Version 3 removes obsolete `multi_route_enabled`, `status_api`, `tray.enabled`, `tray.show_console`, global `discord.enabled`, and legacy single-route keys. `MeshBridgeSettings.exe` and `open_settings.bat` are no longer used.

## Run from source

Python 3.14 on Windows is recommended.

```powershell
.\setup_windows.bat
.\.venv\Scripts\python.exe .\main.py
```

Do not copy `.venv` between computers. Run `setup_windows.bat` on each development machine.

## Tests and build

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m bandit -r . -x ./.venv,./build,./dist,./tests -ll
.\.venv\Scripts\python.exe -m pip_audit -r requirements.txt
.\build_release.ps1
```

The release build produces only `dist\MeshBridge.exe`. Check its version with:

```powershell
.\dist\MeshBridge.exe --version
```

## Configuration example

See [`config.json.example`](config.json.example). Never commit your real `config.json`.

## Security

- Local chat, status, and logs stay inside the running process; v3 does not expose a localhost status HTTP API.
- Known credentials are redacted from UI logs and error messages.
- Automatic update downloads accept only the official `MeshBridge.exe` asset with a GitHub-provided SHA-256 digest.
- MQTT channel encryption is not a substitute for a trusted broker or transport security.

## License

See [LICENSE](LICENSE).
