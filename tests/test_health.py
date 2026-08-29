from __future__ import annotations

from lib.health import (
    BusHealth,
    BusHealthInfo,
    HealthSnapshot,
    MotorHealth,
    MotorHealthInfo,
)


class TestEnumValues:
    def test_bus_health_values(self) -> None:
        assert BusHealth.OK.value == "ok"
        assert BusHealth.DEGRADED.value == "degraded"
        assert BusHealth.DOWN.value == "down"

    def test_motor_health_values(self) -> None:
        assert MotorHealth.OK.value == "ok"
        assert MotorHealth.STALE.value == "stale"
        assert MotorHealth.WARNING.value == "warning"
        assert MotorHealth.FAULT.value == "fault"


class TestBusHealthInfo:
    def test_construct_with_all_fields(self) -> None:
        info = BusHealthInfo(
            name="m3508_bus",
            channel="can0",
            state=BusHealth.OK,
            last_tx_at=1714377600.5,
            last_rx_at=1714377600.7,
            tx_error_count=2,
            rx_error_count=1,
            bus_off=False,
        )
        assert info.name == "m3508_bus"
        assert info.channel == "can0"
        assert info.state is BusHealth.OK
        assert info.tx_error_count == 2
        assert info.rx_error_count == 1
        assert info.bus_off is False

    def test_to_dict_serializes_state_as_string(self) -> None:
        info = BusHealthInfo(
            name="m3508_bus",
            channel="can0",
            state=BusHealth.OK,
            last_tx_at=1714377600.5,
            last_rx_at=1714377600.7,
            tx_error_count=0,
            rx_error_count=0,
            bus_off=False,
        )
        d = info.to_dict()
        assert d["state"] == "ok"
        assert d["name"] == "m3508_bus"
        assert d["channel"] == "can0"
        assert d["last_tx_at"] == 1714377600.5
        assert d["last_rx_at"] == 1714377600.7
        assert d["tx_error_count"] == 0
        assert d["rx_error_count"] == 0
        assert d["bus_off"] is False

    def test_to_dict_handles_none_timestamps(self) -> None:
        info = BusHealthInfo(
            name="bus",
            channel="can0",
            state=BusHealth.DOWN,
            last_tx_at=None,
            last_rx_at=None,
            tx_error_count=0,
            rx_error_count=0,
            bus_off=True,
        )
        d = info.to_dict()
        assert d["last_tx_at"] is None
        assert d["last_rx_at"] is None
        assert d["state"] == "down"
        assert d["bus_off"] is True


class TestMotorHealthInfo:
    def test_construct_with_all_fields(self) -> None:
        info = MotorHealthInfo(
            name="lift_motor",
            bus="m3508_bus",
            state=MotorHealth.OK,
            last_feedback_at=1714377600.0,
            feedback_age_ms=23.4,
            temperature=35.0,
            detail=None,
        )
        assert info.name == "lift_motor"
        assert info.bus == "m3508_bus"
        assert info.state is MotorHealth.OK
        assert info.feedback_age_ms == 23.4
        assert info.temperature == 35.0
        assert info.detail is None

    def test_to_dict_serializes_state_as_string(self) -> None:
        info = MotorHealthInfo(
            name="lift_motor",
            bus="m3508_bus",
            state=MotorHealth.WARNING,
            last_feedback_at=1714377600.0,
            feedback_age_ms=23.4,
            temperature=70.0,
            detail="temperature high",
        )
        d = info.to_dict()
        assert d["state"] == "warning"
        assert d["name"] == "lift_motor"
        assert d["bus"] == "m3508_bus"
        assert d["last_feedback_at"] == 1714377600.0
        assert d["feedback_age_ms"] == 23.4
        assert d["temperature"] == 70.0
        assert d["detail"] == "temperature high"

    def test_to_dict_handles_none_optional_fields(self) -> None:
        info = MotorHealthInfo(
            name="lift_motor",
            bus="m3508_bus",
            state=MotorHealth.STALE,
            last_feedback_at=None,
            feedback_age_ms=None,
            temperature=None,
            detail=None,
        )
        d = info.to_dict()
        assert d["state"] == "stale"
        assert d["last_feedback_at"] is None
        assert d["feedback_age_ms"] is None
        assert d["temperature"] is None
        assert d["detail"] is None


