from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Collection, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING

from lib.drivers.base import ControlMode
from lib.motion_guard import AxisReading, AxisStateReader, MotionGuard, unknown_axis_state
from lib.sequence.positions import PositionLookupError

if TYPE_CHECKING:
    from lib.can_manager import CANManager
    from lib.drivers.base import MotorDriver, MotorState
    from lib.match_state import Court
    from lib.sequence.positions import AxisSpec, PositionTable

_DEFAULT_POLL_INTERVAL_S = 0.01

TargetSink = Callable[[ControlMode, float], Awaitable[None]]
EStopChecker = Callable[[], bool]
#: センサ名 → 接触の有無。**``None`` は「読めていない」**で、``False``
#: (押されていない) とは別の事実として扱う (`lib/motion_guard.py` の docstring)
SensorReader = Callable[[str], "bool | None"]


def _unknown_sensor_state(_name: str) -> None:
    """センサの状態を注入されなかった経路が使う既定の読み口。**常に「読めていない」。**

    ``False`` (押されていない) を既定にすると、配線し忘れた経路だけが
    インターロックを丸ごと素通りし、**しかもそれが画面にもログにも出ない**。
    `guard:` を書いた軸へ指令が届かないという形で必ず表に出す。
    """
    return None


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
    def __init__(
        self,
        handles: Mapping[str, MotorHandle] | None = None,
        *,
        sensor_active: SensorReader | None = None,
    ) -> None:
        self._handles: dict[str, MotorHandle] = dict(handles) if handles else {}
        # 可動端インターロックが読むセンサ状態。`AxisHandle` を作る 3 箇所
        # (手動・シーケンス・統合動作確認) がここから引き継ぐので、束ねる側が
        # 1 度配線すれば 3 経路とも同じものを見る
        self._sensor_active = sensor_active
        self._axis_state: AxisStateReader | None = None

    def add(self, handle: MotorHandle) -> None:
        self._handles[handle.name] = handle

    def bind_axis_state(self, reader: AxisStateReader) -> None:
        """軸間干渉の判定が読む「他の軸は今どこか」の読み口を配線する。

        束ねる側が 1 度配線すれば、ここから `AxisHandle` を作る 3 経路
        (手動・シーケンス・零点確定) が同じものを見る。**読み口は
        `MotorGroup` そのものを引くので、束ね終わってから配線する**
        (コンストラクタでは受け取れない)。
        """
        self._axis_state = reader

    @property
    def sensor_active(self) -> SensorReader | None:
        """可動端センサの読み口。配線されていなければ None。"""
        return self._sensor_active

    @property
    def axis_state(self) -> AxisStateReader | None:
        """他の軸の実測と目標の読み口。配線されていなければ None。"""
        return self._axis_state

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
        sensor_active: SensorReader | None = None,
        axis_state: AxisStateReader | None = None,
    ) -> None:
        # 3 経路 (手動・move_to・零点確定) が必ずここを通るので、解決忘れはここで落とす
        spec.require_resolved()
        self._spec = spec
        self._handles = tuple(handles)
        self._motors = {motor.name: motor for motor in spec.motors}
        # `guard:` を書かなかった軸は今までどおり素通り (歯止めを既定値で作らない)
        self._guard = None if spec.guard is None else MotionGuard(spec.guard)
        self._sensor_active = sensor_active or _unknown_sensor_state
        # 未配線は「常に読めていない」。素通りへ倒すと、配線を忘れた経路だけが
        # 干渉の歯止めを丸ごと失い、それが画面にもログにも出ない
        self._axis_state = axis_state or unknown_axis_state

    @property
    def name(self) -> str:
        return self._spec.name

    async def set_target_value(
        self,
        commands: Mapping[str, float],
        *,
        pending_targets: Mapping[str, float] | None = None,
    ) -> None:
        try:
            values = [(handle, commands[handle.name]) for handle in self._handles]
        except KeyError as exc:
            raise KeyError(
                f"軸 '{self.name}' のモータ {exc.args[0]!r} に対する指令値がありません"
            ) from exc

        self.check_target_value(commands, pending_targets=pending_targets)

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

    def check_target_value(
        self,
        commands: Mapping[str, float],
        *,
        pending_targets: Mapping[str, float] | None = None,
    ) -> None:
        """指令を 1 通も出す前に歯止めを通す。**判断は `lib/motion_guard.py` が持つ。**

        ここに置くのは、手動操縦・シーケンス (`move_to`)・零点確定の 3 経路が
        **すべて `set_target_value` を通る**ため。経路ごとに書き写すと、
        片方だけ緩んだ状態が作れる (実際に踏んだ事故は零点確定の経路で起きた)。

        **公開しているのは `move_to` が「全軸検査 → 全軸送信」を組むためだけ**で、
        `set_target_value` は必ずここを通る (入口の一本化は崩さない)。

        `pending_targets` は**この 1 通と同時に書かれる他の軸の目標** [その軸の
        unit]。同じ指令に入る軸は、その指令が書き終わった後の目標で評価しないと、
        まだ書き換わっていない古い目標で拒否される。

        **トルクは「動く指令」にしか掛けない。** 実測位置と同じ値を送り直す指令
        (`HomingRunner` が接触を検出した瞬間に出す「その場で止まれ」) まで塞ぐと、
        **押し込む向きの古い目標が生き残る** —— 止めるための指令が、止まって
        いないことを理由に拒否されるという逆立ちが起きる。可動端の判定と干渉の
        判定が `delta == 0` を見ないのと同じ理由である。

        Raises:
            GuardViolation: 歯止めに掛かった
        """
        if self._guard is None:
            return
        current = self.observed_value()
        target = self._spec.to_value(commands)
        self._guard.check_command(
            axis=self.name,
            current=current,
            target=target,
            unit=self._spec.unit,
            sensor_active=self._sensor_active,
            axis_state=self._axis_state_with(pending_targets),
        )
        if target != current:
            self._guard.check_torque(axis=self.name, torque=self._observed_torque())

    def check_not_with(self, siblings: Collection[str]) -> None:
        """同じ 1 通の指令に入っている軸と一緒に動かしてよいか。

        呼ぶのは `Sequence.move_to` だけである (`MotionGuard.check_not_with`)。

        Raises:
            GuardViolation: 一緒に動かしてはならない軸が同じ指令に入っている
        """
        if self._guard is None:
            return
        self._guard.check_not_with(axis=self.name, siblings=siblings)

    def _axis_state_with(self, pending_targets: Mapping[str, float] | None) -> AxisStateReader:
        if not pending_targets:
            return self._axis_state

        def read(axis: str) -> AxisReading:
            reading = self._axis_state(axis)
            if axis not in pending_targets:
                return reading
            # 実測は読めたままにする。目標だけを差し替えるので、「これから書く目標は
            # 中だが実測はまだ外」は今までどおり拒否される
            return AxisReading(value=reading.value, target=pending_targets[axis])

        return read

    def _observed_torque(self) -> float | None:
        """軸に掛かっているトルク [Nm]。測れるモータが 1 台も無ければ None。

        DM3520 / EDULITE 05 はトルクを ``MotorState.current`` に載せる。
        **測れるかどうかは `TelemetrySupport` だけが答える** —— 測る手段の無い
        基板 (DC / 電磁弁) が常に運ぶ 0.0 を混ぜると、「測ったように見える 0」が
        そのまま「異常なし」に化ける。

        複数モータ軸では絶対値の最大を採る。左右直結ペアは片側だけが機構に
        当たることがあり、平均を採るともう片方の余裕で薄まって検出が遅れる。
        """
        torques = [
            abs(handle.driver.state.current)
            for handle in self._handles
            if handle.driver.telemetry.current
        ]
        return max(torques) if torques else None

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
        return self._spec.to_value(self.observed_commands())

    def observed_commands(self) -> dict[str, float]:
        """実測位置を**指令の単位のまま**返す。「その場で止まれ」を書く唯一の口。

        値へ換算して指令へ戻すと (`to_commands(to_value(…))`)、複数モータ軸では
        平均を挟むぶん往復が丸め誤差を生み、それが非ゼロの `delta` として
        `_check_guard` に届く —— **止めるための指令が、止まっていないことを理由に
        拒否される**。実測を素通しすれば `delta` は厳密に 0 になる (`current` を
        作るのもこの同じ辞書だから)。

        ついでに各モータが**自分の位置**を保持する。平均へ寄せる指令は、止めるべき
        瞬間に左右を動かしに行く。
        """
        if self._spec.command_mode is not ControlMode.POSITION:
            raise PositionLookupError(
                f"軸 '{self.name}' は位置フィードバックを持ちません"
                f" (command_mode={self._spec.command_mode.value})"
            )
        return {handle.name: handle.driver.feedback_position() for handle in self._handles}

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


