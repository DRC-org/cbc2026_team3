from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, patch

import can

from lib.drivers.base import MotorDriver, MotorState
from lib.health import MotorCheckResult
from lib.motor_check import (
    DEFAULT_MAGNITUDES,
    DEFAULT_PER_MOTOR_TIMEOUT_MS,
    SKIP_DETAIL_CONFIG_EXCLUDED,
    SKIP_DETAIL_UNSUPPORTED_DRIVER,
    MotorCheckRunner,
)

# ---------------------------------------------------------------------- #
#  テスト用ダミー実装
# ---------------------------------------------------------------------- #


class _MockMotor(MotorDriver):
    """MotorCheckRunner のロジックだけを検証するための最小ドライバ実装。

    本物のドライバが返す check_command/evaluate_check_result の挙動を
    コンストラクタ引数で差し替えられるようにし、CAN プロトコルに依存しない。
    """

    def __init__(
        self,
        name: str,
        can_id: int = 0,
        *,
        evaluate_passed: bool = True,
        evaluate_detail: str | None = None,
        observed_position: float = 0.0,
        observed_velocity: float = 0.0,
        target_value: float = 1.0,
    ) -> None:
        super().__init__(name, can_id)
        self.evaluate_passed = evaluate_passed
        self.evaluate_detail = evaluate_detail
        self.target_value = target_value
        self._observed_position = observed_position
        self._observed_velocity = observed_velocity
        # check_command が呼ばれた最後の magnitude を記録 (override 検証用)
        self.last_magnitude: float | None = None
        # 動作確認後の reset_after_check が呼ばれた回数 (常に呼ばれることの検証用)
        self.reset_calls = 0

    def encode_target(self, mode, value):  # pragma: no cover - 本テストでは未使用
        return can.Message(arbitration_id=0x100 + self.can_id, data=bytes(8))

    def decode_feedback(self, msg: can.Message) -> MotorState:  # pragma: no cover
        return self._state

    def matches_feedback(self, msg: can.Message) -> bool:  # pragma: no cover
        return False

    def check_command(self, *, magnitude: float) -> tuple[can.Message, dict]:
        self.last_magnitude = magnitude
        msg = can.Message(arbitration_id=0x100 + self.can_id, data=bytes(8))
        return msg, {"target": self.target_value, "mode": "current"}

    def evaluate_check_result(
        self,
        state: MotorState,
        context: dict,
        *,
        tolerance: float | None = None,
    ) -> tuple[bool, str | None]:
        return self.evaluate_passed, self.evaluate_detail

    def reset_after_check(self) -> can.Message:
        self.reset_calls += 1
        return can.Message(arbitration_id=0x100 + self.can_id, data=bytes(8))

    def set_observed(self, position: float = 0.0, velocity: float = 0.0) -> None:
        self._state = MotorState(position=position, velocity=velocity)


class _MockCANManager:
    """CANManager の MotorCheckRunner から触られる API のみ実装したスタブ。

    `_last_rx_at` を直接書き換えることでフィードバック受信を瞬時に模擬する。
    `send` 後に自動でフィードバック更新するフックを切り替えできるようにする。
    """

    def __init__(self, motors: dict[str, MotorDriver]) -> None:
        self._motors = motors
        self._motor_bus = {name: "test_bus" for name in motors}
        self._last_rx_at: dict[str, float] = {}
        # send が成功するたびに呼ばれるフック (フィードバック模擬)
        self._post_send_hook: Any = None
        self._send_failures: dict[str, Exception] = {}
        # 全 send 履歴 (順序とメッセージ確認用)
        self.sent: list[tuple[str, can.Message]] = []

    def get_motor(self, name: str) -> MotorDriver:
        return self._motors[name]

    def set_post_send_hook(self, hook) -> None:
        self._post_send_hook = hook

    def set_send_failure(self, motor_name: str, exc: Exception) -> None:
        self._send_failures[motor_name] = exc

    def set_rx_at(self, motor_name: str, ts: float) -> None:
        self._last_rx_at[motor_name] = ts

    async def send(self, motor_name: str, msg: can.Message) -> None:
        self.sent.append((motor_name, msg))
        if motor_name in self._send_failures:
            raise self._send_failures[motor_name]
        if self._post_send_hook is not None:
            await self._post_send_hook(motor_name, msg)


