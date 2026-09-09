"""指令を出してよいかの判断 (lib/motion_guard.py)。

見るのは 4 点で、いずれも 2026-09-09 に実機で踏んだ事故に対応する:

- 押されている端へは進まない / **離れる向きは必ず通す**
- 読めていないセンサを「押されていない」へ丸めない
- 桁の違う目標 (スケール・固定小数点レンジの取り違え) をここで止める
- トルクが立ったら止める。ただし測れないモータでは何も言わない

後半 (`TestAxisHandleInterlock` 以降) は**判断を指令経路へ配線できているか**を見る。
手動操縦・シーケンス・零点確定の 3 経路はすべて `AxisHandle.set_target_value` を
通るので、歯止めもそこ 1 箇所にしか無い。
"""

from __future__ import annotations

import pytest

from lib.drivers.base import NO_TELEMETRY, ControlMode, TelemetrySupport
from lib.manual import ManualController
from lib.motion_guard import GuardViolation, LimitSpec, MotionGuard, MotionGuardSpec
from lib.sequence.engine import Sequence
from lib.sequence.homing import HomingRunner
from lib.sequence.motors import AxisHandle, MotorGroup, MotorHandle
from lib.sequence.positions import PositionTable, load_position_table
from tests.fake_can import mock_can_manager
from tests.fake_drivers import StubFeedbackDriver


def _guard(**kwargs: object) -> MotionGuard:
    params: dict = {
        "limits": LimitSpec(plus="front", minus="rear"),
        "max_step": 50.0,
        "stall_torque": 1.0,
    }
    params.update(kwargs)
    return MotionGuard(MotionGuardSpec(**params))  # type: ignore[arg-type]


def _sensors(**states: object):
    def read(name: str):
        return states.get(name, False)

    return read


class TestLimitInterlock:
    def test_押されている端へは進ませない(self) -> None:
        with pytest.raises(GuardViolation, match="押されている"):
            _guard().check_command(
                axis="sub_y_axis",
                current=0.0,
                target=1.0,
                unit="mm",
                sensor_active=_sensors(front=True),
            )

    def test_離れる向きは通す(self) -> None:
        """**塞ぐと機構端に張り付いた軸を手動でも戻せなくなる。**

        零点確定の離脱段もこの向きを使うので、ここを塞ぐと「触れた状態から
        始めたら二度と原点を切れない」機体になる。
        """
        _guard().check_command(
            axis="sub_y_axis",
            current=0.0,
            target=-1.0,
            unit="mm",
            sensor_active=_sensors(front=True),
        )

    def test_逆端のスイッチで止まる(self) -> None:
        """**探索の向きを取り違えたときに押し込む前に止まる経路。**"""
        with pytest.raises(GuardViolation, match="rear"):
            _guard().check_command(
                axis="sub_y_axis",
                current=0.0,
                target=-1.0,
                unit="mm",
                sensor_active=_sensors(rear=True),
            )

    def test_読めていないセンサは押されている扱いにする(self) -> None:
        """**「読めていない」を「押されていない」へ丸めると配線不良が素通りする。**

        センサが 1 本も届いていない構成では、丸めた瞬間にインターロックが
        まるごと無効になり、しかも画面には何も出ない。
        """
        with pytest.raises(GuardViolation, match="読めていない"):
            _guard().check_command(
                axis="sub_y_axis",
                current=0.0,
                target=1.0,
                unit="mm",
                sensor_active=_sensors(front=None),
            )

    def test_端を宣言していない向きは見ない(self) -> None:
        guard = _guard(limits=LimitSpec(plus="front"))
        guard.check_command(
            axis="sub_y_axis",
            current=0.0,
            target=-1.0,
            unit="mm",
            sensor_active=_sensors(rear=True),
        )

    def test_動かない指令は端を見ない(self) -> None:
        """保持のための再送 (同じ値の書き直し) まで塞ぐと、端で保持が切れて落ちる。"""
        _guard().check_command(
            axis="sub_y_axis",
            current=5.0,
            target=5.0,
            unit="mm",
            sensor_active=_sensors(front=True, rear=True),
        )


