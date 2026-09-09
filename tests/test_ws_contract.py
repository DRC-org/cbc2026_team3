from __future__ import annotations

import difflib
import json
import os
import pathlib
import time
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from lib.axis_sync import MotorSpec, SyncGroup
from lib.can_manager import CANManager
from lib.control.limit_monitor import LimitMonitor
from lib.control.periodic import PeriodicTask
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
from lib.match_state import ROLE_PRE_MATCH, ChecklistItem, Court
from lib.sequence.engine import AxisSyncError, Sequence, step
from lib.sequence.homing import HomingError, SwitchMeasurement
from lib.sequence.motors import MotorGroup, MotorHandle
from lib.sequence.positions import AxisSpec, load_position_table
from lib.server_homing import HomingSource
from lib.suction import SuctionSelection
from tests.fake_can import mock_can_manager, set_last_feedback, set_motors, set_sensors
from tests.fake_health import ok_health_snapshot
from tests.feedback_frames import feed_generic
from tests.server_fixtures import ServerFixture, drain, require_type, wait_until

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTRACT_PATH = _REPO_ROOT / "web" / "src" / "test" / "ws-contract.json"

UPDATE_ENV = "UPDATE_WS_CONTRACT"

_REGENERATE_HINT = f"{UPDATE_ENV}=1 uv run pytest tests/test_ws_contract.py"

_ROBOT = "main_hand"
_M3508_BUS = "can_m3508"

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

REQUIRED_TYPES = frozenset(
    {
        "state",
        "server_info",
        "match_state",
        "health_change",
        "e_stop_state",
        "command_rejected",
        "motor_check_state",
        "homing_state",
        "switch_measure_state",
    }
)


class _ContractSequence(Sequence):
    def __init__(self) -> None:
        super().__init__("main_hand_seq")

    @step("初期位置へ移動")
    async def home(self) -> None:
        return None

    @step("ワーク投入待ち", require_trigger=True)
    async def wait_work(self) -> None:
        return None

    @step("Y 軸を投入位置へ")
    async def fail_on_sync(self) -> None:
        raise AxisSyncError(
            "シーケンス 'main_hand_seq': 軸内のモータ位置がずれています "
            "(y_axis: 偏差 3.100 > 許容 2.000)"
        )


def _generic_drivers() -> dict[str, GenericDriver]:
    gripper = GenericDriver("gripper", can_id=9, control_type=ControlMode.POSITION)
    conveyor = GenericDriver("conveyor", can_id=10, control_type=ControlMode.DUTY)
    feed_generic(gripper, position=5.0, reached=True)
    feed_generic(conveyor)
    return {"gripper": gripper, "conveyor": conveyor}


def _sensor_drivers() -> dict[str, GenericDriver]:
    touching = GenericDriver("origin_sensor", can_id=0x44, control_type=ControlMode.POSITION)
    released = GenericDriver("rotate_origin_sensor", can_id=0x43, control_type=ControlMode.POSITION)
    feed_generic(touching, sensor=True)
    feed_generic(released, sensor=False)
    return {"origin_sensor": touching, "rotate_origin_sensor": released}


def _make_can_manager(
    generics: dict[str, GenericDriver], sensors: dict[str, GenericDriver]
) -> CANManager:
    mgr = mock_can_manager(
        {
            "y_axis_r": MotorState(position=1500.0, velocity=0.0, current=0.2, temperature=35.0),
            "y_axis_l": MotorState(position=-1500.0, velocity=0.0, current=0.2, temperature=34.5),
        },
        bus_name=_M3508_BUS,
    )
    set_motors(mgr, {**mgr.motors, **generics})
    set_sensors(mgr, sensors)
    set_last_feedback(mgr, {"origin_sensor": time.time()})
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
    def __init__(self) -> None:
        super().__init__("motor_check")

    @step("メインハンド 初期姿勢へ", axes={"y_axis"})
    async def home(self) -> None:
        return

    @step("サブハンド 電磁弁 6 個 (打音・目視確認)", axes={"valve_1", "valve_2"})
    async def valves(self) -> None:
        return


def _motor_group(
    mgr: CANManager,
    drivers: dict[str, object],
    target_sinks: dict[str, object],
) -> MotorGroup:
    group = MotorGroup()
    for name, driver in drivers.items():
        group.add(MotorHandle(name, driver, mgr, target_sink=target_sinks.get(name)))
    return group


