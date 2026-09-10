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
from lib.motion_guard import (
    GuardViolation,
    LimitSpec,
    MotionGuard,
    MotionGuardSpec,
    SensorSuspension,
)
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


class TestSeveralSwitchesOnOneEnd:
    """**左右直結ペアは同じ端に 1 本ずつ持つ** (`y_axis` の左右の原点スイッチ)。

    片方だけ宣言すると守りが半分になり、しかもどちらが落ちているかは機構が
    壊れるまで分からない。軸は 1 つなので、**1 本でも押されていればその向きへ
    進める余地はもう無い。**
    """

    def _pair_guard(self) -> MotionGuard:
        return MotionGuard(
            MotionGuardSpec(limits=LimitSpec(minus=("origin_r", "origin_l")), max_step=50.0)
        )

    def test_一本目が押されていれば止める(self) -> None:
        with pytest.raises(GuardViolation, match="origin_r"):
            self._pair_guard().check_command(
                axis="y_axis",
                current=0.0,
                target=-1.0,
                unit="mm",
                sensor_active=_sensors(origin_r=True, origin_l=False),
            )

    def test_二本目だけが押されていても止める(self) -> None:
        """**1 本目で打ち切ると、2 本目のスイッチは飾りになる。**

        症状は「左は端で止まるのに右は踏み越える」で、機構が片側だけ壊れる。
        """
        with pytest.raises(GuardViolation, match="origin_l"):
            self._pair_guard().check_command(
                axis="y_axis",
                current=0.0,
                target=-1.0,
                unit="mm",
                sensor_active=_sensors(origin_r=False, origin_l=True),
            )

    def test_二本目だけが読めていなくても止める(self) -> None:
        """**片方が読めているからといって、読めていない側を素通りさせない。**

        丸めると、配線が抜けた 1 本が「押されていない = 進んでよい」に化ける。
        """
        with pytest.raises(GuardViolation, match="読めていない"):
            self._pair_guard().check_command(
                axis="y_axis",
                current=0.0,
                target=-1.0,
                unit="mm",
                sensor_active=_sensors(origin_r=False, origin_l=None),
            )

    def test_読めていない側を先に言う(self) -> None:
        """端に着いていることは機構の姿勢から読めるが、死んだ 1 本は画面にしか出ない。"""
        with pytest.raises(GuardViolation, match="'origin_l' が読めていない"):
            self._pair_guard().check_command(
                axis="y_axis",
                current=0.0,
                target=-1.0,
                unit="mm",
                sensor_active=_sensors(origin_r=True, origin_l=None),
            )

    def test_全部離れていれば通す(self) -> None:
        self._pair_guard().check_command(
            axis="y_axis",
            current=0.0,
            target=-1.0,
            unit="mm",
            sensor_active=_sensors(origin_r=False, origin_l=False),
        )

    def test_離れる向きは押されていても通す(self) -> None:
        """零点確定の離脱段と、端に張り付いた軸の手動退避がこの向きを使う。"""
        self._pair_guard().check_command(
            axis="y_axis",
            current=0.0,
            target=1.0,
            unit="mm",
            sensor_active=_sensors(origin_r=True, origin_l=True),
        )

    def test_止めたセンサ名が文面に出る(self) -> None:
        """どちらのスイッチで止まったかが出ないと、操縦者は 2 本を順に触って探す。"""
        with pytest.raises(GuardViolation, match="'origin_r' / 'origin_l'"):
            self._pair_guard().check_command(
                axis="y_axis",
                current=0.0,
                target=-1.0,
                unit="mm",
                sensor_active=_sensors(origin_r=True, origin_l=True),
            )

    def test_一本を文字列で書いた宣言はそのまま通る(self) -> None:
        """既存の `plus: <センサ名>` を書き換えさせない (差分に紛れて向きを取り違える)。"""
        guard = MotionGuard(MotionGuardSpec(limits=LimitSpec(plus="front")))

        with pytest.raises(GuardViolation, match="'front'"):
            guard.check_limit(axis="sub_y_axis", delta=1.0, sensor_active=_sensors(front=True))


