from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from lib.drivers.base import ControlMode
from lib.sequence.positions import PositionLookupError

if TYPE_CHECKING:
    from lib.can_manager import CANManager
    from lib.drivers.base import MotorDriver, MotorState
    from lib.sequence.positions import AxisSpec

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL_S = 0.01

TargetSink = Callable[[ControlMode, float], Awaitable[None]]
EStopChecker = Callable[[], bool]


@dataclass(frozen=True)
class LimitIntervention:
    """リミット保護がその軸で要求を曲げた回数と、直近でそれを起こしたセンサ名。

    **``count`` は単調増加で、読んでも減らない。** 保護が働いたことを「今
    ラッチしているか」で判定すると、移動の**途中で**触れて離れた接触
    (接点のバウンド) を取りこぼす —— ラッチは外れるのに、層①が書き戻した目標は
    残るので軸は触れた位置で止まったままになる。元の目標を再送する経路は無い。

    **回数と理由を 1 組で運ぶ。** 別々の口に分けると、回数だけを見てセンサ名を
    後から取り直す形が書け、そのあいだにラッチが外れれば「止まったのに理由だけが
    空」になる (``HealthThresholds`` を 1 組で運ぶのと同じ理由)。

    型をここに置くのは、実体側 (``lib/control/limit_guard.py``) が指令口である
    このモジュールを既に import しているため。逆向きに import すると循環になる。
    """

    count: int = 0
    sensors: tuple[str, ...] = ()


class LimitClamp(Protocol):
    """リミットスイッチ保護のうち、指令の入口が要る部分だけを表す構造的な型。

    実体は ``lib.control.limit_guard.LimitGuard`` だが、**ここから
    ``lib/control/`` を import してはならない** —— 保護 (上位) が指令口 (下位) を
    掴む向きは既にあり、逆向きを足すと循環になる。判定そのものは向こうが単一
    情報源で、こちらは「同じ ``clamp()`` を呼ぶ」ことだけを型で約束する。
    """

    @property
    def axis_names(self) -> tuple[str, ...]:
        """保護対象の軸名。ここに無い軸は逆換算すら行わずに素通しする。"""
        ...

    def clamp(self, axis: str, value: float, observed: float) -> float:
        """禁止方向へ ``observed`` より進む指令を ``observed`` で頭打ちにする。"""
        ...

    def intervention(self, axis: str) -> LimitIntervention:
        """その軸で保護が要求を曲げた回数と、直近の理由。"""
        ...


class EStopActiveError(RuntimeError):
    """緊急停止中にモータ指令を出そうとしたときに送出される。"""


class WaitInterruptedError(RuntimeError):
    """到達待ちの最中に目標が消えた。"""


class MotorHandle:
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

    async def set_target(self, mode: ControlMode, value: float) -> None:
        if self._is_estop_active is not None and self._is_estop_active():
            raise EStopActiveError(f"緊急停止中のためモータ '{self._name}' に指令できません")

        value = float(value)
        await self._dispatch(mode, value)

        self._mode = mode
        self._target = value

    async def resend_target(self) -> bool:
        if self._target is None or self._mode is None:
            return False
        if self._is_estop_active is not None and self._is_estop_active():
            return False

        await self._dispatch(self._mode, self._target)
        return True

    async def _dispatch(self, mode: ControlMode, value: float) -> None:
        if self._target_sink is not None:
            await self._target_sink(mode, value)
        else:
            await self._can_manager.send(self._name, self._driver.encode_target(mode, value))

    def clear_target(self) -> None:
        self._target = None
        self._mode = None

    def is_reached(self, *, tolerance: float | None = None) -> bool:
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
    def __init__(self, handles: Mapping[str, MotorHandle] | None = None) -> None:
        self._handles: dict[str, MotorHandle] = dict(handles) if handles else {}
        self._limit_guard: LimitClamp | None = None

    def add(self, handle: MotorHandle) -> None:
        self._handles[handle.name] = handle

    @property
    def limit_guard(self) -> LimitClamp | None:
        """このグループのモータで組む軸へ掛かるリミット保護 (無ければ None)。"""
        return self._limit_guard

    def bind_limit_guard(self, guard: LimitClamp | None) -> None:
        """起動時の配線でリミット保護を結ぶ。**1 グループにつき 1 回だけ。**

        保護は自分を止めるために軸ハンドルを要り、軸ハンドルはこのグループを
        要るので、生成順が循環する。保護側が軸ハンドルを遅延して引く (``main.
        _make_axis_handle_lookup``) ことで解いてあり、こちらは後から結ぶ。

        二重の bind を ``RuntimeError`` にするのは、**後から結んだほうだけが
        効く**形を残さないため —— 症状は「保護を 2 つ書いたのに片方が黙って
        効かない」で、ログにも画面にも出ない (周期タスクの二重 ``start()`` を
        拒否するのと同じ理由)。
        """
        if guard is None:
            return
        if self._limit_guard is not None:
            raise RuntimeError("リミット保護は既に結ばれています (二重の配線)")
        self._limit_guard = guard

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
        if name.startswith("_"):
            raise AttributeError(name)
        handles = self.__dict__.get("_handles", {})
        if name in handles:
            return handles[name]
        available = ", ".join(handles) or "(なし)"
        raise AttributeError(f"モータ '{name}' は存在しません。利用可能なモータ: {available}")


