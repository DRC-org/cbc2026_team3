from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING

from lib.drivers.base import ControlMode
from lib.sequence.positions import PositionLookupError

if TYPE_CHECKING:
    from lib.can_manager import CANManager
    from lib.drivers.base import MotorDriver, MotorState
    from lib.sequence.positions import AxisSpec

# 到達待ちのポーリング間隔。CAN フィードバックは 1kHz 前後で届くため
# 10ms 周期なら取りこぼしがなく、asyncio ループへの負荷も無視できる
_DEFAULT_POLL_INTERVAL_S = 0.01

TargetSink = Callable[[ControlMode, float], Awaitable[None]]
EStopChecker = Callable[[], bool]


class EStopActiveError(RuntimeError):
    """緊急停止中にモータ指令を出そうとしたときに送出される。"""


class WaitInterruptedError(RuntimeError):
    """目標を持った状態で到達待ちに入ったのに、待機中に目標が消えたときに送出される。

    ``is_reached()`` は「目標が無ければ到達済み」を返す (一度も指令していない軸を
    誤って待たせないため)。その判定のまま ``wait_reached`` の実行中に緊急停止が
    目標を刈り取ると、「中断」が「到達」にすり替わり、``move_to`` は中断された動作を
    ステップ成功として扱ってしまう。

    ``SequenceTimeoutError`` にせず専用の例外にしたのは、中断はタイムアウトでは
    ないので「目標位置に到達しませんでした」が嘘になるため。
    """


