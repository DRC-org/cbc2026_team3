"""`scripts/can_watchdog.sh` の滞留判定と復旧を、実機の CAN 無しで検証する。

`ip` と `tc` を PATH のスタブへ差し替えて動かす。ウォッチドッグは
`ip link set <if> down` / `up` 以外の副作用を持たないので、**スタブが記録した
`ip` の呼び出し列がそのまま「何をしたか」**になる。判定そのものはスクリプト側の
本物のロジックを通るため、条件を 1 つ落とせばここが落ちる。

ここでスタブにするのはカーネルとの境界(`ip` / `tc` / `sudo`)だけで、
スクリプトの内部には手を伸ばさない。
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
_WATCHDOG = _PROJECT_ROOT / "scripts" / "can_watchdog.sh"

# 実際に config/can_buses.yaml に居るバス。スタブでは「このバスだけが挿さっている」
# 状況を作り、残りは欠けているものとして扱わせる。
_IFACE = "can_dm3520"

_IP_STUB = r"""#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
iface="${@: -1}"
case "$*" in
  "link show "*)
      [[ " $STUB_IFACES " == *" $iface "* ]] && exit 0 || exit 1 ;;
  "-s link show "*)
      [[ " $STUB_IFACES " == *" $iface "* ]] || exit 1
      f="$STUB_DIR/tx_$iface"
      tx=$(cat "$f" 2>/dev/null || echo 0)
      if [[ "${STUB_TX_GROWS:-0}" == "1" ]]; then echo $(( tx + 1 )) > "$f"; fi
      echo "9: $iface: <NOARP,UP> mtu 16"
      echo "    RX:  bytes packets errors dropped  missed   mcast"
      echo "             0       0      0       0       0       0"
      echo "    TX:  bytes packets errors dropped carrier collsns"
      echo "             0       $tx      0       0       0       0"
      exit 0 ;;
