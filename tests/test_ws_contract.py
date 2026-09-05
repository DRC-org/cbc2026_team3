"""サーバーが実際に配信する WS メッセージを golden ファイルへ焼き付ける。

サーバーと Web UI が「それぞれ想像した契約」を別々にテストしていると、両者の
食い違いは誰にも検出できない。実際に `health_change` から `robot` が抜けたまま
UI 側が `typeof msg.robot === "string"` を受信条件にしており、Python 側は
`target` しか見ず、TS 側はサンプルを自分で捏造していたため、ヘルス異常が実機で
100% 捨てられていることに両方のテストが揃って気付けなかった。

そこで「実物の配信内容」を 1 つの JSON に固定し、Python 側はここで
現在の配信と一致することを、Web 側はこのファイルを読んで型が受理できることを
検証する。サンプルは決して手書きしない (手書きした瞬間に想像の契約へ逆戻りする)。
"""

from __future__ import annotations

import difflib
import json
import os
import pathlib
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from lib.axis_sync import MotorSpec, SyncGroup
from lib.can_manager import CANManager
from lib.control.position_loop import M3508PositionLoop, make_position_pid
from lib.control.sync_monitor import SyncMonitor
from lib.control.target_refresh import GenericTargetRefresher
from lib.drivers.base import ControlMode, MotorState
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import M3508Driver
from lib.health import (
    BusHealth,
    MotorHealth,
)
from lib.manual import ManualController
from lib.match_state import ROLE_PRE_MATCH, ChecklistItem
from lib.sequence.engine import AxisSyncError, Sequence, step
from lib.sequence.motors import MotorGroup, MotorHandle
from lib.sequence.positions import load_position_table
from lib.tuning.metrics import Sample
from lib.tuning.recorder import Capture, PidSnapshot
from tests.fake_can import mock_can_manager, set_motors
from tests.fake_health import ok_health_snapshot
from tests.feedback_frames import feed_generic
from tests.server_fixtures import ServerFixture, require_type, wait_until

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTRACT_PATH = _REPO_ROOT / "web" / "src" / "test" / "ws-contract.json"

#: golden を作り直すときに立てる環境変数。失敗メッセージからそのまま辿れるようにする
UPDATE_ENV = "UPDATE_WS_CONTRACT"

_REGENERATE_HINT = f"{UPDATE_ENV}=1 uv run pytest tests/test_ws_contract.py"

_ROBOT = "main_hand"
_M3508_BUS = "can_m3508"

#: 実行のたびに変わる値の差し替え先。時刻そのものは契約の一部ではないので固定する。
#: 型 (数値か null か) は保つ: null は「まだ受信していない/未完了」という意味を持ち、
#: UI の表示分岐がそこにぶら下がっているため、数値で塗り潰すと契約が変わってしまう。
FIXED_EPOCH = 1700000000.0
FIXED_DURATION_MS = 0.0

_EPOCH_KEYS = frozenset(
    {
        "timestamp",
        "last_tx_at",
        "last_rx_at",
        "last_feedback_at",
        "started_at",
        "finished_at",
        "captured_at",
    }
)
_DURATION_KEYS = frozenset({"feedback_age_ms"})

#: 配信され得るメッセージ型。1 つでも欠けたら golden の意味が無いのでここで固定する
REQUIRED_TYPES = frozenset(
    {
        "state",
        "server_info",
        "match_state",
        "health_change",
        "e_stop_state",
        "command_rejected",
        # 動作確認は進捗も結果も拒否理由も 1 通に載る。4 種類に分けていた頃は、
        # 途中の 1 通を落とした画面が復旧しなかった
        "motor_check_state",
        # PID 調整支援。波形・指標・助言を 1 通で運ぶ
        "tuning_capture",
    }
)


class _ContractSequence(Sequence):
    """golden 用の最小シーケンス (トリガー待ちの有無を両方含める)。"""

    def __init__(self) -> None:
        super().__init__("main_hand_seq")

    @step("初期位置へ移動")
    async def home(self) -> None:
        return None

    @step("ワーク投入待ち", require_trigger=True)
    async def wait_work(self) -> None:
        return None

    # **失敗した形も golden に載せる。** `last_error` が null の形しか無いと、
    # UI が値の入った形を受信条件で弾いても誰も気付けない (到達タイムアウト・
    # 左右ずれ・零点確定失敗はすべてこの形で届く)
    @step("Y 軸を投入位置へ")
    async def fail_on_sync(self) -> None:
        raise AxisSyncError(
            "シーケンス 'main_hand_seq': 軸内のモータ位置がずれています "
            "(y_axis: 偏差 3.100 > 許容 2.000)"
        )


