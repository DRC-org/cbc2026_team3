"""実機で位置定数を決めるための「今いる位置を控える」口。

控えた値はそのまま `config/<robot>_positions.yaml` へ転記され、機体が動く数値になる。
だから守るのは 2 つ —— **嘘の値を控えさせないこと** (零点未確定・実測が古い・緊急停止中・
コート未確定) と、**控えた値が実測どおりに出てくること** (換算・コート符号・yaml 断片)。
"""

from __future__ import annotations

import time

import pytest
import yaml

from lib.match_state import Court, Phase
from lib.sequence.engine import Sequence, step
from lib.sequence.motors import MotorGroup, MotorHandle
from lib.sequence.positions import load_position_table
from tests.fake_can import keep_feedback_fresh, mock_can_manager, set_last_feedback
from tests.fake_drivers import StubFeedbackDriver
from tests.server_fixtures import DEFAULT_CHECKLIST, RecordingClient, ServerFixture

_ROBOT = "sub_hand"
_MOTORS = ("sub_lift", "sub_y_axis_r", "sub_y_axis_l", "pump_vac")

_POSITIONS = {
    "axes": {
        # mm↔rad の換算だけがコートで鏡になる軸 (実機の sub_lift と同じ形)。
        # mm の座標系は両コート共通で、控える値そのものはコートで変わらない
        "sub_lift": {
            "unit": "mm",
            "command_unit": "rad",
            "scale": {"blue": 2.0, "red": -2.0},
            "offset": 0.0,
        },
        "sub_y_axis": {
            "unit": "mm",
            "command_unit": "rad",
            "motors": {"sub_y_axis_r": {"scale": 4.0}, "sub_y_axis_l": {"scale": -4.0}},
        },
        "pump_vac": {
            "unit": "duty",
            "command_unit": "duty",
            "command_mode": "duty",
        },
    },
    "positions": {
        "sub_lift": {"top": -140.0, "pick": -130.0},
        "sub_y_axis": {"retracted": -430.0, "receive": -10.0},
        "pump_vac": {"stop": 0.0, "run": 0.95},
    },
}


class _IdleSequence(Sequence):
    def __init__(self) -> None:
        super().__init__(_ROBOT)

    @step("待機")
    async def idle(self) -> None:
        return None


def _fixture(*, court: Court | None = Court.BLUE, fresh: bool = True, checklist: bool = False):
    fx = ServerFixture.build(checklist_definitions=DEFAULT_CHECKLIST if checklist else None)
    fx.freeze_broadcast()

    mgr = mock_can_manager(_MOTORS)
    group = MotorGroup()
    drivers: dict[str, StubFeedbackDriver] = {}
    for index, name in enumerate(_MOTORS, start=1):
        driver = StubFeedbackDriver(name, index)
        driver.mark_origin_confirmed()
        drivers[name] = driver
        group.add(MotorHandle(name, driver, mgr))

    sequence = _IdleSequence()
    sequence.bind_positions(load_position_table(_POSITIONS, source="<test>"))
    sequence.bind_motors(group)

    if fresh:
        keep_feedback_fresh(mgr)
    fx.add_robot(_ROBOT, sequence, mgr)
    if court is not None:
        fx.match.set_court(court)
    return fx, drivers, mgr


async def _capture(fx: ServerFixture, axis: str, name: str, client: RecordingClient | None = None):
    await fx.command(
        {"type": "position_capture", "robot": _ROBOT, "axis": axis, "name": name},
        requester=client,
    )


def _entries(fx: ServerFixture) -> list[dict]:
    return fx.state_message(_ROBOT)["position_capture"]["entries"]


def _reason(client: RecordingClient) -> str:
    rejected = client.of_type("command_rejected")
    assert rejected, "拒否が操縦者へ届いていない"
    return rejected[-1]["reason"]


class TestCapturedValue:
    async def test_実測を換算した値を控える(self) -> None:
        fx, drivers, _ = _fixture()
        # 指令の単位 (rad) での実測。mm へ戻すと -120.0
        drivers["sub_lift"].set_observed(position=-240.0)

        await _capture(fx, "sub_lift", "top")

        assert _entries(fx) == [
            {
                "axis": "sub_lift",
                "name": "top",
                "value": pytest.approx(-120.0),
                "unit": "mm",
                "captured_at": pytest.approx(time.time(), abs=5.0),
            }
        ]

    async def test_コートで符号が鏡になる軸は今のコートで換算する(self) -> None:
        fx, drivers, _ = _fixture(court=Court.RED)
        drivers["sub_lift"].set_observed(position=-240.0)

        await _capture(fx, "sub_lift", "top")

        # 赤は scale が -2.0。コートで変わるのは換算だけなので、同じ生角の実測からは
        # 逆符号の mm が出る (どちらのコートでも同じ姿勢なら同じ mm になる)
        assert _entries(fx)[0]["value"] == pytest.approx(120.0)

    async def test_左右ペアは両方の実測から軸の値を出す(self) -> None:
        fx, drivers, _ = _fixture()
        drivers["sub_y_axis_r"].set_observed(position=-40.0)
        drivers["sub_y_axis_l"].set_observed(position=44.0)

        await _capture(fx, "sub_y_axis", "receive")

        # 右 -40/4 = -10.0、左 44/-4 = -11.0。平均 -10.5
        assert _entries(fx)[0]["value"] == pytest.approx(-10.5)

    async def test_同じ位置名を控え直すと上書きする(self) -> None:
        fx, drivers, _ = _fixture()
        drivers["sub_lift"].set_observed(position=-240.0)
        await _capture(fx, "sub_lift", "top")
        drivers["sub_lift"].set_observed(position=-260.0)
        await _capture(fx, "sub_lift", "top")

        entries = _entries(fx)
        assert len(entries) == 1
        assert entries[0]["value"] == pytest.approx(-130.0)


