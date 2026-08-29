"""実行中の PID ゲイン差し替え (/pid-tuning タブの土台)。

M3508 の位置制御は PC 側の PIDController が担うため、ゲインは実行中に書き換えられる。
書き換えで機体が跳ねないこと・左右直結ペアが片側だけ別特性にならないことを担保する。
"""

from __future__ import annotations

import can
import pytest

from lib.axis_sync import MotorSpec, SyncGroup
from lib.control.pid import PIDController
from lib.control.position_loop import M3508PositionLoop, make_position_pid
from lib.drivers.m3508 import M3508Driver

BUS = "m3508_bus"


class _StubCANManager:
    def __init__(self) -> None:
        self.sent: list[tuple[str, can.Message]] = []

    async def send_to_bus(self, bus_name: str, msg: can.Message) -> None:
        self.sent.append((bus_name, msg))

    def last_feedback_at(self, motor_name: str) -> float | None:
        return None


class TestPidSetGains:
    def test_kp_kd_are_replaced(self) -> None:
        pid = PIDController(1.0, 0.0, 0.0)
        pid.set_gains(kp=3.0, kd=0.5)
        assert pid.kp == 3.0
        assert pid.kd == 0.5
        assert pid.ki == 0.0

    def test_integral_contribution_is_preserved_when_ki_changes(self) -> None:
        """ki を変えても積分項の出力寄与を保つ (バンプレス)。

        そのまま残すと ki の変化分だけ出力が段差になり、クリアすると昇降軸の
        保持電流が抜ける。どちらも危険なので積分器側を逆比例で作り直す。
        """
        pid = PIDController(0.0, ki=1.0, kd=0.0)
        pid.update(setpoint=10.0, measurement=0.0, dt=0.1)
        before = pid.ki * pid.integral
        assert before != 0.0

        pid.set_gains(ki=4.0)
        assert pid.ki == 4.0
        assert pid.ki * pid.integral == pytest.approx(before)

    def test_output_does_not_jump_on_gain_change(self) -> None:
        """ゲイン差し替え直後の出力が段差にならないこと。"""
        pid = PIDController(0.0, ki=1.0, kd=0.0)
        for _ in range(10):
            pid.update(setpoint=10.0, measurement=10.0, dt=0.1)
        # 偏差 0 なので出力は積分寄与のみ
        pid.update(setpoint=10.0, measurement=0.0, dt=0.1)
        before = pid.last_output

        pid.set_gains(ki=10.0)
        after = pid.update(setpoint=0.0, measurement=0.0, dt=0.0)
        # dt=0 では内部状態を更新せず前回出力を返す。出力の連続性だけを見る
        assert after == pytest.approx(before)

    def test_zero_ki_clears_integral(self) -> None:
        """ki=0 にしたら積分器も捨てる。

        寄与が 0 になった積分を残すと、後で ki を戻した瞬間に古い蓄積が出力へ乗る。
        """
        pid = PIDController(0.0, ki=1.0, kd=0.0)
        pid.update(setpoint=10.0, measurement=0.0, dt=0.1)
        assert pid.integral != 0.0

        pid.set_gains(ki=0.0)
        assert pid.integral == 0.0

    def test_prev_measurement_is_kept(self) -> None:
        """前回測定値は捨てない (捨てると次の周期で D 項が 0 になり制動が抜ける)。"""
        pid = PIDController(1.0, 0.0, kd=1.0)
        pid.update(setpoint=0.0, measurement=0.0, dt=0.1)
        pid.set_gains(kp=2.0)
        out = pid.update(setpoint=0.0, measurement=1.0, dt=0.1)
        # kd=1.0, 測定値が 0→1 に動いた分の微分制動が効いていること
        assert out < 0.0


def _build_loop() -> tuple[M3508PositionLoop, _StubCANManager]:
    manager = _StubCANManager()
    loop = M3508PositionLoop(manager, BUS)  # type: ignore[arg-type]
    for name, can_id in (("y_axis_r", 1), ("y_axis_l", 2), ("solo", 3)):
        loop.add_motor(name, M3508Driver(name, can_id), make_position_pid(2.0))
    loop.add_sync_group(
        SyncGroup(
            name="y_axis",
            members=(
                MotorSpec(name="y_axis_r", scale=1.0, offset=0.0),
                MotorSpec(name="y_axis_l", scale=-1.0, offset=0.0),
            ),
            tolerance=5.0,
        )
    )
    return loop, manager


