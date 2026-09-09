"""吸着パッドの選択をサーバーが持ち、`state` で配る経路 (`suction_pads_set`)。

操縦 UI が 2 台あっても食い違わないよう正はサーバーの 1 集合。**選択は機体を動かさない**
ので緊急停止中も手動中も通す。
"""

from __future__ import annotations

from lib.manual import ManualController
from lib.sequence.engine import Sequence, step
from lib.sequence.motors import MotorGroup
from lib.sequence.positions import load_position_table
from lib.suction import SuctionSelection
from tests.server_fixtures import RecordingClient, ServerFixture

_PADS = ("valve_1", "valve_2", "valve_3")


class _IdleSequence(Sequence):
    @step("何もしない")
    async def noop(self) -> None:
        return


def _manual() -> ManualController:
    """手動モードへ入るためだけの最小構成。"""
    table = load_position_table(
        {
            "axes": {
                "valve_1": {"unit": "on_off", "command_unit": "on_off", "command_mode": "on_off"}
            },
            "positions": {"valve_1": {"open": 1.0, "closed": 0.0}},
        },
        source="<test>",
    )
    return ManualController(MotorGroup(sensor_active=lambda _name: False), table)


def _build() -> tuple[ServerFixture, SuctionSelection]:
    fx = ServerFixture.build()
    fx.freeze_broadcast()
    selection = SuctionSelection.numbered(_PADS)
    fx.add_robot("sub_hand", _IdleSequence("sub_hand"), manual=_manual(), suction=selection)
    fx.add_robot("main_hand", _IdleSequence("main_hand"))
    return fx, selection


def _enabled_in_state(fx: ServerFixture, robot: str) -> list[str]:
    suction = fx.state_message(robot)["suction"]
    return [pad["axis"] for pad in suction["pads"] if pad["enabled"]]


class TestSelection:
    async def test_選んだ弁だけが有効になり_state_に載る(self) -> None:
        fx, selection = _build()

        await fx.command(
            {"type": "suction_pads_set", "robot": "sub_hand", "pads": ["valve_1", "valve_3"]}
        )

        assert selection.enabled() == ("valve_1", "valve_3")
        assert _enabled_in_state(fx, "sub_hand") == ["valve_1", "valve_3"]

    async def test_配信はラベルを持つ_UI_が弁の名前を書き写さないため(self) -> None:
        fx, _ = _build()

        pads = fx.state_message("sub_hand")["suction"]["pads"]

        assert [pad["label"] for pad in pads] == ["1", "2", "3"]
        assert all(pad["enabled"] is True for pad in pads)

    async def test_吸着パッドを持たないロボットは_null(self) -> None:
        fx, _ = _build()

        assert fx.state_message("main_hand")["suction"] is None

    async def test_空の選択も受け付ける(self) -> None:
        fx, selection = _build()

        await fx.command({"type": "suction_pads_set", "robot": "sub_hand", "pads": []})

        assert selection.enabled() == ()
        assert _enabled_in_state(fx, "sub_hand") == []


class TestReject:
    async def test_知らない軸名は拒み選択を変えない(self) -> None:
        fx, selection = _build()
        requester = RecordingClient()
        fx.attach_clients(requester)

        await fx.command(
            {"type": "suction_pads_set", "robot": "sub_hand", "pads": ["valve_1", "pump_vac"]},
            requester=requester,
        )

        rejected = requester.of_type("command_rejected")
        assert len(rejected) == 1
        assert rejected[0]["command"] == "suction_pads_set"
        assert "pump_vac" in rejected[0]["reason"]
        assert selection.enabled() == _PADS

    async def test_吸着パッドを持たないロボットへの指定は拒む(self) -> None:
        fx, _ = _build()
        requester = RecordingClient()
        fx.attach_clients(requester)

        await fx.command(
            {"type": "suction_pads_set", "robot": "main_hand", "pads": []}, requester=requester
        )

        rejected = requester.of_type("command_rejected")
        assert len(rejected) == 1
        assert "main_hand" in rejected[0]["reason"]

    async def test_ロボットを省いた指定は捨てる(self) -> None:
        fx, selection = _build()
        requester = RecordingClient()
        fx.attach_clients(requester)

        await fx.command({"type": "suction_pads_set", "pads": []}, requester=requester)

        assert requester.of_type("command_rejected") == []
        assert selection.enabled() == _PADS


class TestGates:
    async def test_緊急停止中も通す_機体は動かない(self) -> None:
        fx, selection = _build()
        await fx.activate_e_stop(reason="テスト")

        await fx.command({"type": "suction_pads_set", "robot": "sub_hand", "pads": ["valve_2"]})

        assert selection.enabled() == ("valve_2",)

    async def test_手動操縦中も通す(self) -> None:
        fx, selection = _build()
        await fx.command({"type": "set_operation_mode", "robot": "sub_hand", "mode": "manual"})
        assert fx.operation_mode("sub_hand") == "manual"

        await fx.command({"type": "suction_pads_set", "robot": "sub_hand", "pads": ["valve_2"]})

        assert selection.enabled() == ("valve_2",)

    async def test_試合中も通す(self) -> None:
        fx, selection = _build()
        fx.enter_match()

        await fx.command({"type": "suction_pads_set", "robot": "sub_hand", "pads": ["valve_2"]})

        assert selection.enabled() == ("valve_2",)
