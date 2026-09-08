from __future__ import annotations

import asyncio

import can
import pytest

from lib.drivers.base import ControlMode
from lib.manual import ManualController
from lib.match_state import Phase
from lib.sequence.engine import Sequence, step
from lib.sequence.motors import MotorGroup, MotorHandle
from lib.sequence.positions import load_position_table
from tests.fake_can import mock_can_manager
from tests.fake_drivers import StubFeedbackDriver
from tests.server_fixtures import DEFAULT_CHECKLIST, RecordingClient, ServerFixture

_ROBOT = "main_hand"

_POSITIONS = {
    "axes": {
        "y_axis": {
            "unit": "mm",
            "command_unit": "deg",
            "manual": {"min": -2.0, "max": 20.0, "steps": [0.5, 2.0]},
            "motors": {"y_axis_r": {"scale": 55.0}, "y_axis_l": {"scale": -55.0}},
        },
        "gripper": {"unit": "deg", "command_unit": "deg"},
        "conveyor": {
            "unit": "duty",
            "command_unit": "duty",
            "command_mode": "duty",
            "manual_always": True,
        },
    },
    "positions": {
        "y_axis": {"home": 0.0, "work": {"red": 5.0, "blue": 9.0}},
        "gripper": {"open": 5.0, "closed": 0.0},
        "conveyor": {"run": 0.6, "stop": 0.0},
    },
}


class _RecordingDriver(StubFeedbackDriver):
    def __init__(self, name: str) -> None:
        super().__init__(name, 1)
        self.commands: list[tuple[ControlMode, float]] = []

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        self.commands.append((mode, value))
        return super().encode_target(mode, value)


class _SlowSequence(Sequence):
    def __init__(self, name: str = _ROBOT) -> None:
        super().__init__(name)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.advanced = False

    @step("解放されるまで待つ")
    async def hold(self) -> None:
        self.entered.set()
        await self.release.wait()

    @step("次のステップ")
    async def advance(self) -> None:
        self.advanced = True


class _GatedCheckSequence(Sequence):
    def __init__(self) -> None:
        super().__init__("motor_check")
        self.gate = asyncio.Event()

    @step("ゲート待ち")
    async def hold(self) -> None:
        await self.gate.wait()


def _make_manual() -> tuple[ManualController, dict[str, _RecordingDriver]]:
    table = load_position_table(_POSITIONS, source="<test>")
    mgr = mock_can_manager()
    group = MotorGroup()
    drivers: dict[str, _RecordingDriver] = {}
    for name in ("y_axis_r", "y_axis_l", "gripper", "conveyor"):
        driver = _RecordingDriver(name)
        drivers[name] = driver
        group.add(MotorHandle(name, driver, mgr))
    return ManualController(group, table), drivers


def _fixture(
    sequence: Sequence | None = None,
    *,
    with_manual: bool = True,
    checklist: bool = False,
) -> tuple[ServerFixture, dict[str, _RecordingDriver]]:
    fx = ServerFixture.build(checklist_definitions=DEFAULT_CHECKLIST if checklist else None)
    fx.freeze_broadcast()
    manual, drivers = _make_manual()
    fx.add_robot(
        _ROBOT,
        sequence if sequence is not None else _SlowSequence(),
        manual=manual if with_manual else None,
    )
    return fx, drivers


async def _switch(fx: ServerFixture, mode: str) -> None:
    await fx.command({"type": "set_operation_mode", "robot": _ROBOT, "mode": mode})


