"""CAN バスの持ち主を 1 プロセスに限る (`lib/bus_lock.py`)。

判定は OS の `flock` だけに立つ。ファイルの有無や中の PID で判定すると、
生きている持ち主を追い出す (古いファイルを消す) か、死んだ残骸に起動を塞がれる。
"""

from __future__ import annotations

import logging
import os
import pathlib
import signal
import subprocess
import sys
import textwrap

import pytest

from lib.bus_lock import BusClaimedError, claim_bus, lock_path

_PROJECT_DIR = pathlib.Path(__file__).resolve().parent.parent
_CHANNEL = "can_test"
_HOLDER_MARKER = "--holder-marker-7f3a"

_HOLDER_SCRIPT = textwrap.dedent(
    """
    import pathlib, sys, time
    sys.path.insert(0, sys.argv[1])
    from lib.bus_lock import claim_bus
    claim_bus(sys.argv[2], lock_dirs=[pathlib.Path(sys.argv[3])])
    print("ready", flush=True)
    time.sleep(60)
    """
)


def _spawn_holder(lock_dir: pathlib.Path) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _HOLDER_SCRIPT,
            str(_PROJECT_DIR),
            _CHANNEL,
            str(lock_dir),
            _HOLDER_MARKER,
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if "ready" not in line:
        proc.kill()
        pytest.fail(f"持ち主プロセスが掴めませんでした: {line!r}")
    return proc


def _wait_exit(proc: subprocess.Popen[str]) -> None:
    proc.wait(timeout=10)


class TestClaimBus:
    def test_同じチャネルは2つ目が拒否される(self, tmp_path: pathlib.Path) -> None:
        first = claim_bus(_CHANNEL, lock_dirs=[tmp_path])
        try:
            with pytest.raises(BusClaimedError) as exc:
                claim_bus(_CHANNEL, lock_dirs=[tmp_path])
        finally:
            first.release()

        assert _CHANNEL in str(exc.value)
        assert f"PID {os.getpid()}" in str(exc.value)

    def test_拒否の文面に持ち主のPIDとコマンドラインが出る(self, tmp_path: pathlib.Path) -> None:
        holder = _spawn_holder(tmp_path)
        try:
            with pytest.raises(BusClaimedError) as exc:
                claim_bus(_CHANNEL, lock_dirs=[tmp_path])
        finally:
            holder.kill()
            _wait_exit(holder)

        message = str(exc.value)
        assert f"PID {holder.pid}" in message
        assert _HOLDER_MARKER in message

    def test_持ち主が死んだ後は掴める(self, tmp_path: pathlib.Path) -> None:
        holder = _spawn_holder(tmp_path)
        # 後始末なしの死 (SIGKILL) でも主張が残らないこと
        holder.send_signal(signal.SIGKILL)
        _wait_exit(holder)

        claim = claim_bus(_CHANNEL, lock_dirs=[tmp_path])
        claim.release()

    def test_死んだPIDが書かれた古いファイルは掴むのを妨げない(
        self, tmp_path: pathlib.Path
    ) -> None:
        path = lock_path(_CHANNEL, tmp_path)
        path.write_text("999999\nstale main.py\n")

        claim = claim_bus(_CHANNEL, lock_dirs=[tmp_path])
        try:
            assert path.read_text().startswith(f"{os.getpid()}\n")
        finally:
            claim.release()

    def test_手放せば掴み直せる_ファイルは消さない(self, tmp_path: pathlib.Path) -> None:
        first = claim_bus(_CHANNEL, lock_dirs=[tmp_path])
        first.release()
        first.release()

        assert lock_path(_CHANNEL, tmp_path).exists()
        second = claim_bus(_CHANNEL, lock_dirs=[tmp_path])
        second.release()

    def test_別のチャネルは干渉しない(self, tmp_path: pathlib.Path) -> None:
        first = claim_bus(_CHANNEL, lock_dirs=[tmp_path])
        try:
            other = claim_bus("can_other", lock_dirs=[tmp_path])
            other.release()
        finally:
            first.release()

    def test_名乗れない持ち主でも拒否はする(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 判定はロックだけに立つので、PID を書けなくても拒否は変わらない
        def _cannot_write(*_args: object) -> int:
            raise OSError("write failed")

        monkeypatch.setattr("lib.bus_lock.os.pwrite", _cannot_write)
        first = claim_bus(_CHANNEL, lock_dirs=[tmp_path])
        try:
            with pytest.raises(BusClaimedError) as exc:
                claim_bus(_CHANNEL, lock_dirs=[tmp_path])
        finally:
            first.release()
        assert "PID 不明" in str(exc.value)

    def test_主たる置き場所が開けなければ落とし先で主張する(
        self, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        primary = tmp_path / "missing"
        fallback = tmp_path / "alt"
        fallback.mkdir()

        with caplog.at_level(logging.WARNING):
            claim = claim_bus(_CHANNEL, lock_dirs=[primary, fallback])
        try:
            assert claim.path == lock_path(_CHANNEL, fallback)
            assert any("検出" in record.getMessage() for record in caplog.records)
            with pytest.raises(BusClaimedError):
                claim_bus(_CHANNEL, lock_dirs=[primary, fallback])
        finally:
            claim.release()

    def test_主たる置き場所に持ち主が居れば落とし先へ逃げない(self, tmp_path: pathlib.Path) -> None:
        fallback = tmp_path / "alt"
        fallback.mkdir()
        first = claim_bus(_CHANNEL, lock_dirs=[tmp_path])
        try:
            with pytest.raises(BusClaimedError):
                claim_bus(_CHANNEL, lock_dirs=[tmp_path, fallback])
        finally:
            first.release()
        assert not lock_path(_CHANNEL, fallback).exists()

    def test_どの置き場所も開けなければOSErrorで上げる(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(OSError):
            claim_bus(_CHANNEL, lock_dirs=[tmp_path / "missing", tmp_path / "missing2"])