class TestJumpGuard:
    def test_桁の違う目標を止める(self) -> None:
        """**実機で踏んだ形そのもの。**

        p_max の食い違いで位置が 80 倍に読め、その値が保持目標として書かれて
        機構がリミットスイッチを踏み越えた。比が分からなくても「1 指令で
        動かしてよい量」を超えたことは分かる。
        """
        with pytest.raises(GuardViolation, match="スケール"):
            _guard().check_command(
                axis="sub_y_axis",
                current=3.0,
                target=240.0,
                unit="mm",
                sensor_active=_sensors(),
            )

    def test_上限ちょうどは通す(self) -> None:
        _guard().check_command(
            axis="sub_y_axis",
            current=0.0,
            target=50.0,
            unit="mm",
            sensor_active=_sensors(),
        )

    def test_跳躍はセンサより先に見る(self) -> None:
        """桁が違う目標は、端を離れる向きでも出してはならない。

        センサの向き判定を先に通すと、「離れる向きだから」という理由で
        80 倍の目標が素通りする。
        """
        with pytest.raises(GuardViolation, match="スケール"):
            _guard().check_command(
                axis="sub_y_axis",
                current=0.0,
                target=-240.0,
                unit="mm",
                sensor_active=_sensors(rear=True),
            )

    def test_宣言しなければ跳躍を見ない(self) -> None:
        _guard(max_step=None).check_command(
            axis="sub_y_axis",
            current=0.0,
            target=1e6,
            unit="mm",
            sensor_active=_sensors(),
        )


class TestTorqueGuard:
    def test_しきい値を超えたら止める(self) -> None:
        with pytest.raises(GuardViolation, match="トルク"):
            _guard().check_torque(axis="sub_y_axis", torque=1.5)

    def test_符号は問わない(self) -> None:
        with pytest.raises(GuardViolation, match="トルク"):
            _guard().check_torque(axis="sub_y_axis", torque=-1.5)

    def test_測れないモータでは何も言わない(self) -> None:
        """**常に 0 を運ぶ値で判定すると「測ったように見える 0」が異常なしに化ける。**"""
        _guard().check_torque(axis="conveyor", torque=None)

    def test_宣言しなければトルクを見ない(self) -> None:
        _guard(stall_torque=None).check_torque(axis="sub_y_axis", torque=100.0)


class TestSpecValidation:
    @pytest.mark.parametrize("value", [0.0, -1.0])
    def test_max_step_は正の値(self, value: float) -> None:
        with pytest.raises(ValueError, match="max_step"):
            MotionGuardSpec(max_step=value)

    @pytest.mark.parametrize("value", [0.0, -1.0])
    def test_stall_torque_は正の値(self, value: float) -> None:
        with pytest.raises(ValueError, match="stall_torque"):
            MotionGuardSpec(stall_torque=value)


class _NoTorqueDriver(StubFeedbackDriver):
    """トルクを測る手段が無いドライバ (DC 基板・電磁弁基板と同じ宣言)。

    ``MotorState.current`` は常に 0.0 を運ぶので、宣言を見ずに読むと
    「測ったように見える 0」がそのまま「異常なし」に化ける。
    """

    @property
    def telemetry(self) -> TelemetrySupport:
        return NO_TELEMETRY


def _table(*, guard: object, **axis_overrides: object) -> PositionTable:
    axis: dict = {
        "unit": "mm",
        "command_unit": "rad",
        "scale": 2.0,
        "tolerance": 0.1,
    }
    if guard is not None:
        axis["guard"] = guard
    axis.update(axis_overrides)
    return load_position_table(
        {"axes": {"sub_y_axis": axis}, "positions": {"sub_y_axis": {"home": 0.0}}},
        source="<test>",
    )


_GUARD: dict = {
    "limits": {"plus": "front_switch", "minus": "rear_switch"},
    "max_step": 50.0,
    "stall_torque": 1.0,
}


def _group(
    table: PositionTable,
    *,
    sensor_active=None,
    position: float = 0.0,
    torque: float = 0.0,
    driver_class: type[StubFeedbackDriver] = StubFeedbackDriver,
) -> tuple[MotorGroup, list[float]]:
    """1 モータ軸のモータ束と、実際に送られた指令値の記録。

    送信は ``target_sink`` で受ける。**歯止めに掛かった指令が 1 通も出ていない**
    ことを見るには、拒否されたかどうかではなく送信そのものを数える必要がある。
    """
    sent: list[float] = []

    async def sink(_mode: ControlMode, value: float) -> None:
        sent.append(value)

    driver = driver_class("sub_y_axis", 1)
    spec = table.axis("sub_y_axis")
    driver.set_observed(position=spec.motors[0].to_command(position), current=torque)
    group = MotorGroup(sensor_active=sensor_active)
    group.add(MotorHandle("sub_y_axis", driver, mock_can_manager(), target_sink=sink))
    return group, sent