class TestModeSwitch:
    async def test_既定は半自動シーケンス制御(self) -> None:
        fx, _ = _fixture()
        assert fx.operation_mode(_ROBOT) == "sequence"

    async def test_手動へ切り替えられる(self) -> None:
        fx, _ = _fixture()
        await _switch(fx, "manual")
        assert fx.operation_mode(_ROBOT) == "manual"

    async def test_半自動へ戻せる(self) -> None:
        fx, _ = _fixture()
        await _switch(fx, "manual")
        await _switch(fx, "sequence")
        assert fx.operation_mode(_ROBOT) == "sequence"

    async def test_未知のモードは理由付きで拒否する(self) -> None:
        fx, _ = _fixture()
        client = RecordingClient()
        fx.attach_clients(client)
        await fx.command(
            {"type": "set_operation_mode", "robot": _ROBOT, "mode": "全自動"}, requester=client
        )
        assert fx.operation_mode(_ROBOT) == "sequence"
        assert client.of_type("command_rejected")[-1]["reason"].startswith("未知の操作モード")

    async def test_手動を持たないロボットは切り替えを拒否する(self) -> None:
        fx, _ = _fixture(with_manual=False)
        client = RecordingClient()
        fx.attach_clients(client)
        await _switch_as(fx, "manual", client)
        assert fx.operation_mode(_ROBOT) == "sequence"
        assert "手動操縦に対応していません" in client.of_type("command_rejected")[-1]["reason"]

    async def test_モードはロボットごとに独立する(self) -> None:
        fx, _ = _fixture()
        manual2, _ = _make_manual()
        fx.add_robot("sub_hand", _SlowSequence("sub_hand"), manual=manual2)
        await _switch(fx, "manual")
        assert fx.operation_mode(_ROBOT) == "manual"
        assert fx.operation_mode("sub_hand") == "sequence"


class TestPhaseIndependence:
    @pytest.mark.parametrize("phase", [p.value for p in Phase])
    async def test_どのフェーズでも手動へ入れる(self, phase: str) -> None:
        fx, _ = _fixture(checklist=True)
        await _advance_to(fx, phase)
        assert fx.match.phase.value == phase

        await _switch(fx, "manual")
        assert fx.operation_mode(_ROBOT) == "manual"

    @pytest.mark.parametrize("phase", [p.value for p in Phase])
    async def test_どのフェーズでも手動指令が通る(self, phase: str) -> None:
        fx, drivers = _fixture(checklist=True)
        await _advance_to(fx, phase)

        await _switch(fx, "manual")
        await fx.command(
            {"type": "manual_move", "robot": _ROBOT, "axis": "gripper", "position": "open"}
        )
        assert drivers["gripper"].commands == [(ControlMode.POSITION, 5.0)]


class TestControlOwnership:
    async def test_手動へ入るとシーケンスを止める(self) -> None:
        seq = _SlowSequence()
        fx, _ = _fixture(seq)
        fx.enter_match()
        task = asyncio.create_task(seq.run_forever())
        await fx.command({"type": "sequence_start", "robot": _ROBOT})
        await asyncio.wait_for(seq.entered.wait(), timeout=1.0)
        assert seq.is_running

        await _switch(fx, "manual")
        seq.release.set()
        assert await _wait(lambda: not seq.is_running)
        assert not seq.advanced, "手動へ切り替えた後にシーケンスが次のステップへ進んだ"
        task.cancel()

    async def test_手動へ入る直前の開始要求を破棄する(self) -> None:
        seq = _SlowSequence()
        fx, _ = _fixture(seq)
        fx.enter_match()
        task = asyncio.create_task(seq.run_forever())

        await fx.command({"type": "sequence_start", "robot": _ROBOT})
        await _switch(fx, "manual")
        for _ in range(20):
            await asyncio.sleep(0)
        assert not seq.is_running
        task.cancel()

    async def test_手動モード中は動作確認を起動できない(self) -> None:
        fx, _ = _fixture()
        await _switch(fx, "manual")
        assert await fx.start_motor_check() is False

    async def test_半自動へ戻してもシーケンスは自動再開しない(self) -> None:
        seq = _SlowSequence()
        fx, _ = _fixture(seq)
        fx.enter_match()
        task = asyncio.create_task(seq.run_forever())
        await _switch(fx, "manual")
        await _switch(fx, "sequence")
        for _ in range(20):
            await asyncio.sleep(0)
        assert not seq.is_running
        task.cancel()

    async def test_手動モード中はsequence_startを拒否する(self) -> None:
        seq = _SlowSequence()
        fx, _ = _fixture(seq)
        fx.enter_match()
        await _switch(fx, "manual")
        client = RecordingClient()
        fx.attach_clients(client)

        await fx.command({"type": "sequence_start", "robot": _ROBOT}, requester=client)

        assert not seq.is_running
        assert "手動操縦中" in client.of_type("command_rejected")[-1]["reason"]

    async def test_手動モード中はsequence_jumpを拒否する(self) -> None:
        seq = _SlowSequence()
        fx, _ = _fixture(seq)
        fx.enter_match()
        await _switch(fx, "manual")
        client = RecordingClient()
        fx.attach_clients(client)

        await fx.command(
            {"type": "sequence_jump", "robot": _ROBOT, "step_index": 1}, requester=client
        )

        assert seq._jump_request is None
        assert "手動操縦中" in client.of_type("command_rejected")[-1]["reason"]

    async def test_手動モード中はtriggerを拒否する(self) -> None:
        seq = _SlowSequence()
        fx, _ = _fixture(seq)
        fx.enter_match()
        await _switch(fx, "manual")
        client = RecordingClient()
        fx.attach_clients(client)

        await fx.command({"type": "trigger", "robot": _ROBOT}, requester=client)

        assert "手動操縦中" in client.of_type("command_rejected")[-1]["reason"]

    async def test_半自動モード中はsequence_startが通る(self) -> None:
        seq = _SlowSequence()
        fx, _ = _fixture(seq)
        fx.enter_match()
        task = asyncio.create_task(seq.run_forever())

        await fx.command({"type": "sequence_start", "robot": _ROBOT})
        await asyncio.wait_for(seq.entered.wait(), timeout=1.0)

        assert seq.is_running
        seq.release.set()
        task.cancel()