def _contract_capture(positions: list[float], *, target: float) -> Capture:
    """golden 用のステップ応答。

    記録そのものは入力データ (mock のモータ状態と同じ扱い) で、**配信の形は
    実物の `summarize` → `to_payload` → `_fanout` が作る**。ここを手書きの
    ペイロードにすると、UI 側が想像の契約を検証することになる。
    """
    return Capture(
        motor="y_axis_r",
        captured_at=1700000000.0,
        samples=tuple(
            Sample(
                t=round(index * 0.02, 3),
                target=target,
                position=pos,
                output=900.0 - index * 40.0,
                # 飽和を混ぜる。全周期 False だと「飽和した記録」の形が
                # golden に一度も現れず、UI の警告表示を誰も検証しない
                saturated=index < 3,
            )
            for index, pos in enumerate(positions)
        ),
        gains=PidSnapshot(kp=2.0, ki=0.0, kd=0.0, dead_band=1.0),
    )


def _generic_drivers() -> dict[str, GenericDriver]:
    """自作モタドラの 2 枚。**測れる項目が違う 2 種類を必ず両方載せる。**

    サーボ基板 (position) は位置だけを、DC 基板 (duty) は 1 つも測れない
    (仕様書 §3.2)。実ドライバを CANManager へ挿すのは、測定可否の宣言が
    ドライバ側にしか無いため —— モックのままだと「4 値とも数値」の形しか
    golden に現れず、UI が null を受け取れなくても誰も気付けない。

    状態は実機と同じ FEEDBACK フレームで作る (``driver._state`` への直接代入は
    デコード層を丸ごと迂回する)。DC 基板は位置を持たないので DLC=1 で送る。
    """
    gripper = GenericDriver("gripper", can_id=9, control_type=ControlMode.POSITION)
    conveyor = GenericDriver("conveyor", can_id=10, control_type=ControlMode.DUTY)
    feed_generic(gripper, position=5.0, reached=True)
    feed_generic(conveyor)
    return {"gripper": gripper, "conveyor": conveyor}


def _make_can_manager(generics: dict[str, GenericDriver]) -> CANManager:
    mgr = mock_can_manager(
        {
            "y_axis_r": MotorState(position=1500.0, velocity=0.0, current=0.2, temperature=35.0),
            "y_axis_l": MotorState(position=-1500.0, velocity=0.0, current=0.2, temperature=34.5),
        },
        bus_name=_M3508_BUS,
    )
    set_motors(mgr, {**mgr.motors, **generics})
    return mgr


def _sync_group() -> SyncGroup:
    return SyncGroup(
        name="y_axis",
        members=(
            MotorSpec(name="y_axis_r", scale=1.0, offset=0.0),
            MotorSpec(name="y_axis_l", scale=-1.0, offset=0.0),
        ),
        tolerance=5.0,
    )


def _degraded_bus_snapshot(mgr: CANManager):
    snap = ok_health_snapshot(mgr)
    for bus in snap.buses:
        bus.state = BusHealth.DEGRADED
    snap.overall = BusHealth.DEGRADED
    return snap


def _fault_motor_snapshot(mgr: CANManager):
    snap = _degraded_bus_snapshot(mgr)
    for motor in snap.motors:
        if motor.name == "y_axis_r":
            motor.state = MotorHealth.FAULT
            motor.detail = "ドライバが異常フラグを立てています"
    snap.overall = BusHealth.DOWN
    return snap


class _ContractCheckSequence(Sequence):
    """golden 用の最小動作確認シーケンス。ステップ表が配信に載ることを見る。"""

    def __init__(self) -> None:
        super().__init__("motor_check")

    @step("メインハンド 初期姿勢へ")
    async def home(self) -> None:
        return

    @step("サブハンド 電磁弁 6 個 (打音・目視確認)")
    async def valves(self) -> None:
        return


