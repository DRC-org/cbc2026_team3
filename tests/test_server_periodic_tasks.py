"""`RobotServer._periodic_tasks` が周期タスクを漏れなく数え上げるか。

`match_start` (前縁リセット) と `match_finish` (集計 1 行 + リセット) の 2 箇所が
これを呼んで実周期の乱れを扱う。並べる箇所を 1 つにまとめてあるのは「別々に書くと
書き写しで食い違う」危険を消すためだが、**まとめること自体は「足し忘れる」危険までは
消さない** —— 実際に `LimitGuard` (4 種目) を足したとき、この関数のタプルへ書き足す
だけで安心してしまい、周期タスクとして数えられないまま (=`match_finish` の journal に
1 行足りないまま) しばらく残っていた。ここではその「足し忘れ」を再発させないよう、
`ctx.limit_guard` を持つとき / 持たない (`limits:` を書いた軸が無いロボットでは `None`)
ときの両方を直接固定する。
"""

from __future__ import annotations

from lib.control.limit_guard import LimitGuard
from lib.sequence.engine import Sequence, step
from lib.server import RobotContext, RobotServer
from tests.fake_can import mock_can_manager


class _DummySequence(Sequence):
    @step("ノーオペ")
    async def noop(self) -> None:
        return None


def _limit_guard() -> LimitGuard:
    """判定対象の軸を持たない `LimitGuard`。数え上げの対象になるかだけを見るので、
    センサ読み取り・軸ハンドルの中身はここでは呼ばれない (呼ばれたら別のバグ)。
    """
    return LimitGuard(
        [],
        sensor_active=lambda _name: False,
        sensor_contact_count=lambda _name: 0,
        sensor_is_stale=lambda _name: False,
        axis_handle=lambda _name: None,
    )


def _context(*, limit_guard: LimitGuard | None) -> RobotContext:
    return RobotContext(
        sequence=_DummySequence("main_hand"),
        can_manager=mock_can_manager(()),
        limit_guard=limit_guard,
    )


class TestLimitGuardIsCountedAsAPeriodicTask:
    def test_limit_guard_を持つときはタプルへ含める(self) -> None:
        guard = _limit_guard()
        ctx = _context(limit_guard=guard)

        tasks = RobotServer._periodic_tasks(ctx)

        assert guard in tasks

    def test_limit_guard_がNoneのときは落ちずに他だけを返す(self) -> None:
        """`limits:` を書いた軸が 1 本も無いロボット (limit_guard=None) の構成。

        None をそのまま展開しようとすると `TypeError` になるので、除いてから
        展開できていることを固定する。
        """
        ctx = _context(limit_guard=None)

        tasks = RobotServer._periodic_tasks(ctx)

        assert None not in tasks
        assert tasks == ()