class TestManualCommandGate:
    async def test_半自動運転中の手動指令は拒否する(self) -> None:
        fx, drivers = _fixture()
        client = RecordingClient()
        fx.attach_clients(client)
        await fx.command(
            {"type": "manual_move", "robot": _ROBOT, "axis": "gripper", "position": "open"},
            requester=client,
        )
        assert drivers["gripper"].commands == []
        assert "手動操縦モードではありません" in client.of_type("command_rejected")[-1]["reason"]

    @pytest.mark.parametrize(
        "payload",
        [
            {"type": "manual_move", "axis": "gripper", "position": "open"},
            {"type": "manual_set", "axis": "y_axis", "value": 3.0},
            {"type": "manual_jog", "axis": "y_axis", "delta": 1.0},
        ],
    )
    async def test_緊急停止中はどの手動指令も通らない(self, payload: dict) -> None:
        fx, drivers = _fixture()
        await _switch(fx, "manual")
        await fx.activate_e_stop()
        client = RecordingClient()
        fx.attach_clients(client)

        await fx.command({**payload, "robot": _ROBOT}, requester=client)
        assert all(driver.commands == [] for driver in drivers.values())
        assert "緊急停止中" in client.of_type("command_rejected")[-1]["reason"]

    async def test_緊急停止中でもモード切替はできる(self) -> None:
        fx, _ = _fixture()
        await fx.activate_e_stop()
        await _switch(fx, "manual")
        assert fx.operation_mode(_ROBOT) == "manual"

    async def test_軸未指定は理由付きで拒否する(self) -> None:
        fx, _ = _fixture()
        await _switch(fx, "manual")
        client = RecordingClient()
        fx.attach_clients(client)
        await fx.command({"type": "manual_jog", "robot": _ROBOT, "delta": 1.0}, requester=client)
        assert "軸が指定されていません" in client.of_type("command_rejected")[-1]["reason"]

    @pytest.mark.parametrize("value", [None, "3.0", True, float("nan"), float("inf")])
    async def test_数値でない指令値は拒否する(self, value: object) -> None:
        fx, drivers = _fixture()
        await _switch(fx, "manual")
        client = RecordingClient()
        fx.attach_clients(client)
        await fx.command(
            {"type": "manual_set", "robot": _ROBOT, "axis": "y_axis", "value": value},
            requester=client,
        )
        assert drivers["y_axis_r"].commands == []
        assert client.of_type("command_rejected")

    async def test_連続操作できない軸への絶対値指定は理由付きで拒否する(self) -> None:
        fx, drivers = _fixture()
        await _switch(fx, "manual")
        client = RecordingClient()
        fx.attach_clients(client)
        await fx.command(
            {"type": "manual_set", "robot": _ROBOT, "axis": "gripper", "value": 2.5},
            requester=client,
        )
        assert drivers["gripper"].commands == []
        assert "連続操作の対象外" in client.of_type("command_rejected")[-1]["reason"]

    async def test_位置名の誤りで_WS_が切れない(self) -> None:
        fx, _ = _fixture()
        await _switch(fx, "manual")
        client = RecordingClient()
        fx.attach_clients(client)
        await fx.command(
            {"type": "manual_move", "robot": _ROBOT, "axis": "gripper", "position": "半開き"},
            requester=client,
        )
        assert fx.is_connected(client)
        assert client.of_type("command_rejected")


