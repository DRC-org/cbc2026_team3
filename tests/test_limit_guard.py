"""リミットスイッチ保護の判定と 50Hz 常駐層 (lib/control/limit_guard.py)。

**この層だけを見る。** 統合経路 (シーケンス・手動操縦・零点確定) では、緊急停止や
フィードバック途絶といった別の条件が先に指令を止めうるので、1 枚壊しても他が拾って
落ちない。ここでは他の層を 1 つも与えない条件で、この保護だけが効いていることを見る。

**見たいことは 3 つある。**

1. 触れたら止まる (かつ**逆方向は必ず通る** —— 復帰できない軸を作らない)
2. 止まったあと**目標を書き直さない** (毎周期実測へ張り直すと、負荷で下がったぶんへ
   目標が追従して誰も操作していないのに軸がクリープする)
3. **効いていないことが必ず見える** (途絶したセンサでは判定しないが、黙って
   無効にはしない)
"""

from __future__ import annotations

import pytest

from lib.control.limit_guard import LimitGuard
from lib.sequence.motors import AxisHandle, EStopActiveError, MotorHandle
from lib.sequence.positions import AxisSpec, load_position_table
from tests.fake_can import mock_can_manager
from tests.fake_drivers import StubFeedbackDriver

#: 実機の y_axis と同じ逆回転ペア。**単一モータ軸にしない** —— 保護が軸単位の
#: 指令 (`AxisHandle.set_target_value` 1 回) を通っているかは、左右 2 台ぶんが
#: 同じ 1 通に載ることでしか確かめられない
_SCALE = 2.0


class _Sensor:
    """1 本のスイッチ。**現在値と接触回数を別々に置ける。**

    実機のドライバでは接触回数は立ち上がりでしか増えないが、ここでは
    「回数だけが増えた」(ON 区間が観測周期より狭く、現在値としては一度も
    観測されなかった接触) と「現在値だけが ON」(触れっぱなしで立ち上がりは
    もう立たない) を**独立に作れる**ことが要点になる。ラッチ条件から
    どちらか一方を落とした実装は、対応するほうのテストでだけ落ちる。
    """

    def __init__(self) -> None:
        self.active = False
        self.count = 0
        self.stale = False

    def touch(self) -> None:
        """触れた (現在値も回数も動く)。"""
        self.active = True
        self.count += 1

    def brush(self) -> None:
        """観測の隙間を通り抜けた接触。**現在値は ON にならず回数だけが増える。**"""
        self.count += 1

    def release(self) -> None:
        self.active = False


def _spec(limits: list[dict[str, object]]) -> AxisSpec:
    table = load_position_table(
        {
            "axes": {
                "y_axis": {
                    "unit": "mm",
                    "command_unit": "deg",
                    "tolerance": 0.1,
                    "sync_tolerance": 100.0,
                    "limits": limits,
                    "motors": {"y_axis_r": {"scale": _SCALE}, "y_axis_l": {"scale": -_SCALE}},
                }
            },
            "positions": {"y_axis": {"home": 0.0}},
        },
        source="<test>",
    )
    return table.axis("y_axis")


class _Fixture:
    """保護 + 軸ハンドル + スイッチ。既定は「マイナス端に 1 本」。"""

    def __init__(
        self,
        limits: list[dict[str, object]] | None = None,
        *,
        start: float = 0.0,
        e_stop: bool = False,
    ) -> None:
        self.spec = _spec(limits or [{"sensor": "origin_sensor", "direction": -1}])
        self.sensors = {
            str(limit.sensor): _Sensor()  # type: ignore[attr-defined]
            for limit in self.spec.limits
        }
        self.e_stop = e_stop
        self.commands: list[float] = []

        mgr = mock_can_manager()
        self.drivers: dict[str, StubFeedbackDriver] = {}
        handles = []
        for motor in self.spec.motors:
            driver = StubFeedbackDriver(motor.name, 1)
            driver.set_observed(position=motor.to_command(start))
            self.drivers[motor.name] = driver
            handles.append(
                MotorHandle(motor.name, driver, mgr, is_estop_active=lambda: self.e_stop)
            )

        self.handle = AxisHandle(self.spec, handles)
        original = self.handle.set_target_value

        async def _record(commands):
            # 軸の単位へ戻して記録する (逆換算は AxisSpec に委ね、scale をテストへ
            # 書き写さない)。**左右 2 台が同じ 1 通に載っていること**もここで見る
            assert set(commands) == set(self.spec.motor_names)
            self.commands.append(self.spec.to_value(commands))
            return await original(commands)

        self.handle.set_target_value = _record  # type: ignore[method-assign]

        self.guard = LimitGuard(
            [self.spec],
            sensor_active=lambda name: self.sensors[name].active,
            sensor_contact_count=lambda name: self.sensors[name].count,
            sensor_is_stale=lambda name: self.sensors[name].stale,
            axis_handle=lambda name: self.handle if name == self.spec.name else None,
            interval_s=0.001,
        )

    @property
    def sensor(self) -> _Sensor:
        """スイッチが 1 本だけの構成でのその 1 本。"""
        return next(iter(self.sensors.values()))

    def place(self, value: float) -> None:
        """実測の軸位置を与える (順換算はモータ定義に委ねる)。"""
        for motor in self.spec.motors:
            self.drivers[motor.name].set_observed(position=motor.to_command(value))

    async def drive(self, value: float) -> None:
        """操縦者・シーケンス側からの指令 (保護を通さない生の指令)。

        保護が「目標を持っている軸」だけを止めることを見るために、実際に
        ``MotorHandle`` へ目標を持たせる必要がある。
        """
        await self.handle.set_target_value(self.spec.to_commands(value))
        self.commands.clear()

    async def steps(self, count: int = 1) -> None:
        for _ in range(count):
            await self.guard.step()


class TestStopsOnContact:
    """**触れたら止める。** 止め方は「目標を実測位置へ引き戻す」の一手だけ。"""

    async def test_触れたら目標が実測位置へ引き戻される(self) -> None:
        fx = _Fixture()
        await fx.drive(-50.0)
        fx.place(-10.0)
        fx.sensor.touch()

        await fx.steps()

        assert fx.commands == [pytest.approx(-10.0)]
        assert fx.guard.latched == {"y_axis": ("origin_sensor",)}

    async def test_電流0にも無励磁にもしない(self) -> None:
        """``sub_lift`` は保持ブレーキが無く、消磁すると自重で落ちる。"""
        fx = _Fixture()
        await fx.drive(-50.0)
        fx.place(-10.0)
        fx.sensor.touch()

        await fx.steps()

        # 目標は残ったまま (clear_target を使っていない)
        assert fx.handle.has_target is True

    async def test_触れていなければ何も送らない(self) -> None:
        """下のテストが「送らないから緑」ではないことの土台。"""
        fx = _Fixture()
        await fx.drive(-50.0)
        await fx.steps(5)
        assert fx.commands == []

    async def test_目標を持たない軸には書かない(self) -> None:
        """誰も駆動していないなら止めるものが無い。書けば
        「操作していないのに保持が始まる」。
        """
        fx = _Fixture()
        fx.place(-10.0)
        fx.sensor.touch()

        await fx.steps(5)

        assert fx.commands == []
        # ラッチ自体は立つ (以後この向きの指令は clamp が止める)
        assert fx.guard.blocked_directions("y_axis") == frozenset({-1.0})


class TestHoldIsWrittenOnce:
    """**書き直さない。** 毎周期実測を測り直すと、負荷で下がったぶんへ目標が
    追従して**誰も操作していないのに軸がクリープする**
    (``QueryDrivenTargetRefresher`` が ``idle_target_value`` をラッチする理由と同じ)。
    """

    async def test_毎周期書き直さない(self) -> None:
        fx = _Fixture()
        await fx.drive(-50.0)
        fx.place(-10.0)
        fx.sensor.touch()

        await fx.steps(10)

        assert len(fx.commands) == 1

    async def test_実測が動いても目標を追わない(self) -> None:
        """負荷で 1mm 下がったら、そこへ目標を書き直してはならない。"""
        fx = _Fixture()
        await fx.drive(-50.0)
        fx.place(-10.0)
        fx.sensor.touch()
        await fx.steps()

        fx.place(-11.0)
        await fx.steps(5)

        assert fx.commands == [pytest.approx(-10.0)]

    async def test_逆方向へ退避する指令を上書きしない(self) -> None:
        """触れたまま逆方向へ戻す指令は通さなければならない。書き直すと、
        操縦者が戻そうとしても保護が毎周期その場へ引き戻し、**端から出られなくなる。**
        """
        fx = _Fixture()
        await fx.drive(-50.0)
        fx.place(-10.0)
        fx.sensor.touch()
        await fx.steps()

        await fx.drive(20.0)  # 逆方向へ退避
        await fx.steps(5)

        assert fx.commands == []


class TestLatchCondition:
    """ラッチ条件は「回数が増えた」**または**「今 ON」。片方だけでは足りない。"""

    async def test_触れっぱなしでもラッチが続く(self) -> None:
        """立ち上がりは 1 回しか立たないので、回数だけを見る実装は次の周期で解除する。"""
        fx = _Fixture()
        fx.sensor.touch()
        await fx.steps(5)

        assert fx.guard.blocked_directions("y_axis") == frozenset({-1.0})

    async def test_観測の隙間を通り抜けた接触でもラッチする(self) -> None:
        """ON 区間が観測周期より狭いと「今 ON か」では 1 度も見えない。実機で
        「スイッチに当たっているのに止まらず端を越えて回り続けた」壊れ方が起きている。
        """
        fx = _Fixture()
        await fx.drive(-50.0)
        fx.place(-10.0)
        await fx.steps()  # 基準値を取る

        fx.sensor.brush()  # 現在値は OFF のまま、回数だけが増える
        await fx.steps()

        assert fx.guard.blocked_directions("y_axis") == frozenset({-1.0})
        assert fx.commands == [pytest.approx(-10.0)]

    async def test_離れたらラッチが解除される(self) -> None:
        """明示操作を要求すると、逆方向へ退避できたのにボタンを押すまで
        動かせない軸ができる。
        """
        fx = _Fixture()
        fx.sensor.touch()
        await fx.steps(2)
        assert fx.guard.latched

        fx.sensor.release()
        await fx.steps()

        assert fx.guard.latched == {}
        assert fx.guard.blocked_directions("y_axis") == frozenset()

    async def test_同じ周期で入って出たら解除しない(self) -> None:
        """回数が増えていれば、現在値が OFF でも「また触れた」ということ。"""
        fx = _Fixture()
        fx.sensor.touch()
        await fx.steps(2)

        fx.sensor.release()
        fx.sensor.brush()
        await fx.steps()

        assert fx.guard.blocked_directions("y_axis") == frozenset({-1.0})


class TestStaleSensor:
    """**途絶では判定しない。かつ黙って無効にしない。**

    途絶で止めるとスイッチ 1 本の不調で試合中に軸が動かなくなる。「触れていない」と
    読むと保護が黙って消える。だから「判定しないが必ず見える」を選ぶ。
    """

    async def test_途絶中は止めない(self) -> None:
        fx = _Fixture()
        await fx.drive(-50.0)
        fx.place(-10.0)
        fx.sensor.stale = True
        fx.sensor.touch()

        await fx.steps(5)

        assert fx.commands == []
        assert fx.guard.blocked_directions("y_axis") == frozenset()

    async def test_途絶センサは保護が効いていないものとして見える(self) -> None:
        fx = _Fixture()
        fx.sensor.stale = True
        await fx.steps()

        assert fx.guard.blind_sensors == ("origin_sensor",)

    async def test_健全なセンサは載らない(self) -> None:
        fx = _Fixture()
        fx.sensor.touch()
        await fx.steps()
        assert fx.guard.blind_sensors == ()

    async def test_途絶してもラッチは落とさない(self) -> None:
        """既に成立している保護を、センサが黙ったことを理由に外さない。"""
        fx = _Fixture()
        fx.sensor.touch()
        await fx.steps()
        assert fx.guard.blocked_directions("y_axis") == frozenset({-1.0})

        fx.sensor.stale = True
        fx.sensor.release()
        await fx.steps(5)

        assert fx.guard.blocked_directions("y_axis") == frozenset({-1.0})


