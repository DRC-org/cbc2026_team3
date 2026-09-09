"""零点合わせの単独実行 (`homing_start` / `HomingController`)。

動作確認の通し実行から零点確定だけを切り離して走らせる経路。**宛先のロボットを
取り違えないことと、動作確認と同時に走らないことが要**。
"""

from __future__ import annotations

import asyncio

from lib.drivers.generic import GenericDriver
from lib.manual import ManualController
from lib.match_state import Court
from lib.sequence.engine import Sequence, step
from lib.sequence.homing import HomingError
from lib.sequence.motors import AxisHandle, MotorGroup, MotorHandle, build_axis_state_reader
from lib.sequence.positions import AxisSpec, PositionTable, load_position_table
from lib.server_homing import HomingSource
from tests.fake_can import mock_can_manager
from tests.feedback_frames import feed_generic
from tests.server_fixtures import RecordingClient, ServerFixture

_CONFIG = {
    "axes": {
        "y_axis": {
            "unit": "mm",
            "command_unit": "mm",
            "homing": {"sensor": "y_switch", "direction": 1, "search_distance": 5.0, "step": 1.0},
        },
        "gripper": {"unit": "deg", "command_unit": "deg"},
        "sub_y_axis": {
            "unit": "mm",
            "command_unit": "mm",
            "homing": {
                "sensor": "sub_switch",
                "direction": -1,
                "search_distance": 5.0,
                "step": 1.0,
            },
        },
    },
    "positions": {"y_axis": {"home": 0.0}, "gripper": {"open": 5.0}, "sub_y_axis": {"home": 0.0}},
}

_MAIN_AXES = ("y_axis", "gripper")
_SUB_AXES = ("sub_y_axis",)


class _IdleSequence(Sequence):
    @step("何もしない")
    async def noop(self) -> None:
        return


class _RecordingRunner:
    """`HomingRunner` の代役。どの軸が実際に零点確定を通ったかだけを見る。"""

    def __init__(self, *, fails: dict[str, str] | None = None) -> None:
        self.homed: list[str] = []
        self._fails = fails or {}
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def home(self, spec: AxisSpec, _handle: object) -> float:
        self.homed.append(spec.name)
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        message = self._fails.get(spec.name)
        if message is not None:
            raise HomingError(message)
        return 0.0


def _table() -> PositionTable:
    return load_position_table(_CONFIG, source="<test>")


def _motors() -> MotorGroup:
    mgr = mock_can_manager()
    group = MotorGroup(sensor_active=lambda _name: False)
    for index, name in enumerate((*_MAIN_AXES, *_SUB_AXES), start=1):
        group.add(MotorHandle(name, GenericDriver(name, can_id=index), mgr))
    return group


def _build(
    *,
    runner: _RecordingRunner | None = None,
    robots: tuple[str, ...] = ("main_hand", "sub_hand"),
    manual: bool = False,
) -> tuple[ServerFixture, _RecordingRunner]:
    fx = ServerFixture.build()
    fx.freeze_broadcast()
    for name in robots:
        controller = ManualController(_motors(), _table()) if manual else None
        fx.add_robot(name, _IdleSequence(name), manual=controller)

    runner = runner or _RecordingRunner()
    fx.set_homing_source(
        HomingSource(
            runner=runner,  # type: ignore[arg-type]
            table=_table(),
            motors=_motors(),
            court=lambda: Court.RED,
            axes_by_robot={"main_hand": ("y_axis",), "sub_hand": ("sub_y_axis",)},
        )
    )
    return fx, runner


class TestTargetsOneRobot:
    async def test_軸を指定するとその軸だけが零点確定を通る(self) -> None:
        fx, runner = _build()

        assert await fx.start_homing("sub_hand", ["sub_y_axis"]) is None
        await fx.wait_homing_idle()

        assert runner.homed == ["sub_y_axis"]

    async def test_軸を省くとそのロボットの軸だけを回す(self) -> None:
        fx, runner = _build()

        assert await fx.start_homing("main_hand") is None
        await fx.wait_homing_idle()

        assert runner.homed == ["y_axis"]

    async def test_他のロボットの軸を指定しても動かさない(self) -> None:
        fx, runner = _build()

        reason = await fx.start_homing("sub_hand", ["y_axis"])

        assert reason is not None
        assert "y_axis" in reason and "sub_hand" in reason
        assert runner.homed == []

    async def test_ロボットを省いたら拒む(self) -> None:
        fx, runner = _build()

        reason = await fx.start_homing(None, None)  # type: ignore[arg-type]

        assert reason is not None
        assert runner.homed == []

    async def test_零点確定を持たないロボットは拒む(self) -> None:
        fx, runner = _build(robots=("main_hand", "sub_hand", "third_hand"))

        reason = await fx.start_homing("third_hand")

        assert reason is not None
        assert runner.homed == []


