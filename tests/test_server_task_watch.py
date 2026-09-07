"""投げっぱなしタスクの失敗を安全機構へ届ける `RobotServer.watch_task` の回帰テスト。

`asyncio.create_task` して待たない (fire-and-forget な) タスクは、例外が起きても
CPython が "Task exception was never retrieved" を journal へ出すだけで、
どのロボットのどの経路か・いつ起きたかが画面から読めない。`watch_task` は
`done_callback` で例外を拾い、`safety.failed_tasks` として配信する
(先行事例は `BusHealthInfo.rx_down_episodes`)。

呼び出し口 (`_cmd_e_stop_release` / `_cmd_reenergize_motors`) の配線は
`TestCallSitesWireWatchTask` が、実処理は `TestWatchTaskRecordsFailure` が持つ。
同期ずれ検出 (main.py 側) の配線は `tests/test_main_sync_violation_watch.py` が持つ。
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import Mock

from lib.sequence.engine import Sequence
from tests.server_fixtures import ServerFixture


class _EmptySequence(Sequence):
    """ステップを 1 つも持たないシーケンス (`__init_subclass__` を通すためだけ)。"""


def _build_fixture(*names: str) -> ServerFixture:
    fx = ServerFixture.build()
    for name in names:
        fx.add_robot(name, _EmptySequence(name))
    return fx


async def _watch_failure(fx: ServerFixture, *, context: str, robots: list[str]) -> None:
    """失敗するタスクを 1 つ作って `watch_task` に登録し、完了まで待つ。

    `asyncio.wait` を使うのは、`await task` だと例外がこのコルーチンへ
    そのまま伝播するため (fire-and-forget の実運用と違い、テストのここは
    「失敗した」という事実だけを確かめたい)。
    """

    async def _boom() -> None:
        raise RuntimeError("模擬故障")

    task = asyncio.create_task(_boom())
    fx.server.watch_task(task, context=context, robots=robots)
    await asyncio.wait([task])


class TestWatchTaskRecordsFailure:
    """`watch_task` 自体の振る舞い。"""

    async def test_failed_task_appears_in_safety(self) -> None:
        fx = _build_fixture("main_hand", "sub_hand")
        await _watch_failure(fx, context="テスト用タスク", robots=["main_hand"])

        assert fx.state_message("main_hand")["safety"]["failed_tasks"] == [
            "テスト用タスク (RuntimeError)"
        ]
        # 登録していないロボットは巻き込まない
        assert fx.state_message("sub_hand")["safety"]["failed_tasks"] == []

    async def test_successful_task_is_not_reported(self) -> None:
        fx = _build_fixture("main_hand")

        async def _ok() -> None:
            return None

        task = asyncio.create_task(_ok())
        fx.server.watch_task(task, context="成功するタスク", robots=["main_hand"])
        await asyncio.wait([task])

        assert fx.state_message("main_hand")["safety"]["failed_tasks"] == []

    async def test_cancelled_task_is_not_reported_and_does_not_crash(self, caplog) -> None:
        """**`t.cancelled()` を先に見る契約そのものを固定する。**

        見ないと `Task.exception()` が `CancelledError` を送出し、`done_callback`
        自身が例外を撒く (shutdown の一斉キャンセルで実際に起こりうる)。
        `add_done_callback` のコールバックが例外を投げても呼び出し元 (`await
        asyncio.wait(...)`) へは伝播しない —— asyncio のデフォルト例外ハンドラが
        拾って `asyncio` ロガーへ "Exception in callback" として出すだけなので、
        「報告されない」だけを見ても cancelled() 判定を落とす変異は拾えない
        (どちらの経路でも `failed_tasks` は空のまま)。**コールバック自身が
        暴れていないこと**を asyncio ロガーの ERROR で確かめる。
        """
        fx = _build_fixture("main_hand")
        gate = asyncio.Event()

        async def _never_finishes() -> None:
            await gate.wait()

        task = asyncio.create_task(_never_finishes())
        fx.server.watch_task(task, context="キャンセルされるタスク", robots=["main_hand"])
        await asyncio.sleep(0)
        task.cancel()
        with caplog.at_level(logging.ERROR, logger="asyncio"):
            await asyncio.wait([task])

        assert not any("Exception in callback" in r.message for r in caplog.records)
        assert fx.state_message("main_hand")["safety"]["failed_tasks"] == []

    async def test_duplicate_label_is_not_repeated(self) -> None:
        """同じラベルの失敗が連発しても 1 件にまとめる (障害の連打で埋め尽くさせない)。"""
        fx = _build_fixture("main_hand")
        for _ in range(3):
            await _watch_failure(fx, context="同じ経路", robots=["main_hand"])

        assert fx.state_message("main_hand")["safety"]["failed_tasks"] == [
            "同じ経路 (RuntimeError)"
        ]

    async def test_backlog_caps_and_drops_oldest_first(self) -> None:
        fx = _build_fixture("main_hand")
        for i in range(7):
            await _watch_failure(fx, context=f"経路{i}", robots=["main_hand"])

        # 上限 5 件。古い順 (経路0, 経路1) から捨てられ、新しい 5 件が残る
        assert fx.state_message("main_hand")["safety"]["failed_tasks"] == [
            f"経路{i} (RuntimeError)" for i in range(2, 7)
        ]

    async def test_single_task_can_be_attributed_to_multiple_robots(self) -> None:
        """緊急停止解除の再励磁は全ロボットぶんまとめて 1 本なので、失敗は全員に帰属する。"""
        fx = _build_fixture("main_hand", "sub_hand")
        await _watch_failure(fx, context="全体の処理", robots=["main_hand", "sub_hand"])

        assert fx.state_message("main_hand")["safety"]["failed_tasks"] == [
            "全体の処理 (RuntimeError)"
        ]
        assert fx.state_message("sub_hand")["safety"]["failed_tasks"] == [
            "全体の処理 (RuntimeError)"
        ]

    async def test_reset_only_on_match_start(self) -> None:
        """通常の配信では消えず、`match_start` の前縁リセットでだけ消える。

        `can_manager.reset_rx_down_episodes()` と同じ位置・同じ理由 (前縁リセット)。
        """
        fx = _build_fixture("main_hand")
        await _watch_failure(fx, context="残る失敗", robots=["main_hand"])
        assert fx.state_message("main_hand")["safety"]["failed_tasks"] != []

        # 通常の配信では消えない
        await fx.publish_state()
        assert fx.state_message("main_hand")["safety"]["failed_tasks"] != []

        fx.complete_all_checklists()
        await fx.command({"type": "match_start"})

        assert fx.state_message("main_hand")["safety"]["failed_tasks"] == []


class TestCallSitesWireWatchTask:
    """呼び出し口が `watch_task` を正しい引数で呼ぶこと (配線の検査)。

    `watch_task` 自体の振る舞いは `TestWatchTaskRecordsFailure` が持つので、
    ここでは実配線を差し替えて「呼ばれたか・何を渡したか」だけを見る。
    """

    async def test_e_stop_release_watches_the_reactivate_task_for_all_robots(self) -> None:
        fx = _build_fixture("main_hand", "sub_hand")
        watch = Mock()
        fx.server.watch_task = watch  # type: ignore[method-assign]

        await fx.command({"type": "e_stop"})
        await fx.command({"type": "e_stop_release"})
        await fx.wait_reactivation()

        watch.assert_called_once()
        _task, kwargs = watch.call_args
        assert kwargs["context"] == "緊急停止解除の再励磁"
        # 全ロボットぶんまとめて 1 本のタスクなので、失敗は全ロボットへ帰属させる
        assert set(kwargs["robots"]) == {"main_hand", "sub_hand"}

    async def test_reenergize_motors_watches_the_task_for_that_robot_only(self) -> None:
        fx = _build_fixture("main_hand", "sub_hand")
        watch = Mock()
        fx.server.watch_task = watch  # type: ignore[method-assign]

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        watch.assert_called_once()
        _task, kwargs = watch.call_args
        assert kwargs["context"] == "再励磁"
        # 対象ロボット 1 台だけ (もう一方を巻き込まない)
        assert kwargs["robots"] == ["main_hand"]
