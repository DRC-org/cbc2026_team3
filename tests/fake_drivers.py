"""テスト用 MotorDriver のひな型。

``StubFeedbackDriver`` は CAN プロトコルに関心のないテスト向け、
``HealthFlagDriver`` はヘルス判定の分岐だけを見るテスト向け。
"""

from __future__ import annotations

import can

from lib.drivers.base import ControlMode, MotorDriver, MotorState


class StubFeedbackDriver(MotorDriver):
    """CAN プロトコルを持たず、観測値をテストから直接与えられるドライバ。

    到達判定・偏差監視・ヘルス判定は ``MotorState`` しか読まないため、これらを
    検証するテストに本物のフレーム往復まで要求すると、検証したい判定と関係の
    ないエンコード仕様がテストへ写り込む。

    観測値の投入口を ``set_observed`` として公開するのは、テストが
    ``driver._state`` へ外から代入するのを不要にするため。外から代入できる形を
    残すと、フレーム解釈を伴うドライバ (M3508 の多回転など) のテストまで同じ
    書き方に流れ、``update_state`` の副作用ごと迂回したテストが増える。
    """

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        return can.Message(arbitration_id=0x100 + self.can_id, data=bytes(8), is_extended_id=False)

    def decode_feedback(self, msg: can.Message) -> MotorState:  # pragma: no cover
        return self._state

    def matches_feedback(self, msg: can.Message) -> bool:  # pragma: no cover
        return False

    def set_observed(self, **kwargs: float) -> None:
        """フィードバック観測値を差し替える (``MotorState`` のフィールド名で指定)。"""
        self._state = MotorState(**kwargs)


class HealthFlagDriver(MotorDriver):
    """ヘルス判定 (温度・過電流・fault) の分岐だけを属性で制御できるドライバ。

    ``health()`` の優先順位は「ハード fault > 温度 critical > 途絶 > warning > OK」で、
    どの判定 API が呼ばれたかだけが問われる。本物のフレーム解釈まで通すと、
    検証したい優先順位と関係のないエンコード仕様がテストへ写り込む。

    一方で ``matches_feedback`` は成立させる。ヘルスの STALE 判定はフィードバック
    鮮度を読むので、「鮮度を進める」テストは受信経路 (宛先判定 → デコード) を
    実際に通す必要がある。``feedback_message()`` がその 1 通を返す。
    """

    #: フィードバックとして扱う CAN ID のベース (C620 の 0x200+id に合わせただけで意味は無い)
    FEEDBACK_ID_BASE = 0x200

    def __init__(self, name: str, can_id: int) -> None:
        super().__init__(name, can_id)
        self.thermal_warning = False
        self.thermal_fault = False
        self.overcurrent = False
        self.fault = False

    def feedback_message(self) -> can.Message:
        """このドライバ宛と判定されるフィードバックフレーム。"""
        return can.Message(arbitration_id=self.FEEDBACK_ID_BASE + self.can_id, data=bytes(8))

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:  # pragma: no cover
        return can.Message(arbitration_id=0x100 + self.can_id, data=bytes(8))

    def decode_feedback(self, msg: can.Message) -> MotorState:
        return self._state

    def matches_feedback(self, msg: can.Message) -> bool:
        return msg.arbitration_id == self.FEEDBACK_ID_BASE + self.can_id

    def has_thermal_warning(self, temp_warning_c: float) -> bool:
        return self.thermal_warning

    def has_thermal_fault(self, temp_critical_c: float) -> bool:
        return self.thermal_fault

    def has_overcurrent_warning(self) -> bool:
        return self.overcurrent

    def is_fault(self) -> bool:
        return self.fault
