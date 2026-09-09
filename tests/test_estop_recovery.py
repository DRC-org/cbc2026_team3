"""物理緊急停止(モータ電源が落ちる)から E-STOP 解除で復帰するまでの通し。

層をまたぐので統合寄りだが、**復帰できるかどうかは 1 つの層だけでは決まらない** ——
生角度の連続化(ドライバ)・原点を控え直さないこと(再励磁経路)・偏差判定
(`SyncGroup`)の 3 つが揃って初めて「解除したら即また止まる」が消える。
単独のテストは `tests/drivers/test_edulite05.py` と `tests/test_can_manager.py` にある。
"""

from __future__ import annotations

import math

import can
import pytest

from lib.axis_sync import MotorSpec, SyncGroup
from lib.can_manager import CANManager
from lib.drivers.edulite05 import Edulite05Driver, Edulite05ModeState
from tests.fake_can import deliver_frame, direct_runner, mock_bus
from tests.feedback_frames import edulite_feedback

_DEG = math.radians(1.0)

# 2026-09-09 の実測。電源投入で `rotate_l` の報告値が -181.7142deg → +178.2634deg へ
# 飛んだ(機構は 1LSB も動いていない)。左右の機械ゼロ差は 175.879deg。
_RAW_BEFORE = {"rotate_r": -5.8352, "rotate_l": -181.7142}
_RAW_AFTER = {"rotate_r": -5.8352, "rotate_l": 178.2634}

# `config/main_hand_positions.yaml` の rotate と同じ(逆回転ペアを scale の符号で表す)
_GROUP = SyncGroup(
    name="rotate",
    members=(
        MotorSpec(name="rotate_r", scale=_DEG, offset=0.0),
        MotorSpec(name="rotate_l", scale=-_DEG, offset=0.0),
    ),
    tolerance=5.0,
)


def _violation(mgr: CANManager) -> float | None:
    # `SyncMonitor` が毎周期組み立てるのと同じ形(論理位置で見る)
    return _GROUP.violation(
        {name: mgr.motors[name].feedback_position() for name in ("rotate_r", "rotate_l")}
    )


def _manager() -> tuple[CANManager, dict[str, float]]:
    """`posture` の値を書き換えると、機構の姿勢と電文の畳まれ方を作れる。"""
    mgr = CANManager(run_blocking=direct_runner())
    mgr.add_bus("can_edulite", mock_bus())
    posture = dict(_RAW_BEFORE)
    for name, can_id in (("rotate_r", 0x11), ("rotate_l", 0x12)):
        mgr.add_motor("can_edulite", Edulite05Driver(name, can_id=can_id, set_zero_on_start=True))

    async def _send(motor_name: str, _msg: can.Message) -> None:
        # 本機のフィードバックは問い合わせ駆動。自分宛の 1 通に 1 通返る
        deliver_frame(
            mgr,
            "can_edulite",
            edulite_feedback(
                mgr.motors[motor_name],
                position=math.radians(posture[motor_name]),
                # 電源が落ちた個体は復帰後 RESET を報告する(起動直後も同じ)
                mode_state=Edulite05ModeState.RESET,
            ),
        )

    mgr.send = _send  # type: ignore[method-assign]
    return mgr, posture


async def test_物理緊急停止から復帰しても同期ずれが立たない() -> None:
    """**これが立つと、解除するたび即座に全体緊急停止が掛かって復帰できない。**

    実機では左右差 360.2853deg(`sync_tolerance` 5deg)になり、操縦者からは
    「解除ボタンが効かない」としか見えない。
    """
    mgr, posture = _manager()

    assert await mgr.initialize_motors() == []
    origins = {name: mgr.motors[name].origin_offset for name in ("rotate_r", "rotate_l")}
    assert _violation(mgr) is None, "起動時の暫定原点が左右の機械ゼロ差を消す"

    # 物理緊急停止: モータ電源が数秒落ち、そのあいだフィードバックは 1 通も届かない。
    # 復帰後の最初のフレームは [0, 360) へ畳み直された値で来る。
    posture.update(_RAW_AFTER)

    assert await mgr.activate_motors() == []

    for name in ("rotate_r", "rotate_l"):
        driver = mgr.motors[name]
        assert driver.origin_offset == pytest.approx(origins[name]), (
            "再励磁で原点を控え直してはならない"
        )
        assert driver.feedback_position() == pytest.approx(0.0, abs=2 * _DEG), (
            "機構は 1LSB も動いていないので論理位置も動いてはならない"
        )
    assert _violation(mgr) is None
