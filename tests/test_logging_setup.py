"""ログの体裁と、アクセスログを黙らせる仕掛けを固定する。

壊れると困るのは 2 つ —— 体裁 (固定幅が崩れると列で読めなくなる) と、アクセスログ
(`access_log=None` が 1 箇所外れるだけで CAN とシーケンスのログが押し流される)。
どちらも「動かなくなる」形では現れず、実機のログを目で見るまで気付けない。
"""

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
    """TTY を名乗るストリーム。色付けの分岐だけを切り替えたいので中身は StringIO。"""

    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """ルートロガーはプロセス共有なので、テストごとに元へ戻す。

    戻さないと、色付きハンドラを張ったテストの後続が pytest の捕捉を失い、
    失敗の理由が出力から読めなくなる。
    """
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
    """設定済みのルートロガー経由で 1 行出し、その行を返す。"""
    logging.getLogger(name).log(level, "テスト本文")
    return stream.getvalue()


class TestLoggerNameShortening:
    """ロガー名は末尾要素へ畳む。フルパスは毎行 20 桁を食う割に何も言わない。"""

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
        """幅が揃っていないと本文の開始位置が行ごとにずれ、列で読めなくなる。"""
        stream = io.StringIO()
        configure_logging(stream=stream)
        logging.getLogger("main").info("A")
        logging.getLogger("lib.can_manager").info("B")
        first, second = stream.getvalue().splitlines()
        assert first.index("A") == second.index("B")

    def test_周期タスクの長い名前でも桁が揃う(self) -> None:
        """欄幅が足りないと、**その行だけ**本文が右へずれて列が崩れる。

        `position_loop` (13) と `target_refresh` (14) は 200Hz / 20Hz の周期タスク
        なので、溢れるといちばん量の多い行が揃わないまま残る。
        """
        stream = io.StringIO()
        configure_logging(stream=stream)
        logging.getLogger("main").info("A")
        logging.getLogger("lib.control.position_loop").info("B")
        logging.getLogger("lib.control.target_refresh").info("C")
        short, mid, longest = stream.getvalue().splitlines()
        assert short.index("A") == mid.index("B") == longest.index("C")


class TestFormatting:
    """レベルは 5 桁固定、時刻は HH:MM:SS.mmm。日付は 1 回の起動を追うのに要らない。"""

    def test_レベルは5桁の略称になる(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """区切りの空白ではなく**位置**で切り出す。

        `split(" ")` で読むと `"INFO "` の右詰め空白が落ちても次の欄との区切りに
        紛れて通ってしまい、幅が揃っていないことを検出できない。
        """
        monkeypatch.setenv("JOURNAL_STREAM", "8:12345")  # 時刻を落として桁を固定する
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
        """幅が 1 桁でも崩れると、レベルごとに本文がずれて列で読めなくなる。"""
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
        # 日付が残っていると、この位置に "-" が現れる
        assert "-" not in stamp

    def test_例外のトレースバックは残る(self) -> None:
        """ログを読む唯一の理由が例外の出どころなので、体裁を変えても落とさない。"""
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
    """色は「飛ばしてよい欄」の目印。本文に付けると読む対象がぶれる。"""

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
        # 本文の直前でリセット済み = 本文に色コードが掛かっていない
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
    """journald は自前で時刻を付ける。こちらも出すと 1 行に時刻が 2 つ並ぶ。"""

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
    """2 回呼んでもハンドラは 1 本。増えると同じ行が 2 度出る。"""

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
    """SPA を配るのでリロード 1 回で数十行出る。読みたいログが押し流される。"""

    def test_aiohttp_accessのINFOは出ない(self) -> None:
        stream = io.StringIO()
        configure_logging(stream=stream)
        logging.getLogger("aiohttp.access").info('GET /index.html HTTP/1.1" 200')
        assert stream.getvalue() == ""

    def test_aiohttp_accessのWARNINGは出る(self) -> None:
        """黙らせるのはアクセス記録だけ。本当の異常まで消してはならない。"""
        stream = io.StringIO()
        configure_logging(stream=stream)
        logging.getLogger("aiohttp.access").warning("異常")
        assert "異常" in stream.getvalue()


class _FakeRunner:
    """`AppRunner` の代役。ネットワークを bind せずに引数だけを捕まえる。"""

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
    """`RobotServer.start()` が `access_log=None` で `AppRunner` を作ること。

    外れても機能は 1 つも壊れないので振る舞いのテストでは検出できず、症状は
    「実機のログがアクセス記録で埋まる」だけ。
    """

    async def test_AppRunnerにaccess_log_Noneを渡す(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _FakeRunner.instances.clear()
        monkeypatch.setattr(web, "AppRunner", _FakeRunner)
        monkeypatch.setattr(web, "TCPSite", _FakeSite)

        fixture = ServerFixture.build()
        # start() は asyncio.Event().wait() で永久に待つので、捕まえたら畳む
        task = asyncio.create_task(fixture.server.start())
        try:
            assert await wait_until(lambda: bool(_FakeRunner.instances))
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert _FakeRunner.instances[0].kwargs.get("access_log", "未指定") is None


class TestStartupLineNamesDryRun:
    """`--dry-run` で起動したことは、起動 1 行目から読めなければならない。

    dry-run でも UI は平常どおり値を描き接続表示も緑になる —— **画面からは本物と
    区別が付かない。** 会場で「繋がるのに機体が動かない」を切り分ける最初の手掛かり。
    """

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
        """常に付けると目印にならない (付いていないことが実機の証拠である)。"""
        assert "dry-run" not in await self._startup_line(monkeypatch, dry_run=False)