def _make_bus(name: str, state: BusHealth) -> BusHealthInfo:
    return BusHealthInfo(
        name=name,
        channel="can0",
        state=state,
        last_tx_at=None,
        last_rx_at=None,
        tx_error_count=0,
        rx_error_count=0,
        bus_off=False,
    )


def _make_motor(name: str, state: MotorHealth) -> MotorHealthInfo:
    return MotorHealthInfo(
        name=name,
        bus="bus",
        state=state,
        last_feedback_at=None,
        feedback_age_ms=None,
        temperature=None,
        detail=None,
    )


class TestHealthSnapshot:
    def test_construct_with_buses_and_motors(self) -> None:
        snap = HealthSnapshot(
            timestamp=1714377600.0,
            overall=BusHealth.OK,
            buses=[_make_bus("m3508_bus", BusHealth.OK)],
            motors=[_make_motor("lift_motor", MotorHealth.OK)],
        )
        assert snap.overall is BusHealth.OK
        assert len(snap.buses) == 1
        assert len(snap.motors) == 1

    def test_to_dict_serializes_all(self) -> None:
        snap = HealthSnapshot(
            timestamp=1714377600.0,
            overall=BusHealth.DEGRADED,
            buses=[
                _make_bus("m3508_bus", BusHealth.OK),
                _make_bus("edulite_bus", BusHealth.DEGRADED),
            ],
            motors=[
                _make_motor("lift_motor", MotorHealth.OK),
                _make_motor("arm_motor", MotorHealth.WARNING),
            ],
        )
        d = snap.to_dict()
        assert d["timestamp"] == 1714377600.0
        assert d["overall"] == "degraded"
        assert isinstance(d["buses"], list)
        assert isinstance(d["motors"], list)
        assert len(d["buses"]) == 2
        assert len(d["motors"]) == 2
        assert d["buses"][0]["state"] == "ok"
        assert d["buses"][1]["state"] == "degraded"
        assert d["motors"][1]["state"] == "warning"


class TestComputeOverall:
    def test_all_ok(self) -> None:
        buses = [_make_bus("a", BusHealth.OK), _make_bus("b", BusHealth.OK)]
        motors = [_make_motor("m1", MotorHealth.OK)]
        assert HealthSnapshot.compute_overall(buses, motors) is BusHealth.OK

    def test_any_bus_degraded(self) -> None:
        buses = [_make_bus("a", BusHealth.OK), _make_bus("b", BusHealth.DEGRADED)]
        motors = [_make_motor("m1", MotorHealth.OK)]
        assert HealthSnapshot.compute_overall(buses, motors) is BusHealth.DEGRADED

    def test_any_bus_down(self) -> None:
        buses = [_make_bus("a", BusHealth.DEGRADED), _make_bus("b", BusHealth.DOWN)]
        motors = [_make_motor("m1", MotorHealth.OK)]
        assert HealthSnapshot.compute_overall(buses, motors) is BusHealth.DOWN

    def test_motor_stale_maps_to_degraded(self) -> None:
        buses = [_make_bus("a", BusHealth.OK)]
        motors = [_make_motor("m1", MotorHealth.STALE)]
        assert HealthSnapshot.compute_overall(buses, motors) is BusHealth.DEGRADED

    def test_motor_warning_maps_to_degraded(self) -> None:
        buses = [_make_bus("a", BusHealth.OK)]
        motors = [_make_motor("m1", MotorHealth.WARNING)]
        assert HealthSnapshot.compute_overall(buses, motors) is BusHealth.DEGRADED

    def test_motor_fault_maps_to_down(self) -> None:
        buses = [_make_bus("a", BusHealth.OK)]
        motors = [_make_motor("m1", MotorHealth.FAULT)]
        assert HealthSnapshot.compute_overall(buses, motors) is BusHealth.DOWN

    def test_bus_down_overrides_motor_warning(self) -> None:
        buses = [_make_bus("a", BusHealth.DOWN)]
        motors = [_make_motor("m1", MotorHealth.WARNING)]
        assert HealthSnapshot.compute_overall(buses, motors) is BusHealth.DOWN

    def test_empty_inputs_default_ok(self) -> None:
        assert HealthSnapshot.compute_overall([], []) is BusHealth.OK