def _handle(group: MotorGroup, table: PositionTable) -> AxisHandle:
    spec = table.axis("sub_y_axis")
    return AxisHandle(spec, [group["sub_y_axis"]], sensor_active=group.sensor_active)


async def _send(handle: AxisHandle, table: PositionTable, value: float) -> None:
    await handle.set_target_value(table.axis("sub_y_axis").to_commands(value))


class TestAxisHandleInterlock:
    """`AxisHandle.set_target_value` が唯一の口である。

    手動操縦・シーケンス (`move_to`)・零点確定の 3 経路がすべてここを通るので、
    歯止めもここにしか無い。**層ごとに書き写すと片方だけ緩んだ状態が作れる。**
    """

    async def test_押されている端へ進む指令は_1_通も送らない(self) -> None:
        table = _table(guard=_GUARD)
        group, sent = _group(table, sensor_active=_sensors(front_switch=True))

        with pytest.raises(GuardViolation, match="押されている"):
            await _send(_handle(group, table), table, 1.0)

        assert sent == []

    async def test_離れる向きは通す(self) -> None:
        """**塞ぐと端に張り付いた軸を手動でも戻せない。** 零点確定の離脱段も使う。"""
        table = _table(guard=_GUARD)
        group, sent = _group(table, sensor_active=_sensors(front_switch=True))

        await _send(_handle(group, table), table, -1.0)

        assert sent == [-2.0]

    async def test_読めていないセンサでは進ませない(self) -> None:
        table = _table(guard=_GUARD)
        group, sent = _group(table, sensor_active=_sensors(front_switch=None))

        with pytest.raises(GuardViolation, match="読めていない"):
            await _send(_handle(group, table), table, 1.0)

        assert sent == []

    async def test_読み口を配線しなければ全部読めていない扱いになる(self) -> None:
        """**既定を「押されていない」にしてはならない。**

        `False` を既定にすると、配線し忘れた経路だけがインターロックを丸ごと
        素通りし、しかもそれが画面にもログにも出ない。
        """
        table = _table(guard=_GUARD)
        group, sent = _group(table, sensor_active=None)

        with pytest.raises(GuardViolation, match="読めていない"):
            await _send(_handle(group, table), table, 1.0)

        assert sent == []

    async def test_桁の違う目標を止める(self) -> None:
        """p_max の食い違いで位置が 80 倍に読めた事故がここに掛かる。"""
        table = _table(guard=_GUARD)
        group, sent = _group(table, sensor_active=_sensors())

        with pytest.raises(GuardViolation, match="スケール"):
            await _send(_handle(group, table), table, 240.0)

        assert sent == []

    async def test_guard_を書かない軸は素通りする(self) -> None:
        """既存の軸の振る舞いを変えない (書かなかった軸に歯止めは無い)。"""
        table = _table(guard=None)
        group, sent = _group(table, sensor_active=_sensors(front_switch=True))

        await _send(_handle(group, table), table, 1.0)

        assert sent == [2.0]


class TestAxisHandleTorque:
    async def test_トルクが立ったら止める(self) -> None:
        table = _table(guard=_GUARD)
        group, sent = _group(table, sensor_active=_sensors(), torque=1.5)

        with pytest.raises(GuardViolation, match="トルク"):
            await _send(_handle(group, table), table, 1.0)

        assert sent == []

    async def test_その場で止める指令はトルクが立っていても通す(self) -> None:
        """**止めるための指令が、止まっていないことを理由に拒否されてはならない。**

        零点確定は接触を検出した瞬間に「実測位置」をそのまま送り直して行き過ぎを
        止める。ここを塞ぐと、押し込む向きの古い目標が生き残る。
        """
        table = _table(guard=_GUARD)
        group, sent = _group(table, sensor_active=_sensors(), position=3.0, torque=1.5)

        await _send(_handle(group, table), table, 3.0)

        assert sent == [6.0]

    async def test_測れないモータのトルクは見ない(self) -> None:
        """常に 0 を運ぶ値で判定すると「測ったように見える 0」が異常なしに化ける。

        逆に測れないことを異常へ倒すと、DC 基板を 1 枚積んだだけで全部止まる。
        """
        table = _table(guard=_GUARD)
        group, sent = _group(
            table, sensor_active=_sensors(), torque=99.0, driver_class=_NoTorqueDriver
        )

        await _send(_handle(group, table), table, 1.0)

        assert sent == [2.0]


