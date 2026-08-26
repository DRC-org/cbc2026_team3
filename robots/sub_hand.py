from __future__ import annotations

import logging

from lib.sequence.engine import Sequence, step

logger = logging.getLogger(__name__)


class SubHandSequence(Sequence):
    """サブハンドのシーケンス (メインハンドの補助)。

    目標値は `config/sub_hand_positions.yaml` に外出ししてある。
    機構が確定したら yaml の数値だけを差し替えればよい。
    """

    def __init__(self, name: str = "sub_hand") -> None:
        super().__init__(name)

    @step("初期位置へ移動")
    async def move_to_home(self) -> None:
        logger.info("[sub_hand] 初期位置へ移動")
        await self.move_to({"sub_arm_joint": "home", "sub_gripper": "open"})

    @step("補助ハンド展開", require_trigger=True)
    async def extend_sub_arm(self) -> None:
        logger.info("[sub_hand] 補助ハンド展開")
        await self.move_to({"sub_arm_joint": "extended"})

    @step("ワーク受け取り位置へ")
    async def move_to_handoff(self) -> None:
        logger.info("[sub_hand] ワーク受け取り位置へ")
        await self.move_to({"sub_arm_joint": "handoff"})

    # メインハンドと機構同士が向かい合う唯一の動作。ずれたまま閉じると両機構が衝突するため、
    # 操縦者の目視確認で止める
    @step("ハンド閉じる (受け取り)", require_trigger=True)
    async def grip_handoff(self) -> None:
        logger.info("[sub_hand] ハンド閉じる")
        await self.move_to({"sub_gripper": "closed"})

    @step("配置位置へ移動", require_trigger=True)
    async def move_to_place(self) -> None:
        logger.info("[sub_hand] 配置位置へ移動")
        await self.move_to({"sub_arm_joint": "place"})

    # リリースはやり直しが利かないので、配置位置到達を目視で確認させる
    @step("ハンド開く (配置)", require_trigger=True)
    async def release_at_place(self) -> None:
        logger.info("[sub_hand] ハンド開く")
        await self.move_to({"sub_gripper": "open"})

    @step("初期位置へ復帰")
    async def return_home(self) -> None:
        logger.info("[sub_hand] 初期位置へ復帰")
        await self.move_to({"sub_arm_joint": "home", "sub_gripper": "open"})
