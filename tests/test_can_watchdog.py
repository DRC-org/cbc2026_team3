from __future__ import annotations

import pathlib
import subprocess

import pytest

pytestmark = pytest.mark.slow

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
_WATCHDOG = _PROJECT_ROOT / "scripts" / "can_watchdog.sh"

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
    return sum(1 for c in calls if c == f"link set {_IFACE} down")


class TestStallDetection:
    def test_idle_bus_is_never_recovered(self, tmp_path: pathlib.Path) -> None:
        calls = _run(tmp_path, backlog_pkts=0)

        assert _recoveries(calls) == 0

    def test_stalled_bus_is_recovered(self, tmp_path: pathlib.Path) -> None:
        calls = _run(tmp_path, backlog_pkts=12)

        assert _recoveries(calls) >= 1
        idx = calls.index(f"link set {_IFACE} down")
        assert calls[idx + 1] == f"link set {_IFACE} up"

    def test_busy_bus_making_progress_is_not_recovered(self, tmp_path: pathlib.Path) -> None:
        calls = _run(tmp_path, backlog_pkts=12, tx_grows=True)

        assert _recoveries(calls) == 0

    def test_missing_device_is_skipped(self, tmp_path: pathlib.Path) -> None:
        calls = _run(tmp_path, backlog_pkts=12, ifaces="")

        assert _recoveries(calls) == 0

    def test_recovery_waits_for_the_configured_number_of_ticks(
        self, tmp_path: pathlib.Path
    ) -> None:
        calls = _run(tmp_path, backlog_pkts=12, stall_ticks=99, max_ticks=5)

        assert _recoveries(calls) == 0


class TestRecoveryRateLimit:
    def test_repeated_stall_recovers_only_once_within_the_interval(
        self, tmp_path: pathlib.Path
    ) -> None:
        calls = _run(tmp_path, backlog_pkts=12, max_ticks=30, min_recover_interval=60)

        assert _recoveries(calls) == 1

    def test_without_the_interval_it_recovers_repeatedly(self, tmp_path: pathlib.Path) -> None:
        calls = _run(tmp_path, backlog_pkts=12, max_ticks=30, min_recover_interval=0)

        assert _recoveries(calls) > 1


class TestBusList:
    def test_bus_names_are_not_hardcoded_in_the_script(self) -> None:
        source = _WATCHDOG.read_text()
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))

        assert "can_dm3520" not in code
        assert "can_config.py" in source


@pytest.mark.parametrize("flag", ["--interval", "--stall-ticks", "--max-ticks"])
def test_unknown_and_incomplete_arguments_are_rejected(tmp_path: pathlib.Path, flag: str) -> None:
    result = subprocess.run(
        [str(_WATCHDOG), flag],
        capture_output=True,
        timeout=30,
    )

    assert result.returncode != 0
