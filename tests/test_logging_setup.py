from __future__ import annotations

import asyncio
import contextlib
import io
import logging
from typing import ClassVar

import pytest
from aiohttp import web

from lib.logging_setup import configure_logging, shorten_logger_name
from tests.server_fixtures import ServerFixture, wait_until


class _TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _restore_root_logger():
    # ルートロガーはプロセス共有。色付きハンドラを張ったまま返すと後続テストが
    # pytest の出力捕捉を失う。
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_access = logging.getLogger("aiohttp.access").level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)
    logging.getLogger("aiohttp.access").setLevel(saved_access)


def _emit(stream: io.StringIO, *, level: int = logging.INFO, name: str = "lib.can_manager") -> str:
    logging.getLogger(name).log(level, "テスト本文")
    return stream.getvalue()


class TestLoggerNameShortening:
    @pytest.mark.parametrize(
        ("full", "expected"),
        [
            ("lib.control.position_loop", "position_loop"),
            ("lib.can_manager", "can_manager"),
            ("sequences.main_hand", "main_hand"),
            ("__main__", "main"),
            ("main", "main"),
        ],
    )
    def test_ドット区切りの末尾要素になる(self, full: str, expected: str) -> None:
        assert shorten_logger_name(full) == expected

    def test_出力行にフルパスは残らない(self) -> None:
        stream = io.StringIO()
        configure_logging(stream=stream)
        line = _emit(stream, name="lib.control.position_loop")
        assert "position_loop" in line
        assert "lib.control" not in line

    def test_短い名前は固定幅まで空白で埋まる(self) -> None:
        stream = io.StringIO()
        configure_logging(stream=stream)
        logging.getLogger("main").info("A")
        logging.getLogger("lib.can_manager").info("B")
        first, second = stream.getvalue().splitlines()
        assert first.index("A") == second.index("B")

    def test_周期タスクの長い名前でも桁が揃う(self) -> None:
        stream = io.StringIO()
        configure_logging(stream=stream)
        logging.getLogger("main").info("A")
        logging.getLogger("lib.control.position_loop").info("B")
        logging.getLogger("lib.control.target_refresh").info("C")
        short, mid, longest = stream.getvalue().splitlines()
        assert short.index("A") == mid.index("B") == longest.index("C")


