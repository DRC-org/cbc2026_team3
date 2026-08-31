from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict

from lib.axis_sync import SyncGroup
from lib.config_schema import DEFAULT_HEALTH, TuningSettings
from lib.control.feedback import FeedbackFreshness
from lib.control.periodic import PausablePeriodicTask
from lib.control.pid import PIDController
from lib.control.sync_guard import SyncGuard
from lib.drivers.base import ControlMode
from lib.drivers.m3508 import CURRENT_MAX, CURRENT_MIN, M3508Driver
from lib.tuning.recorder import Capture, MotorStepRecorder, PidSnapshot

if TYPE_CHECKING:
    from lib.can_manager import CANManager

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_INTERVAL_S",
    "MAX_TUNABLE_GAIN",
    "TUNABLE_PID_KEYS",
    "M3508PositionLoop",
    "PidGains",
    "make_position_pid",
]


class PidGains(TypedDict):
    """UI へ配る 1 モータ分の現在ゲイン。

    ``applies_to`` を同じ dict に入れてあるのは、「現在値」と「送ると誰に効くか」を
    別々に運ぶと片方だけ更新された状態が作れてしまうため。
    """

    kp: float
    ki: float
    kd: float
    applies_to: list[str]


# 実行中に差し替えてよい PID パラメータ。出力レンジ・不感帯・積分上限は機構の
# 保護値なので操縦者の調整対象にしない (誤って緩めると保護が消える)
TUNABLE_PID_KEYS: tuple[str, ...] = ("kp", "ki", "kd")

# 実行中に受け付けるゲインの上限。出力は ±CURRENT_MAX [counts] に飽和するので、
# これを超えるゲインは「不感帯を出た瞬間に必ず上限へ張り付く」バンバン制御に
# しかならず、調整の意味を持たない。下限 (負のゲイン = 正帰還) だけを弾いて
# 上限を置かないと、kp=1e6 のような打ち間違いがそのまま通り、目標を入れた瞬間や
# 緊急停止を解除した瞬間にフルスケール電流が出る
MAX_TUNABLE_GAIN: float = float(CURRENT_MAX)

# 制御周期 200Hz。C620 のフィードバックは 1kHz で届くので取りこぼしはなく、
# asyncio のジッタ (数 ms) に対しても十分な余裕がある
DEFAULT_INTERVAL_S = 0.005

# asyncio が詰まって周期が飛んだときの dt 上限 (制御周期の 10 倍)。
# 実測 dt をそのまま渡すと、復帰した瞬間に積分項と微分項が跳ねて機構に衝撃が出る
DEFAULT_MAX_DT_S = 0.05

TargetSink = Callable[[ControlMode, float], Awaitable[None]]
EStopChecker = Callable[[], bool]
SleepFunc = Callable[[float], Awaitable[None]]
#: 記録の受け渡し先。制御周期の中から呼ばれるので同期かつ O(1) であること
CaptureSink = Callable[[Capture], None]


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
    # 直近周期の出力と飽和。テレメトリ (20Hz) が読む値なので、制御周期ごとに
    # 更新して持たせる。**PID の内部状態から後で計算し直してはならない** —
    # 途絶や緊急停止で PID を reset した後は last_output が 0 に戻り、
    # 「飽和していたのに飽和していないと見える」周期ができる
    last_output: float = field(default=0.0)
    saturated: bool = field(default=False)
    # ステップ応答の記録器。tuning が無効なら None
    recorder: MotorStepRecorder | None = field(default=None)


