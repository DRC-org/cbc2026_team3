"""ログの体裁を決める単一情報源。

**読みたいのは CAN とシーケンスのログである。** 既定の `logging.basicConfig` は
1 行の先頭に「日付 + 時刻 + レベル全綴り + ロガーのフルパス」を並べるため、
`2026-09-08 09:20:45,337 [INFO] lib.control.position_loop:` だけで 60 桁を超え、
本文が右端へ押し出される。1 回の起動を追うのに日付は要らず、ロガー名も
モジュール名まで分かれば十分なので、時刻は `HH:MM:SS.mmm`、ロガー名は末尾要素の
固定幅に畳む。**列が揃っていることが目的**で、揃っていれば読み手は左の 25 桁を
視線で飛ばして本文だけを追える。

色を付けるのも同じ理由 —— 「どこを飛ばしてよいか」の目印であって装飾ではない。
だから時刻とロガー名を dim に沈め、レベルだけを警告色にし、**本文には一切色を
付けない** (本文が読みたいものなので、色で強弱を付けると読む対象がぶれる)。
"""

from __future__ import annotations

import logging
import os
import sys
from typing import TextIO

#: systemd の journal 配下で起動されたときだけ立つ環境変数 (systemd が設定する)。
#: journald は行ごとに自前のタイムスタンプを付けるので、こちらも出すと 1 行に
#: 時刻が 2 つ並ぶ
_JOURNAL_ENV = "JOURNAL_STREAM"

#: レベル名を 5 桁固定へ畳んだ表。幅が揃っていないとロガー名の開始位置が行ごとに
#: ずれ、列で読めなくなる
_LEVEL_ABBREV = {
    "DEBUG": "DEBUG",
    "INFO": "INFO ",
    "WARNING": "WARN ",
    "ERROR": "ERROR",
    "CRITICAL": "CRIT ",
}

_ANSI_RESET = "\x1b[0m"
_ANSI_DIM = "\x1b[2m"

#: レベルごとの色。INFO は無色 —— 平常のログが色付きだと、色が付いていること自体が
#: 異常の目印にならなくなる
_LEVEL_COLOR = {
    "DEBUG": _ANSI_DIM,
    "INFO": "",
    "WARNING": "\x1b[33m",
    "ERROR": "\x1b[31m",
    "CRITICAL": "\x1b[1;31m",
}

#: ロガー名の欄幅。実際に出る名前のうち最も長い `target_refresh` (14) に合わせてある
#: —— 1 つでも溢れると、そこから右の本文だけが行ごとにずれて列で読めなくなる。
#: 超えたものは切らずに溢れさせる (名前を削ると、どのモジュールか分からなくなる)
_NAME_WIDTH = 14

_TIME_FORMAT = "%H:%M:%S"


def shorten_logger_name(name: str) -> str:
    """ロガー名をドット区切りの末尾要素へ畳む。

    `lib.control.position_loop` → `position_loop`。パッケージのパスは
    「どこにあるか」であって「何が言っているか」ではないので、毎行そこへ 20 桁
    払う価値がない。`__main__` だけは末尾要素がそのままだと読みにくいので `main`。
    """
    if name == "__main__":
        return "main"
    tail = name.rpartition(".")[2]
    return tail or name


class CompactFormatter(logging.Formatter):
    """`HH:MM:SS.mmm LEVEL logger      message` の 1 行を組み立てる。

    時刻と色の有無は生成時に固定する。出力先が変わらない以上、レコードごとに
    判定し直す意味がないうえ、判定を毎行やると「途中から色が付く / 消える」
    という説明のつかない出力を作れてしまう。
    """

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

        # 本文は無色のまま。色は「飛ばしてよい欄」の目印に限る
        fields.append(record.getMessage())

        text = " ".join(fields)

        # トレースバックとスタックは従来どおり本文の後ろへ続ける。
        # 例外の出どころが消えると、ログを読む唯一の理由が失われる
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
        # 既に閉じたストリームは TTY ではない扱いにする (ここで落ちるとログ設定
        # そのものが失敗し、以後の失敗が 1 行も残らなくなる)
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
    """ルートロガーへハンドラを 1 本だけ張る (冪等)。

    既存のハンドラを外してから張るので、2 回呼んでも同じ行が 2 度出ることはない。
    `logging.basicConfig` は既にハンドラがあると**黙って何もしない**ので、
    ライブラリが先に 1 本張っていると体裁の指定ごと無視される。ここで明示的に
    張り替えるのは、その「効いているつもりで効いていない」を作らないため。
    """
    target = sys.stderr if stream is None else stream

    root = logging.getLogger()
    # close() はしない —— 呼び出し元が渡したストリームを閉じてしまう恐れがある。
    # ルートから外れた時点で配信対象ではなくなるので、二重出力は起きない
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

    # SPA を配るのでリロード 1 回で数十行出る。`RobotServer.start()` は
    # `access_log=None` でアクセスログ自体を止めているが、`AppRunner` を別経路で
    # 作られたときのために二重の歯止めを置く (押し流されて困るのは CAN と
    # シーケンスのログで、HTTP の可否は UI 側に出るのでログで追う価値がない)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