# ---------------------------------------------------------------------- #
#  個別ケース
# ---------------------------------------------------------------------- #


class TestMotorCheckRunnerBasics:
    """MotorCheckRunner の基本 API 形状と既定値の検証。"""

    def test_module_constants(self) -> None:
        assert DEFAULT_PER_MOTOR_TIMEOUT_MS == 1500.0
        assert DEFAULT_MAGNITUDES == {"m3508": 500.0, "edulite05": 5.0, "generic": 0.1}

    def test_initial_snapshot_state(self) -> None:
        motors = {"m1": _MockMotor("m1")}
        manager = _MockCANManager(motors)
        runner = MotorCheckRunner(
            "main_hand",
            manager,
            motors,
            default_magnitude={"_MockMotor": 1.0},
        )
        assert runner.is_running is False
        snap = runner.snapshot
        assert snap.robot == "main_hand"
        assert snap.records == []


class TestMotorCheckRunnerHappyPath:
    async def test_all_motors_passed_overall_ok(self) -> None:
        motors = {
            "m1": _MockMotor("m1", evaluate_passed=True),
            "m2": _MockMotor("m2", evaluate_passed=True),
            "m3": _MockMotor("m3", evaluate_passed=True),
        }
        manager = _MockCANManager(motors)

        async def hook(motor_name: str, _msg: can.Message) -> None:
            # 即時フィードバックを模擬: send 完了直後に rx を進める
            manager.set_rx_at(motor_name, time.time() + 0.001)

        manager.set_post_send_hook(hook)
        runner = MotorCheckRunner(
            "main_hand",
            manager,
            motors,
            default_magnitude={"_MockMotor": 1.0},
        )

        snap = await runner.run()
        assert snap.overall == "ok"
        assert all(r.result is MotorCheckResult.PASSED for r in snap.records)
        assert len(snap.records) == 3

    async def test_prepare_messages_are_sent_before_check_command(self) -> None:
        class _PreparingMotor(_MockMotor):
            def prepare_check(self) -> list[can.Message]:
                return [
                    can.Message(arbitration_id=0x201, data=bytes(8)),
                    can.Message(arbitration_id=0x202, data=bytes(8)),
                ]

        motors = {"m1": _PreparingMotor("m1")}
        manager = _MockCANManager(motors)

        async def hook(motor_name: str, _msg: can.Message) -> None:
            manager.set_rx_at(motor_name, time.time() + 0.001)

        manager.set_post_send_hook(hook)
        runner = MotorCheckRunner(
            "main_hand", manager, motors, default_magnitude={"_PreparingMotor": 1.0}
        )

        snap = await runner.run()

        assert snap.records[0].result is MotorCheckResult.PASSED
        assert [msg.arbitration_id for _, msg in manager.sent] == [0x201, 0x202, 0x100, 0x100]

    async def test_prepare_steps_observe_declared_delays(self) -> None:
        class _DelayedMotor(_MockMotor):
            def prepare_check_steps(self) -> list[tuple[can.Message, float]]:
                return [
                    (can.Message(arbitration_id=0x201, data=bytes(8)), 0.05),
                    (can.Message(arbitration_id=0x202, data=bytes(8)), 0.2),
                ]

        motors = {"m1": _DelayedMotor("m1")}
        manager = _MockCANManager(motors)

        async def hook(motor_name: str, _msg: can.Message) -> None:
            manager.set_rx_at(motor_name, time.time() + 0.001)

        manager.set_post_send_hook(hook)
        runner = MotorCheckRunner(
            "main_hand", manager, motors, default_magnitude={"_DelayedMotor": 1.0}
        )
        with patch.object(
            runner, "_wait_step_delay", new_callable=AsyncMock, return_value=True
        ) as wait_delay:
            snap = await runner.run()

        assert snap.records[0].result is MotorCheckResult.PASSED
        assert [call.args[0] for call in wait_delay.await_args_list] == [0.05, 0.2]

    async def test_known_safety_error_rejects_check_before_preparation(self) -> None:
        class _UnsafeMotor(_MockMotor):
            def check_safety_error(self) -> str | None:
                return "既知fault"

            def prepare_check(self) -> list[can.Message]:
                raise AssertionError("prepare_check must not run")

        motors = {"m1": _UnsafeMotor("m1")}
        manager = _MockCANManager(motors)
        runner = MotorCheckRunner(
            "main_hand", manager, motors, default_magnitude={"_UnsafeMotor": 1.0}
        )

        snap = await runner.run()

        assert snap.records[0].result is MotorCheckResult.FAILED
        assert snap.records[0].detail == "既知fault"
        assert [msg.arbitration_id for _, msg in manager.sent] == [0x100]

    async def test_missing_or_stale_required_feedback_rejects_check(self) -> None:
        class _FeedbackRequiredMotor(_MockMotor):
            def requires_fresh_feedback_for_check(self) -> bool:
                return True

        for last_rx_at, expected in [(None, "未受信"), (99.0, "STALE")]:
            motors = {"m1": _FeedbackRequiredMotor("m1")}
            manager = _MockCANManager(motors)
            if last_rx_at is not None:
                manager.set_rx_at("m1", last_rx_at)
            runner = MotorCheckRunner(
                "main_hand",
                manager,
                motors,
                feedback_freshness_ms=500.0,
                default_magnitude={"_FeedbackRequiredMotor": 1.0},
            )

            with patch("lib.motor_check.time.time", return_value=100.0):
                snap = await runner.run()

            assert snap.records[0].result is MotorCheckResult.FAILED
            assert expected in snap.records[0].detail
            assert len(manager.sent) == 1

    async def test_feedback_is_rechecked_during_preparation(self) -> None:
        class _FeedbackRequiredMotor(_MockMotor):
            def requires_fresh_feedback_for_check(self) -> bool:
                return True

            def prepare_check_steps(self) -> list[tuple[can.Message, float]]:
                return [
                    (can.Message(arbitration_id=0x201, data=bytes(8)), 0.0),
                    (can.Message(arbitration_id=0x202, data=bytes(8)), 0.0),
                ]

        motors = {"m1": _FeedbackRequiredMotor("m1")}
        manager = _MockCANManager(motors)
        manager.set_rx_at("m1", 100.0)

        async def hook(_motor_name: str, msg: can.Message) -> None:
            if msg.arbitration_id == 0x201:
                manager.set_rx_at("m1", 99.0)

        manager.set_post_send_hook(hook)
        runner = MotorCheckRunner(
            "main_hand",
            manager,
            motors,
            feedback_freshness_ms=500.0,
            default_magnitude={"_FeedbackRequiredMotor": 1.0},
        )

        with patch("lib.motor_check.time.time", return_value=100.0):
            snap = await runner.run()

        assert snap.records[0].result is MotorCheckResult.FAILED
        assert "STALE" in snap.records[0].detail
        assert [msg.arbitration_id for _, msg in manager.sent] == [0x201, 0x100]

    async def test_abort_during_preparation_stops_steps_and_disables(self) -> None:
        class _PreparingMotor(_MockMotor):
            def prepare_check_steps(self) -> list[tuple[can.Message, float]]:
                return [
                    (can.Message(arbitration_id=0x201, data=bytes(8)), 0.1),
                    (can.Message(arbitration_id=0x202, data=bytes(8)), 0.1),
                ]

        motors = {"m1": _PreparingMotor("m1")}
        manager = _MockCANManager(motors)
        runner = MotorCheckRunner(
            "main_hand", manager, motors, default_magnitude={"_PreparingMotor": 1.0}
        )

        async def hook(_motor_name: str, msg: can.Message) -> None:
            if msg.arbitration_id == 0x201:
                runner.abort()

        manager.set_post_send_hook(hook)
        snap = await runner.run()

        assert snap.records[0].result is MotorCheckResult.SKIPPED
        assert [msg.arbitration_id for _, msg in manager.sent] == [0x201, 0x100]

    async def test_partial_failed_overall_partial(self) -> None:
        motors = {
            "m1": _MockMotor("m1", evaluate_passed=True),
            "m2": _MockMotor("m2", evaluate_passed=False, evaluate_detail="差分過大"),
        }
        manager = _MockCANManager(motors)

        async def hook(motor_name: str, _msg: can.Message) -> None:
            manager.set_rx_at(motor_name, time.time() + 0.001)

        manager.set_post_send_hook(hook)
        runner = MotorCheckRunner(
            "main_hand", manager, motors, default_magnitude={"_MockMotor": 1.0}
        )

        snap = await runner.run()
        assert snap.overall == "partial"
        assert snap.records[0].result is MotorCheckResult.PASSED
        assert snap.records[1].result is MotorCheckResult.FAILED
        assert snap.records[1].detail == "差分過大"

    async def test_all_failed_overall_failed(self) -> None:
        motors = {
            "m1": _MockMotor("m1", evaluate_passed=False, evaluate_detail="x"),
            "m2": _MockMotor("m2", evaluate_passed=False, evaluate_detail="y"),
        }
        manager = _MockCANManager(motors)

        async def hook(motor_name: str, _msg: can.Message) -> None:
            manager.set_rx_at(motor_name, time.time() + 0.001)

        manager.set_post_send_hook(hook)
        runner = MotorCheckRunner(
            "main_hand", manager, motors, default_magnitude={"_MockMotor": 1.0}
        )

        snap = await runner.run()
        assert snap.overall == "failed"
        assert all(r.result is MotorCheckResult.FAILED for r in snap.records)