class M3508PositionLoop(PausablePeriodicTask):
    """1 CAN バス上の M3508 群をまとめて位置制御する非同期ループ。

    M3508 は C620 ESC 経由で電流指令しか受け付けないため、位置決めは PC 側の PID で
    ``累積角 [deg] → 電流指令 [counts]`` に変換して行う。

    バス単位でまとめる理由: C620 の電流指令フレーム (0x200) は 1 通で 4 モータ分の
    スロットを持つ。モータごとに個別送信すると、自分以外のスロットを 0 で上書きして
    同一バス上の他モータがカクつくため、必ず全モータ分を 1 フレームに束ねて送る。
    **このクラスが「M3508 かつバス単位」でなければならないのはこの 1 点に尽きる。**
    周期タスクの骨格・鮮度判定・ペアの保護判断はいずれもバスに固有ではないので、
    それぞれ ``PausablePeriodicTask`` / ``FeedbackFreshness`` / ``SyncGuard`` が持つ。

    安全側の挙動:
      - 緊急停止中は電流 0 + PID リセット + 目標解除
      - フィードバックが ``feedback_timeout_ms`` を超えて途絶したら電流 0 + PID リセット
      - ``add_sync_group`` で束ねた左右直結ペアは、途絶・偏差超過をペア単位で扱う
        (1 台でも異常ならペア全員を電流 0。片側だけ生かすと機構が壊れる)
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
        feedback_timeout_ms: float = DEFAULT_HEALTH.feedback_timeout_ms,
        is_estop_active: EStopChecker | None = None,
        tuning: TuningSettings | None = None,
        capture_sink: CaptureSink | None = None,
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
            tuning: ステップ応答の記録設定。None または enabled=False で記録しない
            capture_sink: 窓が閉じた記録の受け渡し先。**同期関数で、O(1) で返ること。**
                ここで await したり解析を行ったりすると、200Hz の制御周期が
                配信の都合で伸びる (解析と配信は lib/tuning/report.py と
                server.py が受け持つ)
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
        self._tuning = tuning
        self._capture_sink = capture_sink
        # 窓が閉じた記録は、送信を終えてから渡す。制御の送信より先に配信側の
        # 都合を挟むと、記録機能の不具合がそのまま指令の遅れになる
        self._pending_captures: list[Capture] = []

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
        self._axes[name] = _Axis(driver=driver, pid=pid, recorder=self._make_recorder(name, pid))

    def _make_recorder(self, name: str, pid: PIDController) -> MotorStepRecorder | None:
        if self._tuning is None or not self._tuning.enabled:
            return None
        return MotorStepRecorder(
            name,
            # ゲインは記録の起点で読み直す。生成時に値をコピーすると、調整で
            # 差し替えたゲインが記録には古いまま載り、波形とゲインの対応が崩れる
            gains_snapshot=lambda: PidSnapshot(
                kp=pid.kp, ki=pid.ki, kd=pid.kd, dead_band=pid.dead_band
            ),
            window_s=self._tuning.window_s,
            pre_trigger_s=self._tuning.pre_trigger_s,
            min_step=self._tuning.min_step_deg,
            # 時刻が進まない異常時の歯止め。窓の長さから決まるので設定を増やさない
            max_samples=int(
                (self._tuning.window_s + self._tuning.pre_trigger_s) / max(self._interval_s, 1e-6)
            )
            + 16,
        )

    def add_sync_group(self, group: SyncGroup) -> None:
        """機構的に直結したモータ組を登録する。

        登録されたグループは「フィードバック途絶の判定単位」かつ「偏差監視の単位」になる。
        メンバが未登録のまま受け入れると、そのモータだけ保護から漏れて片側駆動になるため
        構成時点で弾く。ここでしか分からないのは「このループが握っているモータか」だけで、
        重複登録や二重所属の判定は ``SyncGuard`` が行う。
        """
        for member in group.members:
            if member.name not in self._axes:
                raise ValueError(
                    f"同期グループ '{group.name}' のモータ '{member.name}' が"
                    f"このループ (bus={self._bus_name}) に未登録"
                )
        self._sync.add(group)

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

    def pid_gains(self, name: str) -> PidGains:
        """UI へ配る現在ゲイン。``set_pid_gain`` の対になる読み口。

        ``applies_to`` はこのモータへ送ったときに実際に適用されるモータ名で、
        左右直結ペアなら両方が入る。ここまで含めて配るのは、「1 台だけに効かせて
        よいか」の判断を ``_paired_with()`` の 1 箇所に保つため。UI に名前から
        推測させると判断が 2 箇所に増え、片方だけ直したときに気付けない。

        Raises:
            KeyError: このループに居ないモータ名
        """
        pid = self._axes[name].pid
        return {
            "kp": pid.kp,
            "ki": pid.ki,
            "kd": pid.kd,
            "applies_to": list(self._paired_with(name)),
        }

    def set_pid_gains(self, name: str, gains: Mapping[str, float]) -> tuple[str, ...]:
        """実行中に PID ゲインを差し替え、実際に更新したモータ名を返す。

        3 値を 1 回で入れる。項目ごとに分けて呼ぶと、混ざった状態が 200Hz の
        制御周期をまたいで残る (kp だけ新しく ki は古い、という組み合わせで
        1 周期以上回る)。指定しなかった項目は据え置く。

        同期グループのメンバを指定した場合はグループ全員へ同じ値を入れる。
        左右直結ペアで追従特性が変わると互いに押し合って機構が壊れるため、
        「片側だけ別ゲイン」という状態をサーバー側で作れてはならない
        (チューニング UI はモータ 1 基ずつしか送れない)。

        Raises:
            KeyError: このループに居ないモータ名
            ValueError: 実行中の差し替え対象でないパラメータ名、または空の指定
        """
        if name not in self._axes:
            raise KeyError(name)
        if not gains:
            # 何も指定しない差し替えは誤送信。黙って成功させると、操縦者は
            # 送ったつもりで一切効いていない状態に気付けない
            raise ValueError("差し替えるゲインが 1 つも指定されていません")

        unknown = [key for key in gains if key not in TUNABLE_PID_KEYS]
        if unknown:
            raise ValueError(
                f"実行中に変更できるのは {'/'.join(TUNABLE_PID_KEYS)} のみ: {', '.join(unknown)}"
            )

        applied = {key: float(value) for key, value in gains.items()}
        targets = self._paired_with(name)
        for target in targets:
            self._axes[target].pid.set_gains(**applied)
        # 記録中の窓は捨てる。前半を旧ゲイン・後半を新ゲインで動いた波形は
        # どちらの結果でもなく、しかも「送ってすぐ届いた記録」なので操縦者は
        # 新しいゲインの応答だと読む
        self._abort_recording(targets)
        return targets

    def _paired_with(self, name: str) -> tuple[str, ...]:
        """``name`` と機構的に連動するモータ名の組 (単独なら自分だけ)。

        「1 台だけに効かせてよいか」を判断する場所を 1 つに保つ。ゲイン変更にも
        原点確定にも同じ答えが要る (どちらも片側だけ適用すると機構が壊れる)。
        """
        group_name = self._sync.group_of(name)
        if group_name is None:
            return (name,)
        return self._sync.members_of(group_name)

    def target(self, name: str) -> float | None:
        return self._axes[name].target

    def is_saturated(self, name: str) -> bool:
        """直近周期の出力が出力レンジの端に張り付いたか。

        テレメトリに載せる。飽和している間はゲインを変えても応答が変わらないので、
        これが見えないと操縦者は「kp を上げても下げても同じ」という観察から
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
        self._reset_axis(self._axes[name])
        self._abort_recording((name,))

    @staticmethod
    def _reset_axis(axis: _Axis) -> None:
        """1 軸を「指令も履歴も持たない」状態へ戻す。

        1 軸ぶんのリセットを 2 箇所 (``clear_target`` / ``_disable_all``) に書くと、
        後から項目を足したときに片方を落とせる。``saturated`` を落とすと
        「緊急停止中だけ飽和表示が残る」形になり、操縦者は止まっている機体の
        テレメトリを見て出力が張り付いていると読む。

        **記録の破棄 (``_abort_recording``) はここに含めない。** 1 軸だけを畳む
        ``clear_target`` と全軸を畳む ``_disable_all`` では、溜まっていた完成品を
        捨てるかどうかが違う (前者は残す) ため、呼び分けを潰してはならない。
        """
        axis.mode = None
        axis.target = None
        axis.pid.reset()
        axis.last_output = 0.0
        axis.saturated = False

    def set_origin_here(self, name: str) -> None:
        """現在位置を累積角の原点にする (ホーミング完了時)。

        ホーミングの手順そのもの (どの順にどこへ押し当てるか) はシーケンス側に置き、
        このループが提供するのは「今の位置を 0 と定義し直す」という 1 操作だけに
        留める。手順が変わってもここは触らない。

        指定したモータが同期グループに属していればグループ全員を同時に確定する。
        左右を別々の時刻に原点確定すると、その間に片方が動いた分だけ偏差が最初から
        オフセットを持ち、正常な動作でも即座に偏差超過で止まってしまう。
        await を挟まず 1 回で回すことで、制御周期が割り込む余地を無くしている。

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
            # 動作確認が同一バスの 0x200 を占有している。0 電流フレームでも
            # 送れば動作確認の指令を上書きしてしまうため 1 通も送らない。
            # 記録もここでは触らない — 停止中の動きはこのループの指令ではないが、
            # 窓を捨てる責務は `_on_resume` が 1 箇所で持つ (ここにも書くと、
            # 片方を消しても falls back して落ちない層ができる)
            return

        if estop:
            await self._send([0, 0, 0, 0])
            return

        wall_now = self._freshness.now()
        stale = {name: self._freshness.is_stale(name, wall_now) for name in self._axes}
        blocked = self._sync.blocked(stale=stale, position_of=self._feedback_position)

        currents = [0, 0, 0, 0]
        for name, axis in self._axes.items():
            currents[axis.driver.can_id - 1] = self._compute_current(
                name,
                axis,
                dt,
                stale=stale[name],
                blocked=self._sync.group_of(name) in blocked,
                now=self._last_tick,
            )

        await self._send(currents)
        # 送信を終えてから渡す。指令より先に記録機能の都合を挟むと、そちらの
        # 不具合がそのまま指令の遅れになる
        self._flush_captures()

    async def send_stop_frame(self) -> None:
        """目標を落とし、全スロット 0 の電流指令フレームを即時に 1 通送る。

        緊急停止の送信経路から呼ぶ。M3508 は ``emergency_stop_message()`` を持たず
        自作モタドラ向けの 0x7FF も解釈しないため、この経路が無いと左右直結の
        Y 軸だけは「ループが生きていて電流 0 を送り続けてくれること」に停止を
        委ねることになる。ループが死んでいても止められる形にしておく。

        ``_paused`` でも ``is_running`` でも止めない。動作確認が 0x200 を握って
        いる最中であっても、緊急停止の 0 電流はそのまま上書きしてよい (むしろ
        上書きさせたい)。``_step_lock`` も取らない。送信が詰まった相手を待つと
        停止そのものが止まるため、在庫の 1 周期と競合しても全スロット 0 を
        先に出すことを優先する (次の周期も緊急停止中なら 0 を出す)。
        """
        self._disable_all()
        await self._send([0, 0, 0, 0])

    def _on_resume(self) -> None:
        # 停止中にモータが動かされている (動作確認の駆動) ため、古い積分と
        # 前回測定値を持ち越すと復帰した瞬間に大きな電流が出る
        for axis in self._axes.values():
            axis.pid.reset()
        # 停止していた間の動きは記録できていない。窓を持ち越すと停止前後が
        # 地続きの 1 回の応答として綴じられる (時間の飛んだ波形になる)
        self._abort_recording()
        # 停止していた時間が丸ごと dt に化けないよう基準時刻も取り直す
        self._last_tick = self._time_source()

    # ------------------------------------------------------------------ #
    #  ライフサイクル (骨格は PausablePeriodicTask)
    # ------------------------------------------------------------------ #

    async def _on_run_start(self) -> None:
        self._last_tick = self._time_source()
        # 停止していた間の動きは記録できていない。窓を持ち越すと、起動前後が
        # 地続きの 1 回の応答として綴じられる (時間の飛んだ波形になる)
        self._abort_recording()

    async def _on_tick_error(self) -> None:
        # 例外でループを抜けると電流指令が止まる。C620 は指令断で惰走するため、
        # 握り潰さずログに残しつつ周期は維持し、その周期は 0 電流で埋める
        self._log.exception("tick", "位置制御ループの周期処理で例外 (bus=%s)", self._bus_name)
        await self._send_zero_safely()

    async def _on_run_exit(self) -> None:
        # 一時停止中でもここは送る。制御を降りる以上、0 電流で終えるのが最も安全
        await self._send_zero_safely()

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

    def _compute_current(
        self, name: str, axis: _Axis, dt: float, *, stale: bool, blocked: bool, now: float
    ) -> int:
        """1 モータ分の電流指令を決め、同じ周期の観測を記録する。

        制御と記録を同じ場所に置くのは、「どの周期の指令がどの実測に対応するか」を
        ずらさないため。別の場所で後から集め直すと、途絶や緊急停止で PID を reset
        した後の値を読むことになり、飽和していた周期が飽和していないと記録される。
        """
        output, closed_loop = self._control_output(name, axis, dt, stale=stale, blocked=blocked)

        axis.last_output = output
        axis.saturated = closed_loop and self._is_saturated(axis, output)
        # 位置制御が閉じている周期だけがステップ応答として意味を持つ。開ループの
        # 電流指令 (ホーミングの押し当て) や途絶中を混ぜると、ゲインと無関係な
        # 波形が「応答」として記録される
        self._record(axis, now, target=axis.target if closed_loop else None)
        return round(output)

    @staticmethod
    def _is_saturated(axis: _Axis, output: float) -> bool:
        # 出力レンジの端に届いているかを、レンジ幅に対する相対誤差ではなく
        # 絶対値の近さで見る。C620 の指令は整数 counts なので 1 counts 未満の
        # 差は指令として区別できない
        return output >= axis.pid.output_max - 1.0 or output <= axis.pid.output_min + 1.0

    def _record(self, axis: _Axis, now: float, *, target: float | None) -> None:
        if axis.recorder is None:
            return
        capture = axis.recorder.record(
            now,
            target=target,
            position=axis.driver.multi_turn_position,
            output=axis.last_output,
            saturated=axis.saturated,
        )
        if capture is not None:
            self._pending_captures.append(capture)

    def _control_output(
        self, name: str, axis: _Axis, dt: float, *, stale: bool, blocked: bool
    ) -> tuple[float, bool]:
        """電流指令 [counts] と、それが位置制御ループの出力かどうかを返す。"""
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
            return 0.0, False

        if axis.stale:
            axis.stale = False
            logger.info("フィードバック復帰 (motor=%s, bus=%s)", name, self._bus_name)

        if blocked:
            # 自分は健全でも、同じ機構に直結した相方が止まっている (途絶 or 偏差超過)。
            # 目標は残したまま力だけ抜く (復帰時に保持位置を作り直さずに済む)
            axis.pid.reset()
            return 0.0, False

        if axis.mode is ControlMode.CURRENT:
            return float(axis.target), False

        return axis.pid.update(axis.target, axis.driver.multi_turn_position, dt), True

    def _disable_all(self) -> None:
        for axis in self._axes.values():
            self._reset_axis(axis)
        self._abort_recording()

    def _abort_recording(self, names: tuple[str, ...] | None = None) -> None:
        """記録中の窓を捨てる。

        **応答の意味が変わる事象では必ず呼ぶ。** 途中で電流 0 に落とされた波形を
        残すと「行き過ぎもせず整定もしない応答」として記録され、操縦者はゲインが
        悪いのだと読む。溜まっていた完成品も一緒に捨てるのは、同じ理由で
        「配る価値のある記録かどうか」がこの時点で分からなくなるため。
        """
        for name in names if names is not None else tuple(self._axes):
            recorder = self._axes[name].recorder
            if recorder is not None:
                recorder.abort()
        if names is None:
            self._pending_captures.clear()

    def _flush_captures(self) -> None:
        if self._capture_sink is None:
            self._pending_captures.clear()
            return
        for capture in self._pending_captures:
            self._capture_sink(capture)
        self._pending_captures.clear()

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
