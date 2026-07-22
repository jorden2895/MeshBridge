import contextlib
import io
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from version import __version__


class LoggingAndVersionTests(unittest.TestCase):
    def tearDown(self):
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()

    def test_setup_logging_writes_utf8_file(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = main.setup_logging("INFO", Path(directory))
            logging.getLogger("test").info("中文日誌")
            for handler in logging.getLogger().handlers:
                handler.flush()

            self.assertEqual(log_path, Path(directory) / main.LOG_FILENAME)
            self.assertIn("中文日誌", log_path.read_text(encoding="utf-8"))
            for handler in list(logging.getLogger().handlers):
                logging.getLogger().removeHandler(handler)
                handler.close()

    def test_log_file_failure_keeps_console_logging_available(self):
        with patch("main.RotatingFileHandler", side_effect=OSError("read only")):
            result = main.setup_logging("INFO", Path("unwritable"))

        self.assertIsNone(result)
        self.assertTrue(any(isinstance(item, logging.StreamHandler) for item in logging.getLogger().handlers))

    def test_rotating_log_retains_bounded_backups(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            main, "LOG_MAX_BYTES", 120
        ), patch.object(main, "LOG_BACKUP_COUNT", 2):
            main.setup_logging("INFO", Path(directory))
            for index in range(20):
                logging.getLogger("rotation").info("entry %s %s", index, "x" * 80)
            for handler in list(logging.getLogger().handlers):
                handler.flush()
                logging.getLogger().removeHandler(handler)
                handler.close()

            files = list(Path(directory).glob(f"{main.LOG_FILENAME}*"))
            self.assertLessEqual(len(files), 3)
            self.assertGreaterEqual(len(files), 2)

    def test_version_command_uses_single_version_source(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
            main.main(["--version"])

        self.assertEqual(stopped.exception.code, 0)
        self.assertIn(__version__, output.getvalue())
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