class TestManualAlwaysAxisGate:
    async def test_シーケンス制御中でも対象軸のプリセット指令は通る(self) -> None:
        fx, drivers = _fixture()
        assert fx.operation_mode(_ROBOT) == "sequence"
        client = RecordingClient()
        fx.attach_clients(client)

        await fx.command(
            {"type": "manual_move", "robot": _ROBOT, "axis": "conveyor", "position": "stop"},
            requester=client,
        )

        assert drivers["conveyor"].commands == [(ControlMode.DUTY, pytest.approx(0.0))]
        assert client.of_type("command_rejected") == []

    async def test_シーケンス制御中は非対象軸のプリセット指令を拒否する(self) -> None:
        fx, drivers = _fixture()
        client = RecordingClient()
        fx.attach_clients(client)

        await fx.command(
            {"type": "manual_move", "robot": _ROBOT, "axis": "gripper", "position": "open"},
            requester=client,
        )

        assert drivers["gripper"].commands == []
        assert "手動操縦モードではありません" in client.of_type("command_rejected")[-1]["reason"]

    async def test_未定義の軸はモードではなく軸の理由で拒否する(self) -> None:
        fx, _ = _fixture()
        client = RecordingClient()
        fx.attach_clients(client)

        await fx.command(
            {"type": "manual_move", "robot": _ROBOT, "axis": "no_such_axis", "position": "stop"},
            requester=client,
        )

        reason = client.of_type("command_rejected")[-1]["reason"]
        assert "no_such_axis" in reason
        assert "手動操縦モードではありません" not in reason

    async def test_緊急停止中は対象軸でも拒否される(self) -> None:
        fx, drivers = _fixture()
        await fx.activate_e_stop()
        client = RecordingClient()
        fx.attach_clients(client)

        await fx.command(
            {"type": "manual_move", "robot": _ROBOT, "axis": "conveyor", "position": "run"},
            requester=client,
        )

        assert drivers["conveyor"].commands == []
        assert "緊急停止中" in client.of_type("command_rejected")[-1]["reason"]

    async def test_動作確認の実行中は対象軸でも拒否される(self) -> None:
        fx, drivers = _fixture()
        check = _GatedCheckSequence()
        fx.set_motor_check_sequence(check)
        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_running()
        client = RecordingClient()
        fx.attach_clients(client)

        await fx.command(
            {"type": "manual_move", "robot": _ROBOT, "axis": "conveyor", "position": "run"},
            requester=client,
        )

        assert drivers["conveyor"].commands == []
        assert "動作確認" in client.of_type("command_rejected")[-1]["reason"]

        check.gate.set()
        await fx.wait_motor_check_idle()

    async def test_動作確認が終われば対象軸は通る(self) -> None:
        fx, drivers = _fixture()
        check = _GatedCheckSequence()
        fx.set_motor_check_sequence(check)
        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_running()
        check.gate.set()
        await fx.wait_motor_check_idle()

        await fx.command(
            {"type": "manual_move", "robot": _ROBOT, "axis": "conveyor", "position": "run"}
        )

        assert drivers["conveyor"].commands == [(ControlMode.DUTY, pytest.approx(0.6))]

    @pytest.mark.parametrize(
        "payload",
        [
            {"type": "manual_set", "axis": "conveyor", "value": 0.5},
            {"type": "manual_jog", "axis": "conveyor", "delta": 0.1},
        ],
    )
    async def test_対象軸でも連続値の指令は通らない(self, payload: dict) -> None:
        fx, drivers = _fixture()
        client = RecordingClient()
        fx.attach_clients(client)

        await fx.command({**payload, "robot": _ROBOT}, requester=client)

        assert drivers["conveyor"].commands == []
        assert "連続操作の対象外" in client.of_type("command_rejected")[-1]["reason"]

    async def test_手動モードでの非対象軸は今までどおり通る(self) -> None:
        fx, drivers = _fixture()
        await _switch(fx, "manual")

        await fx.command(
            {"type": "manual_move", "robot": _ROBOT, "axis": "gripper", "position": "open"}
        )

        assert drivers["gripper"].commands == [(ControlMode.POSITION, pytest.approx(5.0))]


