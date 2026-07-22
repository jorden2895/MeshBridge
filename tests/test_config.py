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
