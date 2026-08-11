import logging
import unittest

from app_ui import (
    LOG_LEVEL_TO_UI,
    UI_TO_LOG_LEVEL,
    UI_TO_UPDATE_MODE,
    UPDATE_MODE_TO_UI,
    format_statistics,
    friendly_config_error,
    normalize_log_level,
    translate_status,
)
from log_buffer import InMemoryLogHandler
from runtime_state import STAT_KEYS


class UiLocalizationTests(unittest.TestCase):
    def test_only_three_log_options_are_exposed(self):
        self.assertEqual(list(LOG_LEVEL_TO_UI), ["INFO", "WARNING", "DEBUG"])
        self.assertEqual(
            [UI_TO_LOG_LEVEL[label] for label in LOG_LEVEL_TO_UI.values()],
            ["INFO", "WARNING", "DEBUG"],
        )

    def test_legacy_error_levels_normalize_to_warning(self):
        self.assertEqual(normalize_log_level("ERROR"), "WARNING")
        self.assertEqual(normalize_log_level("CRITICAL"), "WARNING")
        self.assertEqual(normalize_log_level("debug"), "DEBUG")

    def test_update_options_round_trip_to_existing_codes(self):
        self.assertEqual(
            [UI_TO_UPDATE_MODE[label] for label in UPDATE_MODE_TO_UI.values()],
            ["notify", "download", "install"],
        )

    def test_every_runtime_status_has_a_chinese_label(self):
        expected = {
            "starting": "啟動中",
            "connecting": "連線中",
            "running": "運行中",
            "connected": "已連線",
            "reconnecting": "重新連線中",
            "stopping": "停止中",
            "stopped": "已停止",
            "disabled": "未啟用",
            "error": "發生錯誤",
        }
        self.assertEqual({key: translate_status(key) for key in expected}, expected)

    def test_statistics_are_grouped_and_internal_keys_are_hidden(self):
        values = {key: index for index, key in enumerate(STAT_KEYS, start=1)}
        rendered = format_statistics(values)
        self.assertIn("成功轉送", rendered)
        self.assertIn("未傳送或已忽略", rendered)
        self.assertIn("Telegram → Meshtastic：1", rendered)
        self.assertIn("其他未傳送訊息：12", rendered)
        for key in STAT_KEYS:
            self.assertNotIn(key, rendered)
        self.assertEqual(format_statistics(values, False), "執行統計未啟用")

    def test_config_paths_are_presented_with_chinese_names(self):
        rendered = friendly_config_error("mqtt.broker and telegram.bot_token are invalid")
        self.assertIn("伺服器位址（mqtt.broker）", rendered)
        self.assertIn("Telegram 機器人權杖（telegram.bot_token）", rendered)


class LogFilterTests(unittest.TestCase):
    def setUp(self):
        self.handler = InMemoryLogHandler()
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        for level, message in (
            (logging.DEBUG, "debug"),
            (logging.INFO, "info"),
            (logging.WARNING, "warning"),
            (logging.ERROR, "error"),
            (logging.CRITICAL, "critical"),
        ):
            self.handler.emit(logging.LogRecord("test", level, __file__, 1, message, (), None))

    def test_debug_shows_everything(self):
        self.assertEqual(len(self.handler.entries_after(level="DEBUG")), 5)

    def test_info_hides_debug_but_keeps_errors(self):
        self.assertEqual(
            [entry.message for entry in self.handler.entries_after(level="INFO")],
            ["info", "warning", "error", "critical"],
        )

    def test_warning_keeps_warning_and_errors(self):
        self.assertEqual(
            [entry.message for entry in self.handler.entries_after(level="WARNING")],
            ["warning", "error", "critical"],
        )


if __name__ == "__main__":
    unittest.main()
