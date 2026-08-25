from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from lib.control.pid import PIDController
from lib.drivers.base import ControlMode
from lib.drivers.m3508 import CURRENT_MAX, CURRENT_MIN, M3508Driver

if TYPE_CHECKING:
    from lib.can_manager import CANManager

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_INTERVAL_S", "M3508PositionLoop", "make_position_pid"]

# 制御周期 200Hz。C620 のフィードバックは 1kHz で届くので取りこぼしはなく、
# asyncio のジッタ (数 ms) に対しても十分な余裕がある
DEFAULT_INTERVAL_S = 0.005

# asyncio が詰まって周期が飛んだときの dt 上限 (制御周期の 10 倍)。
# 実測 dt をそのまま渡すと、復帰した瞬間に積分項と微分項が跳ねて機構に衝撃が出る
DEFAULT_MAX_DT_S = 0.05

# 同一原因のログを毎周期出すと 200Hz でログが溢れるため、種類ごとに間引く
_LOG_THROTTLE_S = 1.0

TargetSink = Callable[[ControlMode, float], Awaitable[None]]
EStopChecker = Callable[[], bool]
SleepFunc = Callable[[float], Awaitable[None]]


def make_position_pid(
    kp: float,
    ki: float = 0.0,
    kd: float = 0.0,
    *,
    integral_limit: float | None = None,
    dead_band: float = 0.0,
) -> PIDController:
    """M3508 の位置制御用 PID を作る。出力レンジは C620 の電流指令範囲に固定。"""
    return PIDController(
        kp,
        ki,
        kd,
        output_min=float(CURRENT_MIN),
        output_max=float(CURRENT_MAX),
        integral_limit=integral_limit,
        dead_band=dead_band,
    )


@dataclass
class _Axis:
    """1 モータ分の制御状態。"""

    driver: M3508Driver
    pid: PIDController
    mode: ControlMode | None = None
    target: float | None = None
    # フィードバック途絶の遷移でのみログを出すためのフラグ
    stale: bool = field(default=False)


