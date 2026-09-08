from __future__ import annotations

import asyncio
from unittest.mock import Mock

from lib.sequence.engine import Sequence
from lib.sequence.positions import load_position_table
from main import _make_sync_violation_handler
from tests.server_fixtures import ServerFixture


class _EmptySequence(Sequence):
    """ステップを 1 つも持たないシーケンス。"""


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

        watch.assert_called_once()
        args, kwargs = watch.call_args
        assert isinstance(args[0], asyncio.Task)
        assert kwargs["context"] == "main_hand の同期ずれ検出 → 緊急停止"
        assert tuple(kwargs["robots"]) == fx.robot_names

        await asyncio.wait(tasks)

    async def test_failure_is_attributed_to_both_robots(self) -> None:
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
        assert fx.state_message("main_hand")["safety"]["failed_tasks"] == expected
        assert fx.state_message("sub_hand")["safety"]["failed_tasks"] == expected
