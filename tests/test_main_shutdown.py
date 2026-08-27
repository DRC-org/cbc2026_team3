"""SIGTERM (systemd の停止シグナル) で main.py の後始末が完走することを検証する。

``systemctl stop`` / ``restart`` が送るのは SIGTERM で、既定の扱いはプロセスの即死。
そのままだと ``main()`` の ``finally`` に並べた後始末 —— 位置制御ループ停止 →
目標値再送停止 → 同期監視停止 → CAN shutdown → サーバー終了処理 —— が 1 段も走らない。
「止める処理が止まる形を作らない」という不変条件は、サービス化した時点で
シグナルの扱いに依存するようになったので、ここで固定する。

シグナルの扱いはプロセス全体の状態で、``asyncio.run`` の外側から観測するしかない。
そのため実プロセスを ``--dry-run`` (python-can の virtual バス) で起動し、外から
シグナルを送って終了コードとログで確かめる。

2 通目を「後始末の開始ログを見てから」送っているのは、POSIX シグナルがキューされない
ため。連続で送ると 2 通目は 1 通目と合体して消え、2 通目の扱いを一切検証しないテストに
なる (実際にそれで書いて、ハンドラを外す変異を素通しにした)。
"""

from __future__ import annotations

import pathlib
import signal
import socket
import subprocess
import sys
import time

import pytest

_PROJECT_DIR = pathlib.Path(__file__).resolve().parent.parent

# dry-run の起動 (config 検証 + virtual バス + シーケンス登録) に許す時間
_STARTUP_TIMEOUT_S = 30.0
# SIGTERM を受けてから後始末を終えるまでに許す時間。実測は 1 秒未満で、
# cbc-control.service の TimeoutStopSec=10 と同じ桁に合わせてある
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
    """HTTP を受け付けるまで待つ。起動途中に停止させると経路が変わるため。"""
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
    """needle を含む行が出るまで読み進める。プロセスが終了 (EOF) すれば False。"""
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
    """SIGTERM 1 通で後始末が最後まで走り、正常終了する。"""
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

    # 既定の SIGTERM で死ぬと -15 になる。0 は自前のハンドラを通った証拠
    assert returncode == 0, f"SIGTERM で異常終了しました (exit={returncode}):\n{output}"
    assert _STOP_LOG in output, output
    # 後始末の最終行。ここまで到達していれば全段が実行されている
    assert _DONE_LOG in output, output


def test_second_sigterm_during_shutdown_does_not_kill_cleanup() -> None:
    """後始末の最中に届いた 2 通目の SIGTERM が後始末を打ち切らない。

    ``systemctl restart`` の連打で起きる。``remove_signal_handler`` でハンドラを
    外すと SIGTERM の扱いが SIG_DFL へ戻るため、2 通目は「無視」ではなく「即死」に
    なり、CAN を開いたままプロセスが消える。
    """
    port = _free_port()
    proc = _spawn_dry_run(port)
    collected: list[str] = []
    try:
        _wait_until_listening(proc, port)
        proc.send_signal(signal.SIGTERM)
        # 1 通目が処理された (= ハンドラが走った) ことを見届けてから 2 通目を送る。
        # ここを待たずに送ると 2 通目は合体して消え、何も検証しないテストになる
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
