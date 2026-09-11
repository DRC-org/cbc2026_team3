from __future__ import annotations

import pathlib
import signal
import socket
import subprocess
import sys
import time

import pytest

_PROJECT_DIR = pathlib.Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.slow

_STARTUP_TIMEOUT_S = 30.0
_SHUTDOWN_TIMEOUT_S = 15.0

_STOP_LOG = "SIGTERM を受信しました"
_DONE_LOG = "後始末完了"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _spawn_dry_run(port: int) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-u",
            "main.py",
            "--dry-run",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=_PROJECT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait_until_listening(proc: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + _STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"起動前に終了しました (exit={proc.returncode})")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    pytest.fail("起動待ちがタイムアウトしました")


def _read_until(proc: subprocess.Popen[str], needle: str, collected: list[str]) -> bool:
    assert proc.stdout is not None
    while True:
        line = proc.stdout.readline()
        if not line:
            return False
        collected.append(line)
        if needle in line:
            return True


def _collect_rest(proc: subprocess.Popen[str], collected: list[str]) -> tuple[int, str]:
    try:
        rest, _ = proc.communicate(timeout=_SHUTDOWN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        rest, _ = proc.communicate()
        collected.append(rest or "")
        pytest.fail(f"後始末が {_SHUTDOWN_TIMEOUT_S} 秒で終わりませんでした:\n{''.join(collected)}")
    collected.append(rest or "")
    return int(proc.returncode), "".join(collected)


def test_sigterm_runs_full_shutdown() -> None:
    port = _free_port()
    proc = _spawn_dry_run(port)
    collected: list[str] = []
    try:
        _wait_until_listening(proc, port)
        proc.send_signal(signal.SIGTERM)
        returncode, output = _collect_rest(proc, collected)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    # 既定の SIGTERM 処理で死ぬと returncode は -15。0 は自前のハンドラを通った証拠。
    assert returncode == 0, f"SIGTERM で異常終了しました (exit={returncode}):\n{output}"
    assert _STOP_LOG in output, output
    assert _DONE_LOG in output, output


def test_second_sigterm_during_shutdown_does_not_kill_cleanup() -> None:
    port = _free_port()
    proc = _spawn_dry_run(port)
    collected: list[str] = []
    try:
        _wait_until_listening(proc, port)
        proc.send_signal(signal.SIGTERM)
        # POSIX シグナルはキューされないので、待たずに 2 通目を送ると 1 通目と合体して消える。
        assert _read_until(proc, _STOP_LOG, collected), (
            f"後始末の開始ログが出ないまま終了しました:\n{''.join(collected)}"
        )
        proc.send_signal(signal.SIGTERM)
        returncode, output = _collect_rest(proc, collected)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert returncode == 0, f"2 通目の SIGTERM で異常終了しました (exit={returncode}):\n{output}"
    assert _DONE_LOG in output, output
