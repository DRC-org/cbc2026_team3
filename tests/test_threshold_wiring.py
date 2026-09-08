from __future__ import annotations

import ast
import inspect

from aiohttp.test_utils import TestClient, TestServer

from lib.can_manager import CANManager
from lib.config_schema import _HEALTH_KEYS, DEFAULT_HEALTH, HealthThresholds, _parse_health
from lib.control.position_loop import M3508PositionLoop
from lib.control.sync_monitor import SyncMonitor
from lib.drivers.base import MotorDriver
from lib.sequence.engine import Sequence
from lib.server import RobotServer
from tests.server_fixtures import ServerFixture, require_type


def _default_of(func, name: str) -> object:
    return inspect.signature(func).parameters[name].default


def _default_source(func, name: str) -> str:
    tree = ast.parse(inspect.getsource(func).lstrip())
    definition = tree.body[0]
    assert isinstance(definition, ast.FunctionDef)
    args = definition.args

    positional = args.posonlyargs + args.args
    defaults: dict[str, ast.expr] = {}
    for arg, default in zip(
        positional[len(positional) - len(args.defaults) :], args.defaults, strict=True
    ):
        defaults[arg.arg] = default
    for arg, kw_default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        if kw_default is not None:
            defaults[arg.arg] = kw_default

    if name not in defaults:
        raise AssertionError(f"{func.__qualname__} に既定値つきの引数 {name} が無い")
    return ast.unparse(defaults[name])


def _body_references(func, dotted: str) -> bool:
    tree = ast.parse(inspect.getsource(func).lstrip())
    return any(
        isinstance(node, ast.Attribute) and ast.unparse(node) == dotted for node in ast.walk(tree)
    )


class TestDefaultsComeFromConfigSchema:
    def test_can_manager_health_takes_thresholds_object(self) -> None:
        assert _default_of(CANManager.health, "thresholds") is DEFAULT_HEALTH

    def test_robot_server_takes_thresholds_object(self) -> None:
        assert _default_of(RobotServer.__init__, "health") is DEFAULT_HEALTH

    def test_sync_monitor_feedback_timeout_default(self) -> None:
        assert (
            _default_source(SyncMonitor.__init__, "feedback_timeout_ms")
            == "DEFAULT_HEALTH.feedback_timeout_ms"
        )

    def test_position_loop_feedback_timeout_default(self) -> None:
        assert (
            _default_source(M3508PositionLoop.__init__, "feedback_timeout_ms")
            == "DEFAULT_HEALTH.feedback_timeout_ms"
        )

    def test_yaml_省略時の_fallback_も参照で書かれている(self) -> None:
        for key in _HEALTH_KEYS:
            assert _body_references(_parse_health, f"DEFAULT_HEALTH.{key}"), (
                f"health.{key} 省略時の既定値が DEFAULT_HEALTH を参照していない"
            )


class _NoStepSequence(Sequence):
    """ステップを 1 つも持たないシーケンス。"""


class _RecordingCANManager:
    def __init__(self) -> None:
        self.received: HealthThresholds | None = None

    def health(self, *, thresholds: HealthThresholds) -> None:
        self.received = thresholds
        raise RuntimeError("しきい値の記録だけが目的")


class TestServerForwardsThresholdsAsOneUnit:
    def test_compute_health_forwards_injected_thresholds(self) -> None:
        thresholds = HealthThresholds(
            feedback_timeout_ms=11.0,
            temp_warning_c=22.0,
            temp_critical_c=33.0,
            tx_error_threshold=44,
        )
        fx = ServerFixture.build(health=thresholds)
        mgr = _RecordingCANManager()
        fx.add_robot("r", _NoStepSequence("r"), mgr)  # type: ignore[arg-type]

        fx.health("r")

        assert mgr.received is thresholds


class TestThermalWarningTakesOnlyWarningThreshold:
    def test_signature_has_no_critical_argument(self) -> None:
        params = inspect.signature(MotorDriver.has_thermal_warning).parameters
        assert list(params) == ["self", "temp_warning_c"]


class TestServerInfoCarriesTempThresholds:
    async def test_config_values_reach_the_client(self) -> None:
        thresholds = HealthThresholds(
            feedback_timeout_ms=11.0,
            temp_warning_c=22.0,
            temp_critical_c=33.0,
            tx_error_threshold=44,
        )
        fx = ServerFixture.build(health=thresholds)

        async with TestClient(TestServer(fx.create_app())) as client:
            ws = await client.ws_connect("/ws")
            msg = await require_type(ws, "server_info")
            assert msg["temp_warning_c"] == 22.0
            assert msg["temp_critical_c"] == 33.0
            await ws.close()

    async def test_unused_thresholds_are_not_broadcast(self) -> None:
        fx = ServerFixture.build()

        async with TestClient(TestServer(fx.create_app())) as client:
            ws = await client.ws_connect("/ws")
            msg = await require_type(ws, "server_info")
            assert "feedback_timeout_ms" not in msg
            assert "tx_error_threshold" not in msg
            await ws.close()
