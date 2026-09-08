"""指令を出してよいかの判断 (lib/motion_guard.py)。

見るのは 4 点で、いずれも 2026-09-09 に実機で踏んだ事故に対応する:

- 押されている端へは進まない / **離れる向きは必ず通す**
- 読めていないセンサを「押されていない」へ丸めない
- 桁の違う目標 (スケール・固定小数点レンジの取り違え) をここで止める
- トルクが立ったら止める。ただし測れないモータでは何も言わない
"""

from __future__ import annotations

import pytest

from lib.motion_guard import GuardViolation, LimitSpec, MotionGuard, MotionGuardSpec


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
