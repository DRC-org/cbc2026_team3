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
5. **離脱の距離上限** (`homing.release_distance`、既定は `step` の倍数) — 触れた
   状態から始めたときに一度センサの外まで離れるが、その離脱にも上限が要る。
   接点の固着と**極性の取り違え** (ファーム側 `sensorActiveLow` の設定ミス) はどちらも
   「いつまでも OFF にならない」形でしか現れない。**探索距離を流用してはならない**
   (あちらは実ストローク相当まで伸びる値なので、反対側の機構端まで走り抜ける)。
   **既定の `step` 倍数に頼らないこと** —— 本来はスイッチの ON 区間の広さで決まる値で、
   刻み幅とは無関係である。精度のために `step` を詰めると離脱の許容も一緒に縮み、
   **精度を上げるほどスイッチから離れられなくなる**
6. **粗探索が ON 区間を跨ぎ切ったことの検出** (`homing.coarse_step` を書いた軸のみ)
   —— 粗い 1 歩が ON 区間より広いと、当てた直後にもう区間の外 (= 機構端の側) に
   居る。そこから離脱・寄せ直しをすると探索方向へ走り抜けるので、**寄せ直しへ
   入る前に「今 ON か」を問い直して降りる**
7. **緊急停止** — 目標値を送る経路 (`AxisHandle`) が既にインターロックを通る

**精度と時間は 1 つの `step` では両立しない。二段探索がその答えである。**
原点のばらつきは刻み幅そのものなので、0.1mm の精度を要求する軸では `step` を
0.1mm まで詰めることになる。ところが実ストローク 750mm の `sub_y_axis` を
0.1mm 刻みで探索すると 8000 歩 ≒ 8 分かかり、試合前の点検に入らない。
`homing.coarse_step` を書いた軸は **粗い刻みで当てる → 離脱 → `step` で寄せ直す**
の 2 段になり、確定位置の粒度は最後の段の `step` のまま所要時間だけが縮む。
**寄せ直しの前に必ず離脱する** —— 粗い段はスイッチの ON 区間のどこで止まるか
分からないので、そこを原点にすると粗い刻みぶんのばらつきが座標へ焼き付く
(離脱段が存在する理由そのもの)。**書かなかった軸は従来どおりの単段探索**で、
`rotate` (step 2.0deg で 90 歩) のように 1 つの刻みで足りる軸に二段は要らない。

**離脱は常に粗い側の刻み (`coarse_step or step`) で行う。** 離脱の役目は
「ON 区間の外へ出る」ことだけで、原点の粒度を決めるのは最後の寄せ直しである。
細かい刻みで出ると、区間の奥から出るのに `区間幅 / step` 歩かかる —— 寄せ直しへ
回すはずの歩数を離脱で使うだけなので、合計は縮まない。粗い刻みで出れば、
**粗探索が ON 区間を跨ぎ切ってしまった場合にも区間の手前まで戻れる**
(細かい刻みで出ると跨いだ先から 1 歩ぶんしか戻らず、寄せ直しは区間から
離れる向きへ走り出す)。

**探索の到達判定はラッチで見る。「今 ON か」では取りこぼす。** ON 区間が `step` より
狭い機構では、指令 1 回で区間を跨いでしまい `settle_s` 後の観測ではもう OFF になって
いる (`rotate` は step 2.0deg を約 18ms で通過する)。センサの FEEDBACK は 100Hz で
届いているので、受信のたびにラッチしておけば 1 通も取りこぼさない
(`GenericDriver.consume_sensor_latch`)。**離脱は現在値のまま**という非対称は意図した
もので、理由は `HomingRunner._sensor_reached` に書いてある。

