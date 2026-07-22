from __future__ import annotations

import sys
from pathlib import Path


def application_dir() -> Path:
    """Return the source directory or, when frozen, the executable directory."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent
