"""リミットスイッチ保護の状態を配信する (`safety.limit_latched` / `limit_blind_sensors`)。

**保護が働いたことも、保護が効いていないことも、画面から読めなければならない。**
保護は軸ローカルで全体緊急停止に倒さない (端に触れた軸を戻す操作そのものを塞がない
ため) ので、配信しなければ操縦者から見えるのは「その向きへ指令しても動かない」だけに
なり、原因を示すものが画面のどこにも無い。途絶で保護が消えていることも同じで、
黙って無効になれば「守っているつもり」の機体ができる。

**この層だけを見る。** 判定そのもの (何をラッチするか・何を blind と読むか) は
`tests/test_limit_guard.py` が単独で確かめている。ここで見るのは
「`LimitGuard` が持っている状態が `safety` に載って出ていくか」だけである。
"""

from __future__ import annotations

from lib.control.limit_guard import LimitGuard
from lib.sequence.engine import Sequence, step
from lib.sequence.positions import load_position_table
from tests.server_fixtures import ServerFixture

_AXIS = "y_axis"
_SENSOR = "y_axis_r_origin_sensor"


class _DummySequence(Sequence):
    @step("ノーオペ")
    async def noop(self) -> None:
        return None


class _Switch:
    """1 本のスイッチ。現在値・接触回数・途絶を独立に置ける。"""

    def __init__(self) -> None:
        self.active = False
        self.count = 0
        self.stale = False

    def touch(self) -> None:
        self.active = True
        self.count += 1

    def release(self) -> None:
        self.active = False


class _IdleAxis:
    """目標を 1 つも持っていない軸の指令口。

    保護は「誰も駆動していない軸」には何も書かない (書けば操作していないのに保持が
    始まる) ので、**判定とラッチだけを走らせたいここでは指令経路を持たせない**。
    引き戻しそのものは `tests/test_limit_guard.py` が実物の `AxisHandle` で見ている。
    """

    has_target = False


def _guard(switches: dict[str, _Switch]) -> LimitGuard:
    table = load_position_table(
        {
            "axes": {
                _AXIS: {
                    "unit": "mm",
                    "command_unit": "deg",
                    "tolerance": 0.1,
                    "limits": [{"sensor": name, "direction": -1} for name in switches],
                    "motors": {"y_axis_r": {"scale": 2.0}},
                }
            },
            "positions": {_AXIS: {"home": 0.0}},
        },
        source="<test>",
    )
    return LimitGuard(
        [table.axis(_AXIS)],
        sensor_active=lambda name: switches[name].active,
        sensor_contact_count=lambda name: switches[name].count,
        sensor_is_stale=lambda name: switches[name].stale,
        axis_handle=lambda _name: _IdleAxis(),  # type: ignore[arg-type,return-value]
    )


async def _fixture(
    switches: dict[str, _Switch], **build: object
) -> tuple[ServerFixture, LimitGuard]:
    fx = ServerFixture.build(**build)
    guard = _guard(switches)
    fx.add_robot("main_hand", _DummySequence("main_hand"), limit_guard=guard)
    return fx, guard


def _safety(fx: ServerFixture) -> dict:
    return fx.state_message("main_hand")["safety"]


class TestLatchedAxesAreBroadcast:
    async def test_ラッチ中の軸とセンサが載る(self) -> None:
        switch = _Switch()
        fx, guard = await _fixture({_SENSOR: switch})
        switch.touch()
        await guard.step()

        assert _safety(fx)["limit_latched"] == {_AXIS: [_SENSOR]}

    async def test_触れていなければ空(self) -> None:
        """**平常時に静かであること。** 0 件で欄が埋まると、画面は毎試合ずっと
        保護のチップを出し続け、本当に止まった 1 回が埋もれる。
        """
        fx, _ = await _fixture({_SENSOR: _Switch()})
        assert _safety(fx)["limit_latched"] == {}

    async def test_離れたら消える(self) -> None:
        """ラッチは自動解除される (明示操作を要求しない)。配信もそれに追従する。"""
        switch = _Switch()
        fx, guard = await _fixture({_SENSOR: switch})
        switch.touch()
        await guard.step()
        switch.release()
        await guard.step()

        assert _safety(fx)["limit_latched"] == {}

    async def test_介入回数は載せない(self) -> None:
        """**手動で端へ寄せるたびに増える数**なので、シーケンス由来と手動由来が
        混ざり操縦者には読めない。載せるのは「今どの軸がどちら側で止まっているか」だけ。
        """
        switch = _Switch()
        fx, guard = await _fixture({_SENSOR: switch})
        switch.touch()
        await guard.step()
        for _ in range(3):
            guard.clamp(_AXIS, -50.0, 0.0)
        assert guard.intervention(_AXIS).count > 0

        # 値そのものが「軸名 → センサ名の一覧」だけであることを固定する
        assert _safety(fx)["limit_latched"] == {_AXIS: [_SENSOR]}

    async def test_保護を持たないロボットでも欄は出る(self) -> None:
        """欄ごと消すと、受信境界は「読めなかった配信」として異常側へ倒す。"""
        fx = ServerFixture.build()
        fx.add_robot("main_hand", _DummySequence("main_hand"))

        assert _safety(fx)["limit_latched"] == {}
        assert _safety(fx)["limit_blind_sensors"] == []


class TestBlindSensorsAreBroadcast:
    """途絶で保護が効いていないことは、必ず見えなければならない。

    **これは「壊れている」ではなく「保護が働いていない」の報告**
    (`firmware_unconfirmed_motors` と同じ位置付け) なので、ヘルス判定は動かさない。
    """

    async def test_途絶したセンサが載る(self) -> None:
        switch = _Switch()
        switch.stale = True
        fx, guard = await _fixture({_SENSOR: switch})
        await guard.step()

        assert _safety(fx)["limit_blind_sensors"] == [_SENSOR]

    async def test_鮮度が戻れば消える(self) -> None:
        switch = _Switch()
        switch.stale = True
        fx, guard = await _fixture({_SENSOR: switch})
        await guard.step()
        switch.stale = False
        await guard.step()

        assert _safety(fx)["limit_blind_sensors"] == []

    async def test_dry_run_では出さない(self) -> None:
        """virtual バスはセンサの `FEEDBACK` を 1 通も返さないので、机上では全センサが
        恒久的に並び、画面を確かめられなくなる (`firmware_unconfirmed_motors` と同じ)。
        """
        switch = _Switch()
        switch.stale = True
        fx, guard = await _fixture({_SENSOR: switch}, dry_run=True)
        await guard.step()

        assert _safety(fx)["limit_blind_sensors"] == []


class TestEStopReleaseDoesNotResetLatch:
    async def test_緊急停止の解除でラッチが消えない(self) -> None:
        """**`_reset_sync_latches` へ相乗りさせない。**

        まだ触れていれば次の周期でどのみち再ラッチし、離れていれば既に自動解除で
        外れている。解除で落とすと、その 1 周期だけ「触れているのに画面は平常」に
        なり、しかも解除操作がリミットのラッチを外せると読める形が残る。
        """
        switch = _Switch()
        fx, guard = await _fixture({_SENSOR: switch})
        switch.touch()
        await guard.step()

        await fx.activate_e_stop(reason="テスト")
        await fx.command({"type": "e_stop_release"})
        await fx.wait_reactivation()

        assert _safety(fx)["limit_latched"] == {_AXIS: [_SENSOR]}
