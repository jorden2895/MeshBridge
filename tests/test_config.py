import unittest

from config import AppConfig, ConfigError


def valid_config():
    return {
        "logging_level": "INFO",
        "telegram": {"bot_token": "token", "target_chat_id": "-100123"},
        "mqtt": {
            "broker": "localhost",
            "port": 1883,
            "username": "user",
            "password": "pass",
            "root_topic": "msh/TW/2/e",
            "channel_name": "Test",
            "channel_key": "AQ==",
        },
        "node": {"id": "2882392497", "long_name": "Bridge", "short_name": "TGBT"},
    }


class ConfigTests(unittest.TestCase):
    def test_legacy_config_keeps_discord_disabled(self):
        config = AppConfig.from_dict(valid_config())

        self.assertFalse(config.discord.enabled)
        self.assertEqual(config.discord.bot_token, "")
        self.assertIsNone(config.routes[0].discord_channel_id)

    def test_discord_config_and_channel_ids_remain_strings(self):
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
                "discord_channel_id": 123456789012345678,
            }
        ]

        config = AppConfig.from_dict(raw)

        self.assertTrue(config.discord.enabled)
        self.assertEqual(config.routes[0].discord_channel_id, "123456789012345678")

    def test_enabled_discord_requires_token_and_active_channel(self):
        raw = valid_config()
        raw["discord"] = {"enabled": True, "bot_token": ""}
        with self.assertRaisesRegex(ConfigError, "discord.bot_token"):
            AppConfig.from_dict(raw)

        raw["discord"]["bot_token"] = "discord-token"
        with self.assertRaisesRegex(ConfigError, "Discord 頻道 ID"):
            AppConfig.from_dict(raw)

    def test_rejects_invalid_or_duplicate_discord_channel_ids(self):
        raw = valid_config()
        raw["routes"] = []
        for index in range(2):
            raw["routes"].append(
                {
                    "name": f"路由 {index}",
                    "enabled": True,
                    "channel_name": f"Channel{index}",
                    "channel_key": "AQ==",
                    "target_chat_id": -100123 - index,
                    "discord_channel_id": "123456789012345678",
                }
            )
        with self.assertRaisesRegex(ConfigError, "Discord 頻道必須不同"):
            AppConfig.from_dict(raw)

        raw["routes"][1]["discord_channel_id"] = "not-a-number"
        with self.assertRaisesRegex(ConfigError, "正整數字串"):
            AppConfig.from_dict(raw)

    def test_legacy_config_uses_default_bridge_ui_display_name(self):
        config = AppConfig.from_dict(valid_config())

        self.assertEqual(config.bridge_ui.display_name, "Bridge UI")

    def test_custom_bridge_ui_display_name_is_normalized(self):
        raw = valid_config()
        raw["bridge_ui"] = {"display_name": "  基地台  "}

        config = AppConfig.from_dict(raw)

        self.assertEqual(config.bridge_ui.display_name, "基地台")

    def test_rejects_invalid_bridge_ui_display_name(self):
        for display_name in ("", "x" * 33, "line\nbreak", "control\x7f"):
            with self.subTest(display_name=display_name):
                raw = valid_config()
                raw["bridge_ui"] = {"display_name": display_name}
                with self.assertRaisesRegex(ConfigError, "bridge_ui.display_name"):
                    AppConfig.from_dict(raw)

    def test_normalizes_types_topic_and_simple_key(self):
        config = AppConfig.from_dict(valid_config())
        self.assertEqual(config.telegram.target_chat_id, -100123)
        self.assertEqual(config.mqtt.root_topic, "msh/TW/2/e/")
        self.assertEqual(len(config.mqtt.channel_key), 16)
        self.assertEqual(config.node.node_id, 2882392497)

    def test_rejects_invalid_port(self):
        raw = valid_config()
        raw["mqtt"]["port"] = 70000
        with self.assertRaisesRegex(ConfigError, "1 到 65535"):
            AppConfig.from_dict(raw)

    def test_rejects_invalid_node_id(self):
        raw = valid_config()
        raw["node"]["id"] = "not-a-number"
        with self.assertRaisesRegex(ConfigError, "32 位元無號整數"):
            AppConfig.from_dict(raw)

    def test_rejects_invalid_channel_key(self):
        raw = valid_config()
        raw["mqtt"]["channel_key"] = "not-base64!"
        with self.assertRaisesRegex(ConfigError, "channel_key"):
            AppConfig.from_dict(raw)


if __name__ == "__main__":
    unittest.main()
