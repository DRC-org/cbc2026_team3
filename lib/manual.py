"""手動操縦 (調整時・緊急時の補助操縦) の指令経路。

**運用の主体は半自動シーケンス制御のままで、手動はその代替ではなく退避路である。**
機構の調整中や、シーケンスが想定外の状態で止まったときに、操縦者が軸を直接動かして
安全な姿勢へ戻すために置いてある。

指令は必ず ``AxisHandle`` を通す。モータ 1 台ずつ送る API をここに置いてはならない
(左右直結ペアが別々の時刻に動いて機構がねじれる)。``AxisHandle`` 以下の経路は
シーケンスとまったく同じなので、緊急停止インターロック (``MotorHandle.set_target``)、
M3508 の PC 側 PID への迂回 (``target_sink``)、自作モタドラの 20Hz 再送
(``GenericTargetRefresher``)、左右ペアの 3 層保護はすべてそのまま効く。

手動が新たに背負う責務は 2 つだけである:

1. **可動範囲の宣言を強制する。** 通常運用の ``move_to`` は位置名でしか値を引けず、
   「定義した状態以外を送れない」ことが構造的に保証されている。連続値を通す手動は
   その保証を外すので、代わりの境界 (``axes.<軸>.manual``) を持つ軸だけに連続操作を許す。
   境界を持たない軸に残るのは位置名によるプリセット指令だけで、そちらは今までと同じ保証。
2. **ジョグの起点を目標値で積む。** 起点を毎回フィードバックから取ると、追従中に
   連打したぶんが吸われて「押した回数だけ動かない」。直前の手動目標を起点にする。
   ただし緊急停止を挟んだら捨てる — 停止中に自重で下がっていた場合、解除後の 1 回目が
   古い起点から飛ぶ。
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING

from lib.drivers.base import ControlMode
from lib.match_state import Court
from lib.sequence.motors import AxisHandle
from lib.sequence.positions import PositionLookupError

if TYPE_CHECKING:
    from lib.sequence.motors import MotorGroup
    from lib.sequence.positions import AxisSpec, ManualSpec, PositionTable

logger = logging.getLogger(__name__)

__all__ = ["ManualControlError", "ManualController", "OperationMode"]


class OperationMode(StrEnum):
    """1 ロボットの制御権を誰が握っているか。

    ``lib.drivers.base.ControlMode`` (position / velocity / duty) とは別物なので
    名前を分けてある。あちらはモータへ送る指令の種類、こちらは操縦の主体。

    SEQUENCE — 半自動シーケンス制御。通常運用
    MANUAL   — 操縦者が軸を直接動かす。調整時と、シーケンスからの退避に使う
    """

    SEQUENCE = "sequence"
    MANUAL = "manual"


class ManualControlError(RuntimeError):
    """手動指令を受理できないときに送出される (理由は操縦者へそのまま返す)。"""


class ManualController:
    """1 ロボット分の手動指令。軸単位でしか指令できない。

    寿命のある状態はジョグの起点 (``_targets``) だけで、機体の状態は一切持たない。
    現在値はそのつどフィードバックから逆換算する (``AxisSpec.to_value``)。
    """

    def __init__(
        self,
        motors: MotorGroup,
        positions: PositionTable,
        *,
        court: Court = Court.RED,
    ) -> None:
        self._motors = motors
        self._positions = positions
        self._court = court
        #: 軸名 → 直前に手動で送った目標値 (人間の単位)。ジョグの起点
        self._targets: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    #  参照
    # ------------------------------------------------------------------ #

    def set_court(self, court: Court) -> None:
        self._court = court

    # ------------------------------------------------------------------ #
    #  指令
    # ------------------------------------------------------------------ #

    async def move_to_position(self, axis: str, name: str) -> float:
        """位置名で指令する。全軸で使える。

        既定義の点しか送らないので、``manual:`` を持たない軸 (離散状態アクチュエータ・
        duty 軸) でもこれだけは常に許してよい。到達は待たない — 手動は応答性が要る
        操作で、待つと連続した操作が詰まる。
        """
        spec = self._axis(axis)
        value = self._positions.raw(axis, name, court=self._court)
        await self._send(spec, spec.to_commands(value))
        self._targets[axis] = value
        logger.info("manual move: axis=%s position=%s value=%s", axis, name, value)
        return value

    async def set_value(self, axis: str, value: float) -> float:
        """人間の単位の絶対値で指令する。``manual:`` を持つ軸のみ。

        範囲外は拒否ではなくクランプする (拒否だと端で操作そのものが効かなくなる)。
        実際に送った値を返すので、呼び出し側は丸められたことを操縦者へ返せる。
        """
        spec = self._axis(axis)
        manual = self._require_manual(spec)
        clamped = manual.clamp(float(value))
        await self._send(spec, spec.to_commands(clamped))
        self._targets[axis] = clamped
        return clamped

    async def jog(self, axis: str, delta: float) -> float:
        """直前の手動目標から相対移動する。``manual:`` を持つ軸のみ。

        起点にフィードバックを使わないのは、追従中の連打が吸われるため。
        起点が無い (初回・緊急停止後) ときだけ現在値から取り直す。
        """
        spec = self._axis(axis)
        self._require_manual(spec)
        origin = self._targets.get(axis)
        if origin is None:
            origin = self.observed_value(axis)
        return await self.set_value(axis, origin + float(delta))

    # ------------------------------------------------------------------ #
    #  状態
    # ------------------------------------------------------------------ #

    def observed_value(self, axis: str) -> float:
        """フィードバックから逆換算した現在の軸位置 (人間の単位)。"""
        spec = self._axis(axis)
        return spec.to_value(self._feedback_positions(spec))

    def axes_info(self) -> list[dict]:
        """WS 配信用の軸一覧。

        可動範囲とプリセット名は静的だが、``steps`` (シーケンスのステップ表) と同じく
        state に載せる。UI 側にモータ名も軸名もハードコードさせないためで、軸が
        増減しても UI の変更は要らない。
        """
        info: list[dict] = []
        for name in self._positions.axes:
            spec = self._positions.axis(name)
            info.append(
                {
                    "name": name,
                    "unit": spec.unit,
                    "command_mode": spec.command_mode.value,
                    "value": self._safe_observed_value(spec),
                    "target": self._targets.get(name),
                    "manual": spec.manual.to_dict() if spec.manual is not None else None,
                    "deviation": self._safe_deviation(spec),
                    "sync_tolerance": spec.sync_tolerance,
                    "positions": list(self._positions.names(name)),
                    "motors": list(spec.motor_names),
                }
            )
        return info

    # ------------------------------------------------------------------ #
    #  ライフサイクル
    # ------------------------------------------------------------------ #

    def reset(self) -> None:
        """ジョグの起点を捨てる (モード切替時)。

        モータの目標値そのものは消さない。消すと保持トルクを失い、昇降軸が落下する。
        """
        self._targets.clear()

    def on_e_stop(self) -> None:
        """緊急停止で起点を捨てる。

        停止中に機構が自重で下がっていると、解除後 1 回目のジョグが古い起点から
        飛ぶ。次のジョグでフィードバックから取り直させる。
        """
        self._targets.clear()

    # ------------------------------------------------------------------ #
    #  内部
    # ------------------------------------------------------------------ #

    def _axis(self, axis: str) -> AxisSpec:
        """軸を引く。未定義なら定義済みの軸名を添えて拒否する。"""
        try:
            return self._positions.axis(axis)
        except PositionLookupError as exc:
            raise ManualControlError(str(exc)) from exc

    def _require_manual(self, spec: AxisSpec) -> ManualSpec:
        """連続操作を許した軸か。許していなければ理由を返す。"""
        if spec.manual is None:
            allowed = ", ".join(self._positions.manual_axes()) or "(なし)"
            raise ManualControlError(
                f"軸 '{spec.name}' は連続操作の対象外です "
                f"(位置名の指定のみ受け付けます。連続操作できる軸: {allowed})"
            )
        return spec.manual

    async def _send(self, spec: AxisSpec, commands: dict[str, float]) -> None:
        """1 軸へ指令する。**必ず AxisHandle を通す。**

        左右直結ペアを同一フレームで送る責務は ``AxisHandle.set_target_value`` が
        持っている。ここでモータ 1 台ずつ ``MotorHandle.set_target`` を呼ぶと、
        送信の時間差ぶんだけ機構がねじれる。
        """
        handle = AxisHandle(spec, [getattr(self._motors, name) for name in spec.motor_names])
        await handle.set_target_value(commands)

    def _feedback_positions(self, spec: AxisSpec) -> dict[str, float]:
        """軸のモータ名 → 指令単位のフィードバック位置。未登録のモータは載せない。

        載せないことに意味がある。``SyncGroup.deviation`` は比較対象が 2 個未満なら
        None を返すので、途絶したモータを 0 で埋めない限り「揃っていないのに
        揃って見える」偏差は作れない。
        """
        return {
            name: getattr(self._motors, name).driver.feedback_position()
            for name in spec.motor_names
            if name in self._motors
        }

    def _safe_deviation(self, spec: AxisSpec) -> float | None:
        """配信用の左右偏差 (人間の単位)。測れない軸は None を返す。

        **判定と同じ ``SyncGroup.deviation`` を通す。** 逆換算をここへ書き写すと、
        符号を 1 つ落としただけで画面の言う「ずれ」と 3 層の保護が見ている「ずれ」が
        食い違い、しかも画面側だけが壊れるので気付けない。

        ずれようのない軸 (単独モータ・``sync_tolerance`` なし) と、ずれを測れない軸
        (位置指令でない) をどちらも None にするのは、0.0 が「揃っていることを測った」
        ように見えるため。区別が要るなら ``sync_tolerance`` と ``motors`` で付く。
        """
        group = spec.sync_group
        if group is None or spec.command_mode is not ControlMode.POSITION:
            return None
        try:
            return group.deviation(self._feedback_positions(spec))
        except Exception:
            # 20Hz の配信経路から呼ばれる。1 軸の算出失敗で state 全体を落とさない
            logger.debug("軸 '%s' の左右偏差を算出できません", spec.name, exc_info=True)
            return None

    def _safe_observed_value(self, spec: AxisSpec) -> float | None:
        """配信用の現在値。読めない軸は None を返す。

        位置指令でない軸 (``conveyor`` の duty) は None にする。DC 基板は
        エンコーダを持たず ``FEEDBACK`` の位置も持たないので、逆換算した 0 を
        載せると「測ったように見える 0」が UI に流れ込む。
        """
        if spec.command_mode is not ControlMode.POSITION:
            return None
        try:
            return self.observed_value(spec.name)
        except Exception:
            # 20Hz の配信経路から呼ばれる。1 軸の算出失敗で state 全体を落とさない
            logger.debug("軸 '%s' の現在値を算出できません", spec.name, exc_info=True)
            return None
