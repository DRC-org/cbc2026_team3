from __future__ import annotations

import logging
import os
import sys
from typing import TextIO

_JOURNAL_ENV = "JOURNAL_STREAM"

_LEVEL_ABBREV = {
    "DEBUG": "DEBUG",
    "INFO": "INFO ",
    "WARNING": "WARN ",
    "ERROR": "ERROR",
    "CRITICAL": "CRIT ",
}

_ANSI_RESET = "\x1b[0m"
_ANSI_DIM = "\x1b[2m"

_LEVEL_COLOR = {
    "DEBUG": _ANSI_DIM,
    "INFO": "",
    "WARNING": "\x1b[33m",
    "ERROR": "\x1b[31m",
    "CRITICAL": "\x1b[1;31m",
}

_NAME_WIDTH = 14

_TIME_FORMAT = "%H:%M:%S"


def shorten_logger_name(name: str) -> str:
    if name == "__main__":
        return "main"
    tail = name.rpartition(".")[2]
    return tail or name


class CompactFormatter(logging.Formatter):
    def __init__(
        self,
        *,
        color: bool,
        timestamp: bool,
        name_width: int = _NAME_WIDTH,
    ) -> None:
        super().__init__()
        self._color = color
        self._timestamp = timestamp
        self._name_width = name_width

    def _paint(self, text: str, ansi: str) -> str:
        if not self._color or not ansi:
            return text
        return f"{ansi}{text}{_ANSI_RESET}"

    def format(self, record: logging.LogRecord) -> str:
        fields: list[str] = []

        if self._timestamp:
            stamp = f"{self.formatTime(record, _TIME_FORMAT)}.{int(record.msecs):03d}"
            fields.append(self._paint(stamp, _ANSI_DIM))

        level = _LEVEL_ABBREV.get(record.levelname, record.levelname[:5].ljust(5))
        fields.append(self._paint(level, _LEVEL_COLOR.get(record.levelname, "")))

        name = shorten_logger_name(record.name).ljust(self._name_width)
        fields.append(self._paint(name, _ANSI_DIM))

        fields.append(record.getMessage())

        text = " ".join(fields)

        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            text = f"{text}\n{record.exc_text}"
        if record.stack_info:
            text = f"{text}\n{self.formatStack(record.stack_info)}"
        return text


def _in_journal() -> bool:
    return _JOURNAL_ENV in os.environ


def _is_tty(stream: TextIO) -> bool:
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except ValueError:
        return False


def _resolve_level(level: int | str) -> int:
    if isinstance(level, int):
        return level
    resolved = logging.getLevelNamesMapping().get(level.strip().upper())
    if resolved is None:
        raise ValueError(f"未知のログレベル: {level!r}")
    return resolved


def configure_logging(
    level: int | str = logging.INFO,
    *,
    stream: TextIO | None = None,
) -> None:
    target = sys.stderr if stream is None else stream

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(target)
    handler.setFormatter(
        CompactFormatter(
            color=_is_tty(target) and not _in_journal() and "NO_COLOR" not in os.environ,
            timestamp=not _in_journal(),
        )
    )
    root.addHandler(handler)
    root.setLevel(_resolve_level(level))

    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
