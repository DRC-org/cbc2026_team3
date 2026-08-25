from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import can

from lib.health import (
    CheckRunSnapshot,
    MotorCheckRecord,
    MotorCheckResult,
)

if TYPE_CHECKING:
    from lib.can_manager import CANManager
    from lib.drivers.base import MotorDriver

logger = logging.getLogger(__name__)


# 1 モータあたりの観測タイムアウト既定値 (docs/impl_plan.md: 1.5s)。
DEFAULT_PER_MOTOR_TIMEOUT_MS: float = 1500.0

# ドライバ種別ごとの既定 magnitude。
# 物理的に安全な微小量に固定し、各値は config の motor_check.default_magnitude で上書き可能。
DEFAULT_MAGNITUDES: dict[str, float] = {
    "m3508": 500.0,  # mA
    "edulite05": 5.0,  # deg
    "generic": 0.1,  # 0.1 rev / 10% duty 等 (control_type 依存)
}

# クラス名 → DEFAULT_MAGNITUDES のキーへの対応表。
# 実ドライバはこのテーブル経由で magnitude を引く。テスト用 mock など未登録クラスは
# default_magnitude にクラス名そのものをキーとして渡せばフォールバックできる。
_DRIVER_TYPE_KEY: dict[str, str] = {
    "M3508Driver": "m3508",
    "Edulite05Driver": "edulite05",
    "GenericDriver": "generic",
}

# 観測ループの poll 間隔。短すぎると CPU を食い、長すぎると判定が遅れるため 10ms 固定。
_POLL_INTERVAL_S: float = 0.01