def _manual_controller(group: MotorGroup) -> ManualController:
    table = load_position_table(
        {
            "axes": {
                "y_axis": {
                    "unit": "mm",
                    "command_unit": "deg",
                    "sync_tolerance": 2.0,
                    "manual": {"min": -2.0, "max": 20.0, "steps": [0.5, 2.0]},
                    "motors": {"y_axis_r": {"scale": 55.0}, "y_axis_l": {"scale": -55.0}},
                },
                "gripper": {"unit": "deg", "command_unit": "deg"},
                # manual_always が真になる唯一の軸。golden に真の形が無いと、UI が
                # 真を受け取れなくても誰も気付けない
                "conveyor": {
                    "unit": "duty",
                    "command_mode": "duty",
                    "settle_s": 0.0,
                    "manual_always": True,
                },
            },
            "positions": {
                "y_axis": {"home": 0.0, "work": 10.0},
                "gripper": {"open": 5.0, "closed": 0.0},
                "conveyor": {"stop": 0.0, "run": 0.3},
            },
        },
        source="<ws-contract>",
    )
    return ManualController(group, table)


def _suction_selection() -> SuctionSelection:
    """一部だけ ON の形を golden に固定する (全部 ON だと enabled の false が現れない)。"""
    selection = SuctionSelection.numbered(("valve_1", "valve_2"))
    assert selection.select(["valve_2"]) is None
    return selection


class _ContractHoming:
    """`HomingRunner` の代役。

    零点合わせは原点へ届かず失敗し、作動点測定は探索方向 (-1) だけ測れて反対側 (+1) は
    届かない。UI が「失敗した軸と理由」と「実測の形」の両方を受け取れるかを golden で
    固定する。
    """

    async def home(self, spec: AxisSpec, _handle: object) -> float:
        raise HomingError(
            f"軸 '{spec.name}' が 5.0mm 動かしても原点センサ 'origin_sensor' に"
            " 到達しませんでした (探索方向・機構の引っかかり・センサの配線を確認してください)"
        )

    async def measure(
        self,
        spec: AxisSpec,
        _handle: object,
        *,
        direction: float,
        step: float | None = None,
        coarse_step: float | None = None,
        limit: float | None = None,
    ) -> SwitchMeasurement:
        if direction > 0:
            raise HomingError(
                f"軸 '{spec.name}' が 5.0mm 動かしてもリミットスイッチ 'origin_sensor' に"
                " 到達しませんでした (探索方向・機構の引っかかり・センサの配線を確認してください)"
            )
        return SwitchMeasurement(
            axis=spec.name,
            unit=spec.unit,
            direction=direction,
            engage=-447.0,
            release=-441.2,
            width=5.8,
            step=step if step is not None else 1.0,
            coarse_step=coarse_step,
        )


def _homing_source(group: MotorGroup) -> HomingSource:
    table = load_position_table(
        {
            "axes": {
                "y_axis": {
                    "unit": "mm",
                    "command_unit": "deg",
                    "homing": {
                        "sensor": "origin_sensor",
                        "direction": -1,
                        "search_distance": 5.0,
                        "step": 1.0,
                    },
                    "motors": {"y_axis_r": {"scale": 55.0}, "y_axis_l": {"scale": -55.0}},
                }
            },
            "positions": {"y_axis": {"home": 0.0}},
        },
        source="<ws-contract>",
    )
    return HomingSource(
        runner=_ContractHoming(),  # type: ignore[arg-type]
        table=table,
        motors=group,
        court=lambda: Court.RED,
        axes_by_robot={_ROBOT: ("y_axis",)},
    )


def _limit_monitor(group: MotorGroup) -> LimitMonitor:
    """可動端監視 1 本。両端のセンサを宣言した軸の形を golden に固定する。"""
    table = load_position_table(
        {
            "axes": {
                "gripper": {
                    "unit": "deg",
                    "command_unit": "deg",
                    "guard": {
                        "limits": {"plus": "rotate_origin_sensor", "minus": "origin_sensor"},
                        "max_step": 100.0,
                    },
                }
            },
            "positions": {"gripper": {"open": 5.0, "closed": 0.0}},
        },
        source="<ws-contract>",
    )
    return LimitMonitor(
        table,
        group,
        sensor_active=lambda name: name == "origin_sensor",
        sensor_contact_count=lambda _name: 0,
        court=lambda: Court.RED,
    )


def _checklist_definitions() -> dict[str, list[ChecklistItem]]:
    return {
        ROLE_PRE_MATCH: [
            ChecklistItem(id="y_axis_sync", label="Y 軸の左右が揃っている"),
            ChecklistItem(id="sub_arm_home", label="補助アームが初期位置"),
        ],
    }


_Fixture = tuple[ServerFixture, tuple[PeriodicTask, ...], MotorGroup]


