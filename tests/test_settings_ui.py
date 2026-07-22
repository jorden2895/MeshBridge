import json
import tempfile
import unittest
from pathlib import Path

from settings_ui import build_config, flatten_config, save_config_atomic
from test_config import valid_config


class SettingsUiDataTests(unittest.TestCase):
    def test_flatten_and_build_round_trip(self):
        raw = valid_config()
        values = flatten_config(raw)

        result = build_config(values)

        self.assertEqual(result["telegram"]["target_chat_id"], -100123)
        self.assertEqual(result["mqtt"]["port"], 1883)
        self.assertEqual(result["mqtt"]["root_topic"], "msh/TW/2/e/")
        self.assertEqual(result["node"]["id"], 2882392497)

    def test_atomic_save_writes_valid_utf8_json(self):
        data = build_config(flatten_config(valid_config()))
        data["node"]["long_name"] = "台灣橋接器"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config_atomic(data, path)
            loaded = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(loaded["node"]["long_name"], "台灣橋接器")


if __name__ == "__main__":
    unittest.main()
