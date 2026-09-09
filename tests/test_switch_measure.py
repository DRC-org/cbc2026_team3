"""リミットスイッチの作動点測定 (`switch_measure_start` / `HomingRunner.measure`)。

零点確定と同じ二段探索を通しながら**原点を書き込まない**経路。要は 4 つ ——
指定した向きの端を測ること、上限より先へ動かないこと、零点合わせ・動作確認と
同時に走らないこと、そして **SET_ZERO を 1 通も出さないこと**。
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from lib.drivers.base import MotorState
from lib.drivers.edulite05 import Edulite05Driver
from lib.match_state import Court
from lib.sequence.engine import Sequence, step
from lib.sequence.homing import HomingError, SwitchMeasurement, measure_switch, run_homing
from lib.sequence.motors import MotorGroup, MotorHandle
from lib.sequence.positions import AxisSpec, PositionTable, load_position_table
from lib.server_homing import HomingSource
from tests.fake_can import mock_can_manager
from tests.server_fixtures import RecordingClient, ServerFixture

# 零点確定の検証と**同じ模型**で測る。センサの模し方を書き写すと、片方だけが
# 実機の挙動へ追随した状態が作れる
from tests.test_homing import _axis_commands, _handle, _Recorder, _runner, _table


class TestMeasuresBothEdges:
    async def test_作動点と離脱点と_ON_区間を返す(self) -> None:
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_band=(-6.0, -3.0))

        result = await _runner(rec).measure(spec, _handle(spec, rec), direction=-1)

        assert result == SwitchMeasurement(
            axis="y_axis",
            unit="mm",
            direction=-1.0,
            engage=-3.0,
            release=-2.0,
            width=1.0,
            step=1.0,
            coarse_step=None,
        )

    async def test_homing_と逆の端も測れる(self) -> None:
        # `homing.direction` は -1。測りたいのは homing が使わない側の端でもある
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_band=(3.0, 6.0))

        result = await _runner(rec).measure(spec, _handle(spec, rec), direction=1)

        assert (result.direction, result.engage, result.release) == (1.0, 3.0, 2.0)
        assert min(_axis_commands(rec, "y_axis_r")) >= 0.0

    async def test_刻みを指定するとその刻みで測り結果に載る(self) -> None:
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_band=(-6.0, -3.5))

        result = await _runner(rec).measure(spec, _handle(spec, rec), direction=-1, step=0.5)

        assert result.step == 0.5
        assert result.engage == -3.5


class TestDistanceLimit:
    async def test_上限を超えて動かさない(self) -> None:
        table = _table(direction=-1, step=1.0, search_distance=30.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=-20.0)

        with pytest.raises(HomingError, match=r"2\.0mm"):
            await _runner(rec).measure(spec, _handle(spec, rec), direction=-1, limit=2.0)

        # 上限の判定は指令の前にしかないので最後の 1 歩ぶんは超えうる
        assert min(_axis_commands(rec, "y_axis_r")) >= -3.0

    async def test_上限を省くと_search_distance_で止まる(self) -> None:
        table = _table(direction=-1, step=1.0, search_distance=4.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=-20.0)

        with pytest.raises(HomingError, match=r"4\.0mm"):
            await _runner(rec).measure(spec, _handle(spec, rec), direction=-1)

        assert min(_axis_commands(rec, "y_axis_r")) >= -5.0

    async def test_刻みが上限を超える指定は動かす前に拒む(self) -> None:
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=-3.0)

        with pytest.raises(HomingError, match="測定条件"):
            await _runner(rec).measure(spec, _handle(spec, rec), direction=-1, step=5.0, limit=2.0)

        assert rec.commands == []


class TestDoesNotWriteOrigin:
    async def test_原点確定を呼ばない(self) -> None:
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_band=(-6.0, -3.0))

        await _runner(rec).measure(spec, _handle(spec, rec), direction=-1)

        assert rec.origins == []

    async def test_原点を確定できない軸でも測れる(self) -> None:
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_band=(-6.0, -3.0), capturable=False)

        result = await _runner(rec).measure(spec, _handle(spec, rec), direction=-1)

        assert result.engage == -3.0


class _ZeroingDriver(Edulite05Driver):
    """フレームは実物のまま、フィードバックだけ差し込める代役。"""

    def set_observed(self, position: float) -> None:
        self._state = MotorState(position=position)


def _frame_table() -> PositionTable:
    return load_position_table(
        {
            "axes": {
                "y_axis": {
                    "unit": "mm",
                    "command_unit": "rad",
                    "tolerance": 0.1,
                    "homing": {
                        "sensor": "y_switch",
                        "direction": -1,
                        "search_distance": 10.0,
                        "step": 1.0,
                        "settle_s": 0.0,
                    },
                    "motors": {"y_axis_r": {"scale": 1.0}},
                }
            },
            "positions": {"y_axis": {"home": 0.0}},
        },
        source="<test>",
    )


def _last_target(message: object) -> float:
    param, value = struct.unpack("<Hxxf", bytes(message.data))  # type: ignore[attr-defined]
    return value if param == Edulite05Driver.PARAM_LOC_REF else 0.0


class _FrameRecorder:
    """CAN へ出た全フレームを順に持つ。"""

    def __init__(self) -> None:
        self.sent: list[object] = []

    def comm_types(self) -> list[int]:
        return [Edulite05Driver.parse_can_id(msg.arbitration_id)[0] for msg in self.sent]  # type: ignore[attr-defined]


async def _run_frame_rig(*, home: bool) -> _FrameRecorder:
    spec = _frame_table().axis("y_axis")
    rec = _Recorder(active_at_or_below=-3.0)
    frames = _FrameRecorder()

    driver = _ZeroingDriver("y_axis_r", can_id=5)
    driver.set_observed(0.0)
    mgr = mock_can_manager({"y_axis_r": MotorState(position=0.0)})

    async def _send(_name: str, message: object) -> None:
        frames.sent.append(message)
        target = _last_target(message)
        if target:
            driver.set_observed(target)

    mgr.send.side_effect = _send

    group = MotorGroup(sensor_active=rec.sensor_active)
    group.add(MotorHandle("y_axis_r", driver, mgr))
    rec.drivers = {"y_axis_r": driver}
    rec.spec = spec

    async def _capture_origin(_axis: str) -> None:
        await mgr.send("y_axis_r", driver.encode_set_zero())

    runner = _runner(rec)
    runner._capture_origin = _capture_origin  # type: ignore[assignment]

    table = _frame_table()
    if home:
        await run_homing(runner, table, group, court=Court.RED, axes=["y_axis"])
    else:
        await measure_switch(runner, table, group, court=Court.RED, axis="y_axis", direction=-1)
    return frames


class TestNoSetZeroFrame:
    async def test_測定では_SET_ZERO_を出さない(self) -> None:
        frames = await _run_frame_rig(home=False)

        assert frames.sent, "1 通も送っていない (機構が動いていない)"
        assert Edulite05Driver.COMM_TYPE_SET_ZERO not in frames.comm_types()

    async def test_零点合わせなら_SET_ZERO_が出る(self) -> None:
        # 上の否定が「送る経路そのものが無い」ことの言い換えになっていないかを固定する
        frames = await _run_frame_rig(home=True)

        assert Edulite05Driver.COMM_TYPE_SET_ZERO in frames.comm_types()


_CONFIG = {
    "axes": {
        "y_axis": {
            "unit": "mm",
            "command_unit": "mm",
            "homing": {"sensor": "y_switch", "direction": 1, "search_distance": 5.0, "step": 1.0},
        },
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
    "positions": {"y_axis": {"home": 0.0}, "sub_y_axis": {"home": 0.0}},
}


class _IdleSequence(Sequence):
    @step("何もしない")
    async def noop(self) -> None:
        return


class _RecordingRunner:
    """`HomingRunner` の代役。どの軸をどちらへ測りに行ったかだけを見る。"""

    def __init__(self) -> None:
        self.measured: list[tuple[str, float]] = []
        self.homed: list[str] = []
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def home(self, spec: AxisSpec, _handle: object) -> float:
        self.homed.append(spec.name)
        return 0.0

    async def measure(
        self,
        spec: AxisSpec,
        _handle: object,
        *,
        direction: float,
        step: float | None = None,
        coarse_step: float | None = None,
        limit: float | None = None,
    ) -> SwitchMeasurement:
        self.measured.append((spec.name, direction))
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        return SwitchMeasurement(
            axis=spec.name,
            unit=spec.unit,
            direction=direction,
            engage=-447.0,
            release=-441.2,
            width=5.8,
            step=step if step is not None else 1.0,
            coarse_step=coarse_step,
        )


def _motors() -> MotorGroup:
    mgr = mock_can_manager()
    group = MotorGroup(sensor_active=lambda _name: False)
    for index, name in enumerate(("y_axis", "sub_y_axis"), start=1):
        group.add(MotorHandle(name, Edulite05Driver(name, can_id=index), mgr))
    return group


def _build(
    *, robots: tuple[str, ...] = ("main_hand", "sub_hand")
) -> tuple[ServerFixture, _RecordingRunner]:
    fx = ServerFixture.build()
    fx.freeze_broadcast()
    for name in robots:
        fx.add_robot(name, _IdleSequence(name))

    runner = _RecordingRunner()
    fx.set_homing_source(
        HomingSource(
            runner=runner,  # type: ignore[arg-type]
            table=load_position_table(_CONFIG, source="<test>"),
            motors=_motors(),
            court=lambda: Court.RED,
            axes_by_robot={"main_hand": ("y_axis",), "sub_hand": ("sub_y_axis",)},
        )
    )
    return fx, runner


def _payload(**overrides: object) -> dict:
    payload = {"robot": "sub_hand", "axis": "sub_y_axis", "direction": -1}
    payload.update(overrides)
    return payload


class TestTargetsOneAxisOfOneRobot:
    async def test_指定した軸を指定した向きへ測る(self) -> None:
        fx, runner = _build()

        assert await fx.start_switch_measure(_payload()) is None
        await fx.wait_switch_measure_idle()

        assert runner.measured == [("sub_y_axis", -1.0)]

    async def test_他のロボットの軸を指定しても動かさない(self) -> None:
        fx, runner = _build()

        reason = await fx.start_switch_measure(_payload(robot="main_hand"))

        assert reason is not None and "sub_y_axis" in reason and "main_hand" in reason
        assert runner.measured == []

    async def test_ロボットを省いたら拒む(self) -> None:
        fx, runner = _build()

        reason = await fx.start_switch_measure({"axis": "sub_y_axis", "direction": -1})

        assert reason is not None
        assert runner.measured == []

    async def test_軸を省いたら拒む(self) -> None:
        fx, runner = _build()

        reason = await fx.start_switch_measure({"robot": "sub_hand", "direction": -1})

        assert reason is not None
        assert runner.measured == []

    async def test_向きを省いたら拒む(self) -> None:
        fx, runner = _build()

        reason = await fx.start_switch_measure({"robot": "sub_hand", "axis": "sub_y_axis"})

        assert reason is not None and "向き" in reason
        assert runner.measured == []

    @pytest.mark.parametrize("bad", [0, 2, -0.5, "left", True])
    async def test_向きが_プラスマイナス1_でなければ拒む(self, bad: object) -> None:
        fx, runner = _build()

        assert await fx.start_switch_measure(_payload(direction=bad)) is not None
        assert runner.measured == []

    @pytest.mark.parametrize("key", ["step", "coarse_step", "limit"])
    async def test_刻みと上限は正の数でなければ拒む(self, key: str) -> None:
        fx, runner = _build()

        assert await fx.start_switch_measure(_payload(**{key: 0})) is not None
        assert await fx.start_switch_measure(_payload(**{key: "たくさん"})) is not None
        assert runner.measured == []


class TestDenyGate:
    async def test_緊急停止中は拒む(self) -> None:
        fx, runner = _build()
        await fx.activate_e_stop(reason="テスト")

        reason = await fx.start_switch_measure(_payload())

        assert reason is not None and "緊急停止" in reason
        assert runner.measured == []
        assert "緊急停止" in (fx.switch_measure_state()["blocked_reason"] or "")

    async def test_零点合わせの実行中は拒む(self) -> None:
        fx, runner = _build()
        fx.set_homing_running(True)

        reason = await fx.start_switch_measure(_payload())

        assert reason is not None and "零点合わせ" in reason
        assert runner.measured == []

    async def test_動作確認の実行中は拒む(self) -> None:
        fx, runner = _build()
        pending: asyncio.Task[None] = asyncio.create_task(asyncio.Event().wait())  # type: ignore[arg-type]
        fx.set_motor_check_task(pending)
        try:
            reason = await fx.start_switch_measure(_payload())
        finally:
            pending.cancel()

        assert reason is not None and "動作確認" in reason
        assert runner.measured == []

    async def test_測定中は零点合わせを拒む(self) -> None:
        fx, _runner = _build()
        fx.set_switch_measure_running(True)

        reason = await fx.start_homing("sub_hand")

        assert reason is not None and "作動点測定" in reason

    async def test_測定中は動作確認を拒む(self) -> None:
        fx, _runner = _build()
        fx.set_motor_check_sequence(_IdleSequence("motor_check"))
        fx.set_switch_measure_running(True)

        started = await fx.start_motor_check()

        assert started is False
        assert "作動点測定" in (fx.motor_check_error() or "")

    async def test_実行中の重ね掛けを拒む(self) -> None:
        fx, runner = _build()
        runner.release = asyncio.Event()

        assert await fx.start_switch_measure(_payload()) is None
        await asyncio.wait_for(runner.entered.wait(), timeout=2.0)
        reason = await fx.start_switch_measure(_payload(robot="main_hand", axis="y_axis"))
        runner.release.set()
        await fx.wait_switch_measure_idle()

        assert reason is not None and "実行中" in reason
        assert runner.measured == [("sub_y_axis", -1.0)]


class TestBroadcast:
    async def test_結果が配信に載る(self) -> None:
        fx, _runner = _build()
        client = RecordingClient()
        fx.attach_clients(client)

        await fx.start_switch_measure(_payload(step=0.5))
        await fx.wait_switch_measure_idle()

        state = client.of_type("switch_measure_state")[-1]
        assert state["result"] == {
            "axis": "sub_y_axis",
            "unit": "mm",
            "direction": -1.0,
            "engage": -447.0,
            "release": -441.2,
            "width": 5.8,
            "step": 0.5,
            "coarse_step": None,
        }
        assert state["running"] is False

    async def test_ロボットごとの対象軸を配る(self) -> None:
        fx, _runner = _build()

        assert fx.switch_measure_state()["targets"] == {
            "main_hand": ["y_axis"],
            "sub_hand": ["sub_y_axis"],
        }


class TestCommand:
    async def test_コマンドから走らせられる(self) -> None:
        fx, runner = _build()

        await fx.command({"type": "switch_measure_start", **_payload()})
        await fx.wait_switch_measure_idle()

        assert runner.measured == [("sub_y_axis", -1.0)]

    async def test_拒否は_command_rejected_で返る(self) -> None:
        fx, runner = _build()
        client = RecordingClient()

        await fx.command(
            {"type": "switch_measure_start", **_payload(robot="main_hand")}, requester=client
        )

        rejected = client.of_type("command_rejected")
        assert rejected and rejected[-1]["command"] == "switch_measure_start"
        assert runner.measured == []

    async def test_試合中は拒む(self) -> None:
        fx, runner = _build()
        fx.enter_match()
        client = RecordingClient()

        await fx.command({"type": "switch_measure_start", **_payload()}, requester=client)

        assert client.of_type("command_rejected")
        assert runner.measured == []