def _manual_controller(
    mgr: CANManager,
    drivers: dict[str, object],
    target_sinks: dict[str, object],
) -> ManualController:
    """手動操縦の軸一覧。**連続操作できる軸とできない軸を両方入れる。**

    片方だけだと ``manual`` が null になる形か、値が入る形のどちらかしか
    golden に現れず、UI が知らないほうの形を受信条件で弾いても誰も気付けない。
    """
    table = load_position_table(
        {
            "axes": {
                "y_axis": {
                    "unit": "mm",
                    "command_unit": "deg",
                    # 左右偏差を配る軸。**揃っている状態 (deviation = 0.0) を載せる**のが
                    # 狙いで、0.0 は JS では falsy なので `deviation ? ... : null` のような
                    # 受信条件を書くと即座に落ちる。null で埋めた golden ではここが素通りする
                    "sync_tolerance": 2.0,
                    "manual": {"min": -2.0, "max": 20.0, "steps": [0.5, 2.0]},
                    "motors": {"y_axis_r": {"scale": 55.0}, "y_axis_l": {"scale": -55.0}},
                },
                "gripper": {"unit": "deg", "command_unit": "deg"},
                # 位置を測れない軸。value が null になる形も golden に載せないと、
                # UI が数値だけを受け付ける条件を書いても誰も気付けない
                "conveyor": {"unit": "duty", "command_mode": "duty", "settle_s": 0.0},
            },
            "positions": {
                "y_axis": {"home": 0.0, "work": 10.0},
                "gripper": {"open": 5.0, "closed": 0.0},
                "conveyor": {"stop": 0.0, "run": 0.3},
            },
        },
        source="<ws-contract>",
    )
    # mock_can_manager の motors は MagicMock なので、逆換算に実ドライバを使う
    # (MagicMock の feedback_position() は JSON にできず、配信そのものが落ちる)
    # M3508 は電流指令しか受け付けないので、目標値は PC 側 PID ループへ迂回させる。
    # 本番 (main.py の _wire_robot_motors) と同じ配線にしないと、golden を作る側だけが
    # 「y_axis へ位置指令を直接送って失敗する」経路になる
    group = MotorGroup()
    for name, driver in drivers.items():
        group.add(MotorHandle(name, driver, mgr, target_sink=target_sinks.get(name)))
    return ManualController(group, table)


def _checklist_definitions() -> dict[str, list[ChecklistItem]]:
    """指差喚呼の項目。空だと checklists の要素構造が golden に現れない。"""
    return {
        ROLE_PRE_MATCH: [
            ChecklistItem(id="y_axis_sync", label="Y 軸の左右が揃っている"),
            ChecklistItem(id="sub_arm_home", label="補助アームが初期位置"),
        ],
    }


_Fixture = tuple[ServerFixture, M3508PositionLoop, SyncMonitor, GenericTargetRefresher]


def _build_fixture() -> _Fixture:
    fx = ServerFixture.build(checklist_definitions=_checklist_definitions())
    generics = _generic_drivers()
    mgr = _make_can_manager(generics)

    loop = M3508PositionLoop(mgr, _M3508_BUS)
    drivers = {"y_axis_r": M3508Driver("y_axis_r", 1), "y_axis_l": M3508Driver("y_axis_l", 2)}
    for name, driver in drivers.items():
        loop.add_motor(name, driver, make_position_pid(2.0))
    loop.add_sync_group(_sync_group())

    monitor = SyncMonitor(
        [_sync_group()],
        drivers,
        last_feedback_at=lambda _name: None,
    )

    # 目標値再送も 1 台ぶん載せる。空リストだと safety.target_refreshers の
    # 要素構造が golden に現れず、UI 側が形を知る手立てが無くなる。
    # **CANManager に挿したのと同じドライバを使う** —— 別インスタンスを作ると、
    # 手動操縦や再送が触るモータと配信に載るモータが別物になる
    gripper = generics["gripper"]
    conveyor = generics["conveyor"]
    refresher = GenericTargetRefresher([MotorHandle("gripper", gripper, mgr)])

    fx.add_robot(
        _ROBOT,
        _ContractSequence(),
        mgr,
        position_loops=[loop],
        sync_monitors=[monitor],
        target_refreshers=[refresher],
        manual=_manual_controller(
            mgr,
            {**drivers, "gripper": gripper, "conveyor": conveyor},
            loop.target_sinks(),
        ),
    )
    # 動作確認は両ハンド統合の 1 本で、どのロボットにも属さない
    fx.set_motor_check_sequence(_ContractCheckSequence())
    # 周期配信に割り込まれるとヘルス差分の基準が動く。起動直後の 1 回だけ走らせ、
    # 以降はテストが明示的に呼んだ配信だけを捕まえる
    fx.freeze_broadcast()
    return fx, loop, monitor, refresher


