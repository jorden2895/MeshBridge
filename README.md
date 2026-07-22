# MeshTelegram Bridge

MeshTelegram Bridge is a bidirectional bridge between a Meshtastic MQTT channel
and a Telegram chat.

## How it works

- Meshtastic text packets received through MQTT are decrypted and forwarded to
  the configured Telegram chat.
- Text messages sent in the configured Telegram chat are broadcast to the
  Meshtastic channel.
- Recently received Meshtastic messages are deduplicated for 60 seconds.
- Telegram-originated messages are tagged with `[TG:<user_id>]` to prevent
  forwarding loops.
- Telegram-originated messages whose complete UTF-8 payload exceeds the
  Meshtastic 233-byte limit are silently dropped.

## Requirements

- Python 3.10 or newer
- An MQTT broker and Meshtastic channel
- A Telegram bot token and target chat ID

## Installation (Windows)

Do not copy `.venv` from another computer. Python virtual environments contain
machine-specific absolute paths and must be created separately on each PC.

The simplest setup method is to double-click `setup_windows.bat`. It creates or
repairs `.venv` and installs the pinned dependencies from `requirements.txt`.

Manual setup:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item config.json.example config.json
```

Edit `config.json` and provide the Telegram, MQTT, and virtual-node settings.
Do not commit or share this file because it contains credentials and channel
keys.

### Settings UI

On Windows, double-click `open_settings.bat` to open the local settings editor.
It can load, validate, and save `config.json`, mask secret fields, and generate
a random Meshtastic node ID. Saving does not contact MQTT or Telegram.

## Running

With the virtual environment activated:

```powershell
python main.py
```

Alternatively, run `run_meshtelegram_bridge.bat` after ensuring that `python`
uses an environment containing the required dependencies.

Press `Ctrl+C` to stop the bridge.

## Testing

The test suite does not contact MQTT or Telegram:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

It covers configuration validation, Meshtastic-compatible channel hashing,
encryption round trips, disconnected publishing, and Telegram authorization.

## Configuration

The configuration contains four sections:

- `logging_level`: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`.
- `telegram`: the bot token and the only chat ID allowed to use the bridge.
- `mqtt`: broker credentials, root topic, channel name, and channel key.
- `node`: the Meshtastic identity advertised by the virtual bridge node.

Use `config.json.example` as the starting point.
The service validates all required settings before opening either network
connection. A failure to connect to MQTT also stops Telegram polling startup,
so the bridge cannot appear healthy while only one side is working.

## Archived legacy files

Unused files inherited from the upstream project are kept under
`archive/legacy/` for reference. They are not part of the bridge's runtime.

## License

This project is distributed under the terms in `LICENSE`.

## Upstream project

MeshTelegram Bridge was originally derived from
[pdxlocations/connect](https://github.com/pdxlocations/connect), a nodeless
Meshtastic MQTT client. This version substantially changes the application into
a dedicated Meshtastic-to-Telegram bridge, adds configuration validation and a
Traditional Chinese settings UI, and includes a new automated test suite.

The upstream project and this derivative are distributed under the GNU General
Public License; see `LICENSE` for the full terms.
