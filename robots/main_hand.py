from __future__ import annotations

import logging

from lib.sequence.engine import Sequence, step

logger = logging.getLogger(__name__)

#: メインハンドの初期姿勢。**軸名と位置名の一覧であって数値ではない**ので
#: robots/ に置いてよい (単位換算・許容差・待ち時間はすべて位置定数 yaml が持つ)。
#: シーケンスの往路 (`move_to_home`) と復路 (`return_home`)、および
#: 動作確認 (`robots/motor_check.py`) が同じ 1 つを参照する。書き写すと、機構が
#: 変わったときに片方だけ直った状態が作れる。
HOME: dict[str, str] = {
    "y_axis": "home",
    "rotate": "home",
    "gripper": "open",
    "wall_f": "initial",
    "wall_r": "initial",
    "conveyor": "stop",
}


class MainHandSequence(Sequence):
    """メインハンドのシーケンス。

    目標値は `config/main_hand_positions.yaml` に外出ししてある。
    機構が確定したら yaml の数値だけを差し替えればよく、このファイルは触らない。
    `move_to` は到達待ちのタイムアウト時に例外を送出し、シーケンスを停止させる
    (掴めていないワークを搬送するような事故を防ぐため)。

    動作シナリオ (暫定):
      前進してワークの前に着き、エンドエフェクタを `rotate: pick` の姿勢へ倒してから
      グリッパで掴む。掴んだあとは前後の壁 (`wall_f` / `wall_r`) を閉じてワークを機構内に
      保持し、姿勢を戻して配置位置まで運ぶ。配置位置で壁を開き、コンベアを回して
      ワークを送り出しながらグリッパを開いてリリースする。

    !!! この動作シナリオは競技の戦略が未確定のための暫定である !!!
    とくに以下は戦略が決まり次第、ステップの順序と組み合わせごと差し替える:
      - コンベアをリリース時にだけ回すか、搬送中も回し続けるか
      - 壁を「掴んだ直後に閉じる」のか「掴む前に閉じて位置決めに使う」のか
      - `rotate: pick` を唯一の把持姿勢とするか、ワーク列ごとに別の姿勢を持たせるか
    """

    def __init__(self, name: str = "main_hand") -> None:
        super().__init__(name)

    @step("初期位置へ移動")
    async def move_to_home(self) -> None:
        logger.info("[main_hand] 初期位置へ移動")
        await self.move_to(HOME)

    @step("自陣ワーク 3 列目まで前進", require_trigger=True)
    async def move_to_work_3(self) -> None:
        logger.info("[main_hand] 自陣ワーク 3 列目まで前進")
        await self.move_to({"y_axis": "work_3"})

    @step("ワーク前まで前進", require_trigger=True)
    async def approach_work(self) -> None:
        logger.info("[main_hand] ワーク前まで前進")
        await self.move_to({"y_axis": "approach"})

    @step("エンドエフェクタを把持姿勢へ")
    async def rotate_to_pick(self) -> None:
        logger.info("[main_hand] エンドエフェクタを把持姿勢へ")
        await self.move_to({"rotate": "pick"})

    # 位置ずれのまま閉じるとワークと機構の双方を壊すため、操縦者の目視確認で止める
    @step("ハンド閉じる (ワーク把持)", require_trigger=True)
    async def grip_work(self) -> None:
        logger.info("[main_hand] ハンド閉じる")
        await self.move_to({"gripper": "closed"})

    @step("壁を閉じる (ワーク保持)")
    async def close_walls(self) -> None:
        logger.info("[main_hand] 壁を閉じる")
        await self.move_to({"wall_f": "closed", "wall_r": "closed"})

    @step("エンドエフェクタを戻す")
    async def rotate_to_home(self) -> None:
        logger.info("[main_hand] エンドエフェクタを戻す")
        await self.move_to({"rotate": "home"})

    @step("配置位置へ搬送", require_trigger=True)
    async def carry_to_target(self) -> None:
        logger.info("[main_hand] 配置位置へ搬送")
        await self.move_to({"y_axis": "place"})

    @step("壁を開く")
    async def open_walls(self) -> None:
        logger.info("[main_hand] 壁を開く")
        await self.move_to({"wall_f": "open", "wall_r": "open"})

    @step("コンベア稼働")
    async def start_conveyor(self) -> None:
        logger.info("[main_hand] コンベア稼働")
        await self.move_to({"conveyor": "run"})

    # リリースは一度やり直しが利かないので、配置位置到達を目視で確認させる
    @step("ハンド開く (リリース)", require_trigger=True)
    async def release_work(self) -> None:
        logger.info("[main_hand] ハンド開く")
        await self.move_to({"gripper": "open"})

    @step("コンベア停止")
    async def stop_conveyor(self) -> None:
        logger.info("[main_hand] コンベア停止")
        await self.move_to({"conveyor": "stop"})

    @step("初期位置へ復帰")
    async def return_home(self) -> None:
        logger.info("[main_hand] 初期位置へ復帰")
        await self.move_to(HOME)
