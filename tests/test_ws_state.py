from __future__ import annotations

from lib.ws_state import StateThinner


def _message(**overrides: object) -> dict:
    message: dict = {
        "type": "state",
        "robot": "main_hand",
        "e_stop_active": False,
        "safety": {"sync_violations": []},
        "steps": [{"index": 0, "label": "初期位置へ移動"}],
        "health": {"overall": "ok", "timestamp": 1.0},
        "manual": {
            "mode": "manual",
            "axes": [
                {"name": "y_axis", "value": 1.0, "positions": [{"name": "home", "value": 0.0}]},
                {"name": "gripper", "value": 0.0, "positions": []},
            ],
        },
    }
    message.update(overrides)
    return message


class TestStateThinner:
    def test_最初のフレームは全欄で_full_が立つ(self) -> None:
        out = StateThinner().thin(_message(), now=0.0)

        assert out["full"] is True
        assert out["steps"] == _message()["steps"]

    def test_変わらない欄は落ちるが安全の欄は毎フレーム載る(self) -> None:
        thinner = StateThinner()
        thinner.thin(_message(), now=0.0)

        out = thinner.thin(_message(), now=0.05)

        assert "full" not in out
        assert "steps" not in out, "変わっていないシーケンス定義を毎フレーム送っている"
        # 欠けると「止まっているか」を画面から読めなくなる
        assert out["e_stop_active"] is False
        assert out["safety"] == {"sync_violations": []}
        assert out["robot"] == "main_hand"

    def test_間引いた欄も値が変われば次の周期で届く(self) -> None:
        thinner = StateThinner(slow_fields={"health": 0.25})
        thinner.thin(_message(), now=0.0)

        skipped = thinner.thin(_message(health={"overall": "degraded"}), now=0.05)
        assert "health" not in skipped, "間引く設定の欄が間隔前に出ている"

        later = thinner.thin(_message(health={"overall": "degraded"}), now=0.3)
        assert later["health"] == {"overall": "degraded"}, "間引いた欄が二度と届かない"

    def test_手動軸は変わった軸の変わった欄だけ送る(self) -> None:
        thinner = StateThinner()
        thinner.thin(_message(), now=0.0)
        moved = _message()
        moved["manual"]["axes"][0]["value"] = 2.0

        out = thinner.thin(moved, now=0.05)

        assert out["manual"]["axes"] == [{"name": "y_axis", "value": 2.0}]

    def test_軸の顔ぶれが変わったら手動は丸ごと送る(self) -> None:
        thinner = StateThinner()
        thinner.thin(_message(), now=0.0)
        dropped = _message()
        dropped["manual"]["axes"] = dropped["manual"]["axes"][:1]

        out = thinner.thin(dropped, now=0.05)

        assert [axis["name"] for axis in out["manual"]["axes"]] == ["y_axis"]
        assert out["manual"]["axes"][0]["positions"] == [{"name": "home", "value": 0.0}]

    def test_request_full_で全欄へ戻る(self) -> None:
        thinner = StateThinner()
        thinner.thin(_message(), now=0.0)
        thinner.thin(_message(), now=0.05)

        thinner.request_full()
        out = thinner.thin(_message(), now=0.1)

        assert out["full"] is True
        assert "steps" in out

    def test_基準がずれても定期的に全欄へ戻す(self) -> None:
        thinner = StateThinner(full_resync_s=1.0)
        thinner.thin(_message(), now=0.0)

        assert "full" not in thinner.thin(_message(), now=0.5)
        assert thinner.thin(_message(), now=1.0)["full"] is True
