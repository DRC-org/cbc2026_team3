from __future__ import annotations

import logging

from lib.sequence.engine import Sequence, step

logger = logging.getLogger(__name__)


class MainHandSequence(Sequence):
    """メインハンドのシーケンス。

    目標値は `config/main_hand_positions.yaml` に外出ししてある。
    機構が確定したら yaml の数値だけを差し替えればよく、このファイルは触らない。
    `move_to` は到達待ちのタイムアウト時に例外を送出し、シーケンスを停止させる
    (掴めていないワークを搬送するような事故を防ぐため)。
    """

    def __init__(self, name: str = "main_hand") -> None:
        super().__init__(name)

    @step("初期位置へ移動")
    async def move_to_home(self) -> None:
        logger.info("[main_hand] 初期位置へ移動")
        await self.move_to({"lift_motor": "home", "arm_joint": "home", "gripper": "open"})

    @step("自陣ワーク 3 列目まで前進", require_trigger=True)
    async def move_to_work_3(self) -> None:
        logger.info("[main_hand] 自陣ワーク 3 列目まで前進")
        await self.move_to({"lift_motor": "work_3"})

    @step("ワーク前まで前進", require_trigger=True)
    async def approach_work(self) -> None:
        logger.info("[main_hand] ワーク前まで前進")
        await self.move_to({"lift_motor": "approach"})

    @step("アーム展開")
    async def extend_arm(self) -> None:
        logger.info("[main_hand] アーム展開")
        await self.move_to({"arm_joint": "extended"})

    # 位置ずれのまま閉じるとワークと機構の双方を壊すため、全自動でも目視確認で止める
    @step("ハンド閉じる (ワーク把持)", require_trigger=True, auto_stop=True)
    async def grip_work(self) -> None:
        logger.info("[main_hand] ハンド閉じる")
        await self.move_to({"gripper": "closed"})

    @step("アーム引き戻し")
    async def retract_arm(self) -> None:
        logger.info("[main_hand] アーム引き戻し")
        await self.move_to({"arm_joint": "retracted"})

    @step("配置位置へ搬送", require_trigger=True)
    async def carry_to_target(self) -> None:
        logger.info("[main_hand] 配置位置へ搬送")
        await self.move_to({"lift_motor": "place"})

    # リリースは一度やり直しが利かないので、半自動では配置位置到達を目視で確認させる
    @step("ハンド開く (リリース)", require_trigger=True)
    async def release_work(self) -> None:
        logger.info("[main_hand] ハンド開く")
        await self.move_to({"gripper": "open"})

    @step("初期位置へ復帰")
    async def return_home(self) -> None:
        logger.info("[main_hand] 初期位置へ復帰")
        await self.move_to({"lift_motor": "home", "arm_joint": "home", "gripper": "open"})
