"""左右直結ペアの単位換算と「ずれているか」の判定の単一情報源。

``y_axis`` (M3508 2 台) と ``rotate`` (EDULITE 2 台) は機構的に直結しており、
位置がずれたまま駆動すると押し合いになってその場で機構が壊れる。この機体は同じ
``sync_tolerance`` (位置定数 yaml の 1 つの値) を 3 層から参照して守っている。

判定と換算をこのモジュールに閉じ込めるのは、層ごとに別実装を持つと片方だけを
直したときに気付けないため。特に逆回転ペアは ``scale`` の符号で向きを表すので、
逆換算を書き写した箇所が 1 つでも符号を落とすと「ずれていないのに止まる」か
「ずれているのに止まらない」のどちらかになる。

3 層は同じ判定の重複ではなく役割分担であり、debounce とラッチの有無が層ごとに
違うのは意図的である:

1. ``sequence.engine.move_to`` (``AxisHandle.sync_violation``)
   move_to 完了時に 1 回 / debounce なし / ラッチなし / ``AxisSyncError`` で
   シーケンスを止める。静止して到達判定を満たした後の 1 サンプルしか見ない。
   誤発報してもシーケンスが止まるだけで機構は動かないので debounce は要らず、
   停止そのものが次のステップを塞ぐのでラッチも要らない。
2. ``control.sync_guard.SyncGuard`` (``_check_deviation``。位置制御ループが毎周期呼ぶ)
   200Hz / debounce なし / ラッチあり / グループ全員を電流 0。
   「壊れる前に力を抜く」局所保護なので 1 周期でも早く落とす方が安全側で、
   誤発報の代償は電流 0 だけ。復帰は人間がずれを直したという宣言に限るため
   ラッチする。
3. ``control.sync_monitor`` (``_check_group``)
   50Hz / debounce 2 サンプル / ラッチあり / 全体緊急停止。
   試合を止める全体保護なので CAN の取りこぼし 1 回では発報させない。
   2 サンプル (=40ms) 待っても機構破損には間に合う。

このモジュールは ``lib.sequence`` にも ``lib.control`` にも依存しない (両者が
ここへ依存する向き)。制御層をシーケンス層に依存させないために型を分けた結果、
符号付き ``scale`` の逆換算が 2 箇所へコピーされていた経緯があるため、
今後もここから上位モジュールを import してはならない。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["MotorSpec", "SyncGroup"]


@dataclass(frozen=True)
class MotorSpec:
    """論理軸を構成する 1 台のモータの単位換算。

    逆回転で同一動作をするペアは ``scale`` の符号で表す。専用の invert フラグを
    設けないのは、単位換算と回転方向が 2 箇所に分かれると片方だけ直したときに
    気付けないため。
    """

    name: str
    scale: float
    offset: float

    def to_command(self, value: float) -> float:
        """人間の単位をモータの指令単位へ換算する。"""
        return value * self.scale + self.offset

    def to_value(self, command: float) -> float:
        """指令値・フィードバックを人間の単位へ戻す。"""
        return (command - self.offset) / self.scale

    def to_tolerance(self, tolerance: float) -> float:
        """人間の単位の許容幅をこのモータの指令単位へ換算する。

        許容差は幅であって向きを持たないため、``scale`` が負でも正の幅になるようにする。
        符号が残ると比較が常に成立し、逆回転側のモータだけ到達判定が素通りする。
        """
        return abs(tolerance * self.scale)


@dataclass(frozen=True)
class SyncGroup:
    """機構的に直結し、位置が揃っていなければならないモータの組。"""

    name: str
    members: tuple[MotorSpec, ...]
    tolerance: float
    #: 同期補正の比例ゲイン。単位は位置制御 PID の ``kp`` と同じ (指令単位あたりの
    #: 操作量) で、0.0 なら補正を一切出さない (従来どおり各モータが独立に動く)
    sync_kp: float = 0.0
    #: 補正項 1 つあたりの上限 (操作量の単位)。``sync_kp`` を入れるなら必須
    sync_limit: float | None = None

    def __post_init__(self) -> None:
        """補正を入れるなら歯止めも必ず持たせる。

        押し合いは左右直結の機構をその場で壊すので、``sync_limit`` は
        ``homing.search_distance`` と同格の「唯一の無人の歯止め」である。
        ゲインだけを書けてしまうと、打ち間違えた 1 桁がそのまま押し合いの
        フルスケールになる。config 側 (``lib/sequence/positions.py``) も同じ対を
        検証するが、こちらは yaml を経由せずに組み立てた場合も塞ぐ。

        負のゲインは正帰還であり、ずれを縮めるどころか発散させる。
        """
        if self.sync_kp < 0.0:
            raise ValueError(
                f"同期グループ '{self.name}' の sync_kp が負です ({self.sync_kp}): "
                "正帰還になりずれが発散します"
            )
        if self.sync_limit is not None and self.sync_limit < 0.0:
            raise ValueError(
                f"同期グループ '{self.name}' の sync_limit が負です ({self.sync_limit})"
            )
        if self.sync_kp != 0.0 and self.sync_limit is None:
            raise ValueError(
                f"同期グループ '{self.name}' に sync_kp があるのに sync_limit がありません "
                "(押し合いの歯止めが無くなります)"
            )

    def deviation(self, positions: Mapping[str, float]) -> float | None:
        """人間の単位へ逆換算した位置の max - min。比較対象が 2 個未満なら None。

        逆回転ペアでは指令単位のまま引き算しても意味を持たない (符号が逆)。
        人間の単位へ戻してから比較することで「同じ動作をしているか」を直接見る。
        """
        values = [
            member.to_value(positions[member.name])
            for member in self.members
            if member.name in positions
        ]
        if len(values) < 2:
            return None
        return max(values) - min(values)

    def violation(self, positions: Mapping[str, float]) -> float | None:
        """許容差を超えていれば偏差を、超えていなければ None を返す。

        3 層すべてが「ずれているか」をこの 1 メソッドで決める。層ごとに違うのは
        判定の頻度と、超過を検出した後の扱い (debounce / ラッチ / 効果) だけで、
        境界そのものが層ごとにずれてはならない。
        比較対象が揃わない (途絶・未受信) 場合は超過とみなさない。判定できない
        ものを異常として扱うと、起動直後のフィードバック未受信で緊急停止する。
        """
        deviation = self.deviation(positions)
        if deviation is None or deviation <= self.tolerance:
            return None
        return deviation

    def corrections(self, positions: Mapping[str, float]) -> dict[str, float]:
        """各メンバへ加える同期補正量を、そのメンバの**指令単位**で返す。

        3 層の保護 (``violation``) は「ずれたら止める」しかできない。位置制御は
        モータごとに独立した PID なので、左右で負荷や摩擦が違えば過渡の追従差は
        原理的に残り、縮める力はどこからも働かない。ここが返すのはその欠けている
        力 —— 「グループ平均へ引き戻す向きの操作量」である。

        人間の単位で平均からのずれを出し、``scale`` で各メンバの指令単位へ戻して
        から ``sync_kp`` を掛ける。この順序により **``sync_kp`` は位置制御 PID の
        ``kp`` と同じ単位**になり、「kp の何割」で調整できる (単位が違うと実機での
        詰め方を手順として書けない)。

        **逆回転ペアでは 2 台の補正が同符号・同じ大きさになる。** 人間の単位では
        ``e_l = -e_r`` だが ``scale_l = -scale_r`` なので、指令単位へ戻すと
        ``e_l * scale_l == e_r * scale_r`` が成立する。つまりこの補正は軸としての
        運動を一切動かさず、左右の内部のずれだけを縮める。**この方式が成立する
        根拠そのもの**なので、``scale`` の符号を落としてはならない (落とすと補正が
        逆符号になり、軸ごと押し動かしながらずれは縮まらない)。

        平均を基準にするのは 3 台以上でも同じ式で成立させるため。補正の総和は
        定義から 0 になるので、メンバが増えても軸の運動に対して中立である。

        1 台でも位置が欠けていれば空を返す。欠けたメンバを外して平均を取ると、
        残った側だけが「ずれている」と判定されて実在しない補正が出る。
        """
        if self.sync_kp == 0.0:
            return {}
        values = {
            member.name: member.to_value(positions[member.name])
            for member in self.members
            if member.name in positions
        }
        if len(values) != len(self.members) or not values:
            return {}

        mean = sum(values.values()) / len(values)
        corrections: dict[str, float] = {}
        for member in self.members:
            command_error = (mean - values[member.name]) * member.scale
            correction = self.sync_kp * command_error
            if self.sync_limit is not None:
                correction = max(-self.sync_limit, min(self.sync_limit, correction))
            corrections[member.name] = correction
        return corrections
