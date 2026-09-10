"""零点合わせの単独実行 (`homing_start` / `HomingController`)。

動作確認の通し実行から零点確定だけを切り離して走らせる経路。**宛先のロボットを
取り違えないことと、動作確認と同時に走らないことが要**。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import can
import pytest

from lib.drivers.base import ControlMode, MotorDriver
from lib.drivers.generic import GenericDriver
from lib.manual import ManualController
from lib.match_state import Court
from lib.sequence.engine import Sequence, step
from lib.sequence.homing import HomingError
from lib.sequence.motors import AxisHandle, MotorGroup, MotorHandle, build_axis_state_reader
from lib.sequence.positions import AxisSpec, PositionTable, load_position_table
from lib.server_homing import HomingSource
from tests.fake_can import mock_can_manager
from tests.fake_drivers import StubFeedbackDriver
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


class _FollowingDriver(StubFeedbackDriver):
    """指令どおりに動く機体。寄せる段が本当に軸を動かしたかを実測で見る。"""

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        if mode is ControlMode.POSITION:
            self.set_observed(position=value)
        return super().encode_target(mode, value)


class _SteppingRunner:
    """探索の 1 歩を実際に打つ代役。**指令の入口 (= 歯止め) を必ず通る。**

    通った軸は原点を書いたことにする —— 確定していない軸は寄せる段が信用しない。
    """

    def __init__(
        self,
        events: list[tuple[str, object]],
        drivers: Mapping[str, MotorDriver],
        *,
        fails: set[str] | None = None,
    ) -> None:
        self._events = events
        self._drivers = drivers
        self._fails = fails or set()

    async def home(self, spec: AxisSpec, handle: AxisHandle) -> float:
        if spec.name in self._fails:
            raise HomingError(f"軸 '{spec.name}' はセンサに届きません (テスト)")
        homing = spec.homing
        assert homing is not None
        await handle.set_target_value(spec.to_commands(homing.direction * homing.step))
        for name in spec.motor_names:
            self._drivers[name].mark_origin_confirmed()
        self._events.append(("home", spec.name))
        return homing.step


class _Panel:
    """零点合わせパネル 1 台ぶんの組み立て。**寄せる口は動作確認と同じ `move_to`。**"""

    def __init__(
        self, *, lift_mm: float, wire_move_to: bool, fails: set[str] | None = None
    ) -> None:
        self.events: list[tuple[str, object]] = []
        table = load_position_table(_INTERFERING_CONFIG, source="<test>")
        mgr = mock_can_manager()
        group = MotorGroup(sensor_active=lambda _name: False)
        self.drivers: dict[str, _FollowingDriver] = {}
        for index, (name, value) in enumerate(
            (("sub_lift", lift_mm), ("sub_y_axis", 0.0)), start=1
        ):
            driver = _FollowingDriver(name, index)
            driver.set_observed(position=value)
            self.drivers[name] = driver
            group.add(MotorHandle(name, driver, mgr, poll_interval=0.001))
        group.bind_axis_state(
            build_axis_state_reader(
                table, group, court=lambda: Court.RED, is_stale=lambda _name: False
            )
        )

        mover = Sequence("panel")
        mover.bind_motors(group)
        mover.bind_positions(table)

        async def _move_to(targets: Mapping[str, str]) -> None:
            self.events.append(("move", dict(targets)))
            await mover.move_to(targets)

        self.fx = ServerFixture.build()
        self.fx.freeze_broadcast()
        self.fx.add_robot("sub_hand", _IdleSequence("sub_hand"))
        self.fx.set_homing_source(
            HomingSource(
                runner=_SteppingRunner(self.events, self.drivers, fails=fails),  # type: ignore[arg-type]
                table=table,
                motors=group,
                court=lambda: Court.RED,
                axes_by_robot={"sub_hand": ("sub_lift", "sub_y_axis")},
                move_to=_move_to if wire_move_to else None,
            )
        )

    async def run(self, axes: list[str] | None = None) -> list[dict]:
        await self.fx.start_homing("sub_hand", axes)
        await self.fx.wait_homing_idle()
        return self.fx.homing_state()["results"]


class TestPanelOrdersWhatWasSelected:
    """パネルは**選ばれた軸の範囲でだけ**順序を組む。

    昇降と前後の両方を選べば「昇降を確定 → `top` へ寄せる → 前後を確定」で回る。
    前後だけを選んだときに昇降を寄せると「零点確定は選んだ軸しか動かさない」が
    壊れるので、そちらは寄せずに拒否して文面で案内する。**この非対称が仕様である。**
    """

    async def test_両方選べば寄せてから確定する(self) -> None:
        panel = _Panel(lift_mm=-10.0, wire_move_to=True)

        results = await panel.run()

        assert panel.events == [
            ("home", "sub_lift"),
            ("move", {"sub_lift": "top"}),
            ("home", "sub_y_axis"),
        ]
        assert [r["error"] for r in results] == [None, None]
        assert panel.drivers["sub_lift"].state.position == pytest.approx(-20.0)

    async def test_前後だけを選んだら寄せずに拒否する(self) -> None:
        panel = _Panel(lift_mm=-10.0, wire_move_to=True)
        # 昇降は前に確定してある (未確定なら拒否の理由が「零点確定が先」に変わる)
        panel.drivers["sub_lift"].mark_origin_confirmed()

        (result,) = await panel.run(["sub_y_axis"])

        assert panel.events == []
        assert result["axis"] == "sub_y_axis"
        assert "sub_lift" in result["error"]
        assert "top" in result["error"]
        assert "寄せてください" in result["error"]
        # 選んでいない軸は 1mm も動かさない
        assert panel.drivers["sub_lift"].state.position == pytest.approx(-10.0)

    async def test_寄せる口を配線しなければ寄せない(self) -> None:
        """既定は「寄せない」。配線し忘れは黙って通らず、拒否として表に出る。"""
        panel = _Panel(lift_mm=-10.0, wire_move_to=False)

        results = await panel.run()

        assert panel.events == [("home", "sub_lift")]
        assert results[0]["error"] is None
        assert "寄せてください" in results[1]["error"]

    async def test_昇降の零点確定に失敗したら寄せず_前後は動かさずに理由を返す(self) -> None:
        """原点が確定していない軸へ位置名で指令すると、どこへ動くか分からない。"""
        panel = _Panel(lift_mm=-10.0, wire_move_to=True, fails={"sub_lift"})

        results = await panel.run()

        assert panel.events == []
        assert "センサに届きません" in results[0]["error"]
        assert "sub_lift" in results[1]["error"]
        assert "零点確定" in results[1]["error"]
        assert panel.drivers["sub_lift"].state.position == pytest.approx(-10.0)
        assert panel.drivers["sub_y_axis"].state.position == pytest.approx(0.0)

    async def test_前後だけでも昇降が確定済みで条件を満たしていれば通る(self) -> None:
        panel = _Panel(lift_mm=-20.0, wire_move_to=True)
        panel.drivers["sub_lift"].mark_origin_confirmed()

        (result,) = await panel.run(["sub_y_axis"])

        assert result["error"] is None
        assert panel.events == [("home", "sub_y_axis")]

    async def test_昇降が未確定なら前後だけでは通らない(self) -> None:
        """未確定の `sub_lift` が偶然 `top` を読んでも、機構がそこに居るとは限らない。"""
        panel = _Panel(lift_mm=-20.0, wire_move_to=True)

        (result,) = await panel.run(["sub_y_axis"])

        assert panel.events == []
        assert "sub_lift" in result["error"]
        assert "零点が確定していません" in result["error"]
        assert panel.drivers["sub_y_axis"].state.position == pytest.approx(0.0)