class TestMotorCheckRunnerTimeout:
    async def test_no_feedback_results_in_timeout(self) -> None:
        # post_send_hook を設定しない → _last_rx_at が更新されない → タイムアウト
        motors = {"m1": _MockMotor("m1")}
        manager = _MockCANManager(motors)
        # テスト時間を短く保つため per_motor_timeout_ms を 50ms に縮める
        runner = MotorCheckRunner(
            "main_hand",
            manager,
            motors,
            per_motor_timeout_ms=50.0,
            default_magnitude={"_MockMotor": 1.0},
        )

        snap = await runner.run()
        assert snap.records[0].result is MotorCheckResult.TIMEOUT
        assert snap.overall == "failed"


class TestMotorCheckRunnerAbort:
    async def test_abort_skips_remaining_motors(self) -> None:
        motors = {
            "m1": _MockMotor("m1"),
            "m2": _MockMotor("m2"),
            "m3": _MockMotor("m3"),
        }
        manager = _MockCANManager(motors)

        async def hook(motor_name: str, _msg: can.Message) -> None:
            # 1 つ目の指令送信直後に abort。現在モータも即時停止扱いになる。
            manager.set_rx_at(motor_name, time.time() + 0.001)
            if motor_name == "m1":
                runner.abort()

        runner = MotorCheckRunner(
            "main_hand",
            manager,
            motors,
            per_motor_timeout_ms=200.0,
            default_magnitude={"_MockMotor": 1.0},
        )
        manager.set_post_send_hook(hook)

        snap = await runner.run()
        assert snap.records[0].result is MotorCheckResult.SKIPPED
        # 残りも SKIPPED で完結する
        assert snap.records[1].result is MotorCheckResult.SKIPPED
        assert snap.records[2].result is MotorCheckResult.SKIPPED