class TestUnexpectedContact:
    """進む向きと反対側の端が「指令の時点では OFF、今は ON」なら止める。

    向き・配線・`scale` の符号のどれを取り違えても、監視が見張る端と機構が向かう端が
    入れ替わる。指令の時点から ON の端 (張り付きからの退避) は通す。
    """

    def test_指令の時点で_OFF_だった反対側の端が押されたら止める(self) -> None:
        with pytest.raises(GuardViolation, match="front"):
            _guard().check_unexpected_contact(
                axis="sub_y_axis",
                delta=-1.0,
                sensor_active=_sensors(front=True),
                was_active=_sensors(front=False),
            )

    def test_指令の時点から押されていた端は通す(self) -> None:
        _guard().check_unexpected_contact(
            axis="sub_y_axis",
            delta=-1.0,
            sensor_active=_sensors(front=True),
            was_active=_sensors(front=True),
        )

    def test_読めていない反対側の端は退避を妨げない(self) -> None:
        _guard().check_unexpected_contact(
            axis="sub_y_axis",
            delta=-1.0,
            sensor_active=_sensors(front=None),
            was_active=_sensors(front=False),
        )

    def test_指令の時点で読めていなかった端は判断しない(self) -> None:
        _guard().check_unexpected_contact(
            axis="sub_y_axis",
            delta=-1.0,
            sensor_active=_sensors(front=True),
            was_active=_sensors(front=None),
        )

    def test_進む向きの端はここでは見ない(self) -> None:
        """押されている端へ向かう判定は `check_limit` が持つ。二重に書かない。"""
        _guard().check_unexpected_contact(
            axis="sub_y_axis",
            delta=-1.0,
            sensor_active=_sensors(rear=True),
            was_active=_sensors(rear=False),
        )

    def test_動かない指令は見ない(self) -> None:
        _guard().check_unexpected_contact(
            axis="sub_y_axis",
            delta=0.0,
            sensor_active=_sensors(front=True),
            was_active=_sensors(front=False),
        )

    def test_文面にセンサ名と疑う先を載せる(self) -> None:
        with pytest.raises(GuardViolation, match=r"反対.*'front'") as exc_info:
            _guard().check_unexpected_contact(
                axis="sub_y_axis",
                delta=-1.0,
                sensor_active=_sensors(front=True),
                was_active=_sensors(front=False),
            )
        assert "scale" in str(exc_info.value)


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
        state = {"position": start, "contacts": 0, "on": False}

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
            on = sensor_active("front_switch")
            if on and not state["on"]:
                state["contacts"] += 1
            state["on"] = on

        handle.set_target_value = _move  # type: ignore[method-assign]
        return handle, state

    def _runner(self, state: dict) -> HomingRunner:
        async def sleep(_seconds: float) -> None:
            return None

        def contact_count(_name: str) -> int:
            return state["contacts"]

        async def capture_origin(axis: str) -> None:
            state.setdefault("origins", []).append(axis)

        return HomingRunner(
            sensor_active=lambda _name: state["position"] >= 5.0,
            sensor_contact_count=contact_count,
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


class TestSensorSuspension:
    """整列段のあいだだけ、その軸の原点センサを歯止めから外す覆い。

    **要るのは零点確定の整列段ただ 1 つ** (左右に 1 本ずつスイッチが付く軸で、
    まだ当たっていない側だけを端へ進める段)。噛み合わせは
    `tests/test_homing.py::TestAlignsWithTheGuardArmed` が実物どうしで見る。
    """

    def test_覆っている間だけ押されていないと答える(self) -> None:
        suspension = SensorSuspension()
        read = suspension.wrap(_sensors(origin_r=True))

        assert read("origin_r") is True
        with suspension.suspend(["origin_r"]):
            assert read("origin_r") is False
        assert read("origin_r") is True

    def test_名指ししていないセンサは覆わない(self) -> None:
        """**軸まるごと外すと、反対端の守りまで消える。**"""
        suspension = SensorSuspension()
        read = suspension.wrap(_sensors(origin_r=True, rear=True))

        with suspension.suspend(["origin_r"]):
            assert read("rear") is True

    def test_読めていないセンサも押されていない扱いにする(self) -> None:
        """`None` のままだと歯止めが安全側 (止まる) へ転び、覆う目的が果たせない。"""
        suspension = SensorSuspension()
        read = suspension.wrap(_sensors(origin_r=None))

        with suspension.suspend(["origin_r"]):
            assert read("origin_r") is False

    def test_多重に掛けても早く外れない(self) -> None:
        suspension = SensorSuspension()
        read = suspension.wrap(_sensors(origin_r=True))

        with suspension.suspend(["origin_r"]):
            with suspension.suspend(["origin_r"]):
                assert read("origin_r") is False
            assert read("origin_r") is False
        assert read("origin_r") is True

    def test_例外で抜けても必ず外れる(self) -> None:
        """外れ残ると、その端は試合が終わるまで守られない。"""
        suspension = SensorSuspension()
        read = suspension.wrap(_sensors(origin_r=True))

        with pytest.raises(RuntimeError), suspension.suspend(["origin_r"]):
            raise RuntimeError("整列段が落ちた")

        assert read("origin_r") is True

    def test_覆っている間は接触の累計が増えない(self) -> None:
        """現在値だけ覆っても、周期監視は接触の累計で「押されている」と判定する。"""
        suspension = SensorSuspension()
        contacts = {"origin_r": 3}
        count = suspension.wrap_count(contacts.get)
        assert count("origin_r") == 3

        with suspension.suspend(["origin_r"]):
            contacts["origin_r"] = 5
            assert count("origin_r") == 3
            contacts["origin_r"] = 7
            assert count("origin_r") == 3

    def test_覆う前に最後に読めた値で凍らせる(self) -> None:
        """覆ってから最初に読むまでに数えた接触が混ざると、その周期だけ基準値が跳ねる。"""
        suspension = SensorSuspension()
        contacts = {"origin_r": 3}
        count = suspension.wrap_count(contacts.get)
        assert count("origin_r") == 3

        with suspension.suspend(["origin_r"]):
            contacts["origin_r"] = 4

            assert count("origin_r") == 3

    def test_覆いを外すと生の累計に戻る(self) -> None:
        suspension = SensorSuspension()
        contacts = {"origin_r": 3}
        count = suspension.wrap_count(contacts.get)
        assert count("origin_r") == 3

        with suspension.suspend(["origin_r"]):
            contacts["origin_r"] = 5
            assert count("origin_r") == 3
        assert count("origin_r") == 5

        # 凍らせた値を持ち越すと、次の整列段が前回の値で覆い始める
        with suspension.suspend(["origin_r"]):
            assert count("origin_r") == 5

    def test_覆っている間も累計を答える(self) -> None:
        """`None` (カウンタ非対応) へ倒すと、読み手が基準値を進めずに見送るので、
        覆いを外した周期に**覆っている間の増加がまとめて接触として立つ**。"""
        suspension = SensorSuspension()
        contacts = {"origin_r": 3}
        count = suspension.wrap_count(contacts.get)

        with suspension.suspend(["origin_r"]):
            contacts["origin_r"] = 9
            assert count("origin_r") is not None

    def test_名指ししていないセンサの累計は覆わない(self) -> None:
        suspension = SensorSuspension()
        contacts = {"origin_r": 3, "rear": 1}
        count = suspension.wrap_count(contacts.get)

        with suspension.suspend(["origin_r"]):
            contacts["rear"] = 2
            assert count("rear") == 2

    def test_カウンタを持たないセンサは覆っても_None(self) -> None:
        """カウンタの有無まで偽ると、読み手の判断材料そのものが変わる。"""
        suspension = SensorSuspension()
        count = suspension.wrap_count(lambda _name: None)

        with suspension.suspend(["origin_r"]):
            assert count("origin_r") is None

    def test_累計の覆いも多重に掛けて早く外れない(self) -> None:
        suspension = SensorSuspension()
        contacts = {"origin_r": 3}
        count = suspension.wrap_count(contacts.get)
        assert count("origin_r") == 3

        with suspension.suspend(["origin_r"]):
            with suspension.suspend(["origin_r"]):
                contacts["origin_r"] = 5
            assert count("origin_r") == 3
        assert count("origin_r") == 5
