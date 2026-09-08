from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from lib.axis_sync import SyncGroup
from lib.config_schema import DEFAULT_HEALTH
from lib.control.feedback import FeedbackFreshness
from lib.control.periodic import PausablePeriodicTask
from lib.control.pid import PIDController
from lib.control.sync_guard import SyncGuard
from lib.control.trajectory import TrapezoidalProfile
from lib.drivers.base import ControlMode
from lib.drivers.m3508 import CURRENT_MAX, CURRENT_MIN, M3508Driver

if TYPE_CHECKING:
    from lib.can_manager import CANManager

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_INTERVAL_S",
    "M3508PositionLoop",
    "make_position_pid",
]


# 制御周期 200Hz。C620 のフィードバックは 1kHz で届くので取りこぼしはなく、
# asyncio のジッタ (数 ms) に対しても十分な余裕がある
DEFAULT_INTERVAL_S = 0.005

# asyncio が詰まって周期が飛んだときの dt 上限 (制御周期の 10 倍)。
# 実測 dt をそのまま渡すと、復帰した瞬間に積分項と微分項が跳ねて機構に衝撃が出る
DEFAULT_MAX_DT_S = 0.05

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
    # 直近周期の出力と飽和。実機チューニング CLI (scripts/tune_y_axis.py) が読む値
    # なので、制御周期ごとに更新して持たせる。**PID の内部状態から後で計算し直して
    # はならない** — 途絶や緊急停止で PID を reset した後は last_output が 0 に戻り、
    # 「飽和していたのに飽和していないと見える」周期ができる
    last_output: float = field(default=0.0)
    saturated: bool = field(default=False)
    # 台形速度プロファイル。None なら最終目標をそのままステップで PID へ入れる
    profile: TrapezoidalProfile | None = field(default=None)
    # 参照速度に掛けて feedforward へ足す係数 [counts/(指令単位/s)]
    velocity_ff: float = field(default=0.0)
    # 軌道の起点が実測位置と地続きか。False の間は次に位置制御を回す周期で張り直す。
    # 「起点を捨てる」ことが緊急停止・途絶・blocked・pause 復帰での唯一の破棄手段で、
    # ここを False にし忘れた経路は「止まっていた間に進んだはずの中間目標へ飛ぶ」
    profile_anchored: bool = field(default=False)


