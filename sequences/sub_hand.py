from __future__ import annotations

import logging

from lib.sequence.engine import Sequence, step

logger = logging.getLogger(__name__)

#: 吸着パッドの電磁弁。config/sub_hand_positions.yaml の axes と 1:1 で対応する。
#: 数値ではなく軸名の一覧なので sequences/ に置いてよい (値・待ち時間は yaml が持つ)。
#: 動作確認 (`sequences/motor_check.py`) も同じ 1 つを参照する。
VALVE_AXES: tuple[str, ...] = ("valve_1", "valve_2", "valve_3", "valve_4", "valve_5", "valve_6")


def _all_valves(state: str) -> dict[str, str]:
    """全パッドの電磁弁を同じ状態にする move_to の引数。

    **6 個を 1 回の move_to へまとめて渡すこと。** move_to は軸ごとの待ちを
    asyncio.gather で並列に回すので、まとめれば待ちは settle_s 1 回分で済む。
    1 個ずつ move_to を呼ぶと弁の応答待ちが個数ぶん直列に積み上がる。
    """
    return dict.fromkeys(VALVE_AXES, state)


class SubHandSequence(Sequence):
    """サブハンドのシーケンス (メインハンドの補助)。

    目標値は `config/sub_hand_positions.yaml` に外出ししてある。
    機構が確定したら yaml の数値だけを差し替えればよい。

    吸着系は「吸気ポンプ 1 台 + 排気ポンプ 1 台 (DC 基板) + 電磁弁 6 個 (電磁弁基板)」で、
    **吸気ポンプは試合中回しっぱなし**にし、吸着 / 解放は電磁弁だけで切り替える
    (docs/motor_driver_can_protocol.md §9.6)。ポンプを都度起動すると、立ち上がりの
    待ちが毎サイクル入る。**`return_home` でも `pump_vac` を止めないのは意図的で**、
    試合を終えたら操縦者が手動操縦で `pump_vac` に `stop` を送る (シーケンスの末尾で
    止めると、次のサイクルの先頭で必ず立ち上がりを待つことになる)。

    **直動 2 軸 (`sub_y_axis` / `sub_lift`) は、試合中このシーケンスでは動かさない。
    手動操縦で扱う。** 位置定数がまだ `home` と `extended` / `lifted` の仮値しか無く、
    受け渡し・配置に対応する位置名そのものが存在しないため、シーケンス化には機構寸法の
    確定が要る。**位置名を先に増やしてシーケンスへ組み込んではならない** ——
    yaml の値が仮のままだと「シーケンスは通るが機体は狙った場所へ行かない」形になり、
    症状が配線・換算の誤りと区別できなくなる。機構寸法が確定したら
    `config/sub_hand_positions.yaml` に受け渡し / 配置の位置名を足したうえで、
    ここへステップとして組み込むこと。
    2 軸とも動作確認 (`sequences/motor_check.py`) と手動操縦の対象には入っているので、
    シーケンスに現れないこと自体は「未接続」を意味しない。
    """

    def __init__(self, name: str = "sub_hand") -> None:
        super().__init__(name)

    @step("初期位置へ移動")
    async def move_to_home(self) -> None:
        logger.info("[sub_hand] 初期位置へ移動")
        # **弁を閉じてから吸気ポンプを回す。** 逆にすると、前のサイクルで開いたままの
        # 弁からいきなり吸引が始まり、置いたばかりのワークを吸い直す
        await self.move_to(_all_valves("closed") | {"pump_blow": "stop"})
        await self.move_to({"sub_arm_joint": "home", "sub_gripper": "open"})
        await self.move_to({"pump_vac": "run"})

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

    # **吸着したかどうかは PC からは分からない** (基板に圧力センサもリミットスイッチも無く、
    # FEEDBACK の到達フラグも立たない。仕様書 §9.3)。弁を開けて settle_s 待つだけなので、
    # 実際に吸い付いたかは操縦者が目視で確かめる必要がある。トリガーを置くのはそのため
    @step("ワーク吸着", require_trigger=True)
    async def grip_by_suction(self) -> None:
        logger.info("[sub_hand] ワーク吸着")
        await self.move_to(_all_valves("open"))

    @step("配置位置へ移動", require_trigger=True)
    async def move_to_place(self) -> None:
        logger.info("[sub_hand] 配置位置へ移動")
        await self.move_to({"sub_arm_joint": "place"})

    # リリースはやり直しが利かないので、配置位置到達を目視で確認させる
    @step("ワーク解放 (配置)", require_trigger=True)
    async def release_at_place(self) -> None:
        logger.info("[sub_hand] ワーク解放")
        # **吸気ポンプは止めず、弁だけを閉じる。** ポンプを止めて解放しようとすると、
        # 配管に残った負圧が抜けるまでワークが張り付いたままになり、しかも次の
        # サイクルでポンプの立ち上がりを待つことになる
        await self.move_to(_all_valves("closed"))
        # 弁を閉じてもパッド側の残圧で張り付くので、排気で押し離す。
        # 押し出す間だけ回して止める (回しっぱなしにすると次の吸着を邪魔する)
        await self.move_to({"pump_blow": "run"})
        await self.move_to({"pump_blow": "stop"})
        await self.move_to({"sub_gripper": "open"})

    @step("初期位置へ復帰")
    async def return_home(self) -> None:
        logger.info("[sub_hand] 初期位置へ復帰")
        # 吸気ポンプはここでも止めない (次のサイクルで立ち上がりを待たないため)。
        # 試合が終わったら操縦者が手動操縦で pump_vac に stop を送る
        await self.move_to(_all_valves("closed") | {"pump_blow": "stop"})
        await self.move_to({"sub_arm_joint": "home", "sub_gripper": "open"})
