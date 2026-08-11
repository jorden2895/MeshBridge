from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class LogEntry:
    sequence: int
    level: str
    message: str


class InMemoryLogHandler(logging.Handler):
    def __init__(self, capacity: int = 1000) -> None:
        super().__init__()
        self._entries: deque[LogEntry] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._sequence = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            self.handleError(record)
            return
        with self._lock:
            self._sequence += 1
            self._entries.append(LogEntry(self._sequence, record.levelname, message))

    def entries_after(self, sequence: int = 0, level: str = "INFO") -> list[LogEntry]:
        minimum = {
            "DEBUG": logging.DEBUG,
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
        }.get(level, logging.INFO)
        with self._lock:
            return [
                entry
                for entry in self._entries
                if entry.sequence > sequence
                and logging._nameToLevel.get(entry.level, logging.INFO) >= minimum
            ]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