class M3508PositionLoop:
    """1 CAN バス上の M3508 群をまとめて位置制御する非同期ループ。

    M3508 は C620 ESC 経由で電流指令しか受け付けないため、位置決めは PC 側の PID で
    ``累積角 [deg] → 電流 [mA]`` に変換して行う。

    バス単位でまとめる理由: C620 の電流指令フレーム (0x200) は 1 通で 4 モータ分の
    スロットを持つ。モータごとに個別送信すると、自分以外のスロットを 0 で上書きして
    同一バス上の他モータがカクつくため、必ず全モータ分を 1 フレームに束ねて送る。

    安全側の挙動:
      - 緊急停止中は電流 0 + PID リセット + 目標解除
      - フィードバックが ``feedback_timeout_ms`` を超えて途絶したら電流 0 + PID リセット
      - 周期処理で例外が出てもループは継続する (指令が止まると C620 の挙動次第で危険)
      - ``pause()`` 中は 1 通も送らない (アクチュエータ動作確認と 0x200 を奪い合わないため)
    """

    def __init__(
        self,
        can_manager: CANManager,
        bus_name: str,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        max_dt_s: float = DEFAULT_MAX_DT_S,
        feedback_timeout_ms: float = 500.0,
        is_estop_active: EStopChecker | None = None,
        time_source: Callable[[], float] = time.monotonic,
        feedback_clock: Callable[[], float] = time.time,
        sleep: SleepFunc = asyncio.sleep,
    ) -> None:
        """
        Args:
            can_manager: 送信とフィードバック鮮度の取得に使う CANManager
            bus_name: 対象バス名 (config の can_buses キー)
            interval_s: 制御周期 [s]
            max_dt_s: PID に渡す dt の上限 [s]
            feedback_timeout_ms: この時間フィードバックが無ければ電流 0 に落とす
                (config の health.feedback_timeout_ms と揃える)
            is_estop_active: 緊急停止判定 (server.py の状態を後から注入する)
            time_source: 制御周期の計測に使う単調クロック
            feedback_clock: CANManager の受信タイムスタンプと比較する壁時計
            sleep: 周期待ちに使う関数 (テストで差し替え可能)
        """
        self._can_manager = can_manager
        self._bus_name = bus_name
        self._interval_s = interval_s
        self._max_dt_s = max_dt_s
        self._feedback_timeout_ms = feedback_timeout_ms
        self._is_estop_active = is_estop_active
        self._time_source = time_source
        self._feedback_clock = feedback_clock
        self._sleep = sleep

        self._axes: dict[str, _Axis] = {}
        # 生成時を基準にしておく。run() 開始時に取り直すので、生成から起動までの
        # 待ち時間が最初の dt に化けることはない
        self._last_tick: float = time_source()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._log_at: dict[str, float] = {}
        self._paused = False
        # pause() が「送信中の 1 周期」を待ち合わせるための排他。これが無いと
        # 送信途中の周期が動作確認の指令フレームを 0 電流で上書きしうる
        self._step_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    #  構成
    # ------------------------------------------------------------------ #

    def add_motor(self, name: str, driver: M3508Driver, pid: PIDController) -> None:
        if name in self._axes:
            raise ValueError(f"モータ '{name}' は既に登録済み")
        # 同一 can_id はフレームの同じスロットを奪い合い、片方の指令が消える
        for existing_name, axis in self._axes.items():
            if axis.driver.can_id == driver.can_id:
                raise ValueError(f"can_id {driver.can_id} が重複 ('{name}' と '{existing_name}')")
        self._axes[name] = _Axis(driver=driver, pid=pid)

    def set_sleep(self, sleep: SleepFunc) -> None:
        """周期待ち関数を差し替える (テスト用)。"""
        self._sleep = sleep

    @property
    def bus_name(self) -> str:
        return self._bus_name

    @property
    def motor_names(self) -> tuple[str, ...]:
        return tuple(self._axes)

    def pid(self, name: str) -> PIDController:
        return self._axes[name].pid

    def target(self, name: str) -> float | None:
        return self._axes[name].target

    def mode(self, name: str) -> ControlMode | None:
        return self._axes[name].mode

    # ------------------------------------------------------------------ #
    #  目標値
    # ------------------------------------------------------------------ #

    async def set_target(self, name: str, mode: ControlMode, value: float) -> None:
        """目標値を受け取る。実際の CAN 送信は制御ループ側で行う。

        MotorHandle.target_sink から呼ばれるため async シグネチャにしてある。
        """
        axis = self._axes[name]

        if mode is ControlMode.POSITION:
            # 目標更新のたびに積分をクリアすると昇降軸の保持電流が抜けるため、
            # 開ループ/停止状態から位置制御に入るときだけリセットする
            if axis.mode is not ControlMode.POSITION:
                axis.pid.reset()
            axis.mode = ControlMode.POSITION
            axis.target = float(value)
            return

        if mode is ControlMode.CURRENT:
            # ホーミングで機構端に押し当てる等の開ループ指令。PID を通さず素通しする
            axis.pid.reset()
            axis.mode = ControlMode.CURRENT
            axis.target = float(value)
            return

        raise ValueError(
            f"M3508 位置制御ループは POSITION / CURRENT のみ対応 (受け取った: {mode.name})"
        )

    def clear_target(self, name: str) -> None:
        """目標を解除して電流 0 にする。"""
        axis = self._axes[name]
        axis.mode = None
        axis.target = None
        axis.pid.reset()

    def set_origin_here(self, name: str) -> None:
        """現在位置を累積角の原点にする (ホーミング完了時)。

        原点が動くと既存の目標値の意味も変わるため、目標は解除して静止させる。
        """
        axis = self._axes[name]
        axis.driver.reset_multi_turn_origin()
        self.clear_target(name)

    def target_sink(self, name: str) -> TargetSink:
        """MotorHandle に差し込む目標値シンクを返す。"""
        if name not in self._axes:
            raise KeyError(name)

        async def sink(mode: ControlMode, value: float) -> None:
            await self.set_target(name, mode, value)

        return sink

    def target_sinks(self) -> dict[str, TargetSink]:
        """build_motor_group の ``target_sinks`` にそのまま渡せる辞書。"""
        return {name: self.target_sink(name) for name in self._axes}

    # ------------------------------------------------------------------ #
    #  制御ループ
    # ------------------------------------------------------------------ #

    async def step(self) -> None:
        """1 周期分の制御を行う。run() から呼ばれるほか、テストから直接駆動できる。"""
        async with self._step_lock:
            await self._step_locked()

    async def _step_locked(self) -> None:
        dt = self._elapsed()

        estop = self._is_estop_active is not None and self._is_estop_active()
        if estop:
            # 解除直後に溜まった積分が一気に出るのを防ぐ。目標も落として、
            # 停止中に姿勢が崩れていても解除だけでは動き出さないようにする
            self._disable_all()

        if self._paused:
            # 動作確認が同一バスの 0x200 を占有している。0 電流フレームでも
            # 送れば動作確認の指令を上書きしてしまうため 1 通も送らない
            return

        if estop:
            await self._send([0, 0, 0, 0])
            return

        wall_now = self._feedback_clock()
        currents = [0, 0, 0, 0]
        for name, axis in self._axes.items():
            currents[axis.driver.can_id - 1] = self._compute_current(name, axis, dt, wall_now)

        await self._send(currents)

    async def pause(self, *, reason: str = "") -> None:
        """送信を止める。戻り値時点で在庫の周期も送信済みでないことを保証する。

        アクチュエータ動作確認は C620 の電流指令フレーム (0x200) を自前で送るため、
        同一バスでこのループが走っていると互いのフレームを上書きし合う。
        排他は「ループ側が黙る」方向で取る (動作確認は常に短時間 + 通常制御外)。
        """
        async with self._step_lock:
            if self._paused:
                return
            self._paused = True
            logger.info(
                "位置制御ループを一時停止 (bus=%s%s)",
                self._bus_name,
                f", 理由={reason}" if reason else "",
            )

    def resume(self) -> None:
        """一時停止を解除する。

        同期メソッドにしてあるのは、動作確認側の ``finally`` から待ち合わせなしで
        必ず呼べるようにするため。復帰に失敗するとリフトが保持電流を失う。
        """
        if not self._paused:
            return
        # 停止中にモータが動かされている (動作確認の駆動) ため、古い積分と
        # 前回測定値を持ち越すと復帰した瞬間に大きな電流が出る
        for axis in self._axes.values():
            axis.pid.reset()
        # 停止していた時間が丸ごと dt に化けないよう基準時刻も取り直す
        self._last_tick = self._time_source()
        self._paused = False
        logger.info("位置制御ループを再開 (bus=%s)", self._bus_name)

    @property
    def is_paused(self) -> bool:
        return self._paused

    async def run(self) -> None:
        """停止要求まで制御を回し続ける。

        再開する場合は先に ``reset_stop_request()`` を呼ぶこと (start() は自動で呼ぶ)。
        """
        self._last_tick = self._time_source()
        try:
            while not self._stop_event.is_set():
                try:
                    await self.step()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # 例外でループを抜けると電流指令が止まる。C620 は指令断で
                    # 惰走するため、握り潰さずログに残しつつ周期は維持する
                    self._log_throttled("step", "位置制御ループの周期処理で例外", exc_info=True)
                    await self._send_zero_safely()
                await self._sleep(self._interval_s)
        finally:
            # 一時停止中でもここは送る。制御を降りる以上、0 電流で終えるのが最も安全
            await self._send_zero_safely()

    def start(self) -> None:
        """run() をバックグラウンドタスクとして起動する。"""
        if self.is_running:
            raise RuntimeError(f"位置制御ループ (bus={self._bus_name}) は既に実行中です")
        self.reset_stop_request()
        self._task = asyncio.create_task(self.run())

    def reset_stop_request(self) -> None:
        """停止要求をクリアする。run() 開始前に呼ぶ。

        run() 内でクリアしないのは、タスク起動前に stop() が呼ばれた場合に
        その要求を取りこぼして走り続けてしまうため。
        """
        self._stop_event.clear()

    def request_stop(self) -> None:
        """次の周期でループを抜けるよう要求する (同期)。"""
        self._stop_event.set()

    async def stop(self) -> None:
        """ループを止めてタスクの終了を待つ。"""
        self.request_stop()
        task = self._task
        self._task = None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------ #
    #  内部処理
    # ------------------------------------------------------------------ #

    def _elapsed(self) -> float:
        now = self._time_source()
        dt = now - self._last_tick
        self._last_tick = now
        if dt < 0.0:
            return 0.0
        return min(dt, self._max_dt_s)

    def _compute_current(self, name: str, axis: _Axis, dt: float, wall_now: float) -> int:
        if axis.target is None or axis.mode is None:
            return 0

        if self._is_feedback_stale(name, wall_now):
            if not axis.stale:
                axis.stale = True
                logger.warning(
                    "フィードバック途絶のため電流 0 に落とす (motor=%s, bus=%s)",
                    name,
                    self._bus_name,
                )
            # 古い実測値のまま PID を回すと偏差が実態から外れて暴走する
            axis.pid.reset()
            return 0

        if axis.stale:
            axis.stale = False
            logger.info("フィードバック復帰 (motor=%s, bus=%s)", name, self._bus_name)

        if axis.mode is ControlMode.CURRENT:
            return round(axis.target)

        return round(axis.pid.update(axis.target, axis.driver.multi_turn_position, dt))

    def _is_feedback_stale(self, name: str, wall_now: float) -> bool:
        last_rx = self._can_manager.last_feedback_at(name)
        if last_rx is None:
            return True
        return (wall_now - last_rx) * 1000.0 > self._feedback_timeout_ms

    def _disable_all(self) -> None:
        for axis in self._axes.values():
            axis.mode = None
            axis.target = None
            axis.pid.reset()

    async def _send(self, currents: list[int]) -> None:
        await self._can_manager.send_to_bus(
            self._bus_name, M3508Driver.encode_current_frame(currents)
        )

    async def _send_zero_safely(self) -> None:
        try:
            await self._send([0, 0, 0, 0])
        except asyncio.CancelledError:
            raise
        except Exception:
            self._log_throttled("zero", "0 電流フレームの送信に失敗", exc_info=True)

    def _log_throttled(self, key: str, message: str, *, exc_info: bool = False) -> None:
        now = self._time_source()
        last = self._log_at.get(key)
        if last is not None and now - last < _LOG_THROTTLE_S:
            return
        self._log_at[key] = now
        logger.error("%s (bus=%s)", message, self._bus_name, exc_info=exc_info)