def build_axis_state_reader(
    positions: PositionTable,
    motors: MotorGroup,
    *,
    court: Callable[[], Court],
    is_stale: Callable[[str], bool],
) -> AxisStateReader:
    """軸間干渉の判定が読む「他の軸は今どこか」を組む。**読めないものは `None`。**

    `MotorState` の既定 0.0 をそのまま運ぶと、**1 通も受信していない軸が
    「測ったように見える 0」として条件を満たしてしまう**。`None` を返すのは
    次の 5 つで、どれも「測る手段が無い」であって「原点に居る」ではない:

    1. その軸が位置定数に無い
    2. 位置指令の軸でない (duty / on_off は「今どこに居るか」を答えられない)
    3. 軸のモータがこの束に居ない (別ロボットの軸を参照した)
    4. 位置を測れないドライバ (`TelemetrySupport.position`)
    5. フィードバックが鮮度切れ (`is_stale`)

    **目標 (`target`) の `None` は拒否の理由にしない。** 緊急停止で目標が消えた
    後に全軸が動かせなくなると、退避路としての手動操縦が成立しない
    (`MotionGuard.check_interference`)。

    コートを毎回問い直すのは、`scale` がコートで鏡になる軸 (`sub_lift`) では
    実測 rad から mm への換算の符号そのものが変わるため。起動時に固めると、
    試合中のコート切り替えで区間の内外が反転する。
    """

    def read(axis: str) -> AxisReading:
        try:
            spec = positions.axis(axis).for_court(court())
        except PositionLookupError:
            return AxisReading(value=None, target=None)
        if spec.command_mode is not ControlMode.POSITION:
            return AxisReading(value=None, target=None)

        handles: list[MotorHandle] = []
        for name in spec.motor_names:
            if name not in motors:
                return AxisReading(value=None, target=None)
            handle = motors[name]
            if not handle.driver.telemetry.position or is_stale(name):
                return AxisReading(value=None, target=None)
            handles.append(handle)

        value = spec.to_value({h.name: h.driver.feedback_position() for h in handles})

        # 目標は 1 台でも欠けたら軸として「無い」。片側だけの平均は別の位置を指す
        written: dict[str, float] = {}
        for handle in handles:
            if handle.mode is not ControlMode.POSITION or handle.target is None:
                written = {}
                break
            written[handle.name] = handle.target
        return AxisReading(value=value, target=spec.to_value(written) if written else None)

    return read


def build_motor_group(
    can_manager: CANManager,
    motors: Mapping[str, MotorDriver],
    *,
    is_estop_active: EStopChecker | None = None,
    target_sinks: Mapping[str, TargetSink] | None = None,
    sensor_active: SensorReader | None = None,
) -> MotorGroup:
    group = MotorGroup(sensor_active=sensor_active)
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