class TestDenyGate:
    async def test_緊急停止中は拒む(self) -> None:
        fx, runner = _build()
        await fx.activate_e_stop(reason="テスト")

        reason = await fx.start_homing("sub_hand")

        assert reason is not None and "緊急停止" in reason
        assert runner.homed == []
        assert "緊急停止" in (fx.homing_state()["blocked_reason"] or "")

    async def test_動作確認の実行中は拒む(self) -> None:
        fx, runner = _build()
        pending: asyncio.Task[None] = asyncio.create_task(asyncio.Event().wait())  # type: ignore[arg-type]
        fx.set_motor_check_task(pending)
        try:
            reason = await fx.start_homing("sub_hand")
        finally:
            pending.cancel()

        assert reason is not None and "動作確認" in reason
        assert runner.homed == []

    async def test_零点合わせの実行中は動作確認を拒む(self) -> None:
        fx, _runner = _build()
        fx.set_motor_check_sequence(_IdleSequence("motor_check"))
        fx.set_homing_running(True)

        started = await fx.start_motor_check()

        assert started is False
        assert "零点合わせ" in (fx.motor_check_error() or "")

    async def test_手動操縦モードのロボットが居たら拒む(self) -> None:
        """整列段が原点センサを歯止めから外している間、その軸を手動で動かせないことの半分。

        覆いは歯止めが読む口すべてに掛かるので、手動操縦の入口 (`AxisHandle`) が
        見る歯止めも同時に緩む。**緩んだ歯止めが露出しないのは、この排他があるから**。
        """
        fx, runner = _build(manual=True)
        await fx.command({"type": "set_operation_mode", "robot": "main_hand", "mode": "manual"})

        reason = await fx.start_homing("sub_hand")

        assert reason is not None and "手動操縦モード" in reason
        assert runner.homed == []

    async def test_実行中は手動操縦へ切り替えられない(self) -> None:
        """もう半分。走り出した後から制御権を奪う経路も塞がっていないと意味が無い。"""
        fx, _runner = _build(manual=True)
        fx.set_homing_running(True)
        client = RecordingClient()
        fx.attach_clients(client)

        await fx.command(
            {"type": "set_operation_mode", "robot": "main_hand", "mode": "manual"},
            requester=client,
        )

        assert fx.operation_mode("main_hand") == "sequence"
        assert "零点合わせ" in client.of_type("command_rejected")[-1]["reason"]

    async def test_実行中の重ね掛けを拒む(self) -> None:
        fx, runner = _build()
        runner.release = asyncio.Event()

        assert await fx.start_homing("sub_hand") is None
        await asyncio.wait_for(runner.entered.wait(), timeout=2.0)
        reason = await fx.start_homing("main_hand")
        runner.release.set()
        await fx.wait_homing_idle()

        assert reason is not None and "実行中" in reason
        assert runner.homed == ["sub_y_axis"]


class TestBroadcast:
    async def test_失敗した軸の理由が配信に載る(self) -> None:
        fx, _runner = _build(runner=_RecordingRunner(fails={"sub_y_axis": "センサに届きません"}))
        client = RecordingClient()
        fx.attach_clients(client)

        await fx.start_homing("sub_hand")
        await fx.wait_homing_idle()

        results = client.of_type("homing_state")[-1]["results"]
        assert results == [{"axis": "sub_y_axis", "error": "センサに届きません"}]

    async def test_実行中の軸が配信に載る(self) -> None:
        fx, runner = _build()
        runner.release = asyncio.Event()
        client = RecordingClient()
        fx.attach_clients(client)

        await fx.start_homing("sub_hand")
        await asyncio.wait_for(runner.entered.wait(), timeout=2.0)
        running = client.of_type("homing_state")[-1]
        runner.release.set()
        await fx.wait_homing_idle()

        assert running["running"] is True
        assert running["current_axis"] == "sub_y_axis"
        assert running["robot"] == "sub_hand"

    async def test_ロボットごとの対象軸を配る(self) -> None:
        fx, _runner = _build()

        assert fx.homing_state()["targets"] == {
            "main_hand": ["y_axis"],
            "sub_hand": ["sub_y_axis"],
        }


class TestCommand:
    async def test_コマンドから走らせられる(self) -> None:
        fx, runner = _build()

        await fx.command({"type": "homing_start", "robot": "sub_hand"})
        await fx.wait_homing_idle()

        assert runner.homed == ["sub_y_axis"]

    async def test_拒否は_command_rejected_で返る(self) -> None:
        fx, runner = _build()
        client = RecordingClient()

        await fx.command(
            {"type": "homing_start", "robot": "main_hand", "axes": ["sub_y_axis"]}, requester=client
        )

        rejected = client.of_type("command_rejected")
        assert rejected and rejected[-1]["command"] == "homing_start"
        assert runner.homed == []

    async def test_試合中は拒む(self) -> None:
        fx, runner = _build()
        fx.enter_match()
        client = RecordingClient()

        await fx.command({"type": "homing_start", "robot": "sub_hand"}, requester=client)

        assert "試合中" in client.of_type("command_rejected")[-1]["reason"]
        assert runner.homed == []


