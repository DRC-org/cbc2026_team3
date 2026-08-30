"""ステップ応答の配信。

配信の約束事は 3 つ。**制御周期を配信の都合で伸ばさない** (受け取りは append
だけ)、**試合中は配らない** (1 通が数十 KB あり、テレメトリの帯域を奪う)、
**解析の失敗でテレメトリ配信ごと止めない** (調整支援は補助機能であって、
ヘルスや緊急停止の配信を巻き添えにしてよい理由が無い)。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from lib.can_manager import CANManager
from lib.sequence.engine import Sequence, step
from lib.tuning.metrics import Sample
from lib.tuning.recorder import Capture, PidSnapshot
from tests.server_fixtures import ServerFixture


class _NoopSequence(Sequence):
    def __init__(self) -> None:
        super().__init__("noop_seq")

    @step("ノーオペ")
    async def noop(self) -> None:
        return None


def _bare_can_manager() -> CANManager:
    mgr = CANManager()
    mgr.add_bus("bus0", MagicMock(), channel="vbroadcast0")
    return mgr


def _capture(motor: str = "y_axis_r", *, positions: list[float] | None = None) -> Capture:
    values = positions if positions is not None else [0.0, 4.0, 8.0, 9.8, 10.0, 10.0]
    return Capture(
        motor=motor,
        captured_at=1700000000.0,
        samples=tuple(
            Sample(t=index * 0.02, target=10.0, position=pos, output=500.0, saturated=False)
            for index, pos in enumerate(values)
        ),
        gains=PidSnapshot(kp=2.0, ki=0.0, kd=0.0, dead_band=1.0),
    )


class _RecordingClient:
    """配信された JSON を溜めるだけのクライアント。"""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send_str(self, msg: str) -> None:
        self.sent.append(msg)

    async def close(self) -> None:
        self.closed = True

    def types(self) -> list[str]:
        import json

        return [json.loads(msg)["type"] for msg in self.sent]

    def of_type(self, name: str) -> list[dict]:
        import json

        return [json.loads(msg) for msg in self.sent if json.loads(msg)["type"] == name]


def _fixture() -> tuple[ServerFixture, _RecordingClient]:
    fx = ServerFixture.build()
    fx.add_robot("main_hand", _NoopSequence(), _bare_can_manager())
    fx.freeze_broadcast()
    client = _RecordingClient()
    fx.attach_clients(client)
    return fx, client


class TestDelivery:
    async def test_capture_is_broadcast_on_the_next_state_publish(self) -> None:
        fx, client = _fixture()
        fx.server.record_tuning_capture("main_hand", _capture())

        await fx.publish_state()

        assert "tuning_capture" in client.types()

    async def test_payload_carries_waveform_metrics_and_advice(self) -> None:
        """3 つを別々に配ると、途中の 1 通を落とした画面が食い違ったまま固まる。"""
        fx, client = _fixture()
        fx.server.record_tuning_capture("main_hand", _capture())

        await fx.publish_state()

        payload = client.of_type("tuning_capture")[0]
        assert payload["robot"] == "main_hand"
        assert payload["motor"] == "y_axis_r"
        assert payload["samples"]["pos"]
        assert payload["metrics"] is not None
        assert payload["advice"]

    async def test_each_capture_is_delivered_once(self) -> None:
        """在庫を消さないと、同じ記録が配信周期ごとに再送されて画面が固まる。"""
        fx, client = _fixture()
        fx.server.record_tuning_capture("main_hand", _capture())

        await fx.publish_state()
        await fx.publish_state()

        assert client.types().count("tuning_capture") == 1

    async def test_multiple_motors_are_delivered_separately(self) -> None:
        fx, client = _fixture()
        fx.server.record_tuning_capture("main_hand", _capture("y_axis_r"))
        fx.server.record_tuning_capture("main_hand", _capture("y_axis_l"))

        await fx.publish_state()

        assert sorted(p["motor"] for p in client.of_type("tuning_capture")) == [
            "y_axis_l",
            "y_axis_r",
        ]


class TestMatchGate:
    async def test_not_broadcast_during_a_match(self) -> None:
        """1 通が数十 KB あり、試合中のテレメトリ帯域を奪う。"""
        fx, client = _fixture()
        fx.enter_match()
        fx.server.record_tuning_capture("main_hand", _capture())

        await fx.publish_state()

        assert "tuning_capture" not in client.types()

    async def test_state_is_still_broadcast_during_a_match(self) -> None:
        """調整支援を止めることが、本来のテレメトリを止める理由になってはならない。"""
        fx, client = _fixture()
        fx.enter_match()
        fx.server.record_tuning_capture("main_hand", _capture())

        await fx.publish_state()

        assert "state" in client.types()

    async def test_match_captures_do_not_pile_up_for_later(self) -> None:
        """試合中に溜めておいて後で流すと、終了直後に古い記録が一気に降る。"""
        fx, client = _fixture()
        fx.enter_match()
        fx.server.record_tuning_capture("main_hand", _capture())
        await fx.publish_state()

        fx.match.match_finish()
        fx.match.match_reset()
        await fx.publish_state()

        assert "tuning_capture" not in client.types()


class TestBacklog:
    async def test_backlog_is_bounded(self) -> None:
        """誰も見ていない間に記録が溜まり続けないようにする。"""
        fx, client = _fixture()
        for index in range(40):
            fx.server.record_tuning_capture("main_hand", _capture(f"motor_{index}"))

        await fx.publish_state()

        assert 0 < client.types().count("tuning_capture") <= 8

    async def test_newest_captures_survive(self) -> None:
        """調整では最後に試した 1 回が最も重要。新しいほうを捨ててはならない。"""
        fx, client = _fixture()
        for index in range(40):
            fx.server.record_tuning_capture("main_hand", _capture(f"motor_{index}"))

        await fx.publish_state()

        assert "motor_39" in {p["motor"] for p in client.of_type("tuning_capture")}


class TestResilience:
    async def test_analysis_failure_does_not_stop_telemetry(self) -> None:
        """調整支援の失敗がヘルスや緊急停止の配信を巻き添えにしてはならない。"""
        fx, client = _fixture()
        broken = Capture(
            motor="y_axis_r",
            captured_at=1700000000.0,
            # 数値でないサンプルを入れて解析を確実に失敗させる
            samples=(Sample(t=0.0, target="x", position=0.0, output=0.0, saturated=False),),  # type: ignore[arg-type]
            gains=PidSnapshot(kp=2.0, ki=0.0, kd=0.0, dead_band=1.0),
        )
        fx.server.record_tuning_capture("main_hand", broken)
        fx.server.record_tuning_capture("main_hand", _capture())

        await fx.publish_state()

        assert "state" in client.types()
        # 壊れた 1 通で健全な記録まで落とさない
        assert client.types().count("tuning_capture") == 1