class TestMotorCheckRunnerSendFailure:
    async def test_send_canerror_records_failed(self) -> None:
        motors = {"m1": _MockMotor("m1")}
        manager = _MockCANManager(motors)
        manager.set_send_failure("m1", can.CanError("送信できない"))

        runner = MotorCheckRunner(
            "main_hand", manager, motors, default_magnitude={"_MockMotor": 1.0}
        )

        snap = await runner.run()
        rec = snap.records[0]
        assert rec.result is MotorCheckResult.FAILED
        assert rec.detail == "送信失敗"


class TestMotorCheckRunnerCallbacks:
    async def test_on_record_called_per_motor(self) -> None:
        motors = {
            "m1": _MockMotor("m1", evaluate_passed=True),
            "m2": _MockMotor("m2", evaluate_passed=True),
        }
        manager = _MockCANManager(motors)

        async def hook(motor_name: str, _msg: can.Message) -> None:
            manager.set_rx_at(motor_name, time.time() + 0.001)

        manager.set_post_send_hook(hook)

        records_seen: list[str] = []

        runner = MotorCheckRunner(
            "main_hand", manager, motors, default_magnitude={"_MockMotor": 1.0}
        )
        runner.set_on_record(lambda rec: records_seen.append(rec.motor))

        await runner.run()
        assert records_seen == ["m1", "m2"]

    async def test_on_progress_called_per_motor(self) -> None:
        motors = {
            "m1": _MockMotor("m1"),
            "m2": _MockMotor("m2"),
        }
        manager = _MockCANManager(motors)

        async def hook(motor_name: str, _msg: can.Message) -> None:
            manager.set_rx_at(motor_name, time.time() + 0.001)

        manager.set_post_send_hook(hook)

        progress_seen: list[tuple[str, int, int]] = []

        runner = MotorCheckRunner(
            "main_hand", manager, motors, default_magnitude={"_MockMotor": 1.0}
        )
        runner.set_on_progress(lambda name, idx, total: progress_seen.append((name, idx, total)))

        await runner.run()
        assert progress_seen == [("m1", 0, 2), ("m2", 1, 2)]