_INTERFERING_CONFIG = {
    "axes": {
        "sub_lift": {
            "unit": "mm",
            "command_unit": "mm",
            "tolerance": 1.0,
            "homing": {
                "sensor": "lift_switch",
                "direction": 1,
                "search_distance": 5.0,
                "step": 1.0,
            },
        },
        "sub_y_axis": {
            "unit": "mm",
            "command_unit": "mm",
            "tolerance": 1.0,
            "homing": {
                "sensor": "sub_switch",
                "direction": -1,
                "search_distance": 5.0,
                "step": 1.0,
            },
            "guard": {"requires": [{"axis": "sub_lift", "at": "top"}]},
        },
    },
    "positions": {"sub_lift": {"top": -20.0}, "sub_y_axis": {"home": 0.0}},
}


class _SteppingRunner:
    """探索の 1 歩を実際に打つ代役。**指令の入口 (= 歯止め) を必ず通る。**"""

    def __init__(self) -> None:
        self.homed: list[str] = []

    async def home(self, spec: AxisSpec, handle: AxisHandle) -> float:
        homing = spec.homing
        assert homing is not None
        await handle.set_target_value(spec.to_commands(homing.direction * homing.step))
        self.homed.append(spec.name)
        return homing.step


class TestPanelDoesNotMoveOtherAxes:
    """零点合わせパネルは**選んだ軸しか動かさない**。寄せるのは操縦者の仕事。

    自動で寄せると「1 本だけ確定するつもりが別の軸が動いた」が起き、`§3` の
    「零点確定は選んだ軸しか動かさない」を崩す。代わりに**拒否の 1 行に手当てを
    書く** —— 会場ではそれが手順書になる。
    """

    @staticmethod
    def _build(*, lift_mm: float) -> tuple[ServerFixture, _SteppingRunner]:
        table = load_position_table(_INTERFERING_CONFIG, source="<test>")
        mgr = mock_can_manager()
        group = MotorGroup(sensor_active=lambda _name: False)
        for index, (name, value) in enumerate(
            (("sub_lift", lift_mm), ("sub_y_axis", 0.0)), start=1
        ):
            driver = GenericDriver(name, can_id=index)
            feed_generic(driver, position=value)
            group.add(MotorHandle(name, driver, mgr))
        group.bind_axis_state(
            build_axis_state_reader(
                table, group, court=lambda: Court.RED, is_stale=lambda _name: False
            )
        )

        fx = ServerFixture.build()
        fx.freeze_broadcast()
        fx.add_robot("sub_hand", _IdleSequence("sub_hand"))
        runner = _SteppingRunner()
        fx.set_homing_source(
            HomingSource(
                runner=runner,  # type: ignore[arg-type]
                table=table,
                motors=group,
                court=lambda: Court.RED,
                axes_by_robot={"sub_hand": ("sub_lift", "sub_y_axis")},
            )
        )
        return fx, runner

    async def test_寄せていなければ手当ての入った理由が配信に載る(self) -> None:
        fx, _runner = self._build(lift_mm=-10.0)
        client = RecordingClient()
        fx.attach_clients(client)

        await fx.start_homing("sub_hand", ["sub_y_axis"])
        await fx.wait_homing_idle()

        (result,) = client.of_type("homing_state")[-1]["results"]
        assert result["axis"] == "sub_y_axis"
        reason = result["error"]
        assert "sub_lift" in reason
        assert "top" in reason
        assert "寄せてください" in reason

    async def test_選んだ軸以外を勝手に動かさない(self) -> None:
        fx, runner = self._build(lift_mm=-10.0)

        await fx.start_homing("sub_hand", ["sub_y_axis"])
        await fx.wait_homing_idle()

        assert runner.homed == []

    async def test_寄せてあれば通る(self) -> None:
        fx, runner = self._build(lift_mm=-20.0)

        await fx.start_homing("sub_hand", ["sub_y_axis"])
        await fx.wait_homing_idle()

        assert runner.homed == ["sub_y_axis"]

    async def test_まとめて回しても寄せないので後続は拒否される(self) -> None:
        """**参照先を先に回すが、寄せはしない。**

        零点確定は離脱して終わるので、確定しただけの `sub_lift` は `top` に
        居ない。パネルはそこから寄せないので、`sub_y_axis` は手当ての入った理由と
        ともに残る (寄せてから確定する手順を組むのは動作確認の側)。
        """
        fx, runner = self._build(lift_mm=-20.0)
        client = RecordingClient()
        fx.attach_clients(client)

        await fx.start_homing("sub_hand")
        await fx.wait_homing_idle()

        assert runner.homed == ["sub_lift"]
        results = client.of_type("homing_state")[-1]["results"]
        assert [r["axis"] for r in results] == ["sub_lift", "sub_y_axis"]
        assert results[0]["error"] is None
        assert "寄せてください" in results[1]["error"]