async def collect_samples() -> dict[str, dict[str, Any]]:
    """実際の RobotServer に配信させたメッセージを型ごとに 1 通ずつ集める。"""
    fx, loop, monitor, refresher = _build_fixture()
    app = fx.create_app()
    samples: dict[str, dict[str, Any]] = {}

    loop.start()
    monitor.start()
    refresher.start()
    try:
        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            # 接続直後のスナップショット (server_info → match_state の順で届く)
            samples["server_info"] = await require_type(ws, "server_info")
            samples["match_state"] = await require_type(ws, "match_state")

            # 手動で 1 軸だけ動かしてから state を採る。target が null のままだと
            # 「手動目標を持っている軸」の形が golden に一度も現れない
            await fx.command({"type": "set_operation_mode", "robot": _ROBOT, "mode": "manual"})
            await fx.command(
                {"type": "manual_set", "robot": _ROBOT, "axis": "y_axis", "value": 4.0}
            )
            # 手動モードのまま採る。半自動へ戻すと target が捨てられ、
            # 「手動目標を持っている軸」の形が golden から消える
            await fx.publish_state()
            samples["state"] = await require_type(ws, "state")

            # 失敗して止まったシーケンス。常駐ループへ直接ジャンプ要求を出すのは、
            # 準備フェーズでは `sequence_jump` がフェーズゲートで弾かれるため
            sequence = fx.sequence(_ROBOT)
            sequence.request_jump(2)
            assert await wait_until(lambda: sequence.last_error is not None), (
                "失敗するステップが実行されなかった"
            )
            await fx.publish_state()
            samples["state_with_last_error"] = await require_type(ws, "state")

            mgr = fx.can_manager(_ROBOT)
            mgr.health.side_effect = lambda **_kwargs: _degraded_bus_snapshot(mgr)
            await fx.publish_state()
            samples["health_change_bus"] = await require_type(ws, "health_change")

            mgr.health.side_effect = lambda **_kwargs: _fault_motor_snapshot(mgr)
            await fx.publish_state()
            samples["health_change"] = await require_type(ws, "health_change")

            # 準備フェーズなので trigger はフェーズゲートで弾かれる
            await ws.send_json({"type": "trigger", "robot": _ROBOT})
            samples["command_rejected"] = await require_type(ws, "command_rejected")

            await fx.publish_e_stop_state()
            samples["e_stop_state"] = await require_type(ws, "e_stop_state")

            await fx.activate_e_stop(reason="同期ずれを検知しました (y_axis)")
            samples["e_stop_state_with_reason"] = await require_type(ws, "e_stop_state")

            # 進捗も結果も拒否理由も 1 通に載る。理由が入った状態を golden にする
            # (error が null の形は接続直後のスナップショットで既に配られている)
            await fx.publish_motor_check_error("緊急停止中のため動作確認を実行できません")
            samples["motor_check_state"] = await require_type(ws, "motor_check_state")

            # ステップ応答は state と同じ配信周期に相乗りする。
            # **指標が出る形と出ない形を両方載せる。** 片方だけだと、UI が
            # 知らないほうの形を受信条件で弾いても誰も気付けない
            fx.server.record_tuning_capture(
                _ROBOT,
                # 行き過ぎと飽和の両方が出る応答にする。助言が 1 種類しか載らないと、
                # UI の重み別表示 (warning / info) の片方が golden に現れない
                _contract_capture([0.0, 3.0, 7.0, 9.6, 13.5, 11.5, 9.8, 10.0], target=10.0),
            )
            await fx.publish_state()
            samples["tuning_capture"] = await require_type(ws, "tuning_capture")

            # 目標が動いていない記録。ステップとして解釈できないので metrics は null
            fx.server.record_tuning_capture(
                _ROBOT, _contract_capture([10.0, 10.0, 10.0], target=10.0)
            )
            await fx.publish_state()
            samples["tuning_capture_not_a_step"] = await require_type(ws, "tuning_capture")

            await ws.close()
    finally:
        await loop.stop()
        await monitor.stop()
        await refresher.stop()

    return samples