def _build_fixture() -> _Fixture:
    fx = ServerFixture.build(checklist_definitions=_checklist_definitions())
    generics = _generic_drivers()
    mgr = _make_can_manager(generics, _sensor_drivers())

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

    group = _motor_group(
        mgr,
        {**drivers, "gripper": generics["gripper"], "conveyor": generics["conveyor"]},
        loop.target_sinks(),
    )
    refresher = GenericTargetRefresher([group["gripper"], group["conveyor"]])
    limit_monitor = _limit_monitor(group)

    sequence = _ContractSequence()
    sequence.bind_motors(group)

    fx.add_robot(
        _ROBOT,
        sequence,
        mgr,
        position_loops=[loop],
        sync_monitors=[monitor],
        limit_monitors=[limit_monitor],
        target_refreshers=[refresher],
        manual=_manual_controller(group),
        suction=_suction_selection(),
    )
    fx.set_motor_check_sequence(_ContractCheckSequence())
    fx.freeze_broadcast()
    return fx, (loop, monitor, limit_monitor, refresher), group


async def collect_samples() -> dict[str, dict[str, Any]]:
    fx, tasks, group = _build_fixture()
    app = fx.create_app()
    samples: dict[str, dict[str, Any]] = {}

    for task in tasks:
        task.start()
    try:
        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            samples["server_info"] = await require_type(ws, "server_info")
            samples["match_state"] = await require_type(ws, "match_state")

            await fx.command({"type": "set_operation_mode", "robot": _ROBOT, "mode": "manual"})
            await fx.command(
                {"type": "manual_set", "robot": _ROBOT, "axis": "y_axis", "value": 4.0}
            )
            await group["conveyor"].set_target(ControlMode.DUTY, 0.3)
            await fx.publish_state()
            samples["state"] = await require_type(ws, "state")

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

            await ws.send_json({"type": "trigger", "robot": _ROBOT})
            samples["command_rejected"] = await require_type(ws, "command_rejected")

            fx.set_homing_source(_homing_source(group))
            await fx.command({"type": "set_operation_mode", "robot": _ROBOT, "mode": "sequence"})
            await fx.start_homing(_ROBOT, ["y_axis"])
            await fx.wait_homing_idle()
            # 走り終えた形を採る (進捗の途中経過も同じ型で流れるので最後の 1 通を選ぶ)
            samples["homing_state"] = [
                msg for msg in await drain(ws) if msg.get("type") == "homing_state"
            ][-1]

            await fx.publish_switch_measure_state()
            samples["switch_measure_state"] = await require_type(ws, "switch_measure_state")
            for name, direction in (("with_result", -1), ("with_error", 1)):
                await fx.start_switch_measure(
                    {"robot": _ROBOT, "axis": "y_axis", "direction": direction}
                )
                await fx.wait_switch_measure_idle()
                samples[f"switch_measure_state_{name}"] = [
                    msg for msg in await drain(ws) if msg.get("type") == "switch_measure_state"
                ][-1]
            await fx.command({"type": "set_operation_mode", "robot": _ROBOT, "mode": "manual"})

            await fx.publish_e_stop_state()
            samples["e_stop_state"] = await require_type(ws, "e_stop_state")

            await fx.activate_e_stop(reason="同期ずれを検知しました (y_axis)")
            samples["e_stop_state_with_reason"] = await require_type(ws, "e_stop_state")

            await fx.publish_motor_check_error("緊急停止中のため動作確認を実行できません")
            samples["motor_check_state"] = await require_type(ws, "motor_check_state")

            restricted = _ContractCheckSequence()
            restricted.restrict_to_axes({"y_axis"})
            fx.set_motor_check_sequence(restricted)
            await fx.publish_motor_check_error(
                "サブハンドの軸が構成にありません (動作確認はメインハンドのみ)"
            )
            samples["motor_check_state_with_exclusions"] = await require_type(
                ws, "motor_check_state"
            )

            await ws.close()
    finally:
        for task in tasks:
            await task.stop()

    return samples


def _normalize(value: Any, key: str | None = None) -> Any:
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
        assert CONTRACT_PATH.is_file(), f"生成してください: {_REGENERATE_HINT}"
        document = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        covered = {msg["type"] for msg in document["samples"].values()}
        assert covered >= REQUIRED_TYPES, f"golden に無い型: {sorted(REQUIRED_TYPES - covered)}"

    async def test_match_state_carries_timer(self) -> None:
        document = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        timer = document["samples"]["match_state"]["timer"]

        assert isinstance(timer["running"], bool)
        assert isinstance(timer["elapsed_ms"], int)
        assert isinstance(timer["duration_ms"], int) and timer["duration_ms"] > 0

    async def test_health_change_carries_robot_and_target(self) -> None:
        document = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        for name in ("health_change", "health_change_bus"):
            sample = document["samples"][name]
            assert isinstance(sample["robot"], str) and sample["robot"]
            assert sample["target"].split(":")[0] in ("bus", "motor")
