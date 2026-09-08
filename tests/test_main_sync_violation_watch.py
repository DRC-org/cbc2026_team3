"""`main._make_sync_violation_handler` が `RobotServer.watch_task` を配線していることの回帰テスト。

同期ずれ検出 → 全体緊急停止 (`main.py:983` 付近) は `asyncio.create_task` して
待たない投げっぱなしタスクの 1 つ。ここが飛ぶと「緊急停止が発火しなかったこと」が
journal の汎用メッセージにしか残らないので、`server.watch_task` へ渡す。

**`robots` は検知元の 1 台ではなく `server.robot_names` (全ロボット)。**
`activate_e_stop()` は全体緊急停止なので、失敗すればそのとき実際にどのロボットも
保護されていない —— `_reactivate_motors` (全ロボットぶんまとめて 1 本) の失敗を
全ロボットへ帰属させるのとまったく同じ理由で、片方だけ違う扱いにしない。
起点となった軸・ロボットは `context` の文言 (`f"{robot_name} の..."`) に残す ——
起点と影響範囲は別物。

`watch_task` 自体の振る舞い (失敗の記録・キャンセルの無視・上限) は
`tests/test_server_task_watch.py` が持つ。ここでは main.py 側の配線
(呼ばれること・context と robots の中身) だけを見る。**main.py に 2 つ目の
カウンタを作らない** (docs/invariants.md) ので、数える側の正しさはあちらのテストに任せる。
"""

from __future__ import annotations

import asyncio
from unittest.mock import Mock

from lib.sequence.engine import Sequence
from lib.sequence.positions import load_position_table
from main import _make_sync_violation_handler
from tests.server_fixtures import ServerFixture


class _EmptySequence(Sequence):
    """ステップを 1 つも持たないシーケンス (`__init_subclass__` を通すためだけ)。"""


def _rotate_table():
    return load_position_table(
        {
            "axes": {
                "rotate": {
                    "unit": "deg",
                    "command_unit": "deg",
                    "sync_tolerance": 5.0,
                    "motors": {
                        "rotate_r": {"scale": 1.0},
                        "rotate_l": {"scale": -1.0},
                    },
                },
            },
            "positions": {},
        },
        source="<test>",
    )


class TestSyncViolationHandlerWatchesTask:
    async def test_on_violation_registers_the_e_stop_task_with_watch_task(self) -> None:
        fx = ServerFixture.build()
        fx.add_robot("main_hand", _EmptySequence("main_hand"))
        fx.add_robot("sub_hand", _EmptySequence("sub_hand"))
        watch = Mock()
        fx.server.watch_task = watch  # type: ignore[method-assign]

        tasks: set[asyncio.Task[None]] = set()
        on_violation = _make_sync_violation_handler(fx.server, "main_hand", _rotate_table(), tasks)

        on_violation("rotate", 12.3)

        # `watch_task` への登録は `create_task` と同じ同期区間で終わる (`tasks` から
        # `tasks.discard` で取り除かれる前に検査する)
        watch.assert_called_once()
        args, kwargs = watch.call_args
        assert isinstance(args[0], asyncio.Task)
        # 起点は main_hand の軸だが、全体緊急停止の失敗なので帰属は全ロボット
        assert kwargs["context"] == "main_hand の同期ずれ検出 → 緊急停止"
        assert tuple(kwargs["robots"]) == fx.robot_names

        # activate_e_stop() の完了まで待つ (このタスク自身の失敗を見るテストではない)
        await asyncio.wait(tasks)

    async def test_failure_is_attributed_to_both_robots(self) -> None:
        """`watch_task` を実際に効かせ、失敗が両ロボットの `safety.failed_tasks` へ乗ることを見る。

        `server.activate_e_stop` 自体は堅牢 (内部で例外を握り潰す) なので、
        ここでは配線側の効果を実証するために `activate_e_stop` を直接壊す。
        **main_hand の軸から検知した違反でも、失敗の帰属は sub_hand にも及ぶ**
        —— 全体緊急停止が失敗した以上、そのとき実際にどちらのロボットも
        保護されていないため (`_reactivate_motors` と同じ理由)。
        """
        fx = ServerFixture.build()
        fx.add_robot("main_hand", _EmptySequence("main_hand"))
        fx.add_robot("sub_hand", _EmptySequence("sub_hand"))

        async def _boom(*, reason: str | None = None) -> None:
            raise RuntimeError("同期監視コールバックからの模擬故障")

        fx.server.activate_e_stop = _boom  # type: ignore[method-assign]

        tasks: set[asyncio.Task[None]] = set()
        on_violation = _make_sync_violation_handler(fx.server, "main_hand", _rotate_table(), tasks)

        on_violation("rotate", 12.3)
        await asyncio.wait(tasks)

        expected = ["main_hand の同期ずれ検出 → 緊急停止 (RuntimeError)"]
        # 検知元 (main_hand) だけでなく、巻き込まれた側 (sub_hand) にも同じラベルが乗る
        assert fx.state_message("main_hand")["safety"]["failed_tasks"] == expected
        assert fx.state_message("sub_hand")["safety"]["failed_tasks"] == expected