**触れた状態から始めたら、一度離れてから寄せ直す。** リミットスイッチの ON 区間には
幅があるので、触れたその場を原点にすると「区間のどこで探索を始めたか」がそのまま
原点のばらつきになる。症状は「原点合わせをしたのに位置がずれる」だけで、始めた位置は
毎回違うので再現もしない。区間の外まで離してから通常の探索へ渡せば、確定位置は
探索の `step` 粒度に収まる。**離脱は探索と逆向き**なので、機構端で始まったときに
押し込まない性質はそのまま保たれる。

**原点確定はグループ単位でしか行わない。** 左右直結ペアを別々の時刻に確定すると、
その間に片方が動いたぶんだけ消えないオフセットが残り、正常な動作でも即座に
偏差超過で止まる (`M3508PositionLoop.set_group_origin_here` と同じ理由)。

**原点を確定できない軸では 1 歩も動かさない。** 確定手段の有無は探索の前に問う
(`origin_capturable`)。センサまで押し込んでから「確定できません」で降りると、
機構を動かした意味が無いまま姿勢だけが変わる。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

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
#: `homing.release_distance` を書かなかった軸の既定値を step から作るために使う。
#: **`search_distance` を流用してはならない** —— あちらは実ストローク相当まで
#: 伸びる値で、離脱の上限に使うと反対側の機構端まで走り抜ける。
_RELEASE_STEP_LIMIT = 20


def _release_limit(homing: HomingSpec) -> float:
    """離脱に許す距離 [軸の unit]。

    **本来これはスイッチの ON 区間の広さで決まる値で、刻み幅とは無関係である。**
    `step` の倍数を既定にしてあるのは既存の軸を変えないためだけで、精度のために
    `step` を詰める軸では必ず `release_distance` を明示すること —— 詰めた瞬間に
    離脱の許容も一緒に縮み、**精度を上げるほどスイッチから離れられなくなる**。
    実際に sub_y_axis で step 0.5 -> 0.1 にした途端、許容が 10mm から 2mm へ落ちて
    ON 区間 (実測 2mm 以上) を 0.1mm ぶん抜けきれずに失敗した (2026-09-09)。

    **二段探索の軸ではこの既定は使われない** —— 離脱が粗い刻みで動くのに既定は
    `step` から作られて桁が合わないので、`coarse_step` を書いた軸には
    `release_distance` を必須にしてある (`HomingSpec.__post_init__`)。
    """
    if homing.release_distance is not None:
        return homing.release_distance
    return homing.step * _RELEASE_STEP_LIMIT


def _not_reached_message(spec: AxisSpec, homing: HomingSpec, limit: float) -> str:
    """探索距離を使い切った理由。**二段探索では段ごとに残りが違う**ので引数で受ける。"""
    return (
        f"軸 '{spec.name}' が {limit}{spec.unit} 動かしても"
        f" 原点センサ '{homing.sensor}' に到達しませんでした"
        " (探索方向・機構の引っかかり・センサの配線を確認してください)"
    )


class HomingError(RuntimeError):
    """零点を確定できなかった。シーケンスを止めて操縦者に知らせる。

    「原点がずれたまま走る」より「動かないまま止まる」ほうが安全なので、
    黙って続行してはならない。
    """


SensorActive = Callable[[str], bool]
#: センサ名 → 前回読んでから一度でも接触したか。**読むと消える**
#: (`GenericDriver.consume_sensor_latch`)。読み手が複数いると片方が相手のぶんまで
#: 消すので、呼び手は `HomingRunner` 1 つに限る。
SensorLatched = Callable[[str], bool]
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


def _progress_threshold(step: float) -> float:
    """「1 歩ぶん進んだ」と見なす移動量 [軸の unit]。

    追従待ちと停滞判定が**同じ 1 つの基準**を見るために関数へ切り出してある。
    片方に定数を書き写すと、もう片方だけを直せる形が残る。

    **受け取るのは `HomingSpec` ではなく「その段の刻み」である。** 二段探索では
    粗い段と細い段で刻みが 1 桁違うので、粗い段の基準を細い段へ持ち込むと
    「1 歩ぶん動いたのに進まなかった」と数え、**動いている機構を数歩で止める**。
    """
    return step * _PROGRESS_FRACTION


