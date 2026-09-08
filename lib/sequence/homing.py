"""リミットスイッチまで軸を寄せて零点を確定する。

電源投入位置をそのまま原点にすると、前の試合の終了姿勢や、搬送中に手で動かした
ぶんがそのまま座標のずれになる。位置定数はすべて原点からの相対値なので、
ずれた原点のまま走らせると全ステップが同じだけずれた場所へ動く。

**この操作は「当たるまで動かす」ので、止める仕組みが要る。** 7 つ用意してある:

1. **探索距離の上限** (`HomingSpec.search_distance`) — 超えたら失敗として降りる。
   配線が抜けている・センサが死んでいる場合の唯一の無人の歯止め。
   **数えるのは実測位置の移動量**であって、送った指令の積算ではない
   (指令の積算で数えると、指令が実位置から離れているぶんが上限を素通りする)
2. **センサ鮮度の事前確認** — フィードバックが途絶しているセンサでは 1 歩も動かさない。
   死んだセンサは「いつまでも当たらない」形でしか現れず、探索距離いっぱいまで
   機構を押し込んでから初めて分かる
3. **対象軸のフィードバック鮮度の事前確認** — 実測位置が読めないまま始めると、
   未受信の 0.0 を現在位置と信じて全ストロークぶんの指令を 1 回で出す
4. **1 歩ごとの再アンカー** — 指令は毎回**そのときの実測位置** + `step` で組む。
   指令が実位置を追い越して先行し続けることが構造的に起こらず、機構が引っかかった
   ときも 1 step ぶんの偏差しか掛からない。代わりに引っかかった機構は探索距離の
   上限へ永久に届かなくなるので、**停滞判定 (`_STALL_LIMIT`) が対になる**。
   その閾値は 1 歩ぶんの追従を待つ側 (`_wait_step`) と共有する
   (`_progress_threshold`) —— 基準が分かれると、待たずに抜けたことを
   「進まなかった」と数え、動いている機構を止めてしまう
5. **離脱の歩数上限** (`_RELEASE_STEP_LIMIT`) — 触れた状態から始めたときに
   一度センサの外まで離れるが、その離脱にも上限が要る。接点の固着と**極性の
   取り違え** (ファーム側 `sensorActiveLow` の設定ミス) はどちらも
   「いつまでも OFF にならない」形でしか現れない。**探索距離を流用してはならない**
   (あちらは実ストローク相当まで伸びる値なので、反対側の機構端まで走り抜ける)
6. **整列段の移動量上限** (`HomingSpec.align_distance`) — 左右にスイッチが 1 本ずつ
   付く軸 (下記) では、片方だけが押されている間に**押されていない側のモータだけ**を
   進める。極性を取り違えたセンサ・断線したスイッチはここでも「いつまでも押されない」
   形でしか現れないので、片側だけを動かす操作にも無人の歯止めが要る。
   **`search_distance` を流用してはならない** —— 左右のずれは機構の遊びの範囲
   (数 mm) しかありえず、探索距離まで片側を進めるのは軸をねじり切ることに等しい
   (`sync_tolerance` を超えた時点で偏差監視が全体緊急停止を出す)
7. **緊急停止** — 目標値を送る経路 (`AxisHandle`) が既にインターロックを通る

**探索の到達判定は接触回数の増加で見る。「今 ON か」では取りこぼす。** ON 区間が
`step` より狭い機構では、指令 1 回で区間を跨いでしまい `settle_s` 後の観測ではもう
OFF になっている (`rotate` は step 2.0deg を約 18ms で通過する)。センサの FEEDBACK は
100Hz で届いているので、受信のたびに数えておけば 1 通も取りこぼさない
(`GenericDriver.sensor_contact_count`)。**カウンタは読んでも減らない**ので、
零点確定以外の読み手 (リミットスイッチ保護) が同じセンサを見ていても、
どちらかが相手のぶんまで消してしまうことがない。**離脱は現在値のまま**という
非対称は意図したもので、理由は `HomingRunner._sensor_reached` に書いてある。

**走っているあいだ、見るセンサのリミット保護 (`LimitGuard`) は外す。** あちらは
「触れたらその向きへの指令を止める」常駐保護なので、効いたままだとスイッチに
触れた瞬間に自分の指令が実測位置へ引き戻され、原点へ到達できない。外すのは
`homing.sensor_names` の全センサで、**軸まるごとではなくセンサ単位**である
(両端にスイッチのある軸で反対端の保護まで消さない)。それでも無人の歯止めが
消えないのは、上の 7 つがどれも保護とは独立に効いているためである。

**触れた状態から始めたら、一度離れてから寄せ直す。** リミットスイッチの ON 区間には
幅があるので、触れたその場を原点にすると「区間のどこで探索を始めたか」がそのまま
原点のばらつきになる。症状は「原点合わせをしたのに位置がずれる」だけで、始めた位置は
毎回違うので再現もしない。区間の外まで離してから通常の探索へ渡せば、確定位置は
探索の `step` 粒度に収まる。**離脱は探索と逆向き**なので、機構端で始まったときに
押し込まない性質はそのまま保たれる。

**左右にスイッチが 1 本ずつ付く軸は「両方が押された姿勢」を原点にする。**
`HomingSpec.sensors` (モータ名 → センサ名) を書いた軸では、探索でどちらか 1 本が
当たった後に**整列段**が走り、まだ押されていない側のモータだけを探索方向へ進める。
機構には遊びがあるので左右のわずかなずれは物理的に存在し、片方が当たった瞬間の
姿勢をそのまま原点にすると、そのずれが原点のずれとして焼き付く。

**整列段は探索と意図的に非対称で、既に当たった側は「検出時点の実測位置」で固定保持
する (毎歩実測へ再アンカーしない)。** 遊びが無い機構 —— つまり片側駆動が物理的に
成立しない機構 —— では、進めた側が保持側を引きずる。保持側の目標を毎歩実測へ
張り直すと目標が引きずられた先へ追従するので、左右のずれは増えず
`align_distance` にも停滞判定にも掛からないまま、**軸ごと機構端まで走る**。
固定保持なら引きずりは「進めた側が進まない」形で現れ、停滞判定 (`_STALL_LIMIT`) が
拾う —— **「この機構では片側駆動が成立しない」を検出できる唯一の形である。**

**接触は走行中に溜め込む** (`_SensorContacts`)。探索は「いずれか 1 本が当たった」で
止まるが、その後の整列段は「まだ押されていないのはどれか」を知らなければ指令先を
選べないので、どちらが先に当たったかを整列段まで持ち越す必要がある。

**原点確定はグループ単位でしか行わない。** 左右直結ペアを別々の時刻に確定すると、
その間に片方が動いたぶんだけ消えないオフセットが残り、正常な動作でも即座に
偏差超過で止まる (`M3508PositionLoop.set_group_origin_here` と同じ理由)。

**原点を確定できない軸では 1 歩も動かさない。** 確定手段の有無は探索の前に問う
(`origin_capturable`)。センサまで押し込んでから「確定できません」で降りると、
機構を動かした意味が無いまま姿勢だけが変わる。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager

from lib.drivers.base import ControlMode
from lib.sequence.motors import AxisHandle
from lib.sequence.positions import AxisSpec, HomingSpec

logger = logging.getLogger(__name__)

__all__ = ["HomingError", "HomingRunner"]

#: 1 歩ぶんの追従を待つ最大回数 (`settle_s` ごとに再確認する)。
#: 待たずに次の指令を出すと、追従の遅い機構では指令だけが先へ進み、
#: 「step ずつ確かめながら寄せる」動作にならない。
#: **1 回では足りない。** 問い合わせ駆動のドライバ (EDULITE 05 / DM3520) の実測値は
#: `QueryDrivenTargetRefresher` の 20Hz でしか更新されないので、`settle_s` が
#: 50ms の軸では 1 回の確認が「フィードバック 1 更新ぶん」にしかならず、
#: 動いている機構でもその 1 回では移動が見えないことがある。
_FOLLOW_ATTEMPTS = 5

#: 実測が進まないまま許す連続ステップ数。超えたら失敗として降りる。
#: 指令を実測へ再アンカーしているので、引っかかった機構は「指令しても進まない」形で
#: しか現れず、探索距離の上限だけでは永久に降りられない。
_STALL_LIMIT = 3

#: 「1 歩ぶん進んだ」と見なす移動量 (`step` に対する割合)。
#: **追従待ちの完了判定 (`_wait_step`) と停滞判定 (`_seek`) は同じこの 1 つを見る。**
#: 基準が分かれると、待つ側が「まだ動いていない」段階で抜けたぶんを数える側が
#: 「待ったのに進まなかった」と読み、動いている機構が数歩で失敗する。
_PROGRESS_FRACTION = 0.5

#: 離脱 (センサに触れた状態から抜けるまで) に許す最大歩数。
#: リミットスイッチの ON 区間は数 mm しかないので、step の数十倍動いても OFF に
#: ならなければセンサが張り付いている (接点の固着・配線の短絡)。
#: **`search_distance` を流用してはならない** —— あちらは実ストローク相当まで
#: 伸びる値で、離脱の上限に使うと反対側の機構端まで走り抜ける。
_RELEASE_STEP_LIMIT = 20


class HomingError(RuntimeError):
    """零点を確定できなかった。シーケンスを止めて操縦者に知らせる。

    「原点がずれたまま走る」より「動かないまま止まる」ほうが安全なので、
    黙って続行してはならない。
    """


SensorActive = Callable[[str], bool]
#: センサ名 → 立ち上がり (OFF→ON) を数えた接触回数
#: (`GenericDriver.sensor_contact_count`)。**読んでも減らない**ので、
#: 零点確定以外の読み手が同じセンサを見ていても取りこぼしを起こさない。
#: 読み手は自分で基準値を控え、その差だけを見る (`_SensorContacts`)。
SensorContactCount = Callable[[str], int]
SensorStale = Callable[[str], bool]
MotorStale = Callable[[str], bool]
OriginCapturable = Callable[[str], bool]
#: 原点の確定は CAN の往復を伴いうる (EDULITE 05 は無励磁 → SET_ZERO → 再励磁の
#: 3 段で、途中に応答待ちが入る) ため非同期。**可否を問う `OriginCapturable` は
#: 同期のまま**にしておくこと —— 探索を始める前に 1 度だけ問う判定であり、
#: 非同期にすると「押し込んでから確定できませんで降りる」経路を塞いでいる
#: 事前確認が、待ちを挟む重い操作に見えてしまう。
CaptureOrigin = Callable[[str], Awaitable[None]]
SleepFunc = Callable[[float], Awaitable[None]]
#: センサ名の並び → そのセンサのリミット保護だけを外す contextmanager
#: (`LimitGuard.suspend_sensors`)。**軸ではなくセンサ単位**なのは、両端に
#: スイッチのある軸で反対端の保護まで消さないため。
SuspendSensors = Callable[[Iterable[str]], AbstractContextManager[None]]


@contextlib.contextmanager
def _no_suspend(_names: Iterable[str]) -> Iterator[None]:
    """保護を持たない構成用の既定。**何もしない。**

    既定値を置くのは、CAN も `LimitGuard` も無い状態で零点確定を検証できる性質を
    保つため。本番 (`main.py`) からは必ず本物を渡すこと —— 渡し忘れると、
    リミット保護が探索の 1 歩目を引き戻して零点確定が必ず失敗する。
    """
    yield


class _SensorContacts:
    """**基準値からの接触回数の増加**を、走行中センサごとに溜め込む。

    センサ側 (`GenericDriver.sensor_contact_count`) は読んでも減らない単調増加の
    回数なので、**この読み方は他の読み手 (リミットスイッチ保護) と競合しない。**
    かつての「読むと消えるラッチ」は先に読んだ側が相手のぶんまで消したため、
    読み手が 2 人になった瞬間に探索が接触を取りこぼした。

    溜め込むのは、どちらのスイッチが先に押されたかを**整列段まで持ち越す**必要が
    あるため。探索は「いずれか 1 本」で止まるが、その後の整列段は「まだ押されて
    いないのはどれか」を知らなければ指令先を選べない。
    """

    def __init__(self, sensors: tuple[str, ...], count: SensorContactCount) -> None:
        self._sensors = sensors
        self._count = count
        #: センサ名 → 基準値。**初めて見た時点で取る** (それ以前の接触は見ない)。
        #: 探索の直前に `reset()` が取り直す
        self._baseline: dict[str, int] = {}
        self._latched: dict[str, bool] = dict.fromkeys(sensors, False)

    def poll(self) -> None:
        """全センサの回数を 1 回ずつ読み、基準値から増えていた分を記録へ足す。"""
        for name in self._sensors:
            count = self._count(name)
            if count != self._baseline.setdefault(name, count):
                self._latched[name] = True

    def reset(self) -> None:
        """基準値を今の回数へ取り直す。**探索を始める直前に 1 度だけ呼ぶ。**

        離脱段のあいだに触れたぶんや、前回の零点確定・手動操縦でスイッチを
        跨いだぶんまで数えていると、1 歩目の観測でいきなり到達と読み、
        スイッチではなく探索開始位置が原点になる。
        """
        for name in self._sensors:
            self._baseline[name] = self._count(name)
            self._latched[name] = False

    def any_latched(self) -> bool:
        """いずれか 1 本でも接触したか (探索の到達判定)。"""
        self.poll()
        return any(self._latched.values())

    def latched(self, sensor: str) -> bool:
        """そのセンサが接触済みか。**読み直さない** (記録だけを見る)。"""
        return self._latched[sensor]


def _sensor_label(homing: HomingSpec) -> str:
    """メッセージに出すセンサ名。単数形の軸では従来どおり 1 語になる。"""
    return " / ".join(f"'{name}'" for name in homing.sensor_names)


def _progress_threshold(homing: HomingSpec) -> float:
    """「1 歩ぶん進んだ」と見なす移動量 [軸の unit]。

    追従待ちと停滞判定が**同じ 1 つの基準**を見るために関数へ切り出してある。
    片方に定数を書き写すと、もう片方だけを直せる形が残る。
    """
    return homing.step * _PROGRESS_FRACTION


class HomingRunner:
    """1 軸ぶんのホーミングを実行する。

    センサの読み取りと原点確定の実体は注入する。ここが `CANManager` や
    `M3508PositionLoop` を直接掴むと、CAN を立てないと 1 行も検証できなくなる。
    """

    def __init__(
        self,
        *,
        sensor_active: SensorActive,
        sensor_contact_count: SensorContactCount,
        sensor_is_stale: SensorStale,
        motor_is_stale: MotorStale,
        origin_capturable: OriginCapturable,
        capture_origin: CaptureOrigin,
        suspend_sensors: SuspendSensors = _no_suspend,
        sleep: SleepFunc = asyncio.sleep,
    ) -> None:
        """
        Args:
            sensor_active: センサ名 → **今**接触しているか
                (`GenericDriver.sensor_active`)。離脱の判定と、探索前の
                「既に触れているか」がこちらを見る
            sensor_contact_count: センサ名 → 立ち上がりを数えた**接触回数**
                (`GenericDriver.sensor_contact_count`)。読んでも減らないので、
                他の読み手が同じセンサを見ていても取りこぼさない。
                **探索の到達判定だけがこちらを見る** (`_sensor_reached`)。
                **既定値を持たせない** —— 未配線が「取りこぼす探索」に黙って戻る
            sensor_is_stale: センサ名 → フィードバックが途絶しているか
            motor_is_stale: モータ名 → フィードバックが途絶しているか。
                **既定値を持たせない** —— 配線を忘れると「未受信の 0.0 を現在位置と
                信じて全ストローク動く」という、この修正が消したはずの経路が戻る
            origin_capturable: 軸名 → 原点を確定できるか。探索の前に問う
            capture_origin: 軸名 → その軸の現在位置を原点として確定する
                (左右ペアはグループ全員へ展開されること)。CAN の往復を挟む
                実装があるので非同期
            suspend_sensors: そのセンサのリミット保護を外す contextmanager。
                零点確定は「当たるまで動かす」動作なので、保護が効いたままだと
                触れた瞬間に自分の指令が引き戻され原点へ到達できない
            sleep: 1 ステップごとの待ち (テストで差し替える)
        """
        self._sensor_active = sensor_active
        self._sensor_contact_count = sensor_contact_count
        self._sensor_is_stale = sensor_is_stale
        self._motor_is_stale = motor_is_stale
        self._origin_capturable = origin_capturable
        self._capture_origin = capture_origin
        self._suspend_sensors = suspend_sensors
        self._sleep = sleep

    async def home(self, spec: AxisSpec, handle: AxisHandle) -> float:
        """`spec.homing` に従って軸を寄せ、当たった位置を原点として確定する。

        探索の起点は**実測位置**であって 0 ではない。起点を 0 に固定すると、
        1 歩目が「現在位置から 1 step」ではなく原点近傍への 1 回のジャンプになり、
        その移動が探索距離の上限を 1mm も消費しない (手動操縦で軸を動かした後や、
        一度原点確定した後に踏む。どちらもセッティングタイムに普通に起きる)。

        Args:
            spec: 対象軸。`homing` を持たない軸を渡すのは呼び出し側の誤り
            handle: 目標値の送り口。**軸単位で 1 回だけ指令する**
                (左右が別々の時刻に動くとその場で機構が壊れる)

        Returns:
            **探索で**実際に動いた距離 [軸の unit]。ログと検証用。
            離脱 (下記) と整列段のぶんは含めない —— 原点の精度を決めているのは
            「どこから寄せて当たったか」であって、その前に離れた距離ではない。

        Raises:
            HomingError: 原点を確定する手段が無い / センサまたは軸のフィードバックが
                途絶している / 実測が進まない / 探索距離を超えても当たらなかった /
                離脱してもセンサが OFF にならない / 整列段で片側が
                ``align_distance`` 動いてもスイッチに届かない / 整列段で
                進めているモータが動かない (遊びが無く片側駆動が成立しない機構)
        """
        homing = spec.homing
        if homing is None:
            raise HomingError(f"軸 '{spec.name}' に homing 設定がありません")

        self._check_preconditions(spec, homing)
        # **見るセンサ全部の保護を、事前確認の後から原点確定まで外す。**
        # 1 本でも外し忘れると、整列段で「まだ押されていない側」を進めている最中に
        # **既に押されている側**のセンサで保護が発動し、零点確定が必ず失敗する。
        # 場合分けを書かず `sensor_names` を使うのはそのため (単数形・複数形の
        # 違いを畳む唯一の口)。事前確認より前に外さないのは、外さなくても
        # 1 歩も動かない段だからである (保護は指令にしか効かない)。
        with self._suspend_sensors(homing.sensor_names):
            return await self._run_homing(spec, handle, homing)

    async def _run_homing(self, spec: AxisSpec, handle: AxisHandle, homing: HomingSpec) -> float:
        """離脱 → 探索 → 整列 → 原点確定。**リミット保護を外した状態で走る。**"""
        latches = _SensorContacts(homing.sensor_names, self._sensor_contact_count)

        # ここが問うのは「**今**触れているか」なのでラッチではない。ラッチで問うと、
        # 前回の零点確定や手動操縦でスイッチを跨いだ痕跡だけで離脱段へ入り、
        # 触れてもいない位置から _RELEASE_STEP_LIMIT 歩ぶん離れる向きへ動き出す。
        # **いずれか 1 本でも ON なら離脱する** —— 片方が触れたまま探索を始めると、
        # その 1 本は 1 歩目のラッチで必ず到達と読まれ、探索開始位置が原点になる
        if any(self._sensor_active(name) for name in homing.sensor_names):
            # **触れた状態のまま確定してはならない。** リミットスイッチの ON 区間には
            # 幅があるので、その場を原点にすると「区間のどこで探索を始めたか」が
            # そのまま原点のばらつきになる (区間幅ぶん = step の何倍にもなる)。
            # いったん区間の外まで離れてから寄せ直せば、確定位置は探索の step 粒度に
            # 収まり、どこから始めても同じ場所が原点になる。
            # **離脱は探索と逆向き**なので押し込む方向へは動かない (機構端で始まった
            # ときに壊さない、という元の性質は保たれる)。
            logger.info("[homing] %s: 既にセンサに触れているため一度離れて寄せ直す", spec.name)
            await self._seek(
                spec,
                handle,
                homing,
                latches,
                direction=-homing.direction,
                want_active=False,
                limit=homing.step * _RELEASE_STEP_LIMIT,
                limit_message=(
                    f"軸 '{spec.name}' を原点センサ {_sensor_label(homing)} から"
                    "離せませんでした"
                    f" ({homing.step * _RELEASE_STEP_LIMIT}{spec.unit} 動かしても OFF に"
                    " ならない)。**センサの極性が逆だとどこへ動かしても ON のまま**に"
                    " なるので、ファーム側の極性設定 (sensorActiveLow) を"
                    "接点の固着・配線の短絡と併せて確認してください"
                ),
            )

        origin = self._observe(spec, handle)
        observed = await self._seek(
            spec,
            handle,
            homing,
            latches,
            direction=homing.direction,
            want_active=True,
            limit=homing.search_distance,
            limit_message=(
                f"軸 '{spec.name}' が {homing.search_distance}{spec.unit} 動かしても"
                f" 原点センサ {_sensor_label(homing)} に到達しませんでした"
                " (探索方向・機構の引っかかり・センサの配線を確認してください)"
            ),
        )

        travelled = abs(observed - origin)
        logger.info("[homing] %s: %.2f%s 動かして原点に到達", spec.name, travelled, spec.unit)
        if homing.sensors is not None:
            # 左右にスイッチが 1 本ずつ付く軸だけの段。**単数形の軸は 1 歩も動かさない**
            await self._align(spec, handle, homing, homing.sensors, latches)
        await self._capture_origin(spec.name)
        return travelled

    async def _align(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        sensors: Mapping[str, str],
        latches: _SensorContacts,
    ) -> None:
        """**両方のスイッチが押された姿勢**まで、押されていない側のモータだけを進める。

        機構には遊びがあるので左右のわずかなずれは物理的に存在する。片方が当たった
        瞬間の姿勢をそのまま原点にすると、そのずれが原点のずれとして焼き付く。

        **既に当たった側は「ラッチを検出した時点の実測位置」で固定保持し、毎歩実測へ
        再アンカーしない。探索段と意図的に非対称である。** 遊びが無い機構 (= 片側駆動が
        物理的に成立しない機構) では、進めた側が保持側を引きずる。保持側の目標を毎歩
        実測へ張り直すと、目標が引きずられた先へ追従するので**左右のずれが増えず**、
        `align_distance` も停滞判定も永久に発火しないまま**軸ごと機構端まで走る**。
        固定保持なら引きずりは「進めた側が進まない」形でしか現れないので停滞判定が
        拾う —— 「この機構では片側駆動が成立しない」を検出できる唯一の形である。

        Raises:
            HomingError: 進めた側が ``align_distance`` 動いてもスイッチに届かない /
                進めた側が動かない
        """
        pending = [motor for motor, sensor in sensors.items() if not latches.latched(sensor)]
        if not pending:
            logger.info("[homing] %s: 整列段は不要 (全センサが同時に接触)", spec.name)
            self._log_sensor_states(spec, homing)
            return

        logger.info(
            "[homing] %s: 整列段 (未接触: %s)",
            spec.name,
            ", ".join(f"{motor}→{sensors[motor]}" for motor in pending),
        )

        # **保持値も開始位置もモータ単独の実測から取る。** 軸平均 (`observed_value`) は
        # 左右がずれていればどちらか一方が必ず誤りで、まさにそのずれを見る段では使えない
        hold = self._observe_each(spec, handle)
        start = dict(hold)
        stalled: dict[str, int] = dict.fromkeys(pending, 0)
        progress = _progress_threshold(homing)

        while True:
            latches.poll()
            observed = self._observe_each(spec, handle)
            for motor in [motor for motor in pending if latches.latched(sensors[motor])]:
                # **検出した瞬間の実測位置を保持値として固定する。** 以後書き換えない
                hold[motor] = observed[motor]
                pending.remove(motor)
                logger.info(
                    "[homing] %s: %s を %.2f%s 進めて %s に到達",
                    spec.name,
                    motor,
                    abs(observed[motor] - start[motor]),
                    spec.unit,
                    sensors[motor],
                )

            if not pending:
                # 最後に送った「実測 + step」が生きたままだと、原点確定 (`SET_ZERO` の
                # disable) が届くまでスイッチを越えた先へ向かい続ける (探索段と同じ理由)
                await self._command_align(spec, handle, homing, hold, pending, observed)
                self._log_sensor_states(spec, homing)
                return

            self._check_align_distance(spec, homing, sensors, pending, observed, start)

            commanded = await self._command_align(spec, handle, homing, hold, pending, observed)
            await self._wait_align_step(spec, handle, homing, latches, sensors, pending, commanded)

            # 指令を実測へ再アンカーしている以上、動かない側は「指令しても進まない」
            # 形でしか現れず、align_distance だけでは永久に降りられない (探索段と同じ)。
            # **引きずられて相対的に進まない機構もここでしか捕まらない** —— 保持側を
            # 固定しているからこそ、その引きずりが「進めた側が進まない」として出る
            moved = self._observe_each(spec, handle)
            for motor in pending:
                stalled[motor] = (
                    0 if abs(moved[motor] - observed[motor]) >= progress else stalled[motor] + 1
                )
                if stalled[motor] >= _STALL_LIMIT:
                    raise HomingError(
                        f"軸 '{spec.name}' の整列段でモータ '{motor}' が指令しても動きません"
                        f" ({_STALL_LIMIT} 歩連続で {progress}{spec.unit} 進まなかった)。"
                        "**機構に遊びが無いと片側だけを動かせず、相方を引きずったまま"
                        "止まって見えます** —— 機構の引っかかり・モータの励磁と"
                        "併せて確認してください"
                    )

    async def _command_align(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        hold: Mapping[str, float],
        pending: list[str],
        observed: Mapping[str, float],
    ) -> dict[str, float]:
        """整列段の 1 指令。**軸単位で 1 回だけ送る唯一の口。** 送った軸位置を返す。

        進めるモータには「そのモータ単独の実測 + 1 歩」、進めないモータには**必ず
        保持値**を載せる。モータ 1 台だけを指令する経路を作ってはならない ——
        左右直結ペアが別々の時刻に動くとその場で機構が壊れるので、指令は常に
        `AxisHandle.set_target_value` の 1 回に束ねる (`to_commands_each` はキーの
        過不足を `KeyError` で拒否するので、載せ忘れも構造的に塞がっている)。
        """
        values = {
            motor: (
                observed[motor] + homing.direction * homing.step
                if motor in pending
                else hold[motor]
            )
            for motor in spec.motor_names
        }
        await handle.set_target_value(spec.to_commands_each(values))
        return values

    def _check_align_distance(
        self,
        spec: AxisSpec,
        homing: HomingSpec,
        sensors: Mapping[str, str],
        pending: list[str],
        observed: Mapping[str, float],
        start: Mapping[str, float],
    ) -> None:
        """整列開始位置からの移動量の上限。**進めるモータ単独の実測**で数える。

        極性を取り違えたセンサ・断線したスイッチは「押されていない側をいつまでも
        進める」形でしか現れないので、ここが整列段の唯一の無人の歯止めになる。
        """
        limit = homing.align_distance
        if limit is None:
            return
        for motor in pending:
            if abs(observed[motor] - start[motor]) < limit:
                continue
            raise HomingError(
                f"軸 '{spec.name}' の整列段でモータ '{motor}' を {limit}{spec.unit}"
                f" 動かしても原点センサ '{sensors[motor]}' が反応しませんでした。"
                "**センサの極性が逆だと押しても ON になりません** —— ファーム側の"
                "極性設定 (sensorActiveLow) と、スイッチの配線・断線を確認してください"
            )

    async def _wait_align_step(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        latches: _SensorContacts,
        sensors: Mapping[str, str],
        pending: list[str],
        commanded: Mapping[str, float],
    ) -> None:
        """整列段の 1 歩ぶんの追従を待つ。待っている間もセンサを見る。

        考え方は `_wait_step` と同じ (`spec.tolerance` を流用しない理由もそのまま
        当てはまる) が、**判定は進めているモータ単独の実測で行う**ので共有できない
        —— 軸平均で見ると保持側が動かないぶん常に半分しか進んでいないように見え、
        どの歩も `_FOLLOW_ATTEMPTS` を使い切る。
        """
        reached = _progress_threshold(homing)
        for _ in range(_FOLLOW_ATTEMPTS):
            await self._sleep(homing.settle_s)
            latches.poll()
            if any(latches.latched(sensors[motor]) for motor in pending):
                return
            observed = self._observe_each(spec, handle)
            if all(abs(observed[motor] - commanded[motor]) <= reached for motor in pending):
                return

    def _log_sensor_states(self, spec: AxisSpec, homing: HomingSpec) -> None:
        """原点確定の直前に全センサの現在値を残す。**判定には使わない。**

        ON 区間が `homing.step` より狭いスイッチでは検出の直後にはもう抜けている
        (実機の `rotate` は step 2.0deg を約 18ms で通過する)。ここを判定に使うと
        そういう機構では必ず失敗するので、記録だけに留める。
        """
        logger.info(
            "[homing] %s: 原点確定 (センサ現在値: %s)",
            spec.name,
            ", ".join(
                f"{name}={'ON' if self._sensor_active(name) else 'OFF'}"
                for name in homing.sensor_names
            ),
        )

    async def _seek(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        latches: _SensorContacts,
        *,
        direction: int,
        want_active: bool,
        limit: float,
        limit_message: str,
    ) -> float:
        """センサが `want_active` になるまで `direction` 方向へ step ずつ動かす。

        **探索と離脱の両方がこの 1 本を通る。** 歯止め (移動量の上限・停滞判定) を
        向きごとに書き分けると、片方だけ直せてしまう —— 症状は「探索は止まるのに
        離脱は永久に動き続ける」で、離脱は普段踏まない経路なので気付けない。

        **軸全体を動かす段なので、センサが何本あっても指令は軸単位のまま。**
        到達は「いずれか 1 本がラッチした」、離脱完了は「全センサが OFF」で、
        どちらのモータが先に当たったかは `latches` が覚えて整列段へ渡す。

        Args:
            direction: 進む向き。探索は `homing.direction`、離脱はその反対
            want_active: この状態になったら到達。探索は True、離脱は False
            limit: 実測の移動量の上限。超えたら `limit_message` で降りる
        """
        if want_active:
            # **探索を始める直前に基準値を取り直す。** 離脱段のあいだに触れたぶんや、
            # 前回の零点確定・手動操縦でスイッチを跨いだぶんまで数えていると、
            # 1 歩目の観測でいきなり到達と読み、探索開始位置が原点になる
            latches.reset()

        start = self._observe(spec, handle)
        observed = start
        stalled = 0
        while True:
            if abs(observed - start) >= limit:
                raise HomingError(limit_message)

            # **毎回そのときの実測位置へアンカーし直す。** 指令の積算で組むと、
            # 追従が遅れているあいだ指令だけが先行し続け、機構には常に大きな偏差が
            # 掛かったままになる (位置制御ループは電流上限まで使って押す)
            commanded = observed + direction * homing.step
            await handle.set_target_value(spec.to_commands(commanded))

            hit = await self._wait_step(
                spec, handle, homing, latches, commanded, want_active=want_active
            )

            previous = observed
            observed = self._observe(spec, handle)

            if hit:
                # **その場で止める。** 最後に送った「実測 + step」の指令はまだ生きて
                # おり、原点確定 (`capture_origin` の disable) が届くまでモータは
                # スイッチを越えた先へ向かい続ける。検出位置を目標に送り直せば、
                # 行き過ぎは「検出の遅れ」のぶんだけに縮む。
                # 離脱でも同じ 1 本を通す —— 向きごとに書き分けると片方だけ直せる形が
                # 残る (この関数を 1 本にしてある理由そのもの)
                await handle.set_target_value(spec.to_commands(observed))
                return observed

            # 指令を実測へ再アンカーしている以上、引っかかった機構は「指令しても
            # 進まない」形でしか現れない。実測の移動量で数える上限だけでは
            # 永久に降りられないので、進まないことそのものを失敗として扱う
            progress = _progress_threshold(homing)
            stalled = 0 if abs(observed - previous) >= progress else stalled + 1
            if stalled >= _STALL_LIMIT:
                raise HomingError(
                    f"軸 '{spec.name}' が指令しても動きません"
                    f" ({_STALL_LIMIT} 歩連続で {progress}{spec.unit} 進まなかった)。"
                    " 機構の引っかかり・探索方向・モータの励磁を確認してください"
                )

    def _check_preconditions(self, spec: AxisSpec, homing: HomingSpec) -> None:
        """**1 歩も動かす前に**、止められない探索になっていないかを確かめる。"""
        if spec.command_mode is not ControlMode.POSITION:
            # 位置定数の読み込みが既に拒否しているが、ここでも降りる。到達も現在位置も
            # 観測できない軸で「当たるまで少しずつ動かす」は成立しない
            raise HomingError(
                f"軸 '{spec.name}' は位置指令ではないため零点確定できません"
                f" (command_mode={spec.command_mode.value})"
            )

        if not self._origin_capturable(spec.name):
            raise HomingError(
                f"軸 '{spec.name}' の原点を確定する手段がありません。"
                " 零点確定を実行できないため探索を開始しません"
            )

        # **全センサを見る。1 本でも途絶していたら 1 歩も動かさない。**
        # 先頭だけを見る実装では、左右にスイッチが 1 本ずつ付く軸で「探索は当たるが
        # 整列段だけが押しても反応しない側を align_distance いっぱいまで進める」
        # という、最も機構に近い場所での失敗に変わる
        stale_sensors = [name for name in homing.sensor_names if self._sensor_is_stale(name)]
        if stale_sensors:
            label = " / ".join(f"'{name}'" for name in stale_sensors)
            raise HomingError(
                f"軸 '{spec.name}' の原点センサ {label} が応答していません"
                " (配線・基板の電源を確認してください)"
            )

        stale = [name for name in spec.motor_names if self._motor_is_stale(name)]
        if stale:
            # 未受信の 0.0 を現在位置と信じると、1 歩目が原点近傍への
            # ジャンプになり、その移動は探索距離の上限に掛からない
            raise HomingError(
                f"軸 '{spec.name}' の現在位置を読めません"
                f" (モータ {', '.join(stale)} のフィードバックが途絶しています)"
            )

    async def _wait_step(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        latches: _SensorContacts,
        commanded: float,
        *,
        want_active: bool,
    ) -> bool:
        """1 歩ぶんの追従を待つ。待っている間にセンサが `want_active` になったら True。

        追従を待たずに次の指令を出すと、指令だけが `step` ずつ進んで実位置から
        離れ続ける (`settle_s` は 50ms 程度なので、機構の応答より速い)。
        待ち切れなくても失敗にはしない —— 進まないことは呼び出し側の停滞判定が
        まとめて拾う (同じ事象に 2 つの判定を置くと、片方だけ直せてしまう)。

        **待っている間も見る**のは探索と離脱で同じ理由による —— 1 歩の移動中に
        センサの状態が変わるので、歩き終えてからしか見ないと、変化した位置ではなく
        その歩の終点が原点になる (step ぶん余計に行き過ぎる)。
        **見る対象は探索と離脱で違う** —— `_sensor_reached` を参照。
        """
        # **`spec.tolerance` を流用してはならない。** あちらは「軸が目標位置へ
        # 到達したか」の許容差で、ここが問うのは「1 歩ぶんの指令に追従したか」という
        # 別の量である。指令は実測 + step で組むので、**1 歩も動いていないときの
        # 実測と指令の差はちょうど `step`** —— `step <= tolerance` の軸
        # (rotate は step も tolerance も 2.0deg) では動く前に必ず追従完了になり、
        # 待ちが丸ごと消える。そのとき呼び出し側の停滞判定は「待ったのに進まなかった」
        # ではなく「待っていないので進んでいない」を数えるので、**正常に動いている
        # 機構が 0.3 秒で HomingError になる** (実機で発生)。
        reached = _progress_threshold(homing)
        for _ in range(_FOLLOW_ATTEMPTS):
            await self._sleep(homing.settle_s)
            if self._sensor_reached(homing, latches, want_active=want_active):
                return True
            if abs(self._observe(spec, handle) - commanded) <= reached:
                return False
        return False

    def _sensor_reached(
        self, homing: HomingSpec, latches: _SensorContacts, *, want_active: bool
    ) -> bool:
        """センサが目的の状態になったか。**探索と離脱で見るものが違う。**

        探索 (`want_active=True`) は**接触回数の増加**を見る —— 「探索を始めてから
        一度でも ON になったか」。リミットスイッチの ON 区間が `step` より狭いと、
        指令 1 回で区間を跨いでしまい、`settle_s` 後の観測時にはもう OFF になっている
        (`rotate` は step 2.0deg を約 18ms で通過する)。「今 ON か」を 50ms ごとに
        見る方式では 100Hz で届いている接触を原理的に取りこぼし、**そのまま
        スイッチを越えて回り続ける**。越えた先は機構の破損側である。

        離脱 (`want_active=False`) は**現在値**を見る。対称に見えるが、揃えては
        ならない —— 離脱に同じ数え方 (「一度でも OFF になったか」) を持ち込むと、
        接点のチャタリングで OFF が 1 回混じっただけで離脱完了と読み、**まだ ON 区間の
        中にいるのに探索を始めて、区間内のどこかを原点にする**。取りこぼしの向きも
        非対称で、探索の取りこぼしは機構の破損側へ進み続けるのに対し、離脱の
        取りこぼしは「余計に離れる」だけで、次の探索がそのぶんを寄せ直す。

        **センサが複数あっても非対称はそのまま。** 探索は「いずれか 1 本が
        ラッチした」で止まり (残りは整列段が寄せる)、離脱は「全センサが OFF」まで
        続ける —— 1 本でも触れたまま探索を始めると、その 1 本は 1 歩目のラッチで
        必ず到達と読まれ、探索開始位置がそのまま原点になる。
        """
        if want_active:
            return latches.any_latched()
        return not any(self._sensor_active(name) for name in homing.sensor_names)

    def _observe(self, spec: AxisSpec, handle: AxisHandle) -> float:
        """実測の軸位置 (全モータの平均)。読めなければ探索そのものを止める。"""
        try:
            return handle.observed_value()
        except HomingError:
            raise
        except Exception as exc:
            raise HomingError(f"軸 '{spec.name}' の現在位置を読めません ({exc})") from exc

    def _observe_each(self, spec: AxisSpec, handle: AxisHandle) -> dict[str, float]:
        """モータ名 → そのモータ単独の実測位置。**整列段だけが使う。**

        軸平均 (`_observe`) は左右がずれていればどちらか一方が必ず誤りなので、
        ずれそのものを見る整列段では使えない (`AxisHandle.observed_values`)。
        """
        try:
            return handle.observed_values()
        except HomingError:
            raise
        except Exception as exc:
            raise HomingError(f"軸 '{spec.name}' の現在位置を読めません ({exc})") from exc
