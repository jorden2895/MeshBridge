import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from app_paths import application_dir


class ApplicationPathTests(unittest.TestCase):
    def test_frozen_application_uses_executable_directory(self):
        executable = r"C:\Portable\MeshTelegram Bridge\MeshTelegramBridge.exe"
        with patch.object(sys, "frozen", True, create=True), patch.object(
            sys, "executable", executable
        ):
            self.assertEqual(
                application_dir(),
                Path(executable).resolve().parent,
            )


if __name__ == "__main__":
    unittest.main()