class TestManualCommandEffect:
    async def test_絶対値指定が単位換算されて左右へ届く(self) -> None:
        fx, drivers = _fixture()
        await _switch(fx, "manual")
        await fx.command({"type": "manual_set", "robot": _ROBOT, "axis": "y_axis", "value": 4.0})
        assert drivers["y_axis_r"].commands == [(ControlMode.POSITION, pytest.approx(4.0 * 55.0))]
        assert drivers["y_axis_l"].commands == [(ControlMode.POSITION, pytest.approx(-4.0 * 55.0))]

    async def test_可動範囲を超える指定はクランプされる(self) -> None:
        fx, drivers = _fixture()
        await _switch(fx, "manual")
        await fx.command({"type": "manual_set", "robot": _ROBOT, "axis": "y_axis", "value": 500.0})
        assert drivers["y_axis_r"].commands == [(ControlMode.POSITION, pytest.approx(20.0 * 55.0))]

    async def test_プリセットは現在のコートで解決される(self) -> None:
        fx, drivers = _fixture()
        await fx.command({"type": "set_court", "court": "blue"})
        await _switch(fx, "manual")
        await fx.command(
            {"type": "manual_move", "robot": _ROBOT, "axis": "y_axis", "position": "work"}
        )
        assert drivers["y_axis_r"].commands == [(ControlMode.POSITION, pytest.approx(9.0 * 55.0))]


class TestMatchReset:
    async def test_セッティングタイムへの復帰で半自動へ戻す(self) -> None:
        fx, _ = _fixture()
        await _switch(fx, "manual")
        await fx.command({"type": "match_reset"})
        assert fx.operation_mode(_ROBOT) == "sequence"


class TestStateBroadcast:
    async def test_操作モードと軸一覧が配信される(self) -> None:
        fx, _ = _fixture()
        state = fx.state_message(_ROBOT)
        assert state["manual"]["mode"] == "sequence"
        names = [axis["name"] for axis in state["manual"]["axes"]]
        assert names == ["y_axis", "gripper", "conveyor"]

    async def test_可動範囲は連続操作できる軸だけに載る(self) -> None:
        fx, _ = _fixture()
        axes = {a["name"]: a for a in fx.state_message(_ROBOT)["manual"]["axes"]}
        assert axes["y_axis"]["manual"] == {"min": -2.0, "max": 20.0, "steps": [0.5, 2.0]}
        assert axes["gripper"]["manual"] is None

    async def test_手動を持たないロボットでも配信は成立する(self) -> None:
        fx, _ = _fixture(with_manual=False)
        assert fx.state_message(_ROBOT)["manual"] == {"mode": "sequence", "axes": []}


class TestEStopClearsJogOrigin:
    async def test_緊急停止を挟むとジョグ起点を取り直す(self) -> None:
        fx, drivers = _fixture()
        await _switch(fx, "manual")
        await fx.command({"type": "manual_set", "robot": _ROBOT, "axis": "y_axis", "value": 15.0})
        await fx.activate_e_stop()
        drivers["y_axis_r"].set_observed(position=2.0 * 55.0)
        drivers["y_axis_l"].set_observed(position=-2.0 * 55.0)
        await fx.command({"type": "e_stop_release"})
        await fx.wait_reactivation()

        drivers["y_axis_r"].commands.clear()
        await fx.command({"type": "manual_jog", "robot": _ROBOT, "axis": "y_axis", "delta": 1.0})
        assert drivers["y_axis_r"].commands == [(ControlMode.POSITION, pytest.approx(3.0 * 55.0))]


async def _advance_to(fx: ServerFixture, phase: str) -> None:
    if phase == "setup":
        return
    fx.complete_all_checklists()
    if phase == "ready":
        return
    await fx.command({"type": "match_start"})
    if phase == "match":
        return
    await fx.command({"type": "match_finish"})


async def _switch_as(fx: ServerFixture, mode: str, client: RecordingClient) -> None:
    await fx.command(
        {"type": "set_operation_mode", "robot": _ROBOT, "mode": mode}, requester=client
    )


async def _wait(predicate, *, timeout: float = 1.0) -> bool:
    from tests.server_fixtures import wait_until

    return await wait_until(predicate, timeout=timeout)