class TestClamp:
    """**判定の単一情報源。** 指令の入口層もここを呼ぶ。"""

    async def test_禁止方向は実測で頭打ちになる(self) -> None:
        fx = _Fixture()
        fx.place(-10.0)
        fx.sensor.touch()
        await fx.steps()

        assert fx.guard.clamp("y_axis", -50.0, -10.0) == pytest.approx(-10.0)

    async def test_逆方向の指令は通る(self) -> None:
        """**復帰不能な軸を作らないことがこの保護の前提。** 符号を落とすと
        「触れた端から出られない」機体になる。
        """
        fx = _Fixture()
        fx.place(-10.0)
        fx.sensor.touch()
        await fx.steps()

        assert fx.guard.clamp("y_axis", 20.0, -10.0) == pytest.approx(20.0)

    async def test_ラッチしていなければ素通し(self) -> None:
        fx = _Fixture()
        assert fx.guard.clamp("y_axis", -50.0, -10.0) == pytest.approx(-50.0)

    async def test_保護の無い軸は素通し(self) -> None:
        fx = _Fixture()
        fx.sensor.touch()
        await fx.steps()
        assert fx.guard.clamp("rotate", -50.0, 0.0) == pytest.approx(-50.0)

    async def test_両端に触れたら両方向が塞がる(self) -> None:
        fx = _Fixture(
            [
                {"sensor": "front_sensor", "direction": -1},
                {"sensor": "rear_sensor", "direction": 1},
            ]
        )
        for sensor in fx.sensors.values():
            sensor.touch()
        await fx.steps()

        assert fx.guard.clamp("y_axis", -50.0, 0.0) == pytest.approx(0.0)
        assert fx.guard.clamp("y_axis", 50.0, 0.0) == pytest.approx(0.0)


