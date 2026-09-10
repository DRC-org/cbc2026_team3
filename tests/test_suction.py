"""吸着に使うパッドの選択 (`lib/suction.py`)。

正はサーバーが持つ集合 1 つ。**選ばれていない弁を開けない**ことと、**知らない軸名を
黙って受け付けない**ことが要。
"""

from __future__ import annotations

import pytest

from lib.match_state import Court
from lib.suction import SuctionPad, SuctionSelection, suction_of

_AXES = ("valve_1", "valve_2", "valve_3")


class TestDefault:
    def test_既定は全部有効(self) -> None:
        selection = SuctionSelection.numbered(_AXES)

        assert selection.enabled() == _AXES
        assert all(selection.is_enabled(axis) for axis in _AXES)

    def test_番号付けはパッドの並び順(self) -> None:
        selection = SuctionSelection.numbered(_AXES)

        assert [pad.label for pad in selection.pads] == ["1", "2", "3"]

    def test_パッドが無い選択は作れない(self) -> None:
        with pytest.raises(ValueError):
            SuctionSelection(())

    def test_軸名の重複は作れない(self) -> None:
        with pytest.raises(ValueError):
            SuctionSelection([SuctionPad("valve_1", "1"), SuctionPad("valve_1", "2")])


class TestSelect:
    def test_一部だけ有効にできる(self) -> None:
        selection = SuctionSelection.numbered(_AXES)

        assert selection.select(["valve_3", "valve_1"]) is None

        assert selection.enabled() == ("valve_1", "valve_3")
        assert selection.is_enabled("valve_2") is False

    def test_有効な軸の並びはパッドの並び順(self) -> None:
        selection = SuctionSelection.numbered(_AXES)
        selection.select(["valve_3", "valve_1"])

        assert selection.enabled() == ("valve_1", "valve_3")

    def test_空の選択は通す(self) -> None:
        selection = SuctionSelection.numbered(_AXES)

        assert selection.select([]) is None
        assert selection.enabled() == ()

    def test_知らない軸名は拒み選択を変えない(self) -> None:
        selection = SuctionSelection.numbered(_AXES)
        selection.select(["valve_1"])

        reason = selection.select(["valve_1", "pump_vac"])

        assert reason is not None and "pump_vac" in reason
        assert selection.enabled() == ("valve_1",)

    @pytest.mark.parametrize("bad", [None, "valve_1", 1, ["valve_1", 2], {"valve_1": True}])
    def test_軸名の配列でなければ拒む(self, bad: object) -> None:
        selection = SuctionSelection.numbered(_AXES)

        reason = selection.select(bad)

        assert reason is not None
        assert selection.enabled() == _AXES


class TestPayload:
    def test_配信はパッドごとに軸名とラベルと有効を持つ(self) -> None:
        selection = SuctionSelection.numbered(_AXES)
        selection.select(["valve_2"])

        assert selection.to_dict() == {
            "pads": [
                {"axis": "valve_1", "label": "1", "enabled": False},
                {"axis": "valve_2", "label": "2", "enabled": True},
                {"axis": "valve_3", "label": "3", "enabled": False},
            ],
            "fill_from": None,
        }

    @pytest.mark.parametrize(
        ("court", "fill_from"), [(Court.RED, "left"), (Court.BLUE, "right"), (None, None)]
    )
    def test_ON_にしていく端はコートから決め_未確定なら断定しない(
        self, court: Court | None, fill_from: str | None
    ) -> None:
        assert SuctionSelection.numbered(_AXES).to_dict(court=court)["fill_from"] == fill_from


class TestSuctionOf:
    def test_選択を持つシーケンスからそれを取り出す(self) -> None:
        selection = SuctionSelection.numbered(_AXES)

        class _Seq:
            suction = selection

        assert suction_of(_Seq()) is selection

    def test_持たないシーケンスは_None(self) -> None:
        class _Seq:
            pass

        class _Wrong:
            suction = ("valve_1",)

        assert suction_of(_Seq()) is None
        assert suction_of(_Wrong()) is None
