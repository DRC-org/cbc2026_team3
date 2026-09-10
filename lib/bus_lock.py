"""CAN バスの持ち主を OS のファイルロックで 1 プロセスに限る。

`flock` はプロセスが死ねば (異常終了・電源断でも) カーネルが必ず外すので、
残骸の判定を PID の生存確認やファイルの有無に頼らない。ロックファイルは削除しない
—— 消すと、古い inode を開いたまま待つ相手と新しい inode を掴んだ相手が同時に
「持ち主」になれる。中の PID とコマンドラインは、拒否された側が相手を名指しする
ためだけの情報で、判定には使わない。
"""

from __future__ import annotations

import fcntl
import os
import pathlib
import sys
import tempfile

_RUN_LOCK_DIR = pathlib.Path("/run/lock")


def default_lock_dir() -> pathlib.Path:
    # /run/lock は tmpfs で再起動ごとに消える。無い環境だけ /tmp へ落とす
    if _RUN_LOCK_DIR.is_dir():
        return _RUN_LOCK_DIR
    return pathlib.Path(tempfile.gettempdir())


def lock_path(channel: str, *, lock_dir: pathlib.Path | None = None) -> pathlib.Path:
    return (lock_dir or default_lock_dir()) / f"cbc-can-{channel}.lock"


class BusClaimedError(RuntimeError):
    """別の生きているプロセスが同じ CAN バスを掴んでいる。"""

    def __init__(self, channel: str, holder: str) -> None:
        self.channel = channel
        self.holder = holder
        super().__init__(
            f"CAN バス '{channel}' は別のプロセスが掴んでいます ({holder})。"
            " 同じバスへ 2 つのプロセスから指令すると互いの目標を上書きし合い、"
            "機構の故障に見えます (かたかた鳴る・トルクが上限まで立つ・零点確定が進まない)。"
            " その相手を止めてから起動してください"
            " (kill <PID> / systemctl stop cbc-control。--dry-run なら掴みません)"
        )


class BusClaim:
    """掴んだバス 1 本。`release()` で手放す。手放し忘れてもプロセス終了で外れる。"""

    def __init__(self, channel: str, path: pathlib.Path, fd: int) -> None:
        self.channel = channel
        self.path = path
        self._fd: int | None = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def claim_bus(channel: str, *, lock_dir: pathlib.Path | None = None) -> BusClaim:
    """`channel` の持ち主を主張する。既に生きている持ち主が居れば `BusClaimedError`。

    Raises:
        BusClaimedError: 他のプロセスが掴んでいる (文面に相手の PID とコマンドライン)
        OSError: ロックファイルを開けない (権限など)
    """
    path = lock_path(channel, lock_dir=lock_dir)
    # 別ユーザーで起動した相手とも同じファイルで競うために全員へ書き込みを許す
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = _describe_holder(fd)
        os.close(fd)
        raise BusClaimedError(channel, holder) from None
    except BaseException:
        os.close(fd)
        raise

    try:
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n{_own_cmdline()}\n".encode(), 0)
    except OSError:
        # 名乗れなくても持ち主であることは変わらない (拒否された側は PID 不明と出る)
        pass
    return BusClaim(channel, path, fd)


def _own_cmdline() -> str:
    return " ".join([sys.executable, *sys.argv])


def _describe_holder(fd: int) -> str:
    try:
        lines = os.pread(fd, 4096, 0).decode(errors="replace").splitlines()
    except OSError:
        lines = []
    if not lines or not lines[0].strip().isdigit():
        return "PID 不明: ロックを掴んだ直後で名乗る前か、名乗れなかった"
    pid = int(lines[0])
    cmdline = _proc_cmdline(pid) or (lines[1] if len(lines) > 1 else "")
    return f"PID {pid}: {cmdline or 'コマンドライン不明'}"


def _proc_cmdline(pid: int) -> str | None:
    try:
        raw = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return " ".join(part.decode(errors="replace") for part in raw.split(b"\0") if part) or None