class TestMotorCheckRunnerSkipReason:
    """magnitude=0 の SKIPPED で「意図的な除外」と「未対応ドライバ」を区別する。

    セッティングタイムに画面を見る操縦者が、config で意図的に外したペア軸
    (y_axis_* / rotate_*) を異常だと誤解しないことが目的。
    """

    async def test_config_excluded_motor_reports_intentional_skip(self) -> None:
        motor = _MockMotor("y_axis_r")
        motors = {"y_axis_r": motor}
        manager = _MockCANManager(motors)
        runner = MotorCheckRunner(
            "main_hand",
            manager,
            motors,
            default_magnitude={"_MockMotor": 1.0},
            per_motor_overrides={"y_axis_r": {"magnitude": 0.0}},
        )

        snap = await runner.run()

        assert snap.records[0].result is MotorCheckResult.SKIPPED
        assert snap.records[0].detail == SKIP_DETAIL_CONFIG_EXCLUDED
        # 意図的な除外なので CAN へは一切送らない
        assert manager.sent == []
        assert motor.last_magnitude is None

    async def test_unsupported_driver_reports_unsupported_skip(self) -> None:
        # default_magnitude にこのクラスの既定値が無く、magnitude が解決できないケース
        motor = _MockMotor("unknown")
        motors = {"unknown": motor}
        manager = _MockCANManager(motors)
        runner = MotorCheckRunner("main_hand", manager, motors, default_magnitude={})

        snap = await runner.run()

        assert snap.records[0].result is MotorCheckResult.SKIPPED
        assert snap.records[0].detail == SKIP_DETAIL_UNSUPPORTED_DRIVER

    def test_skip_details_are_distinguishable(self) -> None:
        assert SKIP_DETAIL_CONFIG_EXCLUDED != SKIP_DETAIL_UNSUPPORTED_DRIVER
        # 意図的な除外側は「壊れているのではない」と読める文言であること
        assert "指差喚呼" in SKIP_DETAIL_CONFIG_EXCLUDED


class TestMotorCheckRunnerOverrides:
    async def test_per_motor_overrides_magnitude(self) -> None:
        motor = _MockMotor("lift_motor")
        motors = {"lift_motor": motor}
        manager = _MockCANManager(motors)

        async def hook(motor_name: str, _msg: can.Message) -> None:
            manager.set_rx_at(motor_name, time.time() + 0.001)

        manager.set_post_send_hook(hook)
        runner = MotorCheckRunner(
            "main_hand",
            manager,
            motors,
            default_magnitude={"_MockMotor": 1.0},
            per_motor_overrides={"lift_motor": {"magnitude": 800.0}},
        )

        await runner.run()
        # check_command に渡された magnitude が override 値であること
        assert motor.last_magnitude == 800.0

    async def test_per_motor_overrides_timeout(self) -> None:
        # m1 だけ短い timeout でタイムアウト、m2 は十分な timeout で PASSED
        motors = {
            "m1": _MockMotor("m1"),
            "m2": _MockMotor("m2"),
        }
        manager = _MockCANManager(motors)

        async def hook(motor_name: str, _msg: can.Message) -> None:
            # m2 だけ即時フィードバック、m1 はフィードバックなし
            if motor_name == "m2":
                manager.set_rx_at(motor_name, time.time() + 0.001)

        manager.set_post_send_hook(hook)
        runner = MotorCheckRunner(
            "main_hand",
            manager,
            motors,
            per_motor_timeout_ms=2000.0,
            default_magnitude={"_MockMotor": 1.0},
            per_motor_overrides={"m1": {"timeout_ms": 30.0}},
        )

        snap = await runner.run()
        assert snap.records[0].result is MotorCheckResult.TIMEOUT
        assert snap.records[1].result is MotorCheckResult.PASSED