class MotorCheckRunner:
    """1 ロボット分のアクチュエータ動作確認シーケンスを実行する。

    通常シーケンスエンジン (lib/sequence/engine.py) とは別物。1 モータずつ駆動 →
    フィードバック観測 → 元の状態に戻す、を順次繰り返す。各モータ完了ごとに
    on_record コールバックを呼ぶことで Web UI 等への進捗配信を可能にする。

    安全策 (docs/impl_plan.md):
      - reset_after_check は PASSED/FAILED/TIMEOUT どの結末でも必ず送る (駆動状態を残さない)
      - 緊急停止 / 通常シーケンス中の起動拒否は呼び出し側 (server.py) で行う
      - 二重実行は RuntimeError で拒否し、進行中スナップショットの破壊を防ぐ
    """

    def __init__(
        self,
        robot_name: str,
        can_manager: CANManager,
        motors: dict[str, MotorDriver],
        *,
        per_motor_timeout_ms: float = DEFAULT_PER_MOTOR_TIMEOUT_MS,
        feedback_freshness_ms: float = 500.0,
        default_magnitude: dict[str, float] | None = None,
        per_motor_overrides: dict[str, dict] | None = None,
    ) -> None:
        self._robot_name = robot_name
        self._can_manager = can_manager
        self._motors = motors
        self._per_motor_timeout_ms = per_motor_timeout_ms
        self._feedback_freshness_ms = feedback_freshness_ms
        self._default_magnitude: dict[str, float] = (
            dict(DEFAULT_MAGNITUDES) if default_magnitude is None else dict(default_magnitude)
        )
        self._per_motor_overrides: dict[str, dict] = per_motor_overrides or {}

        self._running: bool = False
        self._aborted: bool = False
        self._abort_event = asyncio.Event()
        self._snapshot: CheckRunSnapshot = CheckRunSnapshot(
            robot=robot_name,
            started_at=0.0,
            finished_at=None,
            overall="ok",
            records=[],
        )

        self._on_record: Callable[[MotorCheckRecord], None] | None = None
        self._on_progress: Callable[[str, int, int], None] | None = None

    # ------------------------------------------------------------------ #
    #  公開プロパティ
    # ------------------------------------------------------------------ #

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def snapshot(self) -> CheckRunSnapshot:
        """現在のスナップショット (実行中も呼べる)。"""
        return self._snapshot

    # ------------------------------------------------------------------ #
    #  コールバック登録
    # ------------------------------------------------------------------ #

    def set_on_record(self, cb: Callable[[MotorCheckRecord], None]) -> None:
        """1 モータのチェックが完了するたびに呼ばれるコールバック。"""
        self._on_record = cb

    def set_on_progress(self, cb: Callable[[str, int, int], None]) -> None:
        """次のモータに進むときに呼ばれるコールバック (motor_name, index, total)。"""
        self._on_progress = cb

    # ------------------------------------------------------------------ #
    #  外部制御
    # ------------------------------------------------------------------ #

    def abort(self) -> None:
        """進行中のチェックを中断する。次のモータに進まない。

        観測ループ内でも参照されるため、現在モータの観測タイムアウト前に
        受信があれば判定まで進み、その後 SKIPPED に切り替わる。
        """
        self._aborted = True
        self._abort_event.set()

    # ------------------------------------------------------------------ #
    #  メインループ
    # ------------------------------------------------------------------ #

    async def run(self) -> CheckRunSnapshot:
        """全モータを順次チェックして CheckRunSnapshot を返す。

        二重実行は RuntimeError を送出して拒否する。
        """
        if self._running:
            raise RuntimeError("MotorCheckRunner は既に実行中です")

        self._running = True
        self._aborted = False
        self._abort_event.clear()

        now = time.time()
        # 全レコードを PENDING で初期化してから順次更新していく。
        # 進行中も snapshot プロパティで参照可能にするため、ここで一度組み立てる。
        records: list[MotorCheckRecord] = []
        for name in self._motors:
            bus_name = self._can_manager._motor_bus.get(name, "")
            records.append(
                MotorCheckRecord(
                    motor=name,
                    bus=bus_name,
                    started_at=now,
                    finished_at=None,
                    result=MotorCheckResult.PENDING,
                    expected=None,
                    observed=None,
                    detail=None,
                )
            )
        self._snapshot = CheckRunSnapshot(
            robot=self._robot_name,
            started_at=now,
            finished_at=None,
            overall="running",
            records=records,
        )

        try:
            total = len(self._motors)
            for i, (name, motor) in enumerate(self._motors.items()):
                record = records[i]

                # 中断要求 → 残りはすべて SKIPPED にして抜ける
                if self._aborted:
                    record.result = MotorCheckResult.SKIPPED
                    record.finished_at = time.time()
                    continue

                record.result = MotorCheckResult.RUNNING
                record.started_at = time.time()

                if self._on_progress is not None:
                    with contextlib.suppress(Exception):
                        self._on_progress(name, i, total)

                await self._check_one_motor(name, motor, record)

                record.finished_at = time.time()

                if self._on_record is not None:
                    with contextlib.suppress(Exception):
                        self._on_record(record)

            # 中断時のフォロー: ループ内で SKIPPED になった record の finished_at が
            # 未設定の可能性があるので最終処理として埋める。
            for record in records:
                if record.finished_at is None:
                    record.finished_at = time.time()

            self._snapshot.overall = CheckRunSnapshot.compute_overall(records)
            self._snapshot.finished_at = time.time()
            return self._snapshot
        finally:
            self._running = False

    # ------------------------------------------------------------------ #
    #  内部処理: 1 モータのチェック
    # ------------------------------------------------------------------ #

    async def _check_one_motor(
        self,
        name: str,
        motor: MotorDriver,
        record: MotorCheckRecord,
    ) -> None:
        # magnitude / timeout を override → default の順に解決する
        override = self._per_motor_overrides.get(name, {})
        magnitude = self._resolve_magnitude(motor, override)
        timeout_s = float(override.get("timeout_ms", self._per_motor_timeout_ms)) / 1000.0

        # magnitude=0 (未対応ドライバ) は SKIPPED 扱い。reset も送らずに抜ける。
        if magnitude == 0.0:
            record.result = MotorCheckResult.SKIPPED
            record.detail = "未対応ドライバ種別"
            return

        if not await self._guard_check(name, motor, record):
            return

        try:
            prepare_steps = motor.prepare_check_steps()
            for prepare_msg, delay_after_s in prepare_steps:
                if not await self._guard_check(name, motor, record):
                    return
                await self._can_manager.send(name, prepare_msg)
                if not await self._guard_check(name, motor, record):
                    return
                if not await self._wait_step_delay(delay_after_s):
                    await self._guard_check(name, motor, record)
                    return
                if not await self._guard_check(name, motor, record):
                    return
        except can.CanError:
            record.result = MotorCheckResult.FAILED
            record.detail = "初期化送信失敗"
            await self._safe_reset(name, motor)
            return
        except Exception as exc:
            record.result = MotorCheckResult.FAILED
            record.detail = f"prepare_check 例外: {exc!s}"
            await self._safe_reset(name, motor)
            return

        # 初期化応答を動作確認の応答と誤認しないよう、指令送信直前の時刻を保存する。
        saved_rx_at = self._can_manager._last_rx_at.get(name)

        try:
            msg, context = motor.check_command(magnitude=magnitude)
        except Exception as exc:
            # check_command 内部の予期せぬ例外。原状復帰だけ試みて FAILED にする。
            record.result = MotorCheckResult.FAILED
            record.detail = f"check_command 例外: {exc!s}"
            await self._safe_reset(name, motor)
            return

        record.expected = self._extract_expected(context)

        if not await self._guard_check(name, motor, record):
            return
        try:
            await self._can_manager.send(name, msg)
        except can.CanError:
            record.result = MotorCheckResult.FAILED
            record.detail = "送信失敗"
            await self._safe_reset(name, motor)
            return
        if not await self._guard_check(name, motor, record):
            return

        # フィードバック観測ループ。タイムアウト or abort or 受信のいずれかで抜ける。
        received = await self._wait_for_feedback(name, saved_rx_at, timeout_s)

        if not received:
            record.result = MotorCheckResult.TIMEOUT
            record.detail = "フィードバック無応答"
            await self._safe_reset(name, motor)
            return

        # 受信できた → ドライバの判定ロジックに委譲
        state = motor.state
        try:
            passed, detail = motor.evaluate_check_result(state, context)
        except Exception as exc:
            record.result = MotorCheckResult.FAILED
            record.detail = f"evaluate 例外: {exc!s}"
            await self._safe_reset(name, motor)
            return

        record.observed = self._extract_observed(state, context)

        if passed:
            record.result = MotorCheckResult.PASSED
            record.detail = detail  # PASSED でも警告 note を残せる (generic の overcurrent 等)
        else:
            record.result = MotorCheckResult.FAILED
            record.detail = detail

        await self._safe_reset(name, motor)

    # ------------------------------------------------------------------ #
    #  ヘルパー
    # ------------------------------------------------------------------ #

    async def _guard_check(
        self,
        name: str,
        motor: MotorDriver,
        record: MotorCheckRecord,
    ) -> bool:
        if self._aborted:
            record.result = MotorCheckResult.SKIPPED
            record.detail = "動作確認中断"
            await self._safe_reset(name, motor)
            return False

        try:
            safety_error = motor.check_safety_error()
        except Exception as exc:
            safety_error = f"安全判定例外: {exc!s}"
        if safety_error is not None:
            record.result = MotorCheckResult.FAILED
            record.detail = safety_error
            await self._safe_reset(name, motor)
            return False

        if motor.requires_fresh_feedback_for_check():
            last_rx_at = self._can_manager._last_rx_at.get(name)
            if last_rx_at is None:
                record.result = MotorCheckResult.FAILED
                record.detail = "動作確認前フィードバック未受信"
                await self._safe_reset(name, motor)
                return False
            age_ms = (time.time() - last_rx_at) * 1000.0
            if age_ms > self._feedback_freshness_ms:
                record.result = MotorCheckResult.FAILED
                record.detail = f"動作確認前フィードバックSTALE ({age_ms:.0f}ms)"
                await self._safe_reset(name, motor)
                return False

        return True

    async def _wait_step_delay(self, delay_s: float) -> bool:
        if delay_s <= 0:
            return not self._aborted
        try:
            await asyncio.wait_for(self._abort_event.wait(), timeout=delay_s)
        except TimeoutError:
            return True
        return False

    def _resolve_magnitude(self, motor: MotorDriver, override: dict) -> float:
        """override → ドライバ種別既定値 → クラス名既定値 → 0.0 の順で解決する。"""
        if "magnitude" in override:
            return float(override["magnitude"])

        cls_name = type(motor).__name__
        type_key = _DRIVER_TYPE_KEY.get(cls_name)
        if type_key is not None and type_key in self._default_magnitude:
            return float(self._default_magnitude[type_key])

        # フォールバック: テスト用 mock 等、クラス名そのものがキーになっているケース
        if cls_name in self._default_magnitude:
            return float(self._default_magnitude[cls_name])

        return 0.0

    async def _wait_for_feedback(
        self,
        name: str,
        saved_rx_at: float | None,
        timeout_s: float,
    ) -> bool:
        """フィードバック受信を polling で待つ。受信したら True を返す。

        abort() が呼ばれても「現在モータの受信が既に届いている」場合は
        判定まで進めるため、abort より受信チェックを優先する。
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            new_rx = self._can_manager._last_rx_at.get(name)
            if new_rx is not None and (saved_rx_at is None or new_rx > saved_rx_at):
                return True
            if self._aborted:
                return False
            await asyncio.sleep(_POLL_INTERVAL_S)
        return False

    async def _safe_reset(self, name: str, motor: MotorDriver) -> None:
        """reset_after_check の送信失敗で run() を落とさないよう例外を握る。

        reset の失敗は致命的だがレコードには既に状態が確定しており、
        次のモータ確認に進めるよう warning ログのみ残す。
        """
        try:
            reset_msg = motor.reset_after_check()
        except Exception:
            logger.warning("reset_after_check の生成に失敗 (motor=%s)", name, exc_info=True)
            return

        try:
            await self._can_manager.send(name, reset_msg)
        except Exception:
            logger.warning("reset_after_check の送信に失敗 (motor=%s)", name, exc_info=True)

    @staticmethod
    def _extract_expected(context: dict) -> float | None:
        target = context.get("target")
        if target is None:
            return None
        try:
            return float(target)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_observed(state, context: dict) -> float | None:
        # mode="position" → position、それ以外 (current/velocity/duty) → velocity を採用。
        # M3508 の電流チェックは「rpm の符号一致」を見ているため velocity を保存する。
        mode = context.get("mode")
        if mode == "position":
            return float(state.position)
        return float(state.velocity)
