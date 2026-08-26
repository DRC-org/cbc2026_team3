"""動作確認 API に関心のないテスト向けの MotorDriver ひな型。

``check_command`` / ``evaluate_check_result`` / ``reset_after_check`` は
``@abc.abstractmethod`` である。未実装のドライバが起動できてしまうと、
``reset_after_check`` が送られないまま次のモータへ進む (駆動状態が残る) 事故を
実行時まで検出できないため、本番の契約を緩められない。

一方でヘルス判定など動作確認と無関係なテストにまで 3 メソッドを書き写させると、
「テストの都合で契約を緩めよう」という圧力が戻ってくる。呼ばれない前提の最小実装を
ここに 1 つだけ置いて共有する。
"""

from __future__ import annotations

import can

from lib.drivers.base import CheckContext, ControlMode, MotorDriver


class CheckStubDriver(MotorDriver):
    """動作確認 API だけを埋めたテスト用基底 (プロトコル層は派生側で実装する)。"""

    def check_command(self, *, magnitude: float) -> tuple[can.Message, CheckContext]:
        return (
            can.Message(arbitration_id=0x100 + self.can_id, data=bytes(8)),
            CheckContext(mode=ControlMode.CURRENT, target=float(magnitude)),
        )

    def evaluate_check_result(self, context: CheckContext) -> tuple[bool, str | None]:
        return True, None

    def reset_after_check(self) -> can.Message:
        return can.Message(arbitration_id=0x100 + self.can_id, data=bytes(8))