class M3508PositionLoop(PausablePeriodicTask):
    """1 CAN バス上の M3508 群をまとめて位置制御する非同期ループ。

    M3508 は C620 ESC 経由で電流指令しか受け付けないため、位置決めは PC 側の PID で
    ``累積角 [deg] → 電流指令 [counts]`` に変換して行う。

    **このクラスが「M3508 かつバス単位」でなければならないのは、C620 の電流指令
    フレーム (0x200) が 1 通で 4 モータ分のスロットを持つ 1 点に尽きる**
    (個別送信すると他モータのスロットを 0 で潰す)。周期タスクの骨格・鮮度判定・
    ペアの保護判断はバスに固有ではないので、それぞれ ``PausablePeriodicTask`` /
    ``FeedbackFreshness`` / ``SyncGuard`` が持つ。

    安全側の挙動:
      - 緊急停止中は電流 0 + PID リセット + 目標解除
      - フィードバックが ``feedback_timeout_ms`` を超えて途絶したら電流 0 + PID リセット
      - ``add_sync_group`` で束ねた左右直結ペアは、途絶・偏差超過をペア単位で扱う
        (1 台でも異常ならペア全員を電流 0。片側だけ生かすと機構が壊れる)
      - ``sync_kp`` を設定したペアには同期補正が加わる。**駆動中にずれを縮める
        唯一の経路**で、出さないのは電流 0 の周期と全員が位置制御中でない周期
      - ``set_motion_profile`` を設定した軸は、最終目標ではなく速度・加速度で制限した
        中間目標を PID へ入れる。**電流 0 に落とすどの経路でも軌道の起点を捨てる**
      - 周期処理で例外が出てもループは継続する (指令が止まると C620 の挙動次第で危険)
      - ``pause()`` 中は 1 通も送らない。**アクチュエータ動作確認はこのループを
        pause しない** (`RobotServer._motor_check_pausables`)
    """

    def __init__(
        self,
        can_manager: CANManager,
        bus_name: str,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        max_dt_s: float = DEFAULT_MAX_DT_S,
        feedback_timeout_ms: float = DEFAULT_HEALTH.feedback_timeout_ms,
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
        super().__init__(interval_s=interval_s, time_source=time_source, sleep=sleep, logger=logger)
        self._can_manager = can_manager
        self._bus_name = bus_name
        self._max_dt_s = max_dt_s
        self._is_estop_active = is_estop_active
        self._freshness = FeedbackFreshness(
            can_manager.last_feedback_at,
            timeout_ms=feedback_timeout_ms,
            clock=feedback_clock,
        )
        self._sync = SyncGuard(context=f"bus={bus_name}", logger=logger)

        self._axes: dict[str, _Axis] = {}
        # 生成時を基準にしておく。run() 開始時に取り直すので、生成から起動までの
        # 待ち時間が最初の dt に化けることはない
        self._last_tick: float = time_source()

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

    def add_sync_group(self, group: SyncGroup) -> None:
        """機構的に直結したモータ組を登録する。

        登録されたグループは「フィードバック途絶の判定単位」かつ「偏差監視の単位」になる。
        メンバが未登録のまま受け入れると、そのモータだけ保護から漏れて片側駆動になるため
        構成時点で弾く。重複登録や二重所属の判定は ``SyncGuard`` が行う。
        """
        for member in group.members:
            if member.name not in self._axes:
                raise ValueError(
                    f"同期グループ '{group.name}' のモータ '{member.name}' が"
                    f"このループ (bus={self._bus_name}) に未登録"
                )
        self._sync.add(group)

    def set_motion_profile(
        self, name: str, profile: TrapezoidalProfile, *, velocity_ff: float = 0.0
    ) -> None:
        """1 モータの中間目標生成器を後付けする。書かない軸はステップ入力のまま。

        **``add_motor`` の引数にしない。** 制限値は位置定数が持つのに対し
        ``add_motor`` を呼ぶ配線層はモータ構成しか見ていないので、引数に足すと
        位置定数を持たない呼び出し元 (``scripts/tune_y_axis.py`` 等) が一斉に壊れる。

        Args:
            name: このループに登録済みのモータ名
            profile: **モータの指令単位 (deg) へ換算済み**の制限を持つプロファイル
                (人間の単位からの換算は ``abs(scale)`` を知る配線層が行う)
            velocity_ff: 参照速度に掛けて ``feedforward`` へ足す係数 [counts/(deg/s)]。
                巡航中に D 項が生む制動 (``-kd * 速度``) を打ち消す

        Raises:
            KeyError: このループに居ないモータ名
            ValueError: ``velocity_ff`` が負 (進行方向と逆へ押す)
        """
        if name not in self._axes:
            raise KeyError(name)
        if velocity_ff < 0.0:
            raise ValueError(f"velocity_ff は 0 以上: {velocity_ff}")
        axis = self._axes[name]
        axis.profile = profile
        axis.velocity_ff = float(velocity_ff)
        # 起点は使う直前に実測から張る。ここで張ると、配線時点の実測位置
        # (フィードバック未受信なら 0.0) がそのまま軌道の起点として残る
        axis.profile_anchored = False

    @property
    def bus_name(self) -> str:
        return self._bus_name

    @property
    def motor_names(self) -> tuple[str, ...]:
        return tuple(self._axes)

    @property
    def sync_group_names(self) -> tuple[str, ...]:
        return self._sync.group_names

    @property
    def sync_violations(self) -> frozenset[str]:
        """偏差超過でラッチ中のグループ名。"""
        return self._sync.violations

    def _label(self) -> str:
        return f"位置制御ループ (bus={self._bus_name})"

    def pid(self, name: str) -> PIDController:
        return self._axes[name].pid

    def _paired_with(self, name: str) -> tuple[str, ...]:
        """``name`` と機構的に連動するモータ名の組 (単独なら自分だけ)。

        「1 台だけに効かせてよいか」を判断する場所を 1 つに保つ。片側だけ適用すると
        機構が壊れる操作を足すときも、この判断を書き写さずここを呼ぶこと。
        """
        group_name = self._sync.group_of(name)
        if group_name is None:
            return (name,)
        return self._sync.members_of(group_name)

    def target(self, name: str) -> float | None:
        return self._axes[name].target

    def is_saturated(self, name: str) -> bool:
        """直近周期の出力が出力レンジの端に張り付いたか。

        読み手は実機チューニング CLI (``scripts/tune_y_axis.py``) だけ。飽和している
        間はゲインを変えても応答が変わらないので、これが読めないと調整する人は
        制御以外の原因 (機構の負荷・``output_limit``) へ辿り着けない。
        """
        return self._axes[name].saturated

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
            if axis.profile is not None and axis.profile_anchored:
                # 起点は前周期に出した中間目標のまま差し替える。ここで実測から
                # 起こし直すと、左右の追従誤差の差がそのまま軌道長の差になり、
                # 偏差監視が見ているずれを能動的に作りに行くことになる
                axis.profile.retarget(axis.target)
            return

        if mode is ControlMode.CURRENT:
            # ホーミングで機構端に押し当てる等の開ループ指令。PID を通さず素通しする
            axis.pid.reset()
            # 開ループで動かしている間、中間目標は据え置かれる。位置制御へ戻る周期に
            # 実測から張り直さないと、押し当てで進んだぶんだけ機構が戻される
            axis.profile_anchored = False
            axis.mode = ControlMode.CURRENT
            axis.target = float(value)
            return

        raise ValueError(
            f"M3508 位置制御ループは POSITION / CURRENT のみ対応 (受け取った: {mode.name})"
        )

    def clear_target(self, name: str) -> None:
        """目標を解除して電流 0 にする。"""
        self._reset_axis(self._axes[name])

    @staticmethod
    def _reset_axis(axis: _Axis) -> None:
        """1 軸を「指令も履歴も持たない」状態へ戻す。

        2 箇所 (``clear_target`` / ``_disable_all``) に書くと、後から項目を足した
        ときに片方を落とせる。**中間目標の起点もここで捨てる** —— 残すと、止まって
        いた間に機構が動いていても軌道は元の位置から続き、復帰 1 周期目に
        「止まっていた間に進んだはずの中間目標」へ飛ぶ。
        """
        axis.mode = None
        axis.target = None
        axis.pid.reset()
        axis.last_output = 0.0
        axis.saturated = False
        axis.profile_anchored = False

    def set_origin_here(self, name: str) -> None:
        """現在位置を累積角の原点にする (ホーミング完了時)。

        ホーミングの手順そのものはシーケンス側に置き、ここは「今の位置を 0 と
        定義し直す」1 操作だけに留める。

        指定したモータが同期グループに属していればグループ全員を同時に確定する
        (左右を別々の時刻に確定すると消えないオフセットが残る)。**await を挟まず
        1 回で回す**ことで、制御周期が割り込む余地を無くしている。
        原点が動くと既存の目標値の意味も変わるため、目標は解除して静止させる。
        """
        self._capture_origin(self._paired_with(name))

    def set_group_origin_here(self, name: str) -> None:
        """同期グループ全員の累積角原点を同時に確定する。

        Raises:
            KeyError: 未登録のグループ名
        """
        self._capture_origin(self._sync.members_of(name))

    def _capture_origin(self, names: tuple[str, ...]) -> None:
        for motor in names:
            self._axes[motor].driver.reset_multi_turn_origin()
        for motor in names:
            self.clear_target(motor)

    def reset_sync_violation(self, name: str | None = None) -> None:
        """偏差超過のラッチを解除する (None で全グループ)。

        解除の唯一の経路は操縦者の緊急停止解除。詳細は ``SyncGuard.reset``。
        """
        self._sync.reset(name)

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

    async def _step_locked(self) -> None:
        """1 周期分の制御。``step()`` (基底) から ``_step_lock`` 保持で呼ばれる。"""
        dt = self._elapsed()

        estop = self._is_estop_active is not None and self._is_estop_active()
        if estop:
            # 解除直後に溜まった積分が一気に出るのを防ぐ。目標も落として、
            # 停止中に姿勢が崩れていても解除だけでは動き出さないようにする
            self._disable_all()

        if self._paused:
            # 同一バスの 0x200 を別の経路が握っている。0 電流フレームでも送れば
            # その指令を上書きしてしまうため 1 通も送らない
            return

        if estop:
            await self._send([0, 0, 0, 0])
            return

        wall_now = self._freshness.now()
        stale = {name: self._freshness.is_stale(name, wall_now) for name in self._axes}
        blocked = self._sync.blocked(stale=stale, position_of=self._feedback_position)
        corrections = self._sync.corrections(
            position_of=self._feedback_position,
            skip_groups=blocked | self._open_loop_groups(),
        )

        currents = [0, 0, 0, 0]
        for name, axis in self._axes.items():
            currents[axis.driver.can_id - 1] = self._compute_current(
                name,
                axis,
                dt,
                stale=stale[name],
                blocked=self._sync.group_of(name) in blocked,
                correction=corrections.get(name, 0.0),
            )

        await self._send(currents)

    async def send_stop_frame(self) -> None:
        """目標を落とし、全スロット 0 の電流指令フレームを即時に 1 通送る。

        M3508 は ``emergency_stop_message()`` を持たず 0x7FF も解釈しないため、
        この経路が無いと Y 軸だけは「ループが生きていること」に停止を委ねることに
        なる。**``_paused`` でも ``is_running`` でも止めず、``_step_lock`` も取らない**
        —— 送信が詰まった相手を待つと停止そのものが止まる。
        """
        self._disable_all()
        await self._send([0, 0, 0, 0])

    def _on_resume(self) -> None:
        # 停止中は 0x200 を握った別の経路が機構を動かしている。古い積分と
        # 前回測定値を持ち越すと復帰した瞬間に大きな電流が出る
        for axis in self._axes.values():
            axis.pid.reset()
            # 停止中も機構は動いている。中間目標を持ち越すと、復帰 1 周期目に
            # 「停止前の位置」へ戻す指令が出る (PID を reset するのと同じ理由)
            axis.profile_anchored = False
        # 停止していた時間が丸ごと dt に化けないよう基準時刻も取り直す
        self._last_tick = self._time_source()

    # ------------------------------------------------------------------ #
    #  ライフサイクル (骨格は PausablePeriodicTask)
    # ------------------------------------------------------------------ #

    async def _on_run_start(self) -> None:
        self._last_tick = self._time_source()

    async def _on_tick_error(self) -> None:
        # 例外でループを抜けると電流指令が止まる。C620 は指令断で惰走するため、
        # 握り潰さずログに残しつつ周期は維持し、その周期は 0 電流で埋める
        self._log.exception("tick", "位置制御ループの周期処理で例外 (bus=%s)", self._bus_name)
        self._discard_profile_anchors()
        await self._send_zero_safely()

    async def _on_run_exit(self) -> None:
        # 一時停止中でもここは送る。制御を降りる以上、0 電流で終えるのが最も安全
        self._discard_profile_anchors()
        await self._send_zero_safely()

    def _discard_profile_anchors(self) -> None:
        """電流 0 で終えた周期のぶん、軌道の起点と積分だけを捨てる。

        **この周期は機構へ 1 通も届いていない**のに ``_step_locked`` は送信の前に
        ``profile.advance()`` を回し終えているので、中間目標だけが先へ進む。据え置くと
        送信が戻った最初の 1 周期で PID が全差分をステップ入力として受け、**出力レンジ
        いっぱいの電流が 1 発で出る**。送信だけが落ちている間は途絶判定が立たないので、
        ここで捨てないと回復する経路がどこにも無い (qdisc の ENOBUFS がその場合)。

        **目標そのものは残す。** 送信の一過性の失敗は「シーケンスの指令が無効に
        なった」ことを意味しない —— 捨てると送信が 1 周期落ちるたびに
        ``wait_reached`` が永久に到達せずシーケンスが失敗する。
        """
        for axis in self._axes.values():
            axis.pid.reset()
            axis.profile_anchored = False

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

    def _feedback_position(self, name: str) -> float:
        return self._axes[name].driver.feedback_position()

    def _open_loop_groups(self) -> frozenset[str]:
        """全員が位置制御中とは言えない同期グループ名。

        同期補正は「左右が同じ目標を追っている」ことを前提にするので、前提が崩れる
        周期では 1 台にも出さない。判定を**グループ単位**にするのが要点で、モータ単位で
        書くと、ホーミングの押し当てで片方だけがモードを変えた瞬間その 1 台にだけ
        補正が乗り、左右で打ち消し合うはずの力が軸ごと押し動かす力になる。
        """
        return frozenset(
            group
            for group in self._sync.group_names
            if not all(
                self._axes[member].mode is ControlMode.POSITION
                and self._axes[member].target is not None
                for member in self._sync.members_of(group)
            )
        )

    def _compute_current(
        self,
        name: str,
        axis: _Axis,
        dt: float,
        *,
        stale: bool,
        blocked: bool,
        correction: float,
    ) -> int:
        """1 モータ分の電流指令を決め、飽和も同じ周期のうちに判定する。

        飽和を後から PID の内部状態から計算し直さないのは、途絶や緊急停止で
        reset した後の値を読むことになるため。同期補正も同じ理由でここを通す
        (補正込みの出力で見ないと、上限へ張り付いている周期を見逃す)。
        """
        output, closed_loop = self._control_output(
            name, axis, dt, stale=stale, blocked=blocked, correction=correction
        )

        axis.last_output = output
        axis.saturated = closed_loop and self._is_saturated(axis, output)
        return round(output)

    @staticmethod
    def _is_saturated(axis: _Axis, output: float) -> bool:
        # 出力レンジの端に届いているかを、レンジ幅に対する相対誤差ではなく
        # 絶対値の近さで見る。C620 の指令は整数 counts なので 1 counts 未満の
        # 差は指令として区別できない
        return output >= axis.pid.output_max - 1.0 or output <= axis.pid.output_min + 1.0

    def _control_output(
        self, name: str, axis: _Axis, dt: float, *, stale: bool, blocked: bool, correction: float
    ) -> tuple[float, bool]:
        """電流指令 [counts] と、それが位置制御ループの出力かどうかを返す。

        ``correction`` は左右直結ペアを揃えるための同期補正 [counts]。PID の外で
        足さずに ``feedforward`` として渡すのは、外で足すとクランプが二重になるうえ、
        PID 側のアンチワインドアップが補正を知らないまま積分を進めるため
        (詳細は ``PIDController.update``)。
        """
        if axis.target is None or axis.mode is None:
            return 0.0, False

        if stale:
            if not axis.stale:
                axis.stale = True
                logger.warning(
                    "フィードバック途絶のため電流 0 に落とす (motor=%s, bus=%s)",
                    name,
                    self._bus_name,
                )
            # 古い実測値のまま PID を回すと偏差が実態から外れて暴走する
            axis.pid.reset()
            # 途絶中に機構がどこへ動いたか分からない。軌道の起点は復帰した周期に
            # 実測から張り直す (据え置くと復帰 1 周期目に途絶前の位置へ戻す指令が出る)
            axis.profile_anchored = False
            return 0.0, False

        if axis.stale:
            axis.stale = False
            logger.info("フィードバック復帰 (motor=%s, bus=%s)", name, self._bus_name)

        if blocked:
            # 相方が止まっている (途絶 or 偏差超過)。目標は残したまま力だけ抜く
            # (復帰時に保持位置を作り直さずに済む)
            axis.pid.reset()
            # 力を抜いている間に機構は自重で落ちたり相方に引かれたりするので、
            # 軌道の起点は復帰した周期に実測から張り直す
            axis.profile_anchored = False
            return 0.0, False

        if axis.mode is ControlMode.CURRENT:
            return float(axis.target), False

        setpoint = axis.target
        feedforward = correction
        if axis.profile is not None:
            if not axis.profile_anchored:
                self._anchor_profile(axis)
            # **PID と同じ dt で進める。** 別の値を渡すと、返る参照速度と中間目標の
            # 進み方が食い違い、速度フィードフォワードが実際の軌道と噛み合わなくなる
            setpoint, reference_velocity = axis.profile.advance(dt)
            # 速度 FF も PID の内側へ渡す。外で足すとクランプが二重になるうえ、
            # アンチワインドアップが FF を知らないまま積分を進める
            feedforward += axis.velocity_ff * reference_velocity

        return (
            axis.pid.update(
                setpoint,
                axis.driver.multi_turn_position,
                dt,
                feedforward=feedforward,
            ),
            True,
        )

    @staticmethod
    def _anchor_profile(axis: _Axis) -> None:
        """軌道の起点を実測位置へ張り直す。

        実測を起点にしてよいのは位置制御へ入る最初の 1 周期だけ。毎周期実測から
        起こし直すと、左右直結ペアの追従誤差の差がそのまま軌道長の差になり、
        偏差監視が見ているずれを能動的に作りに行くことになる。
        """
        profile = axis.profile
        if profile is None:
            return
        profile.reset(axis.driver.multi_turn_position)
        if axis.target is not None:
            profile.retarget(axis.target)
        axis.profile_anchored = True

    def _disable_all(self) -> None:
        for axis in self._axes.values():
            self._reset_axis(axis)

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
            self._log.exception("zero", "0 電流フレームの送信に失敗 (bus=%s)", self._bus_name)
