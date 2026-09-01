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


def _pick_at(work: str) -> dict[str, str]:
    """ワーク列へ把持姿勢で寄せる move_to の引数。

    **列が変わっても把持姿勢 (`rotate: pick`) は共通**なので、変わる列名だけを
    引数で受ける。列ごとに組を書き写すと、把持姿勢を変えたときに直し忘れた列だけが
    別の姿勢でワークへ向かう。
    """
    return {"y_axis": work, "rotate": "pick"}


#: ワークをコンベアへ渡すときの指定。ワーク列を増やすたびに同じ組が繰り返されるので
#: 1 か所に置く (書き写すと、姿勢と壁のどちらか片方だけ直った列ができる)。
TO_CONVEYOR: dict[str, str] = {"rotate": "place", "wall_f": "open"}

#: ワークをコンベアへ落としたあと、壁で寄せながら送り出す指定。
#: リリース → 次列への移動のあとに必ず来るので 1 か所に置く。
SWEEP_TO_CONVEYOR: dict[str, str] = {"wall_f": "closed", "conveyor": "run"}

#: グリッパを開く指定。**単独の move_to でしか使わない。**
#: 他の軸と同じ move_to へ混ぜると `asyncio.gather` で同時に動くので、
#: 開き切る前に y 軸が走り出してワークが搬送経路の外へ落ちる。
RELEASE: dict[str, str] = {"gripper": "open"}