def _normalize(value: Any, key: str | None = None) -> Any:
    """実行のたびに変わる値を固定値へ差し替える。

    null は潰さない。「まだ受信していない」「まだ終わっていない」を表す情報であり、
    UI の分岐がそこにぶら下がっているため、数値で塗り潰すと契約自体が変わる。
    """
    if isinstance(value, dict):
        return {k: _normalize(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float):
        if key in _EPOCH_KEYS:
            return FIXED_EPOCH
        if key in _DURATION_KEYS:
            return FIXED_DURATION_MS
    return value


def _build_document(samples: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "$comment": (
            "サーバーが実際に配信した WS メッセージのサンプル。手書き禁止・自動生成。"
            f" 再生成: {_REGENERATE_HINT}"
            " / タイムスタンプ等の変動値は固定値へ差し替えてある (値ではなく構造が契約)。"
        ),
        "$placeholders": {
            "epoch_seconds": FIXED_EPOCH,
            "duration_ms": FIXED_DURATION_MS,
        },
        "samples": {name: _normalize(msg) for name, msg in samples.items()},
    }


def _dump(document: dict[str, Any]) -> str:
    # インデント 2・キー昇順・末尾改行。差分がレビューで読める形を保つ
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


class TestWsContract:
    async def test_contract_matches_actual_broadcast(self) -> None:
        document = _build_document(await collect_samples())
        actual = _dump(document)

        if os.environ.get(UPDATE_ENV) == "1":
            CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONTRACT_PATH.write_text(actual, encoding="utf-8")

        assert CONTRACT_PATH.is_file(), (
            f"{CONTRACT_PATH} がありません。次で生成してください:\n  {_REGENERATE_HINT}"
        )

        expected = CONTRACT_PATH.read_text(encoding="utf-8")
        if expected != actual:
            diff = "".join(
                difflib.unified_diff(
                    expected.splitlines(keepends=True),
                    actual.splitlines(keepends=True),
                    fromfile="ws-contract.json (現在)",
                    tofile="実際の配信内容",
                )
            )
            pytest.fail(
                "WS 配信内容と web/src/test/ws-contract.json が食い違っています。\n"
                "サーバー側の変更が意図通りなら次で再生成し、web/ 側の型と受信条件も"
                "必ず追従させてください:\n"
                f"  {_REGENERATE_HINT}\n\n{diff}"
            )

    async def test_contract_covers_every_broadcast_type(self) -> None:
        """配信し得るメッセージ型が golden に 1 つも欠けていないこと。"""
        assert CONTRACT_PATH.is_file(), f"生成してください: {_REGENERATE_HINT}"
        document = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        covered = {msg["type"] for msg in document["samples"].values()}
        assert covered >= REQUIRED_TYPES, f"golden に無い型: {sorted(REQUIRED_TYPES - covered)}"

    async def test_match_state_carries_timer(self) -> None:
        """タイマーの 3 値が実配信に載っていること。

        golden は再生成で黙らせられるので、UI が読むフィールドは不変条件として
        別に持つ。3 値のどれか 1 つでも落ちると、全デバイスのタイマーが
        「動かない」「上限が分からない」のどちらかになる。
        """
        document = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        timer = document["samples"]["match_state"]["timer"]

        assert isinstance(timer["running"], bool)
        assert isinstance(timer["elapsed_ms"], int)
        # 上限が 0 だと UI 側は残り時間を計算できない (常に時間切れ表示になる)
        assert isinstance(timer["duration_ms"], int) and timer["duration_ms"] > 0

    async def test_health_change_carries_robot_and_target(self) -> None:
        """UI の受信条件が依存するフィールドが実物に載っていること。"""
        document = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        for name in ("health_change", "health_change_bus"):
            sample = document["samples"][name]
            assert isinstance(sample["robot"], str) and sample["robot"]
            assert sample["target"].split(":")[0] in ("bus", "motor")
