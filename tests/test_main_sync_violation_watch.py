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


class TestSyncViolationReason:
    async def _reason(self, *args: object) -> str:
        fx = ServerFixture.build()
        fx.add_robot("main_hand", _EmptySequence("main_hand"))
        captured: list[str | None] = []

        async def _capture(*, reason: str | None = None) -> None:
            captured.append(reason)

        fx.server.activate_e_stop = _capture  # type: ignore[method-assign]

        tasks: set[asyncio.Task[None]] = set()
        on_violation = _make_sync_violation_handler(fx.server, "main_hand", _rotate_table(), tasks)
        on_violation(*args)  # type: ignore[arg-type]
        await asyncio.wait(tasks)

        assert len(captured) == 1
        assert captured[0] is not None
        return captured[0]

    async def test_初回は偏差と許容だけの短い文面(self) -> None:
        reason = await self._reason("rotate", 175.879, 1)
        assert reason == "main_hand の rotate の左右ずれ 175.879deg が 許容 5.000deg を超えました"

    async def test_再発時は回数と次の一手が付く(self) -> None:
        first = await self._reason("rotate", 175.879, 1)
        reason = await self._reason("rotate", 175.879, 3)

        assert reason.startswith(first)
        assert "3 回" in reason
        assert "原点" in reason
        assert "零点合わせ" in reason

    async def test_モータ名もドライバ種別も書き写さない(self) -> None:
        reason = await self._reason("rotate", 175.879, 3)
        assert "rotate_r" not in reason
        assert "rotate_l" not in reason
        assert "EDULITE" not in reason

    async def test_改行を含まない(self) -> None:
        # 理由文は EStopOverlay の 1 要素へそのまま流すので、改行は詰まって消える
        reason = await self._reason("rotate", 175.879, 3)
        assert "\n" not in reason