class HomingRunner:
    """1 軸ぶんのホーミングを実行する。

    センサの読み取りと原点確定の実体は注入する。ここが `CANManager` や
    `M3508PositionLoop` を直接掴むと、CAN を立てないと 1 行も検証できなくなる。
    """

    def __init__(
        self,
        *,
        sensor_active: SensorActive,
        sensor_latched: SensorLatched,
        sensor_is_stale: SensorStale,
        motor_is_stale: MotorStale,
        origin_capturable: OriginCapturable,
        capture_origin: CaptureOrigin,
        sleep: SleepFunc = asyncio.sleep,
    ) -> None:
        """
        Args:
            sensor_active: センサ名 → **今**接触しているか
                (`GenericDriver.sensor_active`)。離脱の判定と、探索前の
                「既に触れているか」がこちらを見る
            sensor_latched: センサ名 → **前回読んでから一度でも**接触したか。
                読むと消える (`GenericDriver.consume_sensor_latch`)。
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
            sleep: 1 ステップごとの待ち (テストで差し替える)
        """
        self._sensor_active = sensor_active
        self._sensor_latched = sensor_latched
        self._sensor_is_stale = sensor_is_stale
        self._motor_is_stale = motor_is_stale
        self._origin_capturable = origin_capturable
        self._capture_origin = capture_origin
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
            **探索で**実際に動いた距離 [軸の unit]。ログと検証用。二段探索では
            粗い段と細い段の合計 (どちらも `search_distance` を食う)。
            離脱 (下記) のぶんは含めない —— 原点の精度を決めているのは
            「どこから寄せて当たったか」であって、その前に離れた距離ではない。

        Raises:
            HomingError: 原点を確定する手段が無い / センサまたは軸のフィードバックが
                途絶している / 実測が進まない / 探索距離を超えても当たらなかった /
                離脱してもセンサが OFF にならない / 粗探索が ON 区間を跨ぎ切った
        """
        homing = spec.homing
        if homing is None:
            raise HomingError(f"軸 '{spec.name}' に homing 設定がありません")

        self._check_preconditions(spec, homing)

        # ここが問うのは「**今**触れているか」なのでラッチではない。ラッチで問うと、
        # 前回の零点確定や手動操縦でスイッチを跨いだ痕跡だけで離脱段へ入り、
        # 触れてもいない位置から _RELEASE_STEP_LIMIT 歩ぶん離れる向きへ動き出す
        if self._sensor_active(homing.sensor):
            logger.info("[homing] %s: 既にセンサに触れているため一度離れて寄せ直す", spec.name)
            await self._release(spec, handle, homing)

        # **探索距離の上限は 2 段の合計に掛ける。** 段ごとに `search_distance` を
        # 与え直すと、唯一の無人の歯止めが黙って 2 倍になる
        travelled = 0.0

        if homing.coarse_step is not None:
            start = self._observe(spec, handle)
            observed = await self._seek(
                spec,
                handle,
                homing,
                step=homing.coarse_step,
                direction=homing.direction,
                want_active=True,
                limit=homing.search_distance,
                limit_message=_not_reached_message(spec, homing, homing.search_distance),
            )
            travelled += abs(observed - start)
            logger.info(
                "[homing] %s: 粗探索 (%g%s 刻み) で %.2f%s 動かして接触。"
                "離脱して %g%s 刻みで寄せ直す",
                spec.name,
                homing.coarse_step,
                spec.unit,
                travelled,
                spec.unit,
                homing.step,
                spec.unit,
            )
            # **跨ぎ切っていたらここで降りる。** 粗い 1 歩が ON 区間より広いと、
            # 当てた時点でもう区間の外 (機構端の側) に居る。そのまま離脱・寄せ直しを
            # すると探索方向へ走り抜けるので、動かす前に問い直す
            if not self._sensor_active(homing.sensor):
                raise HomingError(
                    f"軸 '{spec.name}' は粗探索 ({homing.coarse_step}{spec.unit} 刻み) の"
                    f" 1 歩で原点センサ '{homing.sensor}' の ON 区間を跨ぎ切りました"
                    " (当てた直後にもう OFF)。このまま寄せ直すと探索方向へ走り抜けるので"
                    " 止めます。homing.coarse_step を ON 区間の実測より狭くしてください"
                )
            await self._release(spec, handle, homing)

        start = self._observe(spec, handle)
        remaining = homing.search_distance - travelled
        observed = await self._seek(
            spec,
            handle,
            homing,
            step=homing.step,
            direction=homing.direction,
            want_active=True,
            limit=remaining,
            limit_message=_not_reached_message(spec, homing, remaining),
        )
        travelled += abs(observed - start)

        logger.info("[homing] %s: %.2f%s 動かして原点に到達", spec.name, travelled, spec.unit)
        await self._capture_origin(spec.name)
        return travelled

    async def _release(self, spec: AxisSpec, handle: AxisHandle, homing: HomingSpec) -> float:
        """センサの ON 区間の外まで**探索と逆向きに**離れる。

        **触れた状態のまま確定してはならない。** リミットスイッチの ON 区間には
        幅があるので、その場を原点にすると「区間のどこで探索を始めたか」がそのまま
        原点のばらつきになる (区間幅ぶん = step の何倍にもなる)。いったん区間の外まで
        離れてから寄せ直せば、確定位置は探索の step 粒度に収まり、どこから始めても
        同じ場所が原点になる。**離脱は探索と逆向き**なので押し込む方向へは動かない
        (機構端で始まったときに壊さない、という元の性質は保たれる)。

        **刻みは常に粗い側 (`coarse_step or step`)。** 原点の粒度を決めるのは最後の
        寄せ直しなので、離脱を細かくしても得るものは無く、区間の奥から出るのに
        `区間幅 / step` 歩を使うだけで合計は縮まない。粗い刻みで出れば、粗探索が
        ON 区間を跨ぎ切ってしまったときにも区間の手前まで戻れる (細かい刻みだと
        跨いだ先から 1 歩しか戻らず、寄せ直しが区間から離れる向きへ走り出す)。
        上限は段に依らず `homing.release_distance` (区間の広さで決まる 1 つの量)。
        """
        limit = _release_limit(homing)
        return await self._seek(
            spec,
            handle,
            homing,
            step=homing.coarse_step or homing.step,
            direction=-homing.direction,
            want_active=False,
            limit=limit,
            limit_message=(
                f"軸 '{spec.name}' を原点センサ '{homing.sensor}' から離せませんでした"
                f" ({limit}{spec.unit} 動かしても OFF に"
                " ならない)。**センサの極性が逆だとどこへ動かしても ON のまま**に"
                " なるので、ファーム側の極性設定 (sensorActiveLow) を"
                "接点の固着・配線の短絡と併せて確認してください。"
                " 極性が正しいなら ON 区間がこの距離より広いので"
                " homing.release_distance を実測へ広げてください"
            ),
        )

    async def _seek(
        self,
        spec: AxisSpec,
        handle: AxisHandle,
        homing: HomingSpec,
        *,
        step: float,
        direction: float,
        want_active: bool,
        limit: float,
        limit_message: str,
    ) -> float:
        """センサが `want_active` になるまで `direction` 方向へ `step` ずつ動かす。

        **探索と離脱の両方がこの 1 本を通る。** 歯止め (移動量の上限・停滞判定) を
        向きごとに書き分けると、片方だけ直せてしまう —— 症状は「探索は止まるのに
        離脱は永久に動き続ける」で、離脱は普段踏まない経路なので気付けない。
        二段探索の粗い段・細い段も同じここを通る (`step` と `limit` だけが違う)。

        Args:
            step: 1 歩の移動量。**停滞判定も追従待ちもこの値だけを基準にする**
                —— 粗い段の基準を細い段へ持ち込むと、動いている機構を数歩で止める
            direction: 進む向き。探索は `homing.direction`、離脱はその反対
            want_active: この状態になったら到達。探索は True、離脱は False
            limit: 実測の移動量の上限。超えたら `limit_message` で降りる
        """
        if want_active:
            # **探索を始める前に溜まったラッチを捨てる。** 離脱段のあいだ触れていた
            # ぶんや、前回の零点確定・手動操縦でスイッチを跨いだぶんが残っていると、
            # 1 歩目の観測でいきなり到達と読み、探索開始位置が原点になる
            self._sensor_latched(homing.sensor)

        start = self._observe(spec, handle)
        observed = start
        stalled = 0
        while True:
            if abs(observed - start) >= limit:
                raise HomingError(limit_message)

            # **毎回そのときの実測位置へアンカーし直す。** 指令の積算で組むと、
            # 追従が遅れているあいだ指令だけが先行し続け、機構には常に大きな偏差が
            # 掛かったままになる (位置制御ループは電流上限まで使って押す)
            commanded = observed + direction * step
            await handle.set_target_value(spec.to_commands(commanded))

            hit = await self._wait_step(
                spec, handle, homing, commanded, step=step, want_active=want_active
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
            progress = _progress_threshold(step)
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

        if self._sensor_is_stale(homing.sensor):
            raise HomingError(
                f"軸 '{spec.name}' の原点センサ '{homing.sensor}' が応答していません"
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
        commanded: float,
        *,
        step: float,
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
        reached = _progress_threshold(step)
        for _ in range(_FOLLOW_ATTEMPTS):
            await self._sleep(homing.settle_s)
            if self._sensor_reached(homing.sensor, want_active=want_active):
                return True
            if abs(self._observe(spec, handle) - commanded) <= reached:
                return False
        return False

    def _sensor_reached(self, sensor: str, *, want_active: bool) -> bool:
        """センサが目的の状態になったか。**探索と離脱で見るものが違う。**

        探索 (`want_active=True`) は**ラッチ**を見る —— 「前回読んでから一度でも
        ON になったか」。リミットスイッチの ON 区間が `step` より狭いと、指令 1 回で
        区間を跨いでしまい、`settle_s` 後の観測時にはもう OFF になっている
        (`rotate` は step 2.0deg を約 18ms で通過する)。「今 ON か」を 50ms ごとに
        見る方式では 100Hz で届いている接触を原理的に取りこぼし、**そのまま
        スイッチを越えて回り続ける**。越えた先は機構の破損側である。

        離脱 (`want_active=False`) は**現在値**を見る。対称に見えるが、揃えては
        ならない —— 離脱に同じラッチ方式 (「一度でも OFF になったか」) を持ち込むと、
        接点のチャタリングで OFF が 1 回混じっただけで離脱完了と読み、**まだ ON 区間の
        中にいるのに探索を始めて、区間内のどこかを原点にする**。取りこぼしの向きも
        非対称で、探索の取りこぼしは機構の破損側へ進み続けるのに対し、離脱の
        取りこぼしは「余計に離れる」だけで、次の探索がそのぶんを寄せ直す。
        """
        if want_active:
            return self._sensor_latched(sensor)
        return self._sensor_active(sensor) is False

    def _observe(self, spec: AxisSpec, handle: AxisHandle) -> float:
        """実測の軸位置。読めなければ探索そのものを止める。"""
        try:
            return handle.observed_value()
        except HomingError:
            raise
        except Exception as exc:
            raise HomingError(f"軸 '{spec.name}' の現在位置を読めません ({exc})") from exc