class MotorHandle:
    """1 モータへの目標値送信と到達待ちを担うハンドル。"""

    def __init__(
        self,
        name: str,
        driver: MotorDriver,
        can_manager: CANManager,
        *,
        is_estop_active: EStopChecker | None = None,
        target_sink: TargetSink | None = None,
        poll_interval: float = _DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self._name = name
        self._driver = driver
        self._can_manager = can_manager
        self._is_estop_active = is_estop_active
        self._target_sink = target_sink
        self._poll_interval = poll_interval
        self._target: float | None = None
        self._mode: ControlMode | None = None

    # ---- 参照系 ----

    @property
    def name(self) -> str:
        return self._name

    @property
    def driver(self) -> MotorDriver:
        return self._driver

    @property
    def state(self) -> MotorState:
        return self._driver.state

    @property
    def target(self) -> float | None:
        return self._target

    @property
    def mode(self) -> ControlMode | None:
        return self._mode

    @property
    def has_target(self) -> bool:
        return self._target is not None and self._mode is not None

    # ---- 指令系 ----

    async def set_target(self, mode: ControlMode, value: float) -> None:
        """目標値を送信する。緊急停止中は送信せず EStopActiveError を送出する。"""
        # 緊急停止はサーバ層でも遮断しているが、ロボットが動く経路には多重に安全装置を置く
        if self._is_estop_active is not None and self._is_estop_active():
            raise EStopActiveError(f"緊急停止中のためモータ '{self._name}' に指令できません")

        value = float(value)
        await self._dispatch(mode, value)

        self._mode = mode
        self._target = value

    async def resend_target(self) -> bool:
        """最後に送った目標値をもう一度送る。送ったら True。

        自作モータドライバのファームは 500ms 自分宛の SET_TARGET が来ないと出力を
        止める (docs/motor_driver_can_protocol.md §5.1)。``set_target`` は値が
        変わったときにしか送らないため、この再送が無いとコンベアは回し始めて
        500ms で止まる。

        目標が一度も設定されていなければ何も送らない (起動直後に意図しない駆動を
        作らない)。緊急停止中も送らない — 例外にせず黙って見送るのは、これが
        周期タスクからの呼び出しで、停止中は「送らないことが正常」だからである。
        """
        if self._target is None or self._mode is None:
            return False
        if self._is_estop_active is not None and self._is_estop_active():
            return False

        await self._dispatch(self._mode, self._target)
        return True

    async def _dispatch(self, mode: ControlMode, value: float) -> None:
        """1 指令を送る唯一の経路。**インターロックと状態更新は呼び出し側が持つ。**

        初回 (``set_target``) と再送 (``resend_target``) で経路が分かれていると、
        片方だけ差し込み口を通す形に直せてしまう —— 症状は「初回だけ PID を通り、
        再送は生の CAN へ出る」で、M3508 の位置制御ループが同じ周期に 2 種類の
        電流指令を出すことになる。

        緊急停止の扱いを共通化しないのは、2 つの呼び出し元で正しい振る舞いが
        違うため (``set_target`` は例外、``resend_target`` は黙って False)。
        """
        if self._target_sink is not None:
            # M3508 のようにドライバ単体では目標モードを表現できないモータ向けの差し込み口。
            # PC 側の制御ループが目標値を受け取り、実際の CAN 送信を代行する
            await self._target_sink(mode, value)
        else:
            await self._can_manager.send(self._name, self._driver.encode_target(mode, value))

    def clear_target(self) -> None:
        """到達待ちの対象から外す。"""
        self._target = None
        self._mode = None

    # ---- 到達判定 ----

    def is_reached(self, *, tolerance: float | None = None) -> bool:
        """目標に到達していれば True。目標を持っていなければ常に True。

        後者は「一度も指令していない軸を待たせない」ための意図的な設計であり、
        変えてはならない。**待機の途中で目標が消えた「中断」をここで検出しては
        ならない** —— ここは 1 回きりの状態確認で「消えた」ことを言う立場に無く、
        待ち始めた時点との比較が要る。その区別は ``wait_reached`` 側の責務。
        """
        if self._target is None or self._mode is None:
            return True
        return self._driver.is_target_reached(self._target, self._mode, tolerance=tolerance)

    async def wait_reached(
        self,
        *,
        tolerance: float | None = None,
        timeout: float | None = None,
        expect_target: bool = False,
    ) -> bool:
        """目標到達を待つ。到達すれば True、タイムアウトなら False。

        目標を持った状態で待ち始めたのに、待機中に目標が消えたら
        (``clear_target`` —— 緊急停止の ``TargetRefresher.clear_targets()`` など)
        ``WaitInterruptedError`` を送出する。``is_reached()`` は「目標が無ければ
        到達済み」を返すため、この区別をここでしないと中断がそのまま「到達」に
        すり替わり、``move_to`` は中断された動作を成功として記録してしまう。

        目標を一度も持たずに待ち始めた場合 (``has_target`` が最初から False) は
        従来どおり即座に True を返す —— こちらは中断ではなく「待つ必要が無い」。

        **``expect_target`` は「呼ぶ直前に指令を送った」と呼び出し側が宣言する印。**
        立っていれば、待ち始めた時点で目標が無いことも中断として扱う。
        待機開始時点のスナップショットだけでは、``move_to`` が全軸へ
        ``set_target_value`` を送り終えてから ``wait_reached`` を作るまでの窓で
        消えた目標を拾えない —— その軸は「最初から目標無し」に見えるので
        「待つ必要が無い」と同じ扱いになり、**中断がステップ成功に化ける**。
        窓は数 ms だが、そこへ入るのは緊急停止そのものである。
        ここを時刻の勝負にしない (実時間で狙うテストは踏めた日だけ緑になる)。

        判断を呼び出し側に置くのは、**指令を送ったかどうかを知っているのが
        呼び出し側だけ**だからである。``has_target`` を見て推測すると、
        まさに消えた場合と一度も指令していない場合が区別できない。
        """
        had_target = self.has_target or expect_target
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if had_target and not self.has_target:
                raise WaitInterruptedError(
                    f"モータ '{self._name}' の到達待ちが中断されました"
                    " (緊急停止などで目標値がクリアされました)"
                )
            if self.is_reached(tolerance=tolerance):
                return True
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                await asyncio.sleep(min(self._poll_interval, remaining))
            else:
                await asyncio.sleep(self._poll_interval)


class MotorGroup:
    """モータ名でハンドルを引くコンテナ。シーケンスからは属性アクセスで使う。"""

    def __init__(self, handles: Mapping[str, MotorHandle] | None = None) -> None:
        self._handles: dict[str, MotorHandle] = dict(handles) if handles else {}

    def add(self, handle: MotorHandle) -> None:
        self._handles[handle.name] = handle

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._handles)

    @property
    def handles(self) -> tuple[MotorHandle, ...]:
        return tuple(self._handles.values())

    def __getitem__(self, name: str) -> MotorHandle:
        return self._handles[name]

    def __contains__(self, name: object) -> bool:
        return name in self._handles

    def __iter__(self) -> Iterator[str]:
        return iter(self._handles)

    def __len__(self) -> int:
        return len(self._handles)

    def __getattr__(self, name: str) -> MotorHandle:
        # 内部属性の探索まで拾うと __init__ 前の参照や copy/pickle が壊れる
        if name.startswith("_"):
            raise AttributeError(name)
        handles = self.__dict__.get("_handles", {})
        if name in handles:
            return handles[name]
        # 試合中に原因不明で止まらないよう、利用可能なモータ名を必ず添える
        available = ", ".join(handles) or "(なし)"
        raise AttributeError(f"モータ '{name}' は存在しません。利用可能なモータ: {available}")


