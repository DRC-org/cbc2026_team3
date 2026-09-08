"""リミットスイッチに触れた軸を、そのスイッチのある向きへだけ止める常駐保護。

7 本のスイッチは全て ``can_generic`` のサーボ基板に載るが、止めたいモータは
すべて別バス (``y_axis``=can_m3508 / ``rotate``=can_edulite / ``sub_*``=can_dm3520)
にある。**ファーム側で結線できないので、PC 側の常駐監視にしかならない。**

**方向つきの保護であって、無条件停止ではない。** 止めるのは「そのスイッチの
ある側へ進む指令」だけで、逆方向は必ず通す。無条件に止めると、端に触れた軸を
戻す操作ごと塞がれ、**二度と動かせない軸**ができる。

**軸ローカルで、全体緊急停止に倒さない。** 緊急停止中は ``manual_*`` が通らない
ので、端に触れた軸を戻す操作そのものができなくなる。``SyncMonitor`` が全体停止で
よいのは「ずれた軸はどちらへ動かしても壊れる」からで、リミットは片方向だけが
破壊なので性質が違う。

**判定はここが単一情報源で、2 つの層が同じ ``clamp()`` を呼ぶ。** この 50Hz の
常駐層と、指令の入口 (``AxisHandle`` への差し込み) で違うのは頻度と超過後の
扱いだけであり、境界そのものはずれてはならない。``lib/axis_sync.py`` の
``SyncGroup.violation()`` を 3 層が共有しているのと同じ構成である。

**行き過ぎの見積もり (y_axis)**: 検出まで最悪 35ms (センサ FEEDBACK 10ms +
監視周期 20ms + 送信) で巡航 200mm/s なら 7.0mm、減速に v^2/(2a) = 16.7mm、
合わせて約 24mm。**スイッチは機構端から 25mm 相当以上手前に置く前提**であり、
守れるのは低速接近と「原点がずれたまま走り込む」異常であって、全速での突入では
ない。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING

from lib.control.periodic import PeriodicTask, SleepFunc
from lib.sequence.motors import EStopActiveError, LimitIntervention
from lib.sequence.positions import AxisSpec

if TYPE_CHECKING:
    from lib.sequence.motors import AxisHandle

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_INTERVAL_S", "CombinedLimitGuard", "LimitGuard", "combine_limit_guards"]

#: 監視周期 50Hz。``SyncMonitor`` と同じで、機構が壊れる前に止まればよく
#: 位置制御ループの 200Hz は要らない (検出の遅れは上記の見積もりに織り込み済み)
DEFAULT_INTERVAL_S = 0.02

SensorActive = Callable[[str], bool]
SensorContactCount = Callable[[str], int]
SensorStale = Callable[[str], bool]
#: 軸名 → その軸の指令口。このロボットに無い軸では None
AxisHandleLookup = Callable[[str], "AxisHandle | None"]


class LimitGuard(PeriodicTask):
    """リミットスイッチの接触を監視し、触れた向きへの指令を止める (50Hz)。

    ``CANManager`` も ``PositionTable`` も直接掴まない。センサの読み取りと軸
    ハンドルの取得は注入で受けるので、CAN を 1 本も立てずに全経路を検証できる
    (``SyncMonitor`` / ``HomingRunner`` と同じ作法)。

    ライフサイクル (start / stop / 例外時の継続) は ``PeriodicTask`` と共通。
    """

    def __init__(
        self,
        axes: Sequence[AxisSpec],
        *,
        sensor_active: SensorActive,
        sensor_contact_count: SensorContactCount,
        sensor_is_stale: SensorStale,
        axis_handle: AxisHandleLookup,
        interval_s: float = DEFAULT_INTERVAL_S,
        time_source: Callable[[], float] = time.monotonic,
        sleep: SleepFunc = asyncio.sleep,
    ) -> None:
        """
        Args:
            axes: 保護対象の軸 (``limits`` を持つ軸)。持たない軸は黙って捨てる
            sensor_active: センサ名 → **今**接触しているか
                (``GenericDriver.sensor_active``)
            sensor_contact_count: センサ名 → 立ち上がり (OFF→ON) を数えた接触回数
                (``GenericDriver.sensor_contact_count``)。**読んでも減らない**ので、
                零点確定が同じセンサを見ていても互いの接触を消し合わない
            sensor_is_stale: センサ名 → フィードバックが途絶しているか。
                途絶では**ラッチも解除もしない** (下記 ``blind_sensors``)
            axis_handle: 軸名 → 指令口。**モータ単位の指令口をここへ作らない**
                (左右直結ペアが別々の時刻に動くとその場で機構が壊れる)
            interval_s: 監視周期 [s]
            time_source: 周期とログ間引きに使う単調クロック
            sleep: 周期待ちに使う関数 (テストで差し替え可能)
        """
        super().__init__(interval_s=interval_s, time_source=time_source, sleep=sleep, logger=logger)
        self._axes: dict[str, AxisSpec] = {spec.name: spec for spec in axes if spec.limits}
        #: 軸名 → (センサ名 → 止める向き)。同じ軸に同じセンサを 2 度書けないことは
        #: ``AxisSpec._check_limit_sensors`` が起動時に保証している
        self._directions: dict[str, dict[str, float]] = {
            name: {limit.sensor: limit.direction for limit in spec.limits}
            for name, spec in self._axes.items()
        }
        self._sensor_active = sensor_active
        self._sensor_contact_count = sensor_contact_count
        self._sensor_is_stale = sensor_is_stale
        self._axis_handle = axis_handle

        #: 軸名 → ラッチ中のセンサ名。一時停止中のセンサはここに入らない
        #: (入れると、抜けるまでの 1 周期ぶん `clamp` が指令を止め続ける)
        self._latched: dict[str, frozenset[str]] = {}
        #: センサ名 → 前回の周期で見た接触回数。差だけを見る (回数そのものに意味は無い)
        self._counts: dict[str, int] = {}
        #: センサ名 → 一時停止の入れ子段数。0 になった時点で判定へ戻る。
        #: bool で持つと、入れ子になった 2 つのうち内側が抜けた瞬間に判定が戻る
        self._suspended: dict[str, int] = {}
        #: フィードバック途絶で保護が効いていないセンサ名 (1 周期ごとに作り直す)
        self._blind: tuple[str, ...] = ()
        #: 軸名 → 保護が要求を曲げた回数と直近の理由。**単調増加で、読んでも減らない**
        #: (理由は ``LimitIntervention`` の docstring)
        self._interventions: dict[str, LimitIntervention] = {}

    # ------------------------------------------------------------------ #
    #  状態
    # ------------------------------------------------------------------ #

    @property
    def axis_names(self) -> tuple[str, ...]:
        return tuple(self._axes)

    @property
    def sensor_names(self) -> tuple[str, ...]:
        """監視しているセンサ名 (宣言順・重複なし)。"""
        return tuple(
            dict.fromkeys(sensor for mapping in self._directions.values() for sensor in mapping)
        )

    def watches(self, sensor: str) -> bool:
        """そのセンサを見ているか。零点確定が「外すべき保護」を選ぶために使う。"""
        return any(sensor in mapping for mapping in self._directions.values())

    @property
    def latched(self) -> Mapping[str, tuple[str, ...]]:
        """軸名 → ラッチ中のセンサ名。**読み取り専用の写し**を返す。

        生の集合を返すと、配信や UI の都合でラッチを外せる経路が生まれる。
        解除は「センサが OFF になったこと」でしか起こしてはならない。
        """
        return {
            axis: tuple(sensor for sensor in self._directions[axis] if sensor in sensors)
            for axis, sensors in self._latched.items()
            if sensors
        }

    @property
    def blind_sensors(self) -> tuple[str, ...]:
        """フィードバック途絶で保護が効いていないセンサ名。

        **途絶を「触れていない」と読んで黙って無効にしてはならない。** かといって
        途絶で止めると、スイッチ 1 本の不調で試合中に軸が動かなくなる。だから
        「判定しない」ことを選び、代わりに**効いていないことを必ず見えるように
        する** (ファーム版番号の未確認 ``firmware_unconfirmed_motors`` と同じ、
        「壊れている」ではなく「検出が働いていない」の報告)。

        **一時停止中のセンサは載せない。** あちらは零点確定が意図して外している
        短時間の状態で、手当ての要る異常ではない。
        """
        return self._blind

    def blocked_directions(self, axis: str) -> frozenset[float]:
        """その軸で今禁じられている向き (``LimitSpec.direction``)。

        両端にスイッチがある軸では 2 方向とも入りうる (どちらの端にも触れている =
        機構かセンサ極性の異常だが、判定としては素直に両方を塞ぐ)。
        """
        latched = self._latched.get(axis)
        if not latched:
            return frozenset()
        directions = self._directions.get(axis, {})
        return frozenset(directions[sensor] for sensor in latched if sensor in directions)

    def intervention(self, axis: str) -> LimitIntervention:
        """その軸で保護が要求を曲げた回数と、直近の理由 (センサ名)。

        **``move_to`` が「保護が介入した移動を成功と読まない」ための唯一の口。**
        層① (この class の 50Hz の引き戻し) と層② (``clamp``) のどちらが働いても
        同じカウンタが進む —— どちらも「保護が要求を曲げた」瞬間であり、そこを
        分けると入口でクランプされた場合しか拾えない判定になる (移動の途中で
        触れた場合は指令がもう飛んでいるので、入口の突き合わせには何も残らない)。

        回数は**単調増加で、読んでも減らない** (``LimitIntervention`` の
        docstring)。ラッチと違って ``reset()`` でも落とさない —— 落とすと、
        緊急停止の解除を挟んだ移動だけが「介入していない」ことになる。
        """
        return self._interventions.get(axis, LimitIntervention())

    def _record_intervention(self, axis: str, sensors: Iterable[str]) -> None:
        previous = self._interventions.get(axis, LimitIntervention())
        self._interventions[axis] = LimitIntervention(
            count=previous.count + 1, sensors=tuple(sorted(sensors))
        )

    def clamp(self, axis: str, value: float, observed: float) -> float:
        """禁止方向へ ``observed`` より進む指令を ``observed`` で頭打ちにする。

        **判定の単一情報源。** 50Hz の常駐層 (この class 自身) と指令の入口層は
        どちらもここを呼ぶ。同じ判定を 2 箇所に書くと、片方だけ符号を落としても
        もう片方が拾ってしまい、**壊れた層が壊れたまま残る**
        (``lib/axis_sync.py`` の ``SyncGroup.violation()`` と同じ理由)。

        **拒否ではなくクランプする。** 拒否だと端で操作そのものが効かなくなる
        (手動の可動範囲外を ``ManualSpec.clamp`` が丸めるのと同じ扱い)。
        ラッチしていない向きと保護の無い軸は素通しなので、逆方向へ戻す指令は
        必ず通る —— **復帰できない軸を作らないことがこの保護の前提である。**

        **頭打ちにしたことは ``intervention`` へ数える。** 送った目標との比較で
        ある到達判定は端の位置でも成立してしまうので、数えないと ``move_to`` は
        位置定数とは別の場所で成功する。

        Args:
            axis: 対象軸。保護を持たない軸・知らない軸は ``value`` をそのまま返す
            value: 送ろうとしている軸位置 [軸の unit]
            observed: そのときの実測の軸位置 [軸の unit]
        """
        directions = self._directions.get(axis, {})
        # 「その向きへ実測より進む」= 差の符号が向きと一致すること
        blocking = [
            sensor
            for sensor in self._latched.get(axis, frozenset())
            if sensor in directions and (value - observed) * directions[sensor] > 0.0
        ]
        if not blocking:
            return value
        self._record_intervention(axis, blocking)
        return observed

    def reset(self) -> None:
        """ラッチを全部落とす。

        **通す経路は限定する。** 自動解除 (センサが OFF になったら外れる) がある
        以上、ここを平常運転から呼ぶ必要は無い。想定しているのは「保護そのものを
        仕切り直す」場面 —— 緊急停止解除のように、機体の状態を人間が確認したうえで
        再開する経路だけである。UI から押せるボタンを作ってはならない
        (押せば端に触れたまま指令が通り、そのぶんだけ機構へ押し込む)。

        判定は無効化されないので、まだ触れていれば次の周期で再びラッチする。
        接触回数の基準値は落とさない —— 落とすと「前回から増えたか」の比較対象が
        消え、その 1 周期だけ立ち上がりを取りこぼす。
        """
        self._latched.clear()

    @contextlib.contextmanager
    def suspend_sensors(self, names: Iterable[str]) -> Iterator[None]:
        """指定したセンサの保護だけを一時的に外す。

        通す経路は零点確定 (``HomingRunner.home``) だけ。あちらは「当たるまで
        動かす」動作なので、保護が効いたままだと**スイッチに触れた瞬間に自分の
        指令が引き戻され、原点に到達できない**。

        **軸ごとではなくセンサごとに外す。** 両端にスイッチのある軸
        (``sub_y_axis`` / ``sub_lift``) で軸まるごと外すと、反対端の保護まで
        消える —— 探索が空振りしたときに機構を守るものが 1 つも無くなる。

        再開は ``finally`` で必ず行う。取りこぼすと、その後の試合中ずっとその
        センサの保護が死んだまま残り、しかも画面には何も出ない。

        **入るときにそのセンサのラッチを落とす。** 落とさずに読み取り側だけで
        除くと、一時停止の開始から次の周期までのあいだ ``clamp`` が古いラッチで
        指令を止め続ける (零点確定の 1 歩目がちょうどその窓に入る)。

        Raises:
            KeyError: 監視対象に無いセンサ名 (呼び出し側の取り違えを黙って通さない)
        """
        names = tuple(names)
        unknown = sorted({name for name in names if not self.watches(name)})
        if unknown:
            raise KeyError(f"リミット保護はセンサ {', '.join(unknown)} を持っていません")

        for name in names:
            self._suspended[name] = self._suspended.get(name, 0) + 1
        suspended = set(names)
        for axis, latched in list(self._latched.items()):
            self._set_latched(axis, latched - suspended)
        logger.info("リミット保護を一時停止 (%s, 理由=零点確定)", ", ".join(names))
        try:
            yield
        finally:
            for name in names:
                depth = self._suspended.get(name, 1) - 1
                if depth > 0:
                    self._suspended[name] = depth
                else:
                    self._suspended.pop(name, None)
            logger.info("リミット保護を再開 (%s)", ", ".join(names))

    def is_suspended(self, sensor: str) -> bool:
        """そのセンサの判定が一時停止中か。"""
        return self._suspended.get(sensor, 0) > 0

    def _label(self) -> str:
        return f"リミット保護 ({', '.join(self._axes) or '対象なし'})"

    # ------------------------------------------------------------------ #
    #  監視
    # ------------------------------------------------------------------ #

    async def step(self) -> None:
        """1 周期分の判定を行う。run() から呼ばれるほか、テストから直接駆動できる。"""
        blind: list[str] = []
        for name, spec in self._axes.items():
            await self._check_axis(name, spec, blind)
        # 同じスイッチを 2 軸が見ていても報告は 1 件 (画面には「保護が効いていない
        # スイッチ」として並ぶので、軸の数だけ増えると読み手が数を取り違える)
        self._blind = tuple(dict.fromkeys(blind))

    async def _tick(self) -> None:
        await self.step()

    async def _check_axis(self, axis: str, spec: AxisSpec, blind: list[str]) -> None:
        previous = self._latched.get(axis, frozenset())
        latched: set[str] = set()
        for sensor in self._directions[axis]:
            if self._is_latched(sensor, previous, blind):
                latched.add(sensor)

        if not self._set_latched(axis, latched):
            return
        # **立ち上がりでだけ止める。** 毎周期書き直すと、負荷で下がったぶんへ
        # 目標が追従して誰も操作していないのに軸がクリープする。さらに、逆方向へ
        # 退避しようとした指令をこちらが上書きし続けることになる
        await self._stop_axis(spec)

    def _is_latched(self, sensor: str, previous: frozenset[str], blind: list[str]) -> bool:
        """このセンサが今ラッチされている状態か。**基準値の更新も併せて行う。**

        **ラッチする条件は「前回から接触回数が増えた」または「今 ON」。両方要る。**
        前者だけでは触れっぱなしを拾えない (立ち上がりは 1 回しか立たない)。
        後者だけでは ON 区間が観測周期より狭い接触を取りこぼす —— 実機で
        「スイッチに当たっているのに止まらず可動範囲の端を越えて回り続けた」
        壊れ方が起きている。

        **解除は「今 OFF」かつ「この周期で回数が増えていない」。** 増えていたら、
        その周期のあいだに入って出た (= また触れた) ということなので解除しない。
        """
        # 一時停止中でも基準値だけは進める。止めていた間の接触をまとめて
        # 「増えた」と読むと、抜けた次の周期でいきなりラッチする
        count = self._sensor_contact_count(sensor)
        last = self._counts.get(sensor)
        self._counts[sensor] = count
        rose = last is not None and count != last

        if self.is_suspended(sensor):
            return False
        if self._sensor_is_stale(sensor):
            # 途絶では判定しない (ラッチも解除もしない)。効いていないことは
            # blind_sensors として必ず見えるようにする
            blind.append(sensor)
            return sensor in previous
        return rose or self._sensor_active(sensor)

    def _set_latched(self, axis: str, latched: Iterable[str]) -> bool:
        """ラッチ集合を差し替える。**立ち上がり (0 本 → 1 本以上) なら True。**"""
        previous = self._latched.get(axis, frozenset())
        current = frozenset(latched)
        if current == previous:
            return False
        self._latched[axis] = current
        if not current:
            self._log.info(
                f"limit_release:{axis}",
                "リミットスイッチから離れました (axis=%s, sensor=%s): 保護を解除します",
                axis,
                ", ".join(sorted(previous)),
            )
            return False
        if previous:
            # ラッチ中にもう 1 本増えた (両端に触れた) だけ。止め直さない ——
            # 止めるのは既に済んでおり、書き直すと実測を測り直すことになる
            return False
        self._log.warning(
            f"limit_latch:{axis}",
            "リミットスイッチに接触 (axis=%s, sensor=%s): この向きへの指令を止めます",
            axis,
            ", ".join(sorted(current)),
        )
        return True

    async def _stop_axis(self, spec: AxisSpec) -> None:
        """その軸の目標値を、そのときの実測位置へ**1 度だけ**書き直す。

        **電流 0 にも無励磁にもしない (``clear_target`` を使わない)。**
        ``sub_lift`` は保持ブレーキを持たないので、消磁するとワークごと自重で
        落ちる (``config/sub_hand_positions.yaml`` と ``checklist.yaml`` の
        ``sub_lift_holds`` がその前提に立っている)。

        **目標を 1 つも持っていない軸には書かない。** 誰も駆動していないなら
        止めるものが無く、書けば「誰も操作していないのに保持が始まる」。

        **指令は必ず ``AxisHandle.set_target_value`` を通す。** 左右直結ペアが
        同一フレームで動く唯一の経路であり、モータ単位の指令口をここへ作っては
        ならない。

        **緊急停止中は送らない。** ``EStopActiveError`` は握って黙って見送る
        (停止中は送らないことが正常)。ラッチ自体は続くので、解除後に触れたままなら
        指令の入口層が同じ ``clamp`` で止める。

        **書き戻せたときだけ ``intervention`` へ数える。** 数えるのは「保護が要求を
        曲げた」ことであって「触れた」ことではない —— 送れなかった周期を数えると、
        機構が要求どおり動き切った移動まで ``move_to`` が失敗させる。
        """
        handle = self._axis_handle(spec.name)
        if handle is None:
            self._log.warning(
                f"limit_handle:{spec.name}",
                "リミット保護: 軸 %s の指令口がありません (保護は判定だけになります)",
                spec.name,
            )
            return
        if not handle.has_target:
            return

        try:
            observed = handle.observed_value()
        except Exception:
            self._log.exception(
                f"limit_observe:{spec.name}",
                "リミット保護: 軸 %s の実測位置を読めません",
                spec.name,
            )
            return

        try:
            await handle.set_target_value(spec.to_commands(observed))
        except EStopActiveError:
            return
        except Exception:
            # 1 軸の送信失敗で監視まで死ぬ方が危険。残りの軸の判定は続ける
            self._log.exception(
                f"limit_stop:{spec.name}", "リミット保護: 軸 %s を止められません", spec.name
            )
            return

        self._record_intervention(spec.name, self._latched.get(spec.name, frozenset()))
        self._log.info(
            f"limit_hold:{spec.name}",
            "リミット保護: 軸 %s の目標を実測位置 %.3f%s へ引き戻しました",
            spec.name,
            observed,
            spec.unit,
        )


class CombinedLimitGuard:
    """複数ロボットの保護を 1 つの ``clamp`` 口へ束ねる。

    統合動作確認 (``sequences/motor_check.py``) は両ハンドのモータを 1 つの
    ``MotorGroup`` へ混ぜて 1 本のシーケンスで駆動するので、そこへ結べる保護は
    1 つしか無い。**軸名はロボット横断に一意** (``PositionTable.merged`` が衝突を
    起動ごと落とす) なので、軸名だけでどちらの保護が答えるかは一意に決まる。

    **ここに判定は 1 行も無い。** 保護そのものは各 ``LimitGuard`` が持ち、
    こちらは配るだけである (境界を 2 つに増やさない)。
    """

    def __init__(self, guards: Sequence[LimitGuard]) -> None:
        self._guards = tuple(guards)

    @property
    def axis_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(name for guard in self._guards for name in guard.axis_names))

    def clamp(self, axis: str, value: float, observed: float) -> float:
        for guard in self._guards:
            value = guard.clamp(axis, value, observed)
        return value

    def intervention(self, axis: str) -> LimitIntervention:
        """介入回数を全ロボットぶん足し合わせる。

        軸名はロボット横断に一意なので実際に答えるのは 1 つだが、**足し合わせに
        してあるのは「どれが答えるか」をここで決めないため** —— 選ぶ実装は
        軸名の一意性が崩れた日に片方の保護を黙って無視する。
        """
        count = 0
        sensors: tuple[str, ...] = ()
        for guard in self._guards:
            one = guard.intervention(axis)
            count += one.count
            if one.sensors:
                sensors = one.sensors
        return LimitIntervention(count=count, sensors=sensors)


def combine_limit_guards(guards: Sequence[LimitGuard]) -> CombinedLimitGuard | None:
    """束ねた保護を返す。1 つも無ければ None (結ぶものが無い)。"""
    return CombinedLimitGuard(guards) if guards else None