class MainHandSequence(Sequence):
    """メインハンドのシーケンス。

    目標値は `config/main_hand_positions.yaml` に外出ししてある。
    機構が確定したら yaml の数値だけを差し替えればよく、このファイルは触らない。
    `move_to` は到達待ちのタイムアウト時に例外を送出し、シーケンスを停止させる
    (掴めていないワークを搬送するような事故を防ぐため)。

    動作シナリオ (暫定):
      ワーク列 (3 列目 → 共通 → 1 列目 → 2 列目) を 1 列ずつ回る。各列で
      `rotate: pick` の姿勢のまま y 軸で列へ寄せ、グリッパで掴む。掴んだら
      `rotate: place` へ倒しながら前側の壁 (`wall_f`) を開き、コンベアの上で
      グリッパを開いてリリースする。リリース後は次の列へ y 軸で移動し、
      `wall_f` を閉じてワークを寄せながらコンベアを回して送り出す。
      最後の列を送り出したら初期姿勢へ戻り、そこでコンベアを止める。

    現在の暫定シナリオが**使っていない**もの (docstring と実装を食い違わせない
    ための明示):
      - 後側の壁 `wall_r` は `initial` のまま一度も動かさない。前側の壁だけで
        ワークをコンベアへ寄せられるかは機構待ちで、要るとなったら
        `TO_CONVEYOR` / `SWEEP_TO_CONVEYOR` に足す
      - `y_axis: place` / `y_axis: approach` へは動かさない。ワークはコンベアへ
        渡すだけで、メインハンドは搬送先まで運ばない
      - コンベアは最初のリリース後から試合終了まで回し続ける (列ごとに止めない)

    !!! この動作シナリオは競技の戦略が未確定のための暫定である !!!
    とくに以下は戦略が決まり次第、ステップの順序と組み合わせごと差し替える:
      - コンベアをリリース時にだけ回すか、搬送中も回し続けるか
      - 壁を「掴んだ直後に閉じる」のか「掴む前に閉じて位置決めに使う」のか
      - `rotate: pick` を唯一の把持姿勢とするか、ワーク列ごとに別の姿勢を持たせるか
      - ワーク列を回る順序 (3 → 共通 → 1 → 2)

    ステップ名とラベルは上記のとおり暫定なので、テストで 1 対 1 に固定していない。
    固定してあるのは「壊れたら困る性質」の側 —— 把持とリリースがトリガー待ちを
    持つこと、リリースが単独の move_to であること、ペア軸が逆符号で動くこと。
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
        await self.move_to(_pick_at("work_3"))

    @step("自陣ワーク 3 列目を把持", require_trigger=True)
    async def grab_work_3(self) -> None:
        logger.info("[main_hand] 自陣ワーク 3 列目を把持")
        await self.move_to({"gripper": "closed"})

    @step("3 列目ワークをコンベアの位置へ")
    async def move_work_3_to_conveyor(self) -> None:
        logger.info("[main_hand] 3 列目ワークをコンベアの位置へ")
        await self.move_to(TO_CONVEYOR)

    # リリースはやり直しが利かない (落としたワークは拾えない) ので、コンベアの上に
    # 来ていることを操縦者の目視で確かめてから開く
    @step("3 列目ワークをリリース", require_trigger=True)
    async def release_work_3(self) -> None:
        logger.info("[main_hand] 3 列目ワークをリリース")
        await self.move_to(RELEASE)

    @step("共通ワークへ移動")
    async def move_to_work_shared(self) -> None:
        logger.info("[main_hand] 共通ワークへ移動")
        await self.move_to(_pick_at("work_shared"))

    @step("コンベアの壁を閉じてワークを寄せる")
    async def close_wall_f_3(self) -> None:
        logger.info("[main_hand] コンベアの壁を閉じてワークを寄せる")
        await self.move_to(SWEEP_TO_CONVEYOR)

    @step("共通ワークを把持", require_trigger=True)
    async def grab_work_shared(self) -> None:
        logger.info("[main_hand] 共通ワークを把持")
        await self.move_to({"gripper": "closed"})

    @step("共通ワークをコンベアの位置へ")
    async def move_work_shared_to_conveyor(self) -> None:
        logger.info("[main_hand] 共通ワークをコンベアの位置へ")
        await self.move_to(TO_CONVEYOR)

    @step("共通ワークをリリース", require_trigger=True)
    async def release_work_shared(self) -> None:
        logger.info("[main_hand] 共通ワークをリリース")
        await self.move_to(RELEASE)

    @step("1 列目ワークへ移動")
    async def move_to_work_1(self) -> None:
        logger.info("[main_hand] 1 列目ワークへ移動")
        await self.move_to(_pick_at("work_1"))

    @step("コンベアの壁を閉じてワークを寄せる")
    async def close_wall_f_shared(self) -> None:
        logger.info("[main_hand] コンベアの壁を閉じてワークを寄せる")
        await self.move_to(SWEEP_TO_CONVEYOR)

    @step("1 列目ワークを把持", require_trigger=True)
    async def grab_work_1(self) -> None:
        logger.info("[main_hand] 1 列目ワークを把持")
        await self.move_to({"gripper": "closed"})

    @step("1 列目ワークをコンベアの位置へ")
    async def move_work_1_to_conveyor(self) -> None:
        logger.info("[main_hand] 1 列目ワークをコンベアの位置へ")
        await self.move_to(TO_CONVEYOR)

    @step("1 列目ワークをリリース", require_trigger=True)
    async def release_work_1(self) -> None:
        logger.info("[main_hand] 1 列目ワークをリリース")
        await self.move_to(RELEASE)

    @step("2 列目ワークへ移動")
    async def move_to_work_2(self) -> None:
        logger.info("[main_hand] 2 列目ワークへ移動")
        await self.move_to(_pick_at("work_2"))

    @step("コンベアの壁を閉じてワークを寄せる")
    async def close_wall_f_1(self) -> None:
        logger.info("[main_hand] コンベアの壁を閉じてワークを寄せる")
        await self.move_to(SWEEP_TO_CONVEYOR)

    @step("2 列目ワークを把持", require_trigger=True)
    async def grab_work_2(self) -> None:
        logger.info("[main_hand] 2 列目ワークを把持")
        await self.move_to({"gripper": "closed"})

    @step("2 列目ワークをコンベアの位置へ")
    async def move_work_2_to_conveyor(self) -> None:
        logger.info("[main_hand] 2 列目ワークをコンベアの位置へ")
        await self.move_to(TO_CONVEYOR)

    @step("2 列目ワークをリリース", require_trigger=True)
    async def release_work_2(self) -> None:
        logger.info("[main_hand] 2 列目ワークをリリース")
        await self.move_to(RELEASE)

    @step("コンベアの壁を閉じてワークを寄せる")
    async def close_wall_f_2(self) -> None:
        logger.info("[main_hand] コンベアの壁を閉じてワークを寄せる")
        await self.move_to(SWEEP_TO_CONVEYOR)

    @step("初期位置へ復帰")
    async def return_home(self) -> None:
        logger.info("[main_hand] 初期位置へ復帰")
        # HOME に conveyor: stop が入っているので、コンベアはここで止まる
        await self.move_to(HOME)
