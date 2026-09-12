from __future__ import annotations

from sequences.main_hand import HOME as MAIN_HOME
from sequences.sub_hand import INITIAL_POSES as SUB_INITIAL_POSES

__all__ = ["INITIAL_POSES_BY_ROBOT"]

#: ロボット名 → 試合シーケンスの初期位置へ戻すために**この順で 1 通ずつ**投げる位置名の組。
#: 並び順は干渉制約を踏まないための不変条件なので、定義はシーケンス側が持ちここは束ねるだけ
#: (書き写すと、片方だけ直された並びが作れる)。
INITIAL_POSES_BY_ROBOT: dict[str, tuple[dict[str, str], ...]] = {
    "main_hand": (MAIN_HOME,),
    "sub_hand": SUB_INITIAL_POSES,
}