class TestPositionLoopSetPidGains:
    def test_unknown_motor_raises_key_error(self) -> None:
        loop, _ = _build_loop()
        with pytest.raises(KeyError):
            loop.set_pid_gains("nope", {"kp": 1.0})

    def test_unknown_key_raises_value_error(self) -> None:
        loop, _ = _build_loop()
        with pytest.raises(ValueError, match="kp"):
            loop.set_pid_gains("solo", {"integral_limit": 1.0})

    def test_empty_request_raises_value_error(self) -> None:
        """1 つも指定しない差し替えは誤送信。黙って成功させない。"""
        loop, _ = _build_loop()
        with pytest.raises(ValueError):
            loop.set_pid_gains("solo", {})

    def test_solo_motor_is_updated_alone(self) -> None:
        loop, _ = _build_loop()
        affected = loop.set_pid_gains("solo", {"kp": 7.5})
        assert affected == ("solo",)
        assert loop.pid("solo").kp == 7.5
        assert loop.pid("y_axis_r").kp == 2.0

    def test_three_gains_are_applied_together(self) -> None:
        """3 値は 1 回で入れる。分けて入れると混ざった状態が周期をまたいで残る。"""
        loop, _ = _build_loop()
        affected = loop.set_pid_gains("solo", {"kp": 1.5, "ki": 0.1, "kd": 0.05})

        assert affected == ("solo",)
        pid = loop.pid("solo")
        assert (pid.kp, pid.ki, pid.kd) == (1.5, 0.1, 0.05)

    def test_partial_request_leaves_the_others_alone(self) -> None:
        loop, _ = _build_loop()
        loop.set_pid_gains("solo", {"kp": 1.5, "ki": 0.1, "kd": 0.05})
        loop.set_pid_gains("solo", {"kd": 0.2})

        pid = loop.pid("solo")
        assert (pid.kp, pid.ki, pid.kd) == (1.5, 0.1, 0.2)

    def test_sync_group_members_share_the_gain(self) -> None:
        """左右直結ペアは必ず同じゲインにする。

        追従特性が左右で変わると押し合いになって機構が壊れる。UI は 1 基ずつしか
        送れないため、片側だけ変わった状態がサーバー側で作れてはならない。
        """
        loop, _ = _build_loop()
        affected = loop.set_pid_gains("y_axis_r", {"kp": 4.0})
        assert set(affected) == {"y_axis_r", "y_axis_l"}
        assert loop.pid("y_axis_r").kp == 4.0
        assert loop.pid("y_axis_l").kp == 4.0


class TestPositionLoopPidGains:
    """UI へ配る現在ゲインの読み口。

    書き込み側 (`set_pid_gain`) と対で要る。読み口が無いと UI は現在値を知る手段が
    無く、初期値 0 のまま送って全ゲインを 0 で潰す (実際にこの事故があった)。
    """

    def test_reports_the_gains_actually_in_effect(self) -> None:
        loop, _ = _build_loop()
        loop.set_pid_gains("solo", {"kp": 3.0, "ki": 0.5, "kd": 0.25})

        gains = loop.pid_gains("solo")

        assert gains["kp"] == 3.0
        assert gains["ki"] == 0.5
        assert gains["kd"] == 0.25

    def test_solo_motor_applies_to_itself_only(self) -> None:
        loop, _ = _build_loop()
        assert loop.pid_gains("solo")["applies_to"] == ["solo"]

    def test_pair_member_reports_both_sides(self) -> None:
        """左右直結ペアは「送ると両方に適用される」ことまで配る。

        判断の正は `_paired_with()` の 1 箇所。UI に名前から推測させると
        「1 台だけに効かせてよいか」の判断が 2 箇所に増える。
        """
        loop, _ = _build_loop()
        assert loop.pid_gains("y_axis_r")["applies_to"] == ["y_axis_r", "y_axis_l"]
        assert loop.pid_gains("y_axis_l")["applies_to"] == ["y_axis_r", "y_axis_l"]

    def test_unknown_motor_raises_key_error(self) -> None:
        loop, _ = _build_loop()
        with pytest.raises(KeyError):
            loop.pid_gains("nope")