class TestCommandPathsInheritTheReader:
    """**3 経路とも `MotorGroup` の読み口を引き継ぐ。**

    どれか 1 つが引き継がなければ、その経路だけがインターロックの外側に出る。
    症状は「手動では止まるのにシーケンスでは踏み越える」なので、実機で機構を
    壊すまで気付けない。
    """

    async def test_手動操縦が止まる(self) -> None:
        table = _table(guard=_GUARD, manual={"min": -10.0, "max": 10.0, "steps": [1.0]})
        group, sent = _group(table, sensor_active=_sensors(front_switch=True))

        with pytest.raises(GuardViolation, match="押されている"):
            await ManualController(group, table).set_value("sub_y_axis", 1.0)

        assert sent == []

    async def test_シーケンスの_move_to_が止まる(self) -> None:
        table = _table(guard=_GUARD)
        # 位置定数 `home` (0.0) は現在位置 (-1.0) から見て + 方向 = 前端側にある
        group, sent = _group(table, sensor_active=_sensors(front_switch=True), position=-1.0)
        seq = Sequence("sub_hand")
        seq.bind_motors(group)
        seq.bind_positions(table)

        with pytest.raises(GuardViolation, match="押されている"):
            await seq.move_to({"sub_y_axis": "home"})

        assert sent == []


class TestHomingStillWorks:
    """**探索はスイッチへ向かって動く。** 掛け方を誤ると 1 歩目から拒否される。

    ここが緑であることは、インターロックが「離れる向きは必ず通す」性質に
    寄りかかっていることの確認でもある (触れた状態から始める離脱段は、
    押されているセンサの側から出発する)。
    """

    def _homing_table(self) -> PositionTable:
        return _table(
            guard={"limits": {"plus": "front_switch"}, "max_step": 50.0},
            homing={
                "sensor": "front_switch",
                "direction": 1,
                "search_distance": 30.0,
                "step": 1.0,
                "settle_s": 0.0,
            },
        )

    def _wire(self, table: PositionTable, *, start: float) -> tuple[AxisHandle, dict]:
        """実測位置が `>= 5.0` でスイッチが ON になる機構。

        **指令の後に動かす。** 先に動かすと実測と目標が常に一致し、可動端の判定は
        `delta == 0` として素通りしてしまう (テストが何も見ていない状態になる)。
        """
        spec = table.axis("sub_y_axis")
        state = {"position": start, "latched": False}

        def sensor_active(_name: str) -> bool:
            return state["position"] >= 5.0

        group, _sent = _group(table, sensor_active=sensor_active, position=start)
        driver = group["sub_y_axis"].driver
        handle = _handle(group, table)
        original = handle.set_target_value

        async def _move(commands):
            await original(commands)
            state["position"] = spec.to_value(commands)
            driver.set_observed(position=commands["sub_y_axis"])
            if sensor_active("front_switch"):
                state["latched"] = True

        handle.set_target_value = _move  # type: ignore[method-assign]
        return handle, state

    def _runner(self, state: dict) -> HomingRunner:
        async def sleep(_seconds: float) -> None:
            return None

        def latched(_name: str) -> bool:
            hit = state["latched"]
            state["latched"] = False
            return hit

        async def capture_origin(axis: str) -> None:
            state.setdefault("origins", []).append(axis)

        return HomingRunner(
            sensor_active=lambda _name: state["position"] >= 5.0,
            sensor_latched=latched,
            sensor_is_stale=lambda _name: False,
            motor_is_stale=lambda _name: False,
            motor_is_energized=lambda _name: True,
            origin_capturable=lambda _axis: True,
            capture_origin=capture_origin,
            sleep=sleep,
        )

    async def test_スイッチへ向かう探索が完走する(self) -> None:
        table = self._homing_table()
        handle, state = self._wire(table, start=0.0)

        travelled = await self._runner(state).home(table.axis("sub_y_axis"), handle)

        assert state["position"] >= 5.0
        assert travelled == pytest.approx(5.0)

    async def test_触れた状態から始めても離脱できる(self) -> None:
        """離脱は押されているセンサの側から**離れる向き**へ動く。

        ここを塞ぐと「触れた状態から始めたら二度と原点を切れない」機体になる。
        """
        table = self._homing_table()
        handle, state = self._wire(table, start=6.0)

        await self._runner(state).home(table.axis("sub_y_axis"), handle)

        assert state["position"] >= 5.0
