"""移動中の可動端インターロック。**指令を出す瞬間の歯止めだけでは届かない。**

`MotionGuard` は `AxisHandle.set_target_value` から呼ばれる。つまり **1 通の指令を
書くその瞬間しか見ていない**。小刻みなジョグは 1 歩ごとに判定を通るので端で止まるが、
遠い目標を 1 回書くと、その後スイッチが立っても誰も見ていない —— ドライバは書かれた
目標へサーボし続け、機構はスイッチを踏み越える (2026-09-09 実機: `sub_y_axis` へ
`-450` を 1 回指令し、後端の作動点 `-447.0` を 3mm 越えた)。

そこでこのタスクが周期的に「**今の実測位置から、今書かれている目標へ向かう向き**」の
端センサを見て、押されている / 読めていないなら**その場の実測位置を目標へ書き直す**。
目標そのものを書き換えないと、問い合わせ駆動の再送 (`TargetRefresher`) が古い目標を
送り直して押し込み続ける。

**宣言の前後に依らない貫通防止も重ねる。** `guard.limits` の前後が実物と入れ替わっていると、
進む向きの端だけを見る作りでは機構が当たった側を誰も見ていない (2026-09-10 実機: `sub_y_axis` が
見張っていない側のスイッチを踏み越えた)。そこで端センサごとに **OFF→ON へ変わった周期に出て
いた指令の向き**を覚え、ON のあいだその向きの指令を止める (`MotionGuard.check_pass_through`)。
OFF に戻り、そこから到達許容差 (`tolerance`) 以上離れたら忘れる。**離れないままの OFF→ON は
同じ押しで、向きを覚え直さない** —— 零点確定は端の作動点に座って終わるので、退避の 1 歩目に
ON を読み直す (2026-09-10 実機: `sub_lift` の退避が「- 向きで押された」と誤って止まった)。
判定は `MotionGuard` が持ち、ここは覚える役だけ。

**見るのは可動端だけ。** `max_step` は「1 指令で実測位置から離れてよい量」の判定なので、
移動中の実測位置に当てると長距離移動の途中で必ず誤発火する。トルクは誤発火が怖いので
ここでは見ない。**判定そのものは `MotionGuard.check_limit` が持ち、ここには書き写さない。**

**軸どうしの干渉 (`guard.requires`) も周期監視しない。** 理由は 3 つで、どれも
`docs/invariants.md` §4 にある: ①止める対象が一意に決まらない (回転中の軸か、前進中の
軸か) ②途中の姿勢は試合シーケンスが一度も使わず、掃過を途中で凍らせるほうが回し切る
より危ない可能性がある ③依存を一方向にしたので、参照される軸が読めなくても下流の軸は
動かせる —— 周期監視を足すと、無応答のサーボのせいで退避に使う直動軸が凍る。
そのため**ここが作る `AxisHandle` には読み口を配線しない**。書き戻す「その場で止まれ」は
`delta == 0` なので `check_interference` を必ず素通りする。

**現在値 (`sensor_active`) だけでは観測周期 (20ms) より狭い ON 区間を丸ごと落とす**
(`rotate` はスイッチの 2deg を約 18ms で通過する)。零点確定が探索の到達判定で踏んだのと
同じ穴なので、同じ材料 —— 接触 (OFF→ON) の単調カウンタ —— で塞ぐ。判定へ渡す前の
**入力の作り方**だけがここの仕事で、`MotionGuard` 側は三値をそのまま受け取る。

**発火は回数で数える。** 目標を実測へ書き直すと `is_reached` は必ず成立するので、
`move_to` は「その軸で移動を止めた回数」が増えたかどうかで介入を知る (終了時の状態を
見る方式では、触れて離れた接点のバウンドを取りこぼす)。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from lib.control.periodic import PeriodicTask
from lib.drivers.base import ControlMode
from lib.motion_guard import GuardViolation, MotionGuard
from lib.sequence.engine import NO_LIMIT_INTERVENTION, LimitIntervention
from lib.sequence.motors import AxisHandle

if TYPE_CHECKING:
    from lib.match_state import Court
    from lib.sequence.motors import MotorGroup, MotorHandle, SensorReader
    from lib.sequence.positions import AxisSpec, PositionTable

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_INTERVAL_S", "LimitMonitor"]

DEFAULT_INTERVAL_S = 0.02

CourtSource = Callable[[], "Court | None"]
SleepFunc = Callable[[float], Awaitable[None]]
#: 接触 (OFF→ON) の累計。**単調増加で読んでも減らない**ので読み手が何人いても壊れない。
#: `None` = カウンタを提供しないドライバ (零点確定の `SensorContactCount` と同じ約束)
SensorContactCount = Callable[[str], int | None]


@dataclass
class _Release:
    """端が OFF に変わってから、軸がそこからどれだけ離れたか [軸の unit]。"""

    anchor: float
    excursion: float = 0.0


class LimitMonitor(PeriodicTask):
    def __init__(
        self,
        positions: PositionTable,
        motors: MotorGroup,
        *,
        sensor_active: SensorReader,
        sensor_contact_count: SensorContactCount,
        court: CourtSource,
        interval_s: float = DEFAULT_INTERVAL_S,
        time_source: Callable[[], float] = time.monotonic,
        sleep: SleepFunc = asyncio.sleep,
    ) -> None:
        super().__init__(interval_s=interval_s, time_source=time_source, sleep=sleep, logger=logger)
        self._positions = positions
        self._motors = motors
        self._sensor_active = sensor_active
        self._sensor_contact_count = sensor_contact_count
        self._court = court
        self._guards = _build_guards(positions, motors)
        self._sensor_names = _limit_sensor_names(positions, self._guards)
        # 50Hz で撃ち続けるとバスが埋まるので、同じ目標のあいだは撃ち直さない
        self._stopped: dict[str, float] = {}
        self._interventions: dict[str, LimitIntervention] = {}
        # 端センサの前回値と、軸ごとの {センサ名: OFF→ON に変わった周期の指令の符号}。
        # `None` (読めていない) の周期は前回値を進めない
        self._last_active: dict[str, bool] = {}
        self._pressed_toward: dict[str, dict[str, int]] = {}
        self._released: dict[str, _Release] = {}
        # 読み手は自分で基準値を控える (カウンタは読んでも減らないので、同じセンサを
        # 読む零点確定のぶんを消さない)。周期ごとに 1 度だけ進めるので二重に数えない
        self._contact_baseline: dict[str, int] = {}
        self._contacted: set[str] = set()
        self._unresolved_warned: set[str] = set()

    @property
    def axis_names(self) -> tuple[str, ...]:
        return tuple(self._guards)

    @property
    def stopped_axes(self) -> frozenset[str]:
        return frozenset(self._stopped)

    def intervention(self, axis: str) -> LimitIntervention:
        """その軸で移動を止めた回数と直近の理由。**単調増加で、離れても減らない。**"""
        return self._interventions.get(axis, NO_LIMIT_INTERVENTION)

    def _label(self) -> str:
        return f"可動端監視 ({', '.join(self.axis_names) or '対象なし'})"

    async def _tick(self) -> None:
        await self.step()

    async def step(self) -> None:
        self._poll_contacts()
        for axis in self._guards:
            try:
                await self._check_axis(axis)
            except Exception:
                self._log.exception(f"axis:{axis}", "可動端監視で例外 (axis=%s)", axis)

    def _poll_contacts(self) -> None:
        """この周期に「前回から接触したセンサ」を確定する。

        基準値を周期ごとに 1 度だけ進めるので、同じ接触を次の周期でもう一度
        数えることはない。**初回は基準値を置くだけ** —— 監視を始める前 (前回の
        零点確定や手動操縦) に数えたぶんまで見ると、最初の移動でいきなり止まる。
        """
        self._contacted.clear()
        for name in self._sensor_names:
            count = self._sensor_contact_count(name)
            if count is None:
                continue
            previous = self._contact_baseline.get(name)
            if previous is not None and count > previous:
                self._contacted.add(name)
            self._contact_baseline[name] = count

    def _sensor_state(self, name: str) -> bool | None:
        """判定へ渡すセンサ状態。**三値のまま運ぶ。**

        観測周期より狭い ON 区間は現在値に現れないので、この周期に数えた接触も
        「押されている」として渡す。`None` (読めていない) を `False` へ丸めないのは
        `MotionGuard` 側の約束そのもの。
        """
        active = self._sensor_active(name)
        if active is None:
            return None
        return bool(active) or name in self._contacted

    async def _check_axis(self, axis: str) -> None:
        court = self._court()
        spec = self._positions.axis(axis)
        if court is None and spec.court_dependent:
            self._warn_unresolved(axis)
            return
        self._unresolved_warned.discard(axis)
        spec = spec.for_court(court)
        handles = [self._motors[name] for name in spec.motor_names]
        handle = AxisHandle(spec, handles, sensor_active=self._sensor_state)

        # 書き戻す値は指令の単位のまま持つ。値へ換算して戻すと往復の丸め誤差が
        # そのまま delta に残り、**止めるための指令が入口の歯止めに拒否される**
        observed_commands = handle.observed_commands()
        position = spec.to_value(observed_commands)
        target = self._commanded_value(spec, handles)
        if target is None:
            self._stopped.pop(axis, None)
        moving = target is not None and self._stopped.get(axis) != target
        delta = target - position if moving else 0.0
        # 動かしていない周期も前回値を進める。進めないと、止まっている間に手で押された
        # スイッチが次の指令の周期に「その向きで当たった」と読まれる
        pressed_toward = self._track_pressed(
            axis, spec, delta, position=position, commanded=target is not None
        )
        if not moving:
            return

        guard = self._guards[axis]
        try:
            guard.check_limit(axis=axis, delta=delta, sensor_active=self._sensor_state)
            guard.check_pass_through(
                axis=axis,
                delta=delta,
                sensor_active=self._sensor_state,
                pressed_toward=pressed_toward,
            )
        except GuardViolation as exc:
            await self._stop_here(axis, spec, handle, observed_commands, exc)
            return
        self._stopped.pop(axis, None)

    def _track_pressed(
        self, axis: str, spec: AxisSpec, delta: float, *, position: float, commanded: bool
    ) -> dict[str, int]:
        """この周期の端センサを見て、OFF→ON に変わった端に指令の向きを覚える。

        指令の無い周期 (`delta == 0`) の OFF→ON は覚えない —— 動かしていないのに押されたなら
        機構が当たったのではなく、次の指令はどちらの向きでも退避として通す。

        OFF に戻った端は、軸がそこから到達許容差以上離れるまで覚えたままにする。離れない
        ままの OFF→ON は端の作動点に座った接点のゆらぎで、新しい接触ではない —— 忘れると
        押し込みの続きが通り、覚え直すと退避の向きを「当たった向き」と取り違える。
        """
        pressed = self._pressed_toward.setdefault(axis, {})
        limits = None if spec.guard is None else spec.guard.limits
        names = () if limits is None else (*limits.plus, *limits.minus)
        # 到達許容差の内側は「同じ位置」。持たない軸は離れたかどうかを問わない
        hysteresis = spec.tolerance or 0.0
        for name in names:
            state = self._sensor_state(name)
            if state is None:
                continue
            if state:
                release = self._released.pop(name, None)
                left = release is None or release.excursion >= hysteresis
                if left and self._last_active.get(name) is False and delta != 0.0:
                    pressed[name] = 1 if delta > 0.0 else -1
            else:
                if self._last_active.get(name) is True:
                    self._released[name] = _Release(position)
                release = self._released.get(name)
                if release is not None and not commanded:
                    # 指令の無いあいだの実測の変化は移動ではない (原点の付け替えで跳ぶ)
                    release.anchor, release.excursion = position, 0.0
                elif release is not None:
                    release.excursion = max(release.excursion, abs(position - release.anchor))
                if release is None or release.excursion >= hysteresis:
                    pressed.pop(name, None)
            self._last_active[name] = state
        return pressed

    def _warn_unresolved(self, axis: str) -> None:
        """コート未確定の軸は監視できない。**保護の穴にはならない。**

        コートが決まるまでこの軸へは指令が 1 通も通らない (指令の入口が
        `CourtUnresolvedError` で落ちる) ので、監視する動きがそもそも無い。
        毎周期の例外にすると本物の異常がログから読めなくなる。
        """
        if axis in self._unresolved_warned:
            return
        self._unresolved_warned.add(axis)
        self._logger.warning("可動端監視を保留: 軸 %s はコートが未確定です", axis)

    def _commanded_value(self, spec: AxisSpec, handles: list[MotorHandle]) -> float | None:
        commands: dict[str, float] = {}
        for handle in handles:
            if handle.target is None or handle.mode is not ControlMode.POSITION:
                return None
            commands[handle.name] = handle.target
        return spec.to_value(commands)

    async def _stop_here(
        self,
        axis: str,
        spec: AxisSpec,
        handle: AxisHandle,
        observed_commands: dict[str, float],
        exc: GuardViolation,
    ) -> None:
        # 書き戻しに失敗しても要求を曲げたことは変わらないので、先に数える
        self._interventions[axis] = LimitIntervention(
            count=self.intervention(axis).count + 1, reason=str(exc)
        )
        await handle.set_target_value(observed_commands)
        written = self._commanded_value(spec, [self._motors[name] for name in spec.motor_names])
        if written is not None:
            self._stopped[axis] = written
        self._logger.warning(
            "移動中に可動端で停止: axis=%s, 実測=%.3f%s, 目標を実測へ書き直しました (%s)",
            axis,
            spec.to_value(observed_commands),
            spec.unit,
            exc,
        )


def _build_guards(positions: PositionTable, motors: MotorGroup) -> dict[str, MotionGuard]:
    guards: dict[str, MotionGuard] = {}
    for name in positions.axes:
        spec = positions.axis(name)
        if spec.guard is None or spec.guard.limits is None:
            continue
        missing = [motor for motor in spec.motor_names if motor not in motors]
        if missing:
            logger.warning(
                "可動端監視をスキップ: 軸 %s のモータ %s がこのロボットに存在しません",
                name,
                ", ".join(missing),
            )
            continue
        guards[name] = MotionGuard(spec.guard)
    return guards


def _limit_sensor_names(
    positions: PositionTable, guards: dict[str, MotionGuard]
) -> tuple[str, ...]:
    """監視対象の軸が持つ端センサ名。**両端とも毎周期読む。**

    進む向きの端だけを読むと、反対端は基準値を持たないまま残り、向きが変わった
    最初の周期に「初回なので数えない」で狭い接触を落とす。
    """
    names: list[str] = []
    for axis in guards:
        guard = positions.axis(axis).guard
        limits = None if guard is None else guard.limits
        if limits is None:
            continue
        names.extend((*limits.plus, *limits.minus))
    return tuple(dict.fromkeys(names))