esac
exit 0
"""

_TC_STUB = r"""#!/usr/bin/env bash
echo "qdisc pfifo_fast 0: root refcnt 2 bands 3"
echo " Sent 0 bytes 0 pkt (dropped 0, overlimits 0 requeues 0)"
echo " backlog ${STUB_BACKLOG_BYTES:-0}b ${STUB_BACKLOG_PKTS:-0}p requeues 0"
"""

_SUDO_STUB = """#!/usr/bin/env bash
exec "$@"
"""


def _run(
    tmp_path: pathlib.Path,
    *,
    backlog_pkts: int,
    tx_grows: bool = False,
    ifaces: str = _IFACE,
    stall_ticks: int = 2,
    max_ticks: int = 8,
    min_recover_interval: int = 5,
) -> list[str]:
    """ウォッチドッグを有限周期だけ回し、`ip` に届いた呼び出し列を返す。"""
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    for name, body in (("ip", _IP_STUB), ("tc", _TC_STUB), ("sudo", _SUDO_STUB)):
        path = stub_dir / name
        path.write_text(body)
        path.chmod(0o755)

    log = tmp_path / "ip.log"
    log.touch()

    env = {
        "PATH": f"{stub_dir}:/usr/bin:/bin",
        "STUB_LOG": str(log),
        "STUB_DIR": str(tmp_path),
        "STUB_IFACES": ifaces,
        "STUB_BACKLOG_PKTS": str(backlog_pkts),
        "STUB_BACKLOG_BYTES": str(backlog_pkts * 16),
        "STUB_TX_GROWS": "1" if tx_grows else "0",
    }
    subprocess.run(
        [
            str(_WATCHDOG),
            "--interval",
            "0.01",
            "--stall-ticks",
            str(stall_ticks),
            "--max-ticks",
            str(max_ticks),
            "--min-recover-interval",
            str(min_recover_interval),
        ],
        env=env,
        check=True,
        capture_output=True,
        timeout=60,
    )
    return log.read_text().splitlines()


def _recoveries(calls: list[str]) -> int:
    """復旧の回数。`down` の数で数える (`up` は必ず対で続く)。"""
    return sum(1 for c in calls if c == f"link set {_IFACE} down")


class TestStallDetection:
    """滞留の判定には backlog と tx_packets の両方が要る。"""

    def test_idle_bus_is_never_recovered(self, tmp_path: pathlib.Path) -> None:
        # 送るものが無いだけの平常時。tx_packets は動かないが backlog も 0
        calls = _run(tmp_path, backlog_pkts=0)

        assert _recoveries(calls) == 0

    def test_stalled_bus_is_recovered(self, tmp_path: pathlib.Path) -> None:
        # キューに残っているのに tx_packets が 1 つも進まない = bus-off の症状
        calls = _run(tmp_path, backlog_pkts=12)

        assert _recoveries(calls) >= 1
        # down の直後には必ず up が来る。down で終わるとバスが落ちたまま残る
        idx = calls.index(f"link set {_IFACE} down")
        assert calls[idx + 1] == f"link set {_IFACE} up"

    def test_busy_bus_making_progress_is_not_recovered(self, tmp_path: pathlib.Path) -> None:
        # backlog は乗っているが tx_packets が毎周期進んでいる = ただの連続送信。
        # backlog だけで判定していると、ここで正常なバスを落としてしまう
        calls = _run(tmp_path, backlog_pkts=12, tx_grows=True)

        assert _recoveries(calls) == 0

    def test_missing_device_is_skipped(self, tmp_path: pathlib.Path) -> None:
        # 挿さっていないバスは down/up できない。滞留の条件は揃って見えても触らない
        calls = _run(tmp_path, backlog_pkts=12, ifaces="")

        assert _recoveries(calls) == 0

    def test_recovery_waits_for_the_configured_number_of_ticks(
        self, tmp_path: pathlib.Path
    ) -> None:
        # 1 周期の滞留で飛びつかない。stall_ticks を満たすまでは触らない
        calls = _run(tmp_path, backlog_pkts=12, stall_ticks=99, max_ticks=5)

        assert _recoveries(calls) == 0


class TestRecoveryRateLimit:
    """復旧しても直らないバスで down/up を回し続けない。"""

    def test_repeated_stall_recovers_only_once_within_the_interval(
        self, tmp_path: pathlib.Path
    ) -> None:
        # 相手が最初から居ないバス。滞留は解消しないので、制限が無ければ
        # 何度でも復旧に入る
        calls = _run(tmp_path, backlog_pkts=12, max_ticks=30, min_recover_interval=60)

        assert _recoveries(calls) == 1

    def test_without_the_interval_it_recovers_repeatedly(self, tmp_path: pathlib.Path) -> None:
        # 上の 1 回が「レート制限のおかげ」であることを示す対照。制限を外すと
        # 同じ条件で複数回入る
        calls = _run(tmp_path, backlog_pkts=12, max_ticks=30, min_recover_interval=0)

        assert _recoveries(calls) > 1


class TestBusList:
    """監視対象は can_config.py が単一情報源。スクリプトに名前を書き写さない。"""

    def test_bus_names_are_not_hardcoded_in_the_script(self) -> None:
        source = _WATCHDOG.read_text()
        # 説明のための言及(コメント)は許すが、コードでバスを名指ししない
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))

        assert "can_dm3520" not in code
        assert "can_config.py" in source


@pytest.mark.parametrize("flag", ["--interval", "--stall-ticks", "--max-ticks"])
def test_unknown_and_incomplete_arguments_are_rejected(tmp_path: pathlib.Path, flag: str) -> None:
    """値を伴わないオプションは黙って既定値へ落ちず、起動を拒否する。"""
    result = subprocess.run(
        [str(_WATCHDOG), flag],
        capture_output=True,
        timeout=30,
    )

    assert result.returncode != 0
