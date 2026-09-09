"""コート未確定のあいだ、コート依存軸への指令を止める (lib/server.py のコートゲート)。

`sub_lift` の scale はコートで符号が反転するので、選び忘れたまま動かすと昇降が
逆へ走る。ゲートは 2 枚あり、**それぞれ単独で効くことを別々に確かめる**:

1. コマンドゲート (`CommandSpec.blocked_without_court`) —— 操縦者へ理由文を返す
2. 指令の入口 (`CourtUnresolvedError` / `PositionLookupError`) —— 1 枚目を外しても止まる

**ゲートはロボット単位。** コートに依存しない台は退避路として動かせる必要があるので
塞がない (`docs/invariants.md` §4)。
"""

from __future__ import annotations

import can
import pytest

from lib.drivers.base import ControlMode
from lib.manual import ManualController
from lib.sequence.engine import Sequence, step
from lib.sequence.motors import MotorGroup, MotorHandle
from lib.sequence.positions import load_position_table
from tests.fake_can import mock_can_manager
from tests.fake_drivers import StubFeedbackDriver
from tests.server_fixtures import RecordingClient, ServerFixture

_COURT_ROBOT = "sub_hand"
_PLAIN_ROBOT = "main_hand"

#: 換算がコートで鏡になる軸を持つ台
_COURT_POSITIONS = {
    "axes": {
        "sub_lift": {
            "unit": "mm",
            "command_unit": "rad",
            "scale": {"red": -1.0668451, "blue": 1.0668451},
            "tolerance": 1.0,
            "manual": {"min": -50.0, "max": 50.0, "steps": [1.0]},
        },
    },
    "positions": {"sub_lift": {"top": 0.0, "bottom": 40.0}},
}

#: コートに依存しない台 (退避路)
_PLAIN_POSITIONS = {
    "axes": {
        "y_axis": {
            "unit": "mm",
            "command_unit": "deg",
            "scale": 55.0,
            "tolerance": 1.0,
            "manual": {"min": -2.0, "max": 20.0, "steps": [0.5]},
        },
    },
    "positions": {"y_axis": {"home": 0.0, "work": 10.0}},
}


class _RecordingDriver(StubFeedbackDriver):
    def __init__(self, name: str) -> None:
        super().__init__(name, 1)
        self.commands: list[tuple[ControlMode, float]] = []

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        self.commands.append((mode, value))
        return super().encode_target(mode, value)


class _IdleSequence(Sequence):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.executed: list[str] = []

    @step("何もしない")
    async def idle(self) -> None:
        self.executed.append("idle")


def _wire(name: str, positions: dict) -> tuple[Sequence, ManualController, _RecordingDriver]:
    table = load_position_table(positions, source=f"<{name}>")
    sequence = _IdleSequence(name)
    group = MotorGroup()
    driver: _RecordingDriver | None = None
    mgr = mock_can_manager()
    for axis in table.axes:
        for motor in table.axis(axis).motor_names:
            driver = _RecordingDriver(motor)
            group.add(MotorHandle(motor, driver, mgr))
    assert driver is not None
    sequence.bind_motors(group)
    sequence.bind_positions(table)
    return sequence, ManualController(group, table), driver


def _fixture() -> tuple[ServerFixture, dict[str, _RecordingDriver]]:
    fx = ServerFixture.build()
    fx.freeze_broadcast()
    drivers: dict[str, _RecordingDriver] = {}
    for name, positions in ((_COURT_ROBOT, _COURT_POSITIONS), (_PLAIN_ROBOT, _PLAIN_POSITIONS)):
        sequence, manual, driver = _wire(name, positions)
        fx.add_robot(name, sequence, manual=manual)
        drivers[name] = driver
    return fx, drivers


async def _send(fx: ServerFixture, payload: dict) -> RecordingClient:
    client = RecordingClient()
    fx.attach_clients(client)
    await fx.command(payload, requester=client)
    return client


async def _to_manual(fx: ServerFixture, robot: str) -> None:
    await fx.command({"type": "set_operation_mode", "robot": robot, "mode": "manual"})


def _rejection(client: RecordingClient) -> str | None:
    rejected = client.of_type("command_rejected")
    return None if not rejected else rejected[-1]["reason"]


class TestCourtDependentAxesAreDeclaredByThePositionTable:
    """要否の正は位置定数だけが持つ。**サーバーにも UI にも軸名を書き写さない。**"""

    def test_scale_がコート別の軸だけが挙がる(self) -> None:
        table = load_position_table(_COURT_POSITIONS, source="<test>")
        assert table.court_dependent_axes() == ("sub_lift",)

    def test_コート非依存の台は空(self) -> None:
        table = load_position_table(_PLAIN_POSITIONS, source="<test>")
        assert table.court_dependent_axes() == ()

    def test_コート別の位置は別の口で挙がる(self) -> None:
        table = load_position_table(
            {
                "axes": {"conveyor": {"unit": "duty", "command_mode": "duty"}},
                "positions": {"conveyor": {"stop": 0.0, "run": {"red": 0.3, "blue": -0.3}}},
            },
            source="<test>",
        )
        # 換算は両コートで同じなので、指令そのものを止める必要は無い
        assert table.court_dependent_axes() == ()
        assert table.court_dependent_position_axes() == ("conveyor",)


