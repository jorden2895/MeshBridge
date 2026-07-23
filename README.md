# MeshTelegram Bridge

English | [繁體中文](README.zh-TW.md)

MeshTelegram Bridge forwards text messages in both directions between an
encrypted Meshtastic MQTT channel and one authorized Telegram chat.

## Features

- Decrypts Meshtastic MQTT text packets and forwards them to Telegram.
- Broadcasts messages from the configured Telegram chat to Meshtastic.
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
  configuration and tests Telegram and MQTT connectivity.

## Quick start with Windows executables

1. Download `MeshTelegramBridge.exe` and `MeshTelegramBridgeSettings.exe` from
   the [latest GitHub Release](https://github.com/jorden2895/meshtelegram-bridge/releases/latest).
2. Put both files in the same folder.
3. Open `MeshTelegramBridgeSettings.exe`.
4. Enter the Telegram, MQTT, and virtual-node settings, then select **驗證**.
5. Select **測試連線** to check the Telegram token and MQTT credentials. This
   does not start the bridge or send a message.
6. Save the configuration and start `MeshTelegramBridge.exe`.

The settings application creates `config.json` beside the executable. Keep
this file private: it contains the Telegram bot token, MQTT password, and
Meshtastic channel key.

## Configuration

Start from `config.json.example` when running from source. The configuration
contains four sections:

- `logging_level`: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`.
- `telegram`: bot token and the only chat ID authorized to use the bridge.
- `mqtt`: broker address, port, credentials, root topic, channel name, and
  channel key.
- `node`: the virtual Meshtastic node ID, long name, and short name.

The application validates all required values before opening a network
connection. Failure to establish MQTT connectivity also stops Telegram polling
startup, so the bridge cannot appear ready while only one side is working.

### Finding the Telegram chat ID

Add the bot to the intended private chat or group and send `/start`. The bot
responds only after that chat ID has been configured. For initial setup, obtain
the chat ID using Telegram's Bot API or another trusted chat-ID tool, enter it
in the settings application, and restart the bridge.

Only one running program may poll a Telegram bot token at a time.

## Running from source on Windows

Requirements:

- Python 3.10 or newer
- Windows with the Python launcher (`py`)
- An MQTT broker and Meshtastic channel
- A Telegram bot token and target chat ID

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
.\run_meshtelegram_bridge.bat
```

You can also run `python main.py`. Press `Ctrl+C` to stop it safely.

Show the current application version with:

```powershell
python main.py --version
```

## Logs

The bridge writes `MeshTelegramBridge.log` beside the application and keeps up
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
- **MQTT or Telegram does not connect:** open the settings application and use
  **測試連線**. The two services report their results separately.
- **A Telegram message is not forwarded:** confirm that it came from the
  configured chat and does not exceed 233 UTF-8 bytes.

## Development and testing

Run the test suite without contacting Telegram or MQTT:

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

MeshTelegram Bridge was originally derived from
[pdxlocations/connect](https://github.com/pdxlocations/connect), a nodeless
Meshtastic MQTT client. This project substantially changes the application into
a dedicated Meshtastic-to-Telegram bridge with configuration validation, a
Traditional Chinese settings UI, security checks, and automated tests.

The upstream project and this derivative are distributed under the GNU General
Public License. See [LICENSE](LICENSE).
