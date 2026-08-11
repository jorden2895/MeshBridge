import json
import sys
import tempfile
import types
import unittest
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import settings_ui
except ModuleNotFoundError as exc:
    if exc.name != "tkinter":
        raise
    tkinter_stub = types.ModuleType("tkinter")
    tkinter_stub.Tk = type("Tk", (), {})
    tkinter_stub.StringVar = type("StringVar", (), {})
    tkinter_stub.BooleanVar = type("BooleanVar", (), {})
    tkinter_stub.TclError = Exception
    tkinter_stub.messagebox = types.SimpleNamespace(showerror=None, showinfo=None)
    tkinter_stub.ttk = types.SimpleNamespace(Entry=type("Entry", (), {}))
    sys.modules["tkinter"] = tkinter_stub
    settings_ui = import_module("settings_ui")
from settings_ui import (
    DEFAULT_CONFIG,
    SettingsEditor,
    build_config,
    check_connections,
    flatten_config,
    save_config_atomic,
)
from config import AppConfig
from test_config import valid_config


class SettingsUiDataTests(unittest.TestCase):
    def test_embedded_defaults_have_all_required_fields(self):
        values = flatten_config(DEFAULT_CONFIG)
        self.assertIn("telegram.bot_token", values)
        self.assertIn("discord.bot_token", values)
        self.assertEqual(values["discord.enabled"], "false")
        self.assertIn("mqtt.channel_key", values)
        self.assertIn("node.id", values)

    def test_flatten_and_build_round_trip(self):
        raw = valid_config()
        raw["bridge_ui"] = {"display_name": "基地台"}
        values = flatten_config(raw)

        result = build_config(values)

        self.assertEqual(result["telegram"]["target_chat_id"], -100123)
        self.assertEqual(result["mqtt"]["port"], 1883)
        self.assertEqual(result["mqtt"]["root_topic"], "msh/TW/2/e/")
        self.assertEqual(result["node"]["id"], 2882392497)
        self.assertEqual(result["bridge_ui"]["display_name"], "基地台")

    def test_legacy_config_ui_defaults_bridge_ui_display_name(self):
        values = flatten_config(valid_config())

        self.assertEqual(values["bridge_ui.display_name"], "Bridge UI")

    def test_discord_fields_round_trip_without_numeric_id_conversion(self):
        raw = valid_config()
        raw["discord"] = {"enabled": True, "bot_token": "discord-token"}
        raw["routes"] = [
            {
                "name": "主要路由",
                "enabled": True,
                "channel_name": "Test",
                "channel_key": "AQ==",
                "target_chat_id": -100123,
                "topic_id": None,
                "discord_channel_id": "123456789012345678",
            }
        ]

        result = build_config(flatten_config(raw))

        self.assertTrue(result["discord"]["enabled"])
        self.assertEqual(result["discord"]["bot_token"], "discord-token")
        self.assertEqual(
            result["routes"][0]["discord_channel_id"],
            "123456789012345678",
        )

    def test_per_route_destinations_round_trip(self):
        raw = valid_config()
        raw["telegram"] = {"bot_token": "", "target_chat_id": None}
        raw["discord"] = {"enabled": True, "bot_token": "discord-token"}
        raw["routes"] = [
            {
                "name": "Discord 路由",
                "enabled": True,
                "telegram_enabled": False,
                "discord_enabled": True,
                "channel_name": "Test",
                "channel_key": "AQ==",
                "target_chat_id": None,
                "topic_id": None,
                "discord_channel_id": "123456789012345678",
            }
        ]

        result = build_config(flatten_config(raw))

        self.assertFalse(result["routes"][0]["telegram_enabled"])
        self.assertTrue(result["routes"][0]["discord_enabled"])
        self.assertIsNone(result["routes"][0]["target_chat_id"])
        self.assertTrue(result["discord"]["enabled"])

    def test_atomic_save_writes_valid_utf8_json(self):
        data = build_config(flatten_config(valid_config()))
        data["node"]["long_name"] = "台灣橋接器"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config_atomic(data, path)
            loaded = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(loaded["node"]["long_name"], "台灣橋接器")

    def test_load_handles_non_object_json_without_crashing(self):
        class FakeVar:
            def __init__(self, value: str = ""):
                self.value = value

            def set(self, value: str) -> None:
                self.value = value

            def get(self) -> str:
                return self.value

        initial_values = flatten_config(valid_config())
        non_objects = ([], "hello", None)

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            example_path = Path(directory) / "config.example.json"
            for payload in non_objects:
                with self.subTest(payload=payload):
                    config_path.write_text(json.dumps(payload), encoding="utf-8")
                    editor = SimpleNamespace(
                        variables={key: FakeVar(value) for key, value in initial_values.items()},
                        status=FakeVar("就緒"),
                    )
                    with patch.object(settings_ui, "CONFIG_PATH", config_path), patch.object(
                        settings_ui, "EXAMPLE_PATH", example_path
                    ), patch.object(settings_ui.messagebox, "showerror") as showerror:
                        SettingsEditor.load(editor)
                    self.assertEqual(editor.status.get(), "載入失敗")
                    self.assertEqual(
                        {key: value.get() for key, value in editor.variables.items()},
                        initial_values,
                    )
                    showerror.assert_called_once()
                    self.assertIn("最外層必須是 JSON 物件", showerror.call_args.args[1])

    def test_load_object_json_behavior_unchanged(self):
        class FakeVar:
            def __init__(self, value: str = ""):
                self.value = value

            def set(self, value: str) -> None:
                self.value = value

            def get(self) -> str:
                return self.value

        loaded_values = valid_config()

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            example_path = Path(directory) / "config.example.json"
            config_path.write_text(json.dumps(loaded_values), encoding="utf-8")

            editor = SimpleNamespace(
                variables={
                    key: FakeVar("")
                    for key in ["logging_level"] + [path for _, fields in settings_ui.FIELD_GROUPS for path, _, _ in fields]
                },
                status=FakeVar("就緒"),
            )
            with patch.object(settings_ui, "CONFIG_PATH", config_path), patch.object(
                settings_ui, "EXAMPLE_PATH", example_path
            ), patch.object(settings_ui.messagebox, "showerror") as showerror:
                SettingsEditor.load(editor)

        self.assertEqual(editor.status.get(), "已載入 config.json")
        self.assertEqual(editor.variables["mqtt.port"].get(), "1883")
        self.assertEqual(editor.variables["node.id"].get(), "2882392497")
        showerror.assert_not_called()

    def test_connection_results_report_services_independently_and_mask_secrets(self):
        raw = valid_config()
        raw["discord"] = {"enabled": False, "bot_token": "discord-secret"}
        config = AppConfig.from_dict(raw)

        def telegram_probe(config):
            return "@bridge_bot"

        def mqtt_probe(config):
            raise ConnectionError(
                f"password={config.mqtt.password}; discord={config.discord.bot_token}"
            )

        results = check_connections(config, telegram_probe, mqtt_probe)

        self.assertTrue(results[0].succeeded)
        self.assertIn("@bridge_bot", results[0].message)
        self.assertFalse(results[1].succeeded)
        self.assertNotIn(config.mqtt.password, results[1].message)
        self.assertNotIn(config.discord.bot_token, results[1].message)
        self.assertIn("***", results[1].message)

    def test_enabled_discord_is_tested_independently(self):
        raw = valid_config()
        raw["discord"] = {"enabled": True, "bot_token": "discord-token"}
        raw["routes"] = [
            {
                "name": "主要路由",
                "enabled": True,
                "channel_name": "Test",
                "channel_key": "AQ==",
                "target_chat_id": -100123,
                "discord_channel_id": "123456789012345678",
            }
        ]
        config = AppConfig.from_dict(raw)

        results = check_connections(
            config,
            telegram_probe=lambda config: "@telegram_bot",
            mqtt_probe=lambda config: "localhost:1883",
            discord_probe=lambda config: "discord_bot",
        )

        self.assertEqual([result.service for result in results], ["Telegram", "MQTT", "Discord"])
        self.assertTrue(all(result.succeeded for result in results))


if __name__ == "__main__":
    unittest.main()
