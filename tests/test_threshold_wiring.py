"""ヘルス判定しきい値の既定値が lib/config_schema.py の 1 箇所にしかないことを検証する。

同じ既定値を各モジュールがリテラルで持つと、config を配線し忘れた経路だけが
無言で古い値に戻る。しきい値は「壊れる前に止める」判定の境界なので、
経路によって境界が違う状態は事故そのものになる。
"""

from __future__ import annotations

import ast
import inspect

from lib.can_manager import CANManager
from lib.config_schema import DEFAULT_HEALTH, HealthThresholds
from lib.control.position_loop import M3508PositionLoop
from lib.control.sync_monitor import SyncMonitor
from lib.drivers.base import MotorDriver
from lib.motor_check import MotorCheckRunner
from lib.sequence.engine import Sequence
from lib.server import RobotServer
from tests.server_fixtures import ServerFixture


def _default_of(func, name: str) -> object:
    return inspect.signature(func).parameters[name].default


def _default_source(func, name: str) -> str:
    """引数の既定値が「どう書かれているか」を返す。

    値の一致だけを見るとリテラルで書き直しても通ってしまい、既定値が再び
    分散したことを検出できない。式そのものを見て参照であることを確かめる。
    """
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
    """関数本体がその参照をそのまま書いているか。

    引数の既定値に置けない既定 (None のときだけ使う fallback など) は
    ``_default_source`` では見えない。値の一致で確かめるには本番の private を
    覗くしかなく、テストのためだけに公開 API を生やすことになるので、
    ここでも「どう書かれているか」を見る。
    """
    tree = ast.parse(inspect.getsource(func).lstrip())
    return any(
        isinstance(node, ast.Attribute) and ast.unparse(node) == dotted for node in ast.walk(tree)
    )


class TestDefaultsComeFromConfigSchema:
    """各モジュールの既定値が config_schema の定義を参照しているか。"""

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

    def test_motor_check_feedback_timeout_default(self) -> None:
        assert (
            _default_source(MotorCheckRunner.__init__, "feedback_timeout_ms")
            == "DEFAULT_HEALTH.feedback_timeout_ms"
        )

    def test_motor_check_per_motor_timeout_default(self) -> None:
        assert (
            _default_source(MotorCheckRunner.__init__, "per_motor_timeout_ms")
            == "DEFAULT_MOTOR_CHECK.per_motor_timeout_ms"
        )


class TestFeedbackStalenessHasOneName:
    """動作確認の鮮度判定は health.feedback_timeout_ms と同じ概念・同じ名前。"""

    def test_motor_check_uses_feedback_timeout_ms(self) -> None:
        params = inspect.signature(MotorCheckRunner.__init__).parameters
        assert "feedback_timeout_ms" in params
        assert "feedback_freshness_ms" not in params

    def test_motor_check_default_magnitude_comes_from_config_schema(self) -> None:
        """既定 magnitude は引数既定ではなく本体の fallback にある (None 判定を挟むため)。"""
        assert _body_references(MotorCheckRunner.__init__, "DEFAULT_MOTOR_CHECK.default_magnitude")


class _NoStepSequence(Sequence):
    """ステップを 1 つも持たないシーケンス (ヘルス配線の検証に進行は要らない)。"""


class _RecordingCANManager:
    """RobotServer が health() へ渡したしきい値を記録するだけのスタブ。"""

    def __init__(self) -> None:
        self.received: HealthThresholds | None = None

    def health(self, *, thresholds: HealthThresholds) -> None:
        self.received = thresholds
        raise RuntimeError("しきい値の記録だけが目的")


class TestServerForwardsThresholdsAsOneUnit:
    """4 値が 1 つの値として運ばれ、部分配線が起こりえないこと。"""

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
    """未使用の critical しきい値を受け取らない (呼び出し側の誤配線を作らない)。"""

    def test_signature_has_no_critical_argument(self) -> None:
        params = inspect.signature(MotorDriver.has_thermal_warning).parameters
        assert list(params) == ["self", "temp_warning_c"]
