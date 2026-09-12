"""state メッセージの間引き。

会場の WiFi が細いと、20Hz で 17KB の全欄配信がそのまま詰まりになり、操縦者の指令が
届かなくなる (`docs/invariants.md` §6)。中身のほとんどは毎フレーム同じ値なので、
**前回送った値と同じ欄は落とし、UI 側が前回値で埋める** (`web/src/lib/robotState.ts`)。

落としてよいかの判断はここ 1 箇所に閉じる。サーバー本体へ `if` を散らすと、欄を足した
人が「毎フレーム要る欄か」を宣言しないまま通る。
"""

from __future__ import annotations

import time
from copy import deepcopy
from typing import Any

__all__ = ["ALWAYS_SEND", "FULL_RESYNC_S", "SLOW_FIELDS", "StateThinner"]

# 毎フレーム必ず載せる欄。欠けても UI は前回値を保つが、**安全の判断だけは遅らせない**
ALWAYS_SEND: frozenset[str] = frozenset({"type", "robot", "full", "e_stop_active", "safety"})

# 変化していても最短間隔を空ける欄 (秒)。診断の表示は 4Hz で足りるのに、health は
# タイムスタンプが毎フレーム動くので差分では 1 バイトも減らない
SLOW_FIELDS: dict[str, float] = {"health": 0.25}

# 取りこぼしの保険。差分の基準がずれても、この秒数で必ず全欄へ戻す
FULL_RESYNC_S = 5.0

_MISSING = object()


class StateThinner:
    """`state` メッセージから「相手が既に持っている欄」を落とす。

    基準は「実際に送った欄」だけで更新する。送らなかった欄まで基準へ入れると、
    間引いた値が二度と届かなくなる。
    """

    def __init__(
        self,
        *,
        full_resync_s: float = FULL_RESYNC_S,
        slow_fields: dict[str, float] | None = None,
    ) -> None:
        self._full_resync_s = full_resync_s
        self._slow = dict(SLOW_FIELDS if slow_fields is None else slow_fields)
        self._sent: dict[str, dict[str, Any]] = {}
        self._sent_at: dict[tuple[str, str], float] = {}
        self._full_at: dict[str, float] = {}

    def request_full(self) -> None:
        """次のフレームを全欄にする (新しい操縦者が繋がった / 差分の基準を捨てる)。"""
        self._sent.clear()
        self._sent_at.clear()
        self._full_at.clear()

    def thin(self, message: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now is None else now
        robot = str(message.get("robot", ""))
        previous = self._sent.get(robot)

        if previous is None or now - self._full_at.get(robot, 0.0) >= self._full_resync_s:
            return self._full(robot, message, now)

        out: dict[str, Any] = {}
        for key, value in message.items():
            if key in ALWAYS_SEND:
                out[key] = value
                continue
            if key == "manual":
                manual = _thin_manual(previous.get("manual"), value)
                if manual is not None:
                    out[key] = manual
                    previous[key] = deepcopy(value)
                continue
            if previous.get(key, _MISSING) == value:
                continue
            if now - self._sent_at.get((robot, key), 0.0) < self._slow.get(key, 0.0):
                continue
            out[key] = value
            previous[key] = deepcopy(value)
            self._sent_at[robot, key] = now
        return out

    def _full(self, robot: str, message: dict[str, Any], now: float) -> dict[str, Any]:
        self._sent[robot] = deepcopy(message)
        self._full_at[robot] = now
        for key in self._slow:
            self._sent_at[robot, key] = now
        return {**message, "full": True}


def _thin_manual(previous: Any, current: Any) -> dict[str, Any] | None:
    """手動軸は「値だけが毎フレーム動き、仕様 (位置名・可動範囲) は動かない」。

    軸ごとに変わった欄だけ返す。軸の顔ぶれが変わったら丸ごと送る (UI は名前で
    突き合わせるので、消えた軸は丸ごと送らないと画面から消えない)。
    """
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return current if previous != current else None

    prev_axes = previous.get("axes")
    curr_axes = current.get("axes")
    if not isinstance(prev_axes, list) or not isinstance(curr_axes, list):
        return current if previous != current else None
    if [_axis_name(a) for a in prev_axes] != [_axis_name(a) for a in curr_axes]:
        return current

    axes: list[dict[str, Any]] = []
    for prev_axis, curr_axis in zip(prev_axes, curr_axes, strict=True):
        if not isinstance(prev_axis, dict) or not isinstance(curr_axis, dict):
            axes.append(curr_axis)
            continue
        changed = {k: v for k, v in curr_axis.items() if prev_axis.get(k, _MISSING) != v}
        if not changed:
            continue
        axes.append({"name": curr_axis.get("name"), **changed})

    if not axes and previous.get("mode") == current.get("mode"):
        return None
    return {"mode": current.get("mode"), "axes": axes}


def _axis_name(axis: Any) -> Any:
    return axis.get("name") if isinstance(axis, dict) else axis