class TestSuspendSensors:
    """零点確定は「当たるまで動かす」動作なので、そのあいだ保護を外す。

    **軸ではなくセンサ単位。** 両端にスイッチのある軸で軸まるごと外すと、
    探索が空振りしたときに反対端を守るものが 1 つも無くなる。
    """

    def _both_ends(self) -> _Fixture:
        return _Fixture(
            [
                {"sensor": "front_sensor", "direction": -1},
                {"sensor": "rear_sensor", "direction": 1},
            ]
        )

    async def test_一時停止中は発動しない(self) -> None:
        fx = _Fixture()
        await fx.drive(-50.0)
        fx.place(-10.0)

        with fx.guard.suspend_sensors(["origin_sensor"]):
            fx.sensor.touch()
            await fx.steps(5)
            assert fx.commands == []
            assert fx.guard.blocked_directions("y_axis") == frozenset()

    async def test_入る時点のラッチも落とす(self) -> None:
        """読み取り側だけで除くと、一時停止から次の周期までのあいだ ``clamp`` が
        古いラッチで指令を止める —— 零点確定の 1 歩目がちょうどその窓に入る。
        """
        fx = _Fixture()
        fx.sensor.touch()
        await fx.steps()
        assert fx.guard.blocked_directions("y_axis") == frozenset({-1.0})

        with fx.guard.suspend_sensors(["origin_sensor"]):
            assert fx.guard.blocked_directions("y_axis") == frozenset()
            assert fx.guard.clamp("y_axis", -50.0, -10.0) == pytest.approx(-50.0)

    async def test_抜けたら判定が戻る(self) -> None:
        """**戻し忘れると、その後の試合中ずっと保護が死んだまま残る。**"""
        fx = _Fixture()
        await fx.drive(-50.0)
        fx.place(-10.0)
        with fx.guard.suspend_sensors(["origin_sensor"]):
            fx.sensor.touch()
            await fx.steps(3)

        await fx.steps()

        assert fx.guard.blocked_directions("y_axis") == frozenset({-1.0})
        assert fx.commands == [pytest.approx(-10.0)]

    async def test_例外で抜けても判定が戻る(self) -> None:
        fx = _Fixture()
        with pytest.raises(RuntimeError), fx.guard.suspend_sensors(["origin_sensor"]):
            raise RuntimeError("探索に失敗")

        assert fx.guard.is_suspended("origin_sensor") is False

    async def test_入れ子は内側が抜けても戻らない(self) -> None:
        fx = _Fixture()
        with fx.guard.suspend_sensors(["origin_sensor"]):
            with fx.guard.suspend_sensors(["origin_sensor"]):
                pass
            assert fx.guard.is_suspended("origin_sensor") is True
        assert fx.guard.is_suspended("origin_sensor") is False

    async def test_止めたセンサ以外の保護は残る(self) -> None:
        """両端にスイッチのある軸で、反対端の保護まで消してはならない。"""
        fx = self._both_ends()
        await fx.drive(50.0)
        fx.place(10.0)

        with fx.guard.suspend_sensors(["front_sensor"]):
            fx.sensors["rear_sensor"].touch()
            await fx.steps()

            assert fx.guard.blocked_directions("y_axis") == frozenset({1.0})
            assert fx.commands == [pytest.approx(10.0)]

    async def test_止めている間の接触をまとめて拾わない(self) -> None:
        """基準値を進めずにいると、抜けた次の周期で「増えた」と読んでいきなり
        ラッチする (零点確定は必ずスイッチに触れて終わるので毎回踏む)。
        """
        fx = _Fixture()
        await fx.drive(-50.0)
        with fx.guard.suspend_sensors(["origin_sensor"]):
            fx.sensor.brush()
            fx.sensor.brush()
            await fx.steps(3)

        await fx.steps()

        assert fx.guard.blocked_directions("y_axis") == frozenset()

    async def test_知らないセンサ名は拒否する(self) -> None:
        """呼び出し側の取り違えを黙って通すと、保護を外したつもりで外れていない。"""
        fx = _Fixture()
        with pytest.raises(KeyError), fx.guard.suspend_sensors(["rotate_origin_sensor"]):
            pass


class TestReset:
    async def test_ラッチを落とすが判定は無効化しない(self) -> None:
        fx = _Fixture()
        fx.sensor.touch()
        await fx.steps()
        assert fx.guard.latched

        fx.guard.reset()
        assert fx.guard.latched == {}

        # まだ触れているので次の周期で再びラッチする
        await fx.steps()
        assert fx.guard.blocked_directions("y_axis") == frozenset({-1.0})


class TestFailureContainment:
    """1 軸の失敗で監視まで死なせない (`_dispatch_frame` と同じ封じ込め粒度)。"""

    async def test_送信に失敗しても次の周期が回る(self) -> None:
        fx = _Fixture()
        await fx.drive(-50.0)
        fx.place(-10.0)

        async def _fail(_commands):
            raise RuntimeError("CAN 送信に失敗")

        fx.handle.set_target_value = _fail  # type: ignore[method-assign]
        fx.sensor.touch()
        await fx.steps()

        assert fx.guard.blocked_directions("y_axis") == frozenset({-1.0})
        await fx.steps()

    async def test_緊急停止中は送らないがラッチは続く(self) -> None:
        """停止中は「送らないことが正常」なので例外にしない。**ラッチは続ける** ——
        落とすと、解除した瞬間に端へ触れたままの軸が無防備になる。
        """
        fx = _Fixture()
        await fx.drive(-50.0)
        fx.place(-10.0)
        fx.e_stop = True
        fx.sensor.touch()

        # EStopActiveError が漏れると PeriodicTask の例外ログに化ける
        await fx.steps()
        assert fx.guard.blocked_directions("y_axis") == frozenset({-1.0})

    async def test_estop中の指令はそもそも拒否される(self) -> None:
        """上のテストが「例外が出ないから緑」ではないことの土台。"""
        fx = _Fixture(e_stop=True)
        with pytest.raises(EStopActiveError):
            await fx.handle.set_target_value(fx.spec.to_commands(1.0))
