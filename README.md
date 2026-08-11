# MeshBridge

English | [繁體中文](README.zh-TW.md)

MeshBridge forwards text between encrypted Meshtastic MQTT channels,
authorized Telegram chats or topics, and optional Discord text channels.

## Features

- Fans the same plain-text message out between Meshtastic, Telegram, and Discord.
- Handles each destination independently so one unavailable platform does not
  prevent delivery to the others.
- Ignores Discord attachments, images, stickers, empty content, and bot messages.
- Accepts only encrypted packets with the configured channel hash; plaintext
  MQTT `decoded` packets are rejected.
- Deduplicates MQTT redeliveries by sender and packet ID for 60 seconds.
- Silently ignores unauthorized Telegram chats and commands.
- Detects duplicate Telegram polling and stops the conflicting instance with a
  clear Traditional Chinese error message.
- Enforces the Meshtastic 233-byte UTF-8 payload limit. Oversized Telegram
  messages are silently dropped.
- Writes UTF-8 terminal and rotating file logs without logging message content
  at the default `INFO` level.
- Includes a Traditional Chinese settings application that validates the
  configuration and tests Telegram, MQTT, and optional Discord connectivity.
- Adds a chat tab to the settings application for monitoring active routes and
  sending text to Meshtastic, Telegram, Discord, or all platforms.
- Supports up to five Meshtastic/Telegram/Discord route mappings.
- Shows live MQTT/Telegram/Discord state and per-run forwarding/drop statistics.
- Offers opt-in tray mode, Windows logon startup, and stable Release updates.

## Quick start with Windows executables