class TestMotorCheckRunnerReset:
    async def test_reset_after_check_called_on_passed(self) -> None:
        motor = _MockMotor("m1", evaluate_passed=True)
        motors = {"m1": motor}
        manager = _MockCANManager(motors)

        async def hook(motor_name: str, _msg: can.Message) -> None:
            manager.set_rx_at(motor_name, time.time() + 0.001)

        manager.set_post_send_hook(hook)
        runner = MotorCheckRunner(
            "main_hand", manager, motors, default_magnitude={"_MockMotor": 1.0}
        )

        await runner.run()
        # check_command 1 回 + reset_after_check 1 回 = 2 件 send されている
        assert motor.reset_calls == 1
        assert len(manager.sent) == 2

    async def test_reset_after_check_called_on_failed(self) -> None:
        motor = _MockMotor("m1", evaluate_passed=False, evaluate_detail="x")
        motors = {"m1": motor}
        manager = _MockCANManager(motors)

        async def hook(motor_name: str, _msg: can.Message) -> None:
            manager.set_rx_at(motor_name, time.time() + 0.001)

        manager.set_post_send_hook(hook)
        runner = MotorCheckRunner(
            "main_hand", manager, motors, default_magnitude={"_MockMotor": 1.0}
        )

        await runner.run()
        # FAILED でも必ず reset を送って原状復帰
        assert motor.reset_calls == 1


class TestMotorCheckRunnerLifecycle:
    async def test_is_running_flag(self) -> None:
        motors = {"m1": _MockMotor("m1")}
        manager = _MockCANManager(motors)

        observed_running: list[bool] = []

        async def hook(motor_name: str, _msg: can.Message) -> None:
            observed_running.append(runner.is_running)
            manager.set_rx_at(motor_name, time.time() + 0.001)

        manager.set_post_send_hook(hook)
        runner = MotorCheckRunner(
            "main_hand", manager, motors, default_magnitude={"_MockMotor": 1.0}
        )

        assert runner.is_running is False
        await runner.run()
        assert runner.is_running is False
        # send (check_command + reset_after_check) のどちらの最中でも is_running=True
        assert observed_running and all(observed_running)

    async def test_double_run_raises_runtime_error(self) -> None:
        motors = {"m1": _MockMotor("m1")}
        manager = _MockCANManager(motors)

        # 1 回目の run() が完了する前に 2 回目を呼ぶため、
        # post_send_hook 内で並走する run() を発火し、その例外を捕捉する
        captured: list[Exception] = []

        async def hook(motor_name: str, _msg: can.Message) -> None:
            try:
                await runner.run()
            except RuntimeError as exc:
                captured.append(exc)
            manager.set_rx_at(motor_name, time.time() + 0.001)

        manager.set_post_send_hook(hook)
        runner = MotorCheckRunner(
            "main_hand", manager, motors, default_magnitude={"_MockMotor": 1.0}
        )

        await runner.run()
        # check_command 送信時 + reset_after_check 送信時の 2 回 hook が走り、
        # 2 回とも RuntimeError がキャプチャされる
        assert len(captured) >= 1
        assert all(isinstance(e, RuntimeError) for e in captured)


class TestMotorCheckRunnerObservedField:
    """observed/expected フィールドがコンテキストから埋められることを確認。"""

    async def test_records_expected_and_observed(self) -> None:
        motor = _MockMotor("m1", evaluate_passed=True, target_value=42.0)
        # decode_feedback が動かないので state は手動で設定
        motor.set_observed(position=10.0, velocity=20.0)
        motors = {"m1": motor}
        manager = _MockCANManager(motors)

        async def hook(motor_name: str, _msg: can.Message) -> None:
            manager.set_rx_at(motor_name, time.time() + 0.001)

        manager.set_post_send_hook(hook)
        runner = MotorCheckRunner(
            "main_hand", manager, motors, default_magnitude={"_MockMotor": 1.0}
        )

        snap = await runner.run()
        rec = snap.records[0]
        assert rec.expected == 42.0
        # mode="current" なので velocity を採用
        assert rec.observed == 20.0