class TestFormatting:
    def test_レベルは5桁の略称になる(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JOURNAL_STREAM", "8:12345")
        stream = io.StringIO()
        configure_logging(logging.DEBUG, stream=stream)
        for level in (
            logging.DEBUG,
            logging.INFO,
            logging.WARNING,
            logging.ERROR,
            logging.CRITICAL,
        ):
            logging.getLogger("main").log(level, "本文")
        lines = stream.getvalue().splitlines()
        assert [line[:5] for line in lines] == ["DEBUG", "INFO ", "WARN ", "ERROR", "CRIT "]

    def test_レベルが違っても本文の開始位置は同じ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JOURNAL_STREAM", "8:12345")
        stream = io.StringIO()
        configure_logging(logging.DEBUG, stream=stream)
        for level in (
            logging.DEBUG,
            logging.INFO,
            logging.WARNING,
            logging.ERROR,
            logging.CRITICAL,
        ):
            logging.getLogger("main").log(level, "本文")
        starts = {line.index("本文") for line in stream.getvalue().splitlines()}
        assert len(starts) == 1

    def test_時刻はミリ秒までで日付を含まない(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JOURNAL_STREAM", raising=False)
        stream = io.StringIO()
        configure_logging(stream=stream)
        stamp = _emit(stream).split(" ", 1)[0]
        assert len(stamp) == len("00:00:00.000")
        assert stamp.count(":") == 2 and stamp.count(".") == 1
        assert "-" not in stamp

    def test_例外のトレースバックは残る(self) -> None:
        stream = io.StringIO()
        configure_logging(stream=stream)
        try:
            raise RuntimeError("壊れた")
        except RuntimeError:
            logging.getLogger("main").exception("失敗")
        out = stream.getvalue()
        assert "Traceback (most recent call last)" in out
        assert "RuntimeError: 壊れた" in out

    def test_文字列でもintでもレベルを受ける(self) -> None:
        stream = io.StringIO()
        configure_logging("DEBUG", stream=stream)
        assert logging.getLogger().level == logging.DEBUG
        configure_logging("warning", stream=stream)
        assert logging.getLogger().level == logging.WARNING
        configure_logging(logging.ERROR, stream=stream)
        assert logging.getLogger().level == logging.ERROR

    def test_未知のレベルは拒否する(self) -> None:
        with pytest.raises(ValueError, match="未知のログレベル"):
            configure_logging("verbose", stream=io.StringIO())


class TestColor:
    def test_非TTYではANSIが1文字も混ざらない(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JOURNAL_STREAM", raising=False)
        monkeypatch.delenv("NO_COLOR", raising=False)
        stream = io.StringIO()
        configure_logging(stream=stream)
        logging.getLogger("main").warning("警告")
        assert "\x1b" not in stream.getvalue()

    def test_TTYではWARNINGに色が付く(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JOURNAL_STREAM", raising=False)
        monkeypatch.delenv("NO_COLOR", raising=False)
        stream = _TtyStringIO()
        configure_logging(stream=stream)
        logging.getLogger("main").warning("警告")
        assert "\x1b[33mWARN " in stream.getvalue()

    def test_TTYでもINFOの本文には色が付かない(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JOURNAL_STREAM", raising=False)
        monkeypatch.delenv("NO_COLOR", raising=False)
        stream = _TtyStringIO()
        configure_logging(stream=stream)
        logging.getLogger("main").info("本文だけは無色")
        line = stream.getvalue()
        assert line.endswith("本文だけは無色\n")
        assert "\x1b" not in line[line.index("本文だけは無色") :]

    def test_NO_COLORがあれば色を付けない(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JOURNAL_STREAM", raising=False)
        monkeypatch.setenv("NO_COLOR", "1")
        stream = _TtyStringIO()
        configure_logging(stream=stream)
        logging.getLogger("main").error("赤くしない")
        assert "\x1b" not in stream.getvalue()


class TestJournal:
    def test_JOURNAL_STREAMがあれば時刻を出さない(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JOURNAL_STREAM", "8:12345")
        stream = io.StringIO()
        configure_logging(stream=stream)
        assert _emit(stream).startswith("INFO ")

    def test_JOURNAL_STREAMがあればTTYでも色を付けない(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JOURNAL_STREAM", "8:12345")
        monkeypatch.delenv("NO_COLOR", raising=False)
        stream = _TtyStringIO()
        configure_logging(stream=stream)
        logging.getLogger("main").warning("警告")
        assert "\x1b" not in stream.getvalue()

    def test_JOURNAL_STREAMが無ければ時刻を出す(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JOURNAL_STREAM", raising=False)
        stream = io.StringIO()
        configure_logging(stream=stream)
        assert not _emit(stream).startswith("INFO ")


class TestIdempotence:
    def test_二度呼んでもルートハンドラは1本(self) -> None:
        configure_logging(stream=io.StringIO())
        configure_logging(stream=io.StringIO())
        assert len(logging.getLogger().handlers) == 1

    def test_二度呼んでも同じ行が2度出ない(self) -> None:
        first = io.StringIO()
        configure_logging(stream=first)
        second = io.StringIO()
        configure_logging(stream=second)
        logging.getLogger("main").info("1 回だけ")
        assert first.getvalue() == ""
        assert second.getvalue().count("1 回だけ") == 1


class TestAccessLog:
    def test_aiohttp_accessのINFOは出ない(self) -> None:
        stream = io.StringIO()
        configure_logging(stream=stream)
        logging.getLogger("aiohttp.access").info('GET /index.html HTTP/1.1" 200')
        assert stream.getvalue() == ""

    def test_aiohttp_accessのWARNINGは出る(self) -> None:
        stream = io.StringIO()
        configure_logging(stream=stream)
        logging.getLogger("aiohttp.access").warning("異常")
        assert "異常" in stream.getvalue()


class _FakeRunner:
    instances: ClassVar[list[_FakeRunner]] = []

    def __init__(self, app: web.Application, **kwargs: object) -> None:
        self.app = app
        self.kwargs = kwargs
        _FakeRunner.instances.append(self)

    async def setup(self) -> None:
        return None

    async def cleanup(self) -> None:
        return None


class _FakeSite:
    def __init__(self, runner: object, host: str, port: int) -> None:
        self.runner = runner

    async def start(self) -> None:
        return None


class TestServerAccessLogDisabled:
    async def test_AppRunnerにaccess_log_Noneを渡す(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _FakeRunner.instances.clear()
        monkeypatch.setattr(web, "AppRunner", _FakeRunner)
        monkeypatch.setattr(web, "TCPSite", _FakeSite)

        fixture = ServerFixture.build()
        task = asyncio.create_task(fixture.server.start())
        try:
            assert await wait_until(lambda: bool(_FakeRunner.instances))
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert _FakeRunner.instances[0].kwargs.get("access_log", "未指定") is None


class TestStartupLineNamesDryRun:
    async def _startup_line(self, monkeypatch: pytest.MonkeyPatch, *, dry_run: bool) -> str:
        _FakeRunner.instances.clear()
        monkeypatch.setattr(web, "AppRunner", _FakeRunner)
        monkeypatch.setattr(web, "TCPSite", _FakeSite)

        stream = io.StringIO()
        configure_logging(stream=stream)
        fixture = ServerFixture.build(dry_run=dry_run)
        task = asyncio.create_task(fixture.server.start())
        try:
            assert await wait_until(lambda: "サーバー起動" in stream.getvalue())
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return next(line for line in stream.getvalue().splitlines() if "サーバー起動" in line)

    async def test_dry_runなら起動行が名乗る(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert "(dry-run)" in await self._startup_line(monkeypatch, dry_run=True)

    async def test_実機起動では名乗らない(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert "dry-run" not in await self._startup_line(monkeypatch, dry_run=False)