1. Download `MeshBridge.exe` and `MeshBridgeSettings.exe` from
   the [latest GitHub Release](https://github.com/jorden2895/MeshBridge/releases/latest).
2. Put both files in the same folder.
3. Open `MeshBridgeSettings.exe`.
4. Enter the Telegram, MQTT, and virtual-node settings. To use Discord, enable
   it and enter its bot token and a channel ID for each applicable route.
5. Select **測試連線** to check every enabled service. This
   does not start the bridge or send a message.
6. Save the configuration and start `MeshBridge.exe`.
7. Keep the Bridge running, then use the **聊天** tab to monitor messages or
   send text through a selected route.

The settings application creates `config.json` beside the executable. Keep
this file private: it can contain Telegram and Discord bot tokens, the MQTT
password, and Meshtastic channel keys.

Version 2.0 renames the executables to `MeshBridge.exe` and
`MeshBridgeSettings.exe`. Existing `config.json` files remain compatible. Users
coming from a 1.x build should download the renamed files manually because the
old automatic installer searches for the former asset names.

## Configuration

Start from `config.json.example` when running from source. Existing single-route
configuration files remain compatible. New settings include:

- `logging_level`: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`.
- `telegram`: bot token and the only chat ID authorized to use the bridge.
- `discord`: optional enable flag and bot token; disabled by default.
- `mqtt`: broker address, port, credentials, root topic, channel name, and
  channel key.
- `node`: the virtual Meshtastic node ID, long name, and short name.
- `bridge_ui.display_name`: the name shown in locally sent message prefixes and
  the chat monitor; defaults to `Bridge UI` for existing configurations.
- `routes`: up to five mappings containing a Meshtastic channel/key, Telegram
  chat/topic, and optional string-valued Discord channel ID.
- `features`: statistics, local status API, multi-route, tray, and update options.

`mqtt.root_topic` is safely normalized, but MQTT wildcards `+` and `#` are
rejected. MQTT credentials may both be blank for anonymous brokers. The node
short name remains limited to four characters.

The application validates all required values before opening a network
connection. Enabling Discord requires a token and at least one enabled route
with a Discord channel ID. A required service that fails during initial startup
causes the Bridge to show the reason and stop.

## Status, tray, and updates

The status API listens only on `127.0.0.1` and uses a random token per run. The
settings tool treats a heartbeat older than five seconds as offline. It never
shows bot tokens, MQTT credentials, or channel keys, and known secrets are
redacted from recent errors. Statistics reset whenever the Bridge restarts.

The settings tool's **聊天** tab monitors all active routes and keeps only the
latest 200 messages in Bridge memory. Message history is never persisted and is
cleared when the Bridge restarts. Select a route and send to Meshtastic,
Telegram, Discord, or all platforms independently. UI-originated messages use the
configured display-name prefix (for example, `[Base Station]: `), and the same
name appears as their source in the monitor. Meshtastic-bound text, including
that prefix, must fit the 233-byte UTF-8 payload limit. The chat tab is
unavailable when the Bridge is stopped or the local status API is disabled.

Discord-originated text uses the `[DC:username]: ` prefix; Telegram-originated
text uses `[TG:UID]: `. Discord messages are limited to 2,000 characters, while
Meshtastic still applies its 233-byte limit after adding the prefix.

Tray mode is part of `MeshBridge.exe`; double-clicking opens the
settings tool. Release builds hide the console by default in tray mode, with
an option to show it.

Update checks use stable GitHub Releases only, send no device identifier or
usage analytics, and default to notification mode with a 24-hour interval.
Optional downloads and delayed installs require GitHub's SHA-256 digest to
match both portable executables.

### Finding the Telegram chat ID

Add the bot to the intended private chat or group and send `/start`. The bot
responds only after that chat ID has been configured. For initial setup, obtain
the chat ID using Telegram's Bot API or another trusted chat-ID tool, enter it
in the settings application, and restart the bridge.

Only one running program may poll a Telegram bot token at a time.

### Configuring a Discord bot

1. Create an application and bot in the
   [Discord Developer Portal](https://discord.com/developers/applications), then
   copy the bot token.
2. Under **Bot → Privileged Gateway Intents**, enable **Message Content Intent**.
   Presence and Server Members intents are not required.
3. Install the bot to the server with the `bot` scope. Grant only **View
   Channels**, **Send Messages**, and **Read Message History**; Administrator is
   not required.
4. Enable Discord Developer Mode, right-click the destination text channel, and
   copy its channel ID.
5. Enable Discord in the settings application, enter the token, and assign the
   channel ID to the desired route.

Treat the bot token as a password and reset it immediately if exposed. Without
Message Content Intent, the bot cannot read ordinary message text.

## Running from source on Windows

Requirements:

- Python 3.10 or newer
- Windows with the Python launcher (`py`)
- An MQTT broker and Meshtastic channel
- A Telegram bot token and target chat ID
- Optional Discord bot token and text channel ID

Double-click `setup_windows.bat` to create or repair `.venv` and install the
pinned runtime dependencies. Do not copy `.venv` from another computer;
virtual environments contain machine-specific paths.

Manual setup:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item config.json.example config.json
```

Open the settings application:

```powershell
.\open_settings.bat
```

Run the bridge:

```powershell
.\run_meshbridge.bat
```

You can also run `python main.py`. Press `Ctrl+C` to stop it safely.

Show the current application version with:

```powershell
python main.py --version
```

## Logs

The bridge writes `MeshBridge.log` beside the application and keeps up
to five 1 MiB files. Terminal output remains enabled. Use **開啟日誌資料夾**
in the settings application to locate the files.

The default `INFO` level records connection and forwarding metadata without
message bodies. `DEBUG` may contain message content and should be enabled only
for controlled troubleshooting.

## Troubleshooting

- **A window closes immediately:** open Command Prompt or PowerShell, run the
  `.bat` file there, and read the displayed error.
- **A copied `.venv` references Python on another PC:** delete that `.venv` and
  run `setup_windows.bat` on the destination computer.
- **Telegram reports a polling conflict:** stop every other program or computer
  using the same bot token, then start one bridge instance.
- **MQTT, Telegram, or Discord does not connect:** open the settings application
  and use **測試連線**. Each service reports its result independently.
- **The Discord bot is online but does not forward text:** enable Message Content
  Intent and verify its view/send permissions in that channel.
- **A Telegram message is not forwarded:** confirm that it came from the
  configured chat and does not exceed 233 UTF-8 bytes.

## Development and testing

Run the test suite without contacting Telegram, Discord, or MQTT:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Build both standalone executables:

```powershell
.\build_release.ps1
```

The build reads `version.py` as the single version source and validates a
GitHub Release tag against it.

## License and upstream project

MeshBridge was originally derived from
[pdxlocations/connect](https://github.com/pdxlocations/connect), a nodeless
Meshtastic MQTT client. This project substantially changes the application into
a dedicated Meshtastic/Telegram/Discord text bridge with configuration
validation, a Traditional Chinese settings UI, security checks, and automated tests.

The upstream project and this derivative are distributed under the GNU General
Public License. See [LICENSE](LICENSE).
