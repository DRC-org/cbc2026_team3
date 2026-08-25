from __future__ import annotations

import asyncio

import pytest

from lib.control.sync_monitor import SyncGroup, SyncMember, SyncMonitor

# 実機の y_axis (ラックアンドピニオン) と同じ構成。左右は逆回転で同一動作
SCALE = 864.15


class _StubDriver:
    """SyncMonitor が触る API (feedback_position) だけを実装したスタブ。"""

    def __init__(self, position: float = 0.0) -> None:
        self.position = position

    def feedback_position(self) -> float:
        return self.position


class _FakeClock:
    def __init__(self, start: float = 5000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


def _pair_group(name: str = "y_axis", tolerance: float = 2.0) -> SyncGroup:
    return SyncGroup(
        name=name,
        members=(
            SyncMember(motor_name=f"{name}_r", scale=SCALE, offset=0.0),
            SyncMember(motor_name=f"{name}_l", scale=-SCALE, offset=0.0),
        ),
        tolerance=tolerance,
    )


class _Fixture:
    """監視器 + スタブ一式。既定は y_axis ペア 1 組。"""

    def __init__(
        self,
        groups: tuple[SyncGroup, ...] | None = None,
        *,
        feedback_timeout_ms: float = 500.0,
        violation_samples: int = 2,
        on_violation: object | None = None,
    ) -> None:
        self.groups = groups if groups is not None else (_pair_group(),)
        self.clock = _FakeClock()
        self.drivers: dict[str, _StubDriver] = {}
        self.feedback_at: dict[str, float] = {}
        for group in self.groups:
            for member in group.members:
                self.drivers[member.motor_name] = _StubDriver()
                self.feedback_at[member.motor_name] = self.clock.now
        self.violations: list[tuple[str, float]] = []
        self.monitor = SyncMonitor(
            self.groups,
            self.drivers,  # type: ignore[arg-type]
            last_feedback_at=self.feedback_at.get,
            feedback_timeout_ms=feedback_timeout_ms,
            interval_s=0.001,
            violation_samples=violation_samples,
            on_violation=on_violation or self._record,  # type: ignore[arg-type]
            feedback_clock=self.clock,
        )

    def _record(self, group_name: str, deviation: float) -> None:
        self.violations.append((group_name, deviation))

    def place(self, name: str, value: float, *, fresh: bool = True) -> None:
        """人間の単位で位置を与える (指令単位へ順換算してフィードバックに載せる)。"""
        member = self._member(name)
        self.drivers[name].position = value * member.scale + member.offset
        if fresh:
            self.feedback_at[name] = self.clock.now

    def _member(self, name: str) -> SyncMember:
        for group in self.groups:
            for member in group.members:
                if member.motor_name == name:
                    return member
        raise KeyError(name)


class TestSyncMember:
    def test_to_value_is_inverse_of_command_conversion(self) -> None:
        member = SyncMember(motor_name="y_axis_r", scale=SCALE, offset=100.0)
        command = 10.0 * SCALE + 100.0
        assert member.to_value(command) == pytest.approx(10.0)

    def test_to_value_handles_negative_scale(self) -> None:
        member = SyncMember(motor_name="y_axis_l", scale=-SCALE, offset=0.0)
        assert member.to_value(-10.0 * SCALE) == pytest.approx(10.0)


class TestSyncGroupDeviation:
    def test_reverse_pair_in_sync_has_zero_deviation(self) -> None:
        group = _pair_group()
        positions = {"y_axis_r": 10.0 * SCALE, "y_axis_l": -10.0 * SCALE}
        assert group.deviation(positions) == pytest.approx(0.0)

    def test_deviation_reflects_mismatch_in_human_units(self) -> None:
        group = _pair_group()
        positions = {"y_axis_r": 10.0 * SCALE, "y_axis_l": -7.0 * SCALE}
        assert group.deviation(positions) == pytest.approx(3.0)

    def test_deviation_is_none_with_fewer_than_two_members(self) -> None:
        group = _pair_group()
        assert group.deviation({"y_axis_r": 0.0}) is None
        assert group.deviation({}) is None


class TestViolationDetection:
    async def test_no_violation_within_tolerance(self) -> None:
        fx = _Fixture()
        fx.place("y_axis_r", 10.0)
        fx.place("y_axis_l", 11.0)
        for _ in range(5):
            fx.monitor.step()
        assert fx.violations == []

    async def test_single_outlier_does_not_fire(self) -> None:
        fx = _Fixture(violation_samples=2)
        fx.place("y_axis_r", 10.0)
        fx.place("y_axis_l", 20.0)
        fx.monitor.step()

        # 1 サンプルの外れ値で試合中に緊急停止させないためのノイズ対策
        fx.place("y_axis_l", 10.0)
        fx.monitor.step()
        assert fx.violations == []

    async def test_fires_after_consecutive_violations(self) -> None:
        fx = _Fixture(violation_samples=2)
        fx.place("y_axis_r", 10.0)
        fx.place("y_axis_l", 20.0)

        fx.monitor.step()
        assert fx.violations == []

        fx.monitor.step()
        assert len(fx.violations) == 1
        name, deviation = fx.violations[0]
        assert name == "y_axis"
        assert deviation == pytest.approx(10.0)
        assert fx.monitor.violated == frozenset({"y_axis"})

    async def test_violation_is_latched_until_reset(self) -> None:
        fx = _Fixture(violation_samples=1)
        fx.place("y_axis_r", 10.0)
        fx.place("y_axis_l", 20.0)

        for _ in range(5):
            fx.monitor.step()
        # 緊急停止が連打されないよう、同じ軸では 1 度しか発報しない
        assert len(fx.violations) == 1

        fx.monitor.reset()
        assert fx.monitor.violated == frozenset()
        fx.monitor.step()
        assert len(fx.violations) == 2


class TestFeedbackFreshness:
    async def test_stale_member_is_excluded_and_skips_judgement(self) -> None:
        fx = _Fixture(violation_samples=1, feedback_timeout_ms=500.0)
        fx.place("y_axis_r", 10.0)
        fx.place("y_axis_l", 20.0)

        fx.clock.advance(0.6)
        fx.place("y_axis_r", 10.0)  # 片側だけ鮮度を更新

        fx.monitor.step()
        # 比較対象が 1 台では偏差を判定できない
        assert fx.violations == []

    async def test_missing_feedback_does_not_fire(self) -> None:
        fx = _Fixture(violation_samples=1)
        fx.feedback_at.clear()
        fx.place("y_axis_r", 10.0, fresh=False)
        fx.place("y_axis_l", 20.0, fresh=False)

        fx.monitor.step()
        # 起動直後のフィードバック未受信で緊急停止させない
        assert fx.violations == []

    async def test_stale_member_resets_consecutive_count(self) -> None:
        fx = _Fixture(violation_samples=2, feedback_timeout_ms=500.0)
        fx.place("y_axis_r", 10.0)
        fx.place("y_axis_l", 20.0)
        fx.monitor.step()

        fx.clock.advance(0.6)
        fx.place("y_axis_r", 10.0)
        fx.monitor.step()

        fx.place("y_axis_l", 20.0)
        fx.monitor.step()
        # 判定スキップを挟んだら連続カウントはやり直し
        assert fx.violations == []

        fx.monitor.step()
        assert len(fx.violations) == 1


class TestCallbackRobustness:
    async def test_monitor_survives_callback_exception(self) -> None:
        calls: list[str] = []

        def boom(group_name: str, deviation: float) -> None:
            calls.append(group_name)
            raise RuntimeError("コールバック内部エラー (テスト)")

        groups = (_pair_group("y_axis"), _pair_group("rotate"))
        fx = _Fixture(groups, violation_samples=1, on_violation=boom)
        for name in ("y_axis", "rotate"):
            fx.place(f"{name}_r", 10.0)
            fx.place(f"{name}_l", 20.0)

        fx.monitor.step()

        # 監視が死ぬ方が危険なので、例外は握って残りの軸も判定し続ける
        assert calls == ["y_axis", "rotate"]
        assert fx.monitor.violated == frozenset({"y_axis", "rotate"})


class TestLifecycle:
    async def test_start_and_stop_leaves_no_task(self) -> None:
        fx = _Fixture(violation_samples=1)
        fx.place("y_axis_r", 10.0)
        fx.place("y_axis_l", 20.0)

        fx.monitor.start()
        assert fx.monitor.is_running is True
        for _ in range(10):
            await asyncio.sleep(0.001)

        await fx.monitor.stop()
        assert fx.monitor.is_running is False
        assert len(fx.violations) == 1

    async def test_double_start_does_not_spawn_second_task(self) -> None:
        fx = _Fixture()
        before = len(asyncio.all_tasks())
        fx.monitor.start()
        fx.monitor.start()
        assert len(asyncio.all_tasks()) == before + 1
        await fx.monitor.stop()

    async def test_stop_without_start_is_noop(self) -> None:
        fx = _Fixture()
        await fx.monitor.stop()
        assert fx.monitor.is_running is False

    async def test_run_propagates_cancellation(self) -> None:
        fx = _Fixture()
        task = asyncio.create_task(fx.monitor.run())
        await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
