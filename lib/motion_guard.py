"""軸へ指令を出してよいかの判断。**判断だけを持ち、送信も状態も持たない。**

`lib/axis_sync.py` と同じ最下位層で、上位モジュールを import してはならない。
判断がここに閉じているのは、手動操縦・シーケンス・零点確定の 3 経路が同じ
`AxisHandle` を通るのに対し、**層ごとに書き写すと片方だけ緩んだ状態が作れる**ため
(左右ペアの偏差判定を `SyncGroup.violation()` へ一本化しているのと同じ理由)。

守るのは 4 つで、どれも 2026-09-09 に実機で踏んだ事故に対応する:

1. **可動端のインターロック** —— リミットスイッチが押されている向きへは、それ以上
   指令を出さない。**逆向き (離れる向き) は必ず通す** —— 塞ぐと機構端に張り付いた
   軸を手動でも戻せなくなり、退避路としての手動操縦が成立しなくなる。零点確定の
   離脱段もこの向きを使う。**1 つの向きに複数本のスイッチを宣言でき、1 本でも
   押されていれば塞ぐ** (左右直結ペアは同じ端に 1 本ずつ持つ)
2. **1 指令の跳躍量** —— 実測位置から `max_step` を超えて離れた目標を拒む。
   スケールや固定小数点レンジの取り違えは「桁が変わった目標値」として現れるので、
   **比が分からなくてもここで止まる**。実機では `p_max` の食い違いで位置が 80 倍に
   読め、その値が保持目標として書かれて機構がスイッチを踏み越えた
3. **逆端への到達** —— 探索の向きを取り違えると、狙った端ではなく反対の端へ進む。
   1 の帰結として反対端のスイッチで止まるので、**押し込む前に止まって理由が出る**
4. **急なトルク** —— 何かに当たったことは、位置が進まないことより先にトルクに出る。
   `stall_torque` を超えたら止める

**センサの途絶 (STALE) をここで扱わないのは意図的**である。「押されていない」と
「読めていない」は別の事実で、後者を可動端の判断に混ぜると**配線が抜けたセンサが
「押されていない = 進んでよい」に化ける**。読めていないセンサは `active=None` として
渡され、インターロックは**押されているものと同じ扱い (進ませない)** にする ——
安全側は「止まる」であって「進む」ではない。
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

__all__ = [
    "GuardViolation",
    "LimitSpec",
    "MotionGuard",
    "MotionGuardSpec",
    "SensorSuspension",
]


def _sensor_names(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, Iterable):
        return tuple(str(name) for name in raw)
    raise TypeError(f"センサ名は文字列かその並び: {raw!r}")


def _label(names: Iterable[str]) -> str:
    return " / ".join(f"'{name}'" for name in names)


@dataclass(frozen=True)
class LimitSpec:
    """軸の両端に居るリミットセンサの名前。**向きごとに何本でも書ける。**

    **どちらの向きも省略できる。** 片端にしかスイッチが無い機構は普通にある
    (サブハンド前後は前端が原点で、後端は報告のみだった)。書かなかった側は
    インターロックが掛からないので、書き忘れは「守られていない端」として残る。
    そのため `axes.<軸>.homing.sensor` を書いた軸は、少なくともその 1 本が
    どちらの端かを宣言していることになる。

    **1 つの向きに複数本を書けるのは、左右直結ペアが同じ端に 1 本ずつ持つため**
    (`y_axis` の左右の原点スイッチ)。片方だけ宣言すると守りが半分になり、しかも
    どちらが落ちているかは機構が壊れるまで分からない。**1 本でも押されていれば
    その向きは塞ぐ** —— 軸は 1 つなので、片側が端に着いた時点でその向きへ進める
    余地はもう無い。
    """

    #: + 方向の端に居るセンサ名 (値が増える向きに進むと当たる)。1 本なら文字列でよい
    plus: tuple[str, ...] | str | None = ()
    #: - 方向の端に居るセンサ名
    minus: tuple[str, ...] | str | None = ()

    def __post_init__(self) -> None:
        # 1 本を文字列で書く既存の宣言をそのまま受ける。読み口を 2 つに分けると
        # 「1 本目しか見ない呼び出し側」が書けてしまう
        object.__setattr__(self, "plus", _sensor_names(self.plus))
        object.__setattr__(self, "minus", _sensor_names(self.minus))

    def sensors_for(self, delta: float) -> tuple[str, ...]:
        """`delta` の向きに進むとき、当たりうる端のセンサ名。**全部返す。**"""
        if delta > 0.0:
            return tuple(self.plus)
        if delta < 0.0:
            return tuple(self.minus)
        return ()


@dataclass(frozen=True)
class MotionGuardSpec:
    """1 軸ぶんの歯止めの宣言。**軸の機構的性質**なので位置定数と同居する。

    どの項目も省略できるが、**省略は「その守りが無い」ことを意味する**ので、
    機構が付いた軸には必ず書くこと。既定値で埋めないのは、埋めた値が効いている
    のか書き忘れなのかがコードから読めなくなるため (`HealthThresholds` や
    `homing.search_distance` と同じ方針)。
    """

    limits: LimitSpec | None = None
    #: 1 指令で実測位置から離れてよい最大量 [軸の unit]。None なら跳躍を見ない
    max_step: float | None = None
    #: これを超えたら止める絶対トルク [Nm]。None ならトルクを見ない
    stall_torque: float | None = None

    def __post_init__(self) -> None:
        if self.max_step is not None and self.max_step <= 0.0:
            raise ValueError(f"max_step は正の値: {self.max_step!r}")
        if self.stall_torque is not None and self.stall_torque <= 0.0:
            raise ValueError(f"stall_torque は正の値: {self.stall_torque!r}")


class SensorSuspension:
    """指定したセンサを、その間だけ「押されていない」として読ませる読み口の覆い。

    **要るのは零点確定の整列段ただ 1 つ。** 左右に 1 本ずつスイッチが付く軸
    (`y_axis`) では、片方が当たった後に**まだ当たっていない側のモータだけ**を端へ
    進める。軸としては端へ向かう向きなので、押された 1 本を見た歯止めが
    「その向きへは動かさない」と拒否し、**整列段は必ず失敗する** (指令の入口と
    50Hz の監視の両方で。実際に踏んだ)。進めているモータのスイッチはまだ
    押されていないので、機構から見れば進んでよい。

    **外すのはその軸の `homing.sensor_names` だけで、軸まるごとではない。**
    両端にスイッチがある軸で反対端まで外すと、探索の向きを取り違えたときに
    押し込む前で止まる経路が消える。逆に 1 本でも外し忘れると、上のとおり
    整列段が必ず失敗する。

    覆いを掛けるのは**指令の入口 (`AxisHandle`) と周期監視 (`LimitMonitor`) が
    読む口だけ**で、零点確定自身は生の読み口を使う —— 探索も離脱も「今 ON か」を
    見て進むので、覆った値を渡すと自分の目を塞ぐことになる。

    **覆いは歯止めが読む口すべてに掛かる。** `main()` は 1 つの `SensorSuspension` を
    両ハンドで共有し、覆った読み口を `MotorGroup` へ渡すので、整列段のあいだは
    **手動操縦 (`lib/manual.py`) の指令が通る歯止めも同じだけ緩む**。それでも緩みが
    露出しないのは、**整列段のあいだ当該軸に手動操縦の制御権が無い**ためで、根拠は
    覆いの側ではなく制御権の側にある:

    - 零点確定を走らせる 2 つの経路 (`homing_start` と動作確認) は、**どのロボットかが
      手動操縦モードだと開始できない** (`RobotServer._environment_deny`)
    - 走り出した後は `_busy_label()` が立つので、**手動操縦モードへの切り替えが拒まれ**
      (`RobotServer._set_operation_mode`)、**`manual_always` の軸すら拒まれる**
      (`RobotServer._allow_manual_in_sequence`)
    - そもそも覆う対象になる軸は到達判定を持つ位置制御軸なので `manual_always` を
      宣言できない (`AxisSpec._check_manual_always`)。半自動のまま動かす口が無い

    **この排他が消えたら覆いの適用範囲を絞る必要が出る**ので、両向きを
    `tests/test_server_homing.py::TestDenyGate` が固定している。

    **覆う読み口は現在値だけでは足りない。** 周期監視は現在値に加えて**接触 (OFF→ON)
    の累計** (`LimitMonitor._poll_contacts`) を見て、観測周期より狭い ON 区間を拾う。
    累計を覆わずに渡すと、整列段のあいだに数えた接触がその周期だけ「押されている」に
    化け、覆ったはずの軸の目標が実測位置へ書き直される (症状は整列段の最中の
    「移動中に可動端で停止」)。`wrap_count()` が同じ名前の集合に同じ覆いを掛ける。

    多重に掛かっても数で持つ (掛けた順に外れなくても早く素通りに戻らない)。
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._frozen_counts: dict[str, int] = {}

    @contextlib.contextmanager
    def suspend(self, names: Iterable[str]) -> Iterator[None]:
        held = tuple(names)
        for name in held:
            self._counts[name] = self._counts.get(name, 0) + 1
        try:
            yield
        finally:
            for name in held:
                remaining = self._counts.get(name, 1) - 1
                if remaining > 0:
                    self._counts[name] = remaining
                else:
                    self._counts.pop(name, None)
                    self._frozen_counts.pop(name, None)

    def is_suspended(self, name: str) -> bool:
        return self._counts.get(name, 0) > 0

    def wrap(self, read: Callable[[str], bool | None]) -> Callable[[str], bool | None]:
        """歯止めが読む口を覆う。**覆っている間だけ `False` を返す。**

        `None` (読めていない) へ倒さないのは、倒すと歯止めが「安全側 = 止まる」へ
        転んで整列段が失敗するため —— 覆う目的そのものが達成できない。
        """

        def guarded(name: str) -> bool | None:
            if self.is_suspended(name):
                return False
            return read(name)

        return guarded

    def wrap_count(self, read: Callable[[str], int | None]) -> Callable[[str], int | None]:
        """接触 (OFF→ON) の累計の読み口を覆う。**覆っている間は覆い始めの値で凍らせる。**

        `None` (カウンタを提供しないドライバ) へ倒さないのは、読み手が `None` の周期に
        基準値を進めずに見送るため —— 覆いを外した瞬間に、覆っているあいだに増えたぶんが
        まとめて接触として現れる。凍らせた値を返せば基準値は毎周期進む。

        **元の読み口が `None` を返すセンサはそのまま `None`。** 覆いは「カウンタがあるのに
        数えさせない」ものであって、カウンタの有無まで偽ると読み手の判断材料が変わる。

        凍らせる値は**覆う前に最後に読めた値**で、覆ってから最初に読んだ値ではない ——
        後者だと覆い始めから最初の読みまで (周期監視なら 20ms) に数えた接触が凍った値へ
        混ざり、読み手の基準値がその周期だけ跳ねて接触として立つ。
        """
        last_seen: dict[str, int] = {}

        def guarded(name: str) -> int | None:
            count = read(name)
            if count is None:
                return None
            if not self.is_suspended(name):
                last_seen[name] = count
                return count
            return self._frozen_counts.setdefault(name, last_seen.get(name, count))

        return guarded


