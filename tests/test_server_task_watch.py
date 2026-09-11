from __future__ import annotations

import asyncio
import logging
from unittest.mock import Mock

from lib.sequence.engine import Sequence
from tests.server_fixtures import ServerFixture


class _EmptySequence(Sequence):
    """ステップを 1 つも持たないシーケンス。"""


def _build_fixture(*names: str) -> ServerFixture:
    fx = ServerFixture.build()
    for name in names:
        fx.add_robot(name, _EmptySequence(name))
    return fx


async def _watch_failure(fx: ServerFixture, *, context: str, robots: list[str]) -> None:

    async def _boom() -> None:
        raise RuntimeError("模擬故障")

    task = asyncio.create_task(_boom())
    fx.server.watch_task(task, context=context, robots=robots)
    await asyncio.wait([task])


class TestWatchTaskRecordsFailure:
    async def test_failed_task_appears_in_safety(self) -> None:
        fx = _build_fixture("main_hand", "sub_hand")
        await _watch_failure(fx, context="テスト用タスク", robots=["main_hand"])

        assert fx.state_message("main_hand")["safety"]["failed_tasks"] == [
            "テスト用タスク (RuntimeError)"
        ]
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

        # done コールバックの例外は待ち側へ伝播せず、asyncio のデフォルト例外ハンドラが
        # asyncio ロガーへ "Exception in callback" として出すだけになる。
        assert not any("Exception in callback" in r.message for r in caplog.records)
        assert fx.state_message("main_hand")["safety"]["failed_tasks"] == []

    async def test_duplicate_label_is_not_repeated(self) -> None:
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

        assert fx.state_message("main_hand")["safety"]["failed_tasks"] == [
            f"経路{i} (RuntimeError)" for i in range(2, 7)
        ]

    async def test_single_task_can_be_attributed_to_multiple_robots(self) -> None:
        fx = _build_fixture("main_hand", "sub_hand")
        await _watch_failure(fx, context="全体の処理", robots=["main_hand", "sub_hand"])

        assert fx.state_message("main_hand")["safety"]["failed_tasks"] == [
            "全体の処理 (RuntimeError)"
        ]
        assert fx.state_message("sub_hand")["safety"]["failed_tasks"] == [
            "全体の処理 (RuntimeError)"
        ]

    async def test_reset_only_on_match_start(self) -> None:
        fx = _build_fixture("main_hand")
        await _watch_failure(fx, context="残る失敗", robots=["main_hand"])
        assert fx.state_message("main_hand")["safety"]["failed_tasks"] != []

        await fx.publish_state()
        assert fx.state_message("main_hand")["safety"]["failed_tasks"] != []

        fx.make_ready()
        await fx.command({"type": "match_start"})

        assert fx.state_message("main_hand")["safety"]["failed_tasks"] == []


class TestCallSitesWireWatchTask:
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
        assert kwargs["robots"] == ["main_hand"]