class AxisHandle:
    def __init__(
        self,
        spec: AxisSpec,
        handles: Sequence[MotorHandle],
        *,
        limit_guard: LimitClamp | None = None,
    ) -> None:
        self._spec = spec
        self._handles = tuple(handles)
        self._motors = {motor.name: motor for motor in spec.motors}
        self._limit_guard = limit_guard

    @property
    def name(self) -> str:
        return self._spec.name

    @property
    def has_target(self) -> bool:
        # リミット保護が「止めるものがあるか」を問う口。誰も駆動していない軸へ保持指令を
        # 書くと、操作していないのに保持が始まる。any なのは、片側だけに目標が残る状態
        # (送信 1 通が失敗した直後) こそ保護が最も要る瞬間だから
        return any(handle.has_target for handle in self._handles)

    @property
    def limit_intervention(self) -> LimitIntervention:
        # 保護が働くと目標がその場の実測位置へ書き換わり到達判定は必ず成立するので、
        # move_to は移動の前後でこの回数を比べる。層① (50Hz の引き戻し) と
        # 層② (入口のクランプ) のどちらが働いても同じカウンタが進む
        guard = self._limit_guard
        if guard is None:
            return LimitIntervention()
        return guard.intervention(self.name)

    async def set_target_value(self, commands: Mapping[str, float]) -> dict[str, float]:
        """モータ名 → 指令値をまとめて送る。**実際に送った指令値を返す。**

        逐次 await しないのは、左右直結の軸で送信に時間差が出ると機構がねじれるため。
        リミット保護のクランプはここ (指令の入口 = 層②) で掛かる —— 50Hz の常駐層だけ
        では押し戻されるまでの 1 周期ぶん禁止方向へ進む。判定は書き写さず
        ``LimitGuard.clamp()`` をそのまま呼ぶ。返り値を持つのは、クランプを知らない
        ジョグ起点が押すたびに禁止側へ伸びて退避できなくなるのを防ぐため。

        **1 台でも失敗したら、送信に成功した側の目標だけを捨てる。** 素の ``gather`` は
        最初の例外で抜けるが残りのタスクはキャンセルされずに完走するので、片方の送信だけが
        失敗すると成功した側にだけ新しい目標が残り、問い合わせ駆動のモータでは
        ``QueryDrivenTargetRefresher`` がその 1 台だけを新目標へ押し続ける。
        **失敗した側の目標は捨てない** —— 1 通も飛んでいない以上、旧目標こそが基板が現に
        実行している状態と一致しており、捨てると単一モータの generic 軸で 20Hz の再送が
        止まってファームのウォッチドッグが満了する (電磁弁は消磁してワークが落ちる)。

        Raises:
            BaseException: 送信に失敗した例外のうち、``self._handles`` の並びで最初の
                もの。``ExceptionGroup`` へ包まないのは、呼び出し側が
                ``EStopActiveError`` と ``CanError`` を区別して扱うため。
        """
        commands = self._clamped(commands)
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
        return {handle.name: value for handle, value in values}

    def _clamped(self, commands: Mapping[str, float]) -> Mapping[str, float]:
        """リミット保護が禁じている向きへの指令を、端の位置で頭打ちにする。

        **保護を持たない軸は逆換算も行わずに素通しする。** ``limits`` は位置指令の
        軸にしか書けない (``AxisSpec._check_limits``) ので、ここを素通しにしないと
        到達も現在位置も観測できない duty / on_off 軸で無意味な逆換算が走る。

        **実測が読めなければクランプしない。** 未受信の 0.0 を現在位置と信じて端を
        推定すると、指令のほうを歪めることになる。同じ状況では 50Hz の常駐層も
        引き戻し先を持てないので、ここで拒否しても保護が厚くなるわけではない。

        クランプが掛かったときは軸位置を全モータへ配り直す (``to_commands``)。
        モータごとに違う軸位置を指令する経路 (零点確定の整列段) は、その間
        ``suspend_sensors`` で保護を外しているのでここへは来ない。
        """
        guard = self._limit_guard
        if guard is None or self.name not in guard.axis_names:
            return commands

        try:
            observed = self.observed_value()
        except Exception:
            logger.debug("軸 '%s' の実測位置を読めずクランプを見送ります", self.name, exc_info=True)
            return commands

        value = self._spec.to_value(commands)
        clamped = guard.clamp(self.name, value, observed)
        if clamped == value:
            return commands
        logger.info(
            "リミット保護: 軸 %s への指令 %.3f%s を端の %.3f%s で頭打ちにしました",
            self.name,
            value,
            self._spec.unit,
            clamped,
            self._spec.unit,
        )
        return self._spec.to_commands(clamped)

    async def wait_reached(
        self, *, timeout: float | None = None, expect_target: bool = False
    ) -> bool:
        if self._spec.command_mode is not ControlMode.POSITION:
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
        if self._spec.command_mode is not ControlMode.POSITION:
            raise PositionLookupError(
                f"軸 '{self.name}' は位置フィードバックを持ちません"
                f" (command_mode={self._spec.command_mode.value})"
            )
        return self._spec.to_value(
            {handle.name: handle.driver.feedback_position() for handle in self._handles}
        )

    def observed_values(self) -> dict[str, float]:
        if self._spec.command_mode is not ControlMode.POSITION:
            raise PositionLookupError(
                f"軸 '{self.name}' は位置フィードバックを持ちません"
                f" (command_mode={self._spec.command_mode.value})"
            )
        return {
            handle.name: self._motors[handle.name].to_value(handle.driver.feedback_position())
            for handle in self._handles
        }

    def sync_violation(self) -> float | None:
        group = self._spec.sync_group
        if group is None:
            return None
        return group.violation(
            {handle.name: handle.driver.feedback_position() for handle in self._handles}
        )

    def _tolerance_for(self, motor_name: str) -> float | None:
        if self._spec.tolerance is None:
            return None
        return self._motors[motor_name].to_tolerance(self._spec.tolerance)


def build_axis_handle(spec: AxisSpec, group: MotorGroup) -> AxisHandle:
    """1 論理軸の指令口を組む。**本番コードで軸ハンドルが生まれる唯一の口。**

    ``AxisHandle`` を直に組み立てる経路を各所に残すと、リミット保護を渡し忘れた
    箇所だけが「触れても止まらない」まま残る —— 症状はその経路でしか出ず、config
    にもログにも画面にも現れない。ここへ絞れば、保護はモータ群に結ばれた 1 つが
    自動的に付いてくる (``tests/test_limit_clamp.py`` が直接生成の再発を AST で
    見張る)。

    ``AxisHandle`` は寿命のある状態を持たないので毎回組んでよい。使い回すと、
    同じ軸を別経路 (シーケンス・手動・動作確認) から動かしたときに古い目標値が残る。
    """
    return AxisHandle(
        spec,
        [getattr(group, name) for name in spec.motor_names],
        limit_guard=group.limit_guard,
    )


def build_motor_group(
    can_manager: CANManager,
    motors: Mapping[str, MotorDriver],
    *,
    is_estop_active: EStopChecker | None = None,
    target_sinks: Mapping[str, TargetSink] | None = None,
) -> MotorGroup:
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