class GuardViolation(RuntimeError):
    """歯止めに掛かって指令を出さなかった。

    **握り潰してはならない。** 掛かるのは「配線・スケール・向きのどれかが
    食い違っている」ときだけで、そのまま進めれば機構が壊れる。
    """


class MotionGuard:
    """指令を出す直前の判断。**状態を持たない。**

    センサの状態は呼び出しのたびに注入で受ける (`sensor_active`)。ここで保持すると
    「いつの時点のセンサか」が判断の中に隠れ、`AxisHandle` を作り直すたびに古い値が
    紛れ込む。三値 (`True` / `False` / `None`) をそのまま受けるのは、**`None`
    (読めていない) を `False` (押されていない) へ丸めない**ため。
    """

    def __init__(self, spec: MotionGuardSpec) -> None:
        self._spec = spec

    def check_command(
        self,
        *,
        axis: str,
        current: float,
        target: float,
        unit: str,
        sensor_active: object,
    ) -> None:
        """`current` から `target` へ動かしてよいか。駄目なら送出する。

        Args:
            axis: 軸名 (メッセージ用)
            current: 実測位置 [軸の unit]
            target: 出そうとしている目標 [軸の unit]
            unit: 人間の単位 (メッセージ用)
            sensor_active: 進む向きの端のセンサの状態を返す呼び出し可能オブジェクト
                (`Callable[[str], bool | None]`)。`None` は「読めていない」

        Raises:
            GuardViolation: 跳躍量を超えた / 進む向きの端が押されている
                (または読めていない)
        """
        delta = target - current
        self._check_jump(axis, delta, unit)
        self.check_limit(axis=axis, delta=delta, sensor_active=sensor_active)

    def _check_jump(self, axis: str, delta: float, unit: str) -> None:
        limit = self._spec.max_step
        if limit is None or abs(delta) <= limit:
            return
        raise GuardViolation(
            f"軸 '{axis}' へ 1 度に {abs(delta):.3g}{unit} 動かす指令が出ました"
            f" (上限 {limit}{unit})。**スケールか固定小数点レンジの取り違えを疑うこと** ——"
            " 位置が桁ごとずれて読めていると、保持目標がそのまま桁の違う位置を指し、"
            "機構が可動端まで走ります"
        )

    def check_limit(self, *, axis: str, delta: float, sensor_active: object) -> None:
        """`delta` の向きへ進んでよいか。駄目なら送出する。

        **指令を出す前 (`check_command`) と移動中 (`LimitMonitor`) の両方がここを呼ぶ。**
        監視側へ書き写すと、片方だけが `None` (読めていない) を素通りさせる状態が作れる。

        Raises:
            GuardViolation: 進む向きの端が押されている (または読めていない)
        """
        limits = self._spec.limits
        if limits is None or delta == 0.0:
            return
        sensors = limits.sensors_for(delta)
        if not sensors:
            return
        states = {name: sensor_active(name) for name in sensors}  # type: ignore[operator]

        # **読めていない側を先に言う。** 押されていることは機構の姿勢から読めるが、
        # 読めていないことは画面からしか読めない ——「端に着いているのだから当然」で
        # 片付けられると、死んだ 1 本が押された 1 本の陰に隠れたまま試合に入る
        unreadable = [name for name, state in states.items() if state is None]
        if unreadable:
            raise GuardViolation(
                f"軸 '{axis}' の可動端センサ {_label(unreadable)} が読めていないため、"
                "その向きへは動かしません (配線・基板の電源・デバイス ID を確認してください)。"
                "**読めていないことを「押されていない」と読み替えてはならない**ので、"
                "安全側に倒しています"
            )

        pressed = [name for name, state in states.items() if state is not False]
        if pressed:
            raise GuardViolation(
                f"軸 '{axis}' の可動端センサ {_label(pressed)} が押されているため、"
                "その向きへは動かしません。**離れる向きの指令は通ります**"
            )

    def check_torque(self, *, axis: str, torque: float | None) -> None:
        """フィードバックのトルクが急に立ったら止める。

        当たったことは位置が進まないことより先にトルクへ出るので、停滞判定より
        早く効く。**測れないモータ (`None`) では何もしない** —— 常に 0 を運ぶ値で
        判定すると「測ったように見える 0」がそのまま「異常なし」に化ける。

        Raises:
            GuardViolation: `stall_torque` を超えた
        """
        limit = self._spec.stall_torque
        if limit is None or torque is None or abs(torque) <= limit:
            return
        raise GuardViolation(
            f"軸 '{axis}' のトルクが {abs(torque):.3g}Nm まで立ちました"
            f" (上限 {limit}Nm)。機構が何かに当たっているか、可動端を越えています"
        )