class AxisHandle:
    """1 論理軸 (1〜N モータ) への指令と到達待ちをまとめるハンドル。

    軸の状態は AxisSpec と MotorHandle 側にしかないため、``move_to`` のたびに
    生成してよい。ここに寿命のある状態を持たせないのは、同じ軸を別経路
    (手動操作・動作確認) から動かしたときに古い目標値が残るのを避けるため。
    """

    def __init__(self, spec: AxisSpec, handles: Sequence[MotorHandle]) -> None:
        self._spec = spec
        self._handles = tuple(handles)
        self._motors = {motor.name: motor for motor in spec.motors}

    @property
    def name(self) -> str:
        return self._spec.name

    async def set_target_value(self, commands: Mapping[str, float]) -> None:
        """モータ名 → 指令値をまとめて送る。

        逐次 await しないのは、左右直結の軸で送信に時間差が出ると機構がねじれるため。

        **1 台でも失敗したら、送信に成功した側の目標だけを捨てる。** 素の ``gather`` は
        最初の例外で抜けるが残りのタスクはキャンセルされずに完走するので、片方の送信だけが
        失敗すると**成功した側にだけ新しい目標が残る**。問い合わせ駆動のモータ
        (EDULITE 05 / DM3520) では ``QueryDrivenTargetRefresher`` が 20Hz でその
        1 台だけを新目標へ押し続け、もう片方には ``idle_target_value()`` を書き続ける
        —— 左右直結の軸が片側だけ動き、最後は ``SyncMonitor`` の偏差超過で全体緊急
        停止になる。docs/invariants.md が禁じている「ペア軸に片側だけ効く操作」がエラー経路で
        成立してしまう。捨てた側は再送が ``idle_target_value()`` (今の姿勢を保て) へ
        切り替わるので、既に飛んでしまった 1 通ぶんの動きもその場で止まる。

        **失敗した側の目標は捨てない。捨てる範囲を「指令した全員」へ広げてはならない。**
        ここは ``move_to`` / 手動操縦 / 零点確定の共通口で、通る軸の多くは単一モータの
        generic 軸 (電磁弁 6 個・ポンプ 2 個・コンベア・サーボ 3 軸) である。全員を捨てると
        **モータ 1 台の軸でも送信 1 通の失敗でそれまで有効だった目標が消え**、
        ``GenericTargetRefresher`` は「目標が無い = 送らない」なので 20Hz の再送がその場で
        止まる —— 500ms 後にファームの ``command_timeout_ms`` が満了し、電磁弁は消磁して
        **吸着中のワークが落ちる** (電磁弁基板は「止める = 消磁」の一手しか持たない)。
        失敗した側は 1 通も飛んでいない以上、旧目標こそが基板が現に実行している状態と
        一致しているので、残すほうが整合的である。

        Raises:
            BaseException: 送信に失敗した例外のうち、``self._handles`` の並びで最初の
                もの (``gather`` の結果順であって「最初に送出された例外」ではない)。
                ``ExceptionGroup`` へ包まないのは、呼び出し側 (``move_to`` / 手動) が
                ``EStopActiveError`` と ``CanError`` を区別して扱うため。
        """
        try:
            values = [(handle, commands[handle.name]) for handle in self._handles]
        except KeyError as exc:
            raise KeyError(
                f"軸 '{self.name}' のモータ {exc.args[0]!r} に対する指令値がありません"
            ) from exc

        results = await asyncio.gather(
            *(handle.set_target(self._spec.command_mode, value) for handle, value in values),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            for (handle, _), result in zip(values, results, strict=True):
                if not isinstance(result, BaseException):
                    handle.clear_target()
            raise failures[0]

    async def wait_reached(
        self, *, timeout: float | None = None, expect_target: bool = False
    ) -> bool:
        """軸の到達を待つ。到達すれば True、タイムアウトなら False。

        POSITION 軸では ``MotorHandle.wait_reached`` を束ねて呼ぶため、
        待機中に目標が消えれば (緊急停止など) ``WaitInterruptedError`` が
        そのまま伝播する (duty / velocity 軸は到達判定を持たないため無関係)。

        ``expect_target`` はそのまま各モータへ渡す —— 意味も理由も
        ``MotorHandle.wait_reached`` 側に書いてある。
        """
        if self._spec.command_mode is not ControlMode.POSITION:
            # duty / velocity 指令の軸は目標値と同じ次元のフィードバックを持たず
            # 到達判定ができない。代わりに機構が動き切るまでの固定待ちだけを行う
            if self._spec.settle_s > 0.0:
                await asyncio.sleep(self._spec.settle_s)
            return True

        results = await asyncio.gather(
            *(
                handle.wait_reached(
                    tolerance=self._tolerance_for(handle.name),
                    timeout=timeout,
                    expect_target=expect_target,
                )
                for handle in self._handles
            )
        )
        return all(results)

    def observed_value(self) -> float:
        """フィードバックから逆換算した現在の軸位置 (人間の単位)。

        逆換算は ``AxisSpec.to_value`` に委ねる。ここで書き直すと、逆回転ペアの
        符号付き ``scale`` の扱いがまた 2 実装に分かれる。

        **位置指令でない軸は測る術が無いので拒否する。** DC 基板も電磁弁基板も
        位置を持たず ``MotorState.position`` は常に 0 なので、逆換算すると
        「測ったように見える 0」を返してしまう (``ManualController`` が配信用の
        現在値を None へ倒しているのと同じ理由)。

        Raises:
            PositionLookupError: 位置を持たない軸 / 逆換算できる値が 1 つも無い
        """
        if self._spec.command_mode is not ControlMode.POSITION:
            raise PositionLookupError(
                f"軸 '{self.name}' は位置フィードバックを持ちません"
                f" (command_mode={self._spec.command_mode.value})"
            )
        return self._spec.to_value(
            {handle.name: handle.driver.feedback_position() for handle in self._handles}
        )

    def sync_violation(self) -> float | None:
        """許容差を超えたモータ間のずれ (人間の単位)。超過していなければ None。

        判定は 3 層で共有する ``SyncGroup.violation`` に委ねる。ここは
        ``move_to`` の完了時に 1 回だけ見る層で、静止後の 1 サンプルしか使わない
        (層ごとの違いは lib/axis_sync.py のモジュール docstring を参照)。
        """
        group = self._spec.sync_group
        if group is None:
            return None
        return group.violation(
            {handle.name: handle.driver.feedback_position() for handle in self._handles}
        )

    def _tolerance_for(self, motor_name: str) -> float | None:
        """人間の単位の許容差をモータの指令単位へ換算する。

        軸ではなくモータごとに換算するのは、左右で scale の絶対値が異なる機構でも
        同じ幅の許容差を効かせるため。None のときはドライバ既定値に委ねる。
        """
        if self._spec.tolerance is None:
            return None
        return self._motors[motor_name].to_tolerance(self._spec.tolerance)


def build_motor_group(
    can_manager: CANManager,
    motors: Mapping[str, MotorDriver],
    *,
    is_estop_active: EStopChecker | None = None,
    target_sinks: Mapping[str, TargetSink] | None = None,
) -> MotorGroup:
    """CANManager とモータ辞書から MotorGroup を組み立てる。"""
    group = MotorGroup()
    for name, driver in motors.items():
        group.add(
            MotorHandle(
                name,
                driver,
                can_manager,
                is_estop_active=is_estop_active,
                target_sink=(target_sinks or {}).get(name),
            )
        )
    return group