class TestRejects:
    async def test_零点が未確定の軸は控えられない(self) -> None:
        fx, drivers, _ = _fixture()
        drivers["sub_lift"]._origin_confirmed = False
        client = RecordingClient()
        fx.attach_clients(client)

        await _capture(fx, "sub_lift", "top", client)

        assert _entries(fx) == []
        assert "零点が未確定" in _reason(client)
        assert "零点合わせ" in _reason(client)

    async def test_ペアの片方だけ零点未確定でも控えられない(self) -> None:
        fx, drivers, _ = _fixture()
        drivers["sub_y_axis_l"]._origin_confirmed = False
        client = RecordingClient()
        fx.attach_clients(client)

        await _capture(fx, "sub_y_axis", "receive", client)

        assert _entries(fx) == []
        assert "零点が未確定" in _reason(client)

    async def test_実測が古い軸は控えられない(self) -> None:
        fx, _drivers, mgr = _fixture(fresh=False)
        set_last_feedback(mgr, {name: time.time() - 60.0 for name in _MOTORS})
        client = RecordingClient()
        fx.attach_clients(client)

        await _capture(fx, "sub_lift", "top", client)

        assert _entries(fx) == []
        assert "実測が古い" in _reason(client)

    async def test_1_通も受信していない軸は控えられない(self) -> None:
        fx, _drivers, _mgr = _fixture(fresh=False)
        client = RecordingClient()
        fx.attach_clients(client)

        await _capture(fx, "sub_lift", "top", client)

        assert _entries(fx) == []
        assert "実測が古い" in _reason(client)

    async def test_緊急停止中は控えられない(self) -> None:
        fx, drivers, _ = _fixture()
        drivers["sub_lift"].set_observed(position=-240.0)
        client = RecordingClient()
        fx.attach_clients(client)
        await fx.activate_e_stop(reason="テスト")

        await _capture(fx, "sub_lift", "top", client)

        assert _entries(fx) == []
        assert "緊急停止中" in _reason(client)

    async def test_コート未確定では控えられない(self) -> None:
        fx, drivers, _ = _fixture(court=None)
        drivers["sub_lift"].set_observed(position=-240.0)
        client = RecordingClient()
        fx.attach_clients(client)

        await _capture(fx, "sub_lift", "top", client)

        assert _entries(fx) == []
        assert "コートが未設定" in _reason(client)

    async def test_試合中は控えられない(self) -> None:
        fx, drivers, _ = _fixture(checklist=True)
        drivers["sub_lift"].set_observed(position=-240.0)
        fx.enter_match()
        assert fx.match.phase is Phase.MATCH
        client = RecordingClient()
        fx.attach_clients(client)

        await _capture(fx, "sub_lift", "top", client)

        assert _entries(fx) == []
        assert "試合中" in _reason(client)

    async def test_位置定数に無い名前は受けない(self) -> None:
        fx, _drivers, _ = _fixture()
        client = RecordingClient()
        fx.attach_clients(client)

        await _capture(fx, "sub_lift", "tpo", client)

        assert _entries(fx) == []
        assert "定義されていません" in _reason(client)
        # 使える名前を返さないと、操縦者は打ち直す先が分からない
        assert "top" in _reason(client)

    async def test_位置の実測を持たない軸は受けない(self) -> None:
        fx, _drivers, _ = _fixture()
        client = RecordingClient()
        fx.attach_clients(client)

        await _capture(fx, "pump_vac", "run", client)

        assert _entries(fx) == []
        assert "位置を控えられません" in _reason(client)

    async def test_知らない軸は受けない(self) -> None:
        fx, _drivers, _ = _fixture()
        client = RecordingClient()
        fx.attach_clients(client)

        await _capture(fx, "sub_pitch", "open", client)

        assert _entries(fx) == []
        assert "sub_pitch" in _reason(client)


class TestYamlFragment:
    async def test_控えた値がそのまま位置定数へ貼れる(self) -> None:
        fx, drivers, _ = _fixture()
        drivers["sub_lift"].set_observed(position=-240.0)
        drivers["sub_y_axis_r"].set_observed(position=-40.0)
        drivers["sub_y_axis_l"].set_observed(position=40.0)
        await _capture(fx, "sub_lift", "top")
        await _capture(fx, "sub_y_axis", "receive")

        fragment = fx.state_message(_ROBOT)["position_capture"]["yaml"]

        # `positions:` の下へ貼った形が、控えた値どおりに読み戻せる
        assert yaml.safe_load("positions:\n" + fragment) == {
            "positions": {
                "sub_lift": {"top": -120.0},
                "sub_y_axis": {"receive": -10.0},
            }
        }
        assert "# [mm]" in fragment

    async def test_控えが無ければ断片は出さない(self) -> None:
        fx, _drivers, _ = _fixture()
        # 「まだ 1 つも控えていない」を空文字で埋めない
        assert fx.state_message(_ROBOT)["position_capture"]["yaml"] is None


class TestTargets:
    async def test_控えられる軸と位置名をサーバーが配る(self) -> None:
        fx, _drivers, _ = _fixture()

        targets = fx.state_message(_ROBOT)["position_capture"]["targets"]

        # UI が位置名を書き写さないための配信。duty 軸は位置を控えられないので出ない
        assert targets == {
            "sub_lift": ["top", "pick"],
            "sub_y_axis": ["retracted", "receive"],
        }

    async def test_位置定数を持たないロボットは配信が_null(self) -> None:
        fx = ServerFixture.build()
        fx.freeze_broadcast()
        fx.add_robot("main_hand", _IdleSequence())

        assert fx.state_message("main_hand")["position_capture"] is None