class TestManualIsBlockedWithoutCourt:
    @pytest.mark.parametrize(
        "payload",
        [
            {"type": "manual_move", "axis": "sub_lift", "position": "top"},
            {"type": "manual_set", "axis": "sub_lift", "value": 5.0},
            {"type": "manual_jog", "axis": "sub_lift", "delta": 1.0},
        ],
    )
    async def test_理由付きで拒否される(self, payload: dict) -> None:
        fx, drivers = _fixture()
        client = await _send(fx, {**payload, "robot": _COURT_ROBOT})

        assert "コートが未設定" in (_rejection(client) or "")
        assert drivers[_COURT_ROBOT].commands == []

    async def test_コートを選べば通る(self) -> None:
        fx, drivers = _fixture()
        await fx.command({"type": "set_court", "court": "blue"})
        await _to_manual(fx, _COURT_ROBOT)
        client = await _send(
            fx,
            {"type": "manual_move", "robot": _COURT_ROBOT, "axis": "sub_lift", "position": "top"},
        )

        assert _rejection(client) is None
        assert drivers[_COURT_ROBOT].commands != []


class TestRetreatPathStaysOpen:
    """**メインハンドは退避路。**コートに依存しないので未確定でも動かせる。"""

    async def test_コート非依存の台は手動できる(self) -> None:
        fx, drivers = _fixture()
        await _to_manual(fx, _PLAIN_ROBOT)
        client = await _send(
            fx,
            {"type": "manual_move", "robot": _PLAIN_ROBOT, "axis": "y_axis", "position": "work"},
        )

        assert _rejection(client) is None
        assert drivers[_PLAIN_ROBOT].commands != []

    async def test_コート依存の台だけが止まる(self) -> None:
        fx, drivers = _fixture()
        await _to_manual(fx, _COURT_ROBOT)
        await _to_manual(fx, _PLAIN_ROBOT)
        await _send(
            fx,
            {"type": "manual_move", "robot": _COURT_ROBOT, "axis": "sub_lift", "position": "top"},
        )
        await _send(
            fx,
            {"type": "manual_move", "robot": _PLAIN_ROBOT, "axis": "y_axis", "position": "work"},
        )

        assert drivers[_COURT_ROBOT].commands == []
        assert drivers[_PLAIN_ROBOT].commands != []


class TestSequenceCommandsAreBlockedWithoutCourt:
    async def test_sequence_start_が拒否される(self) -> None:
        fx, _ = _fixture()
        client = await _send(fx, {"type": "sequence_start", "robot": _COURT_ROBOT})

        # フェーズゲートより先にコートが立つことはない。試合へ入れないので理由は
        # フェーズだが、どちらにせよ開始できないことを固定する
        assert _rejection(client) is not None
        assert fx.sequence(_COURT_ROBOT).is_running is False

    async def test_零点合わせが拒否される(self) -> None:
        fx, _ = _fixture()
        client = await _send(
            fx, {"type": "homing_start", "robot": _COURT_ROBOT, "axes": ["sub_lift"]}
        )

        assert "コートが未設定" in (_rejection(client) or "")

    async def test_零点合わせはコート非依存の台では通る(self) -> None:
        fx, _ = _fixture()
        client = await _send(
            fx, {"type": "homing_start", "robot": _PLAIN_ROBOT, "axes": ["y_axis"]}
        )

        assert "コートが未設定" not in (_rejection(client) or "")


class TestRobotWideCommandsBlockWhenAnyRobotNeedsTheCourt:
    """台を名指ししないコマンドは両ハンドを走らせるので、1 台でも要るなら塞ぐ。"""

    async def test_動作確認が拒否される(self) -> None:
        fx, _ = _fixture()
        await fx.command({"type": "motor_check_start"})

        # 動作確認の拒否は状態の error に載る (専用の通知経路)
        assert "コートが未設定" in (fx.motor_check_state()["error"] or "")

    async def test_コートを選べば動作確認は塞がれない(self) -> None:
        fx, _ = _fixture()
        await fx.command({"type": "set_court", "court": "red"})
        await fx.command({"type": "motor_check_start"})

        assert "コートが未設定" not in (fx.motor_check_state()["error"] or "")


class TestSecondLayerHoldsWithoutTheCommandGate:
    """**1 枚目を外しても止まる。**多重防護を 1 枚ずつ確かめる (`invariants.md` §9)。"""

    async def test_コマンドゲートを外しても指令の入口が拒否する(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fx, drivers = _fixture()
        await _to_manual(fx, _COURT_ROBOT)
        monkeypatch.setattr(type(fx.server), "_court_deny_reason", lambda *_args, **_kwargs: None)

        client = await _send(
            fx,
            {"type": "manual_move", "robot": _COURT_ROBOT, "axis": "sub_lift", "position": "top"},
        )

        assert "コートが解決されていません" in (_rejection(client) or "")
        assert drivers[_COURT_ROBOT].commands == []

    async def test_コート別の位置も拒否として返る(self) -> None:
        """コマンドゲートが塞がない台でも、位置名を引く手動はここで止まる。"""
        fx = ServerFixture.build()
        fx.freeze_broadcast()
        positions = {
            "axes": {"conveyor": {"unit": "duty", "command_unit": "duty", "command_mode": "duty"}},
            "positions": {"conveyor": {"stop": 0.0, "run": {"red": 0.3, "blue": -0.3}}},
        }
        sequence, manual, driver = _wire(_PLAIN_ROBOT, positions)
        fx.add_robot(_PLAIN_ROBOT, sequence, manual=manual)
        await _to_manual(fx, _PLAIN_ROBOT)

        client = await _send(
            fx,
            {"type": "manual_move", "robot": _PLAIN_ROBOT, "axis": "conveyor", "position": "run"},
        )

        assert "コートが指定されていません" in (_rejection(client) or "")
        assert driver.commands == []
