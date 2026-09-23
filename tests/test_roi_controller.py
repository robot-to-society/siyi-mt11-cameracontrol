import pytest

from app.mavlink_source import VehicleState
from app.roi_controller import RoiController, RoiTarget

TARGETS = (
    RoiTarget(id="roi_1", name="North", lat=35.009, lon=139.0, alt_msl=0.0),
    RoiTarget(id="roi_2", name="Here", lat=35.0, lon=139.0, alt_msl=1.0),
)


class FakeGimbal:
    def __init__(self):
        self.angles: list[tuple[float, float]] = []
        self.modes: list[int] = []

    def set_gimbal_angle(self, yaw_deg, pitch_deg):
        self.angles.append((yaw_deg, pitch_deg))

    def set_gimbal_motion_mode(self, mode):
        self.modes.append(mode)


class FakeVehicle:
    def __init__(self, state=None):
        self.state = state

    def latest(self):
        return self.state


class FakeClock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def make(state=None, **kwargs):
    gimbal, vehicle, clock = FakeGimbal(), FakeVehicle(state), FakeClock()
    ctrl = RoiController(gimbal, vehicle, clock=clock, **kwargs)
    ctrl.set_targets(TARGETS)
    return ctrl, gimbal, vehicle, clock


def vstate(heading=0.0, received_at=100.0):
    return VehicleState(lat=35.0, lon=139.0, alt_msl=0.0, heading_deg=heading, received_at=received_at)


def test_start_is_non_blocking_and_follow_mode_sent_on_first_step():
    ctrl, gimbal, _, _ = make(vstate())
    ctrl.start("roi_1")
    assert gimbal.modes == []  # no camera I/O in the request path
    assert ctrl.status().active_target_id == "roi_1"
    ctrl.step()
    ctrl.step()
    assert gimbal.modes == [4]
    assert len(gimbal.angles) == 2


def test_start_unknown_target_raises():
    ctrl, _, _, _ = make(vstate())
    with pytest.raises(KeyError):
        ctrl.start("roi_9")


def test_step_sends_angle_relative_to_heading():
    ctrl, gimbal, _, _ = make(vstate(heading=90.0))
    ctrl.start("roi_1")
    ctrl.step()
    yaw, pitch = gimbal.angles[-1]
    assert yaw == pytest.approx(90.0, abs=0.1)  # target north, nose east -> left
    assert pitch == pytest.approx(0.0, abs=0.1)
    status = ctrl.status()
    assert status.distance_m == pytest.approx(1000, rel=0.01)
    assert status.last_error is None


def test_step_does_nothing_when_inactive():
    ctrl, gimbal, _, _ = make(vstate())
    ctrl.step()
    assert gimbal.angles == []


def test_step_skips_without_vehicle_state():
    ctrl, gimbal, _, _ = make(None)
    ctrl.start("roi_1")
    ctrl.step()
    assert gimbal.angles == []
    assert "no vehicle" in ctrl.status().last_error


def test_step_skips_stale_vehicle_state():
    ctrl, gimbal, _, clock = make(vstate(received_at=100.0), max_age_s=2.0)
    ctrl.start("roi_1")
    clock.t = 102.5
    ctrl.step()
    assert gimbal.angles == []
    assert "stale" in ctrl.status().last_error


def test_step_skips_unknown_heading():
    ctrl, gimbal, _, _ = make(vstate(heading=None))
    ctrl.start("roi_1")
    ctrl.step()
    assert gimbal.angles == []
    assert "heading" in ctrl.status().last_error


def test_step_skips_target_too_close():
    ctrl, gimbal, _, _ = make(vstate(), min_distance_m=3.0)
    ctrl.start("roi_2")
    ctrl.step()
    assert gimbal.angles == []
    assert "close" in ctrl.status().last_error


def test_stop_halts_commands():
    ctrl, gimbal, _, _ = make(vstate())
    ctrl.start("roi_1")
    ctrl.stop()
    ctrl.step()
    assert gimbal.angles == []
    assert ctrl.status().active_target_id is None


def test_set_targets_stops_when_active_target_removed():
    ctrl, _, _, _ = make(vstate())
    ctrl.start("roi_1")
    ctrl.set_targets(TARGETS[1:])
    assert ctrl.status().active_target_id is None


def test_yaw_offset_applied():
    ctrl, gimbal, _, _ = make(vstate(), yaw_offset_deg=1.5)
    ctrl.start("roi_1")
    ctrl.step()
    assert gimbal.angles[-1][0] == pytest.approx(1.5, abs=0.1)


def test_gimbal_error_recorded_not_raised():
    ctrl, gimbal, _, _ = make(vstate())

    def boom(*_):
        raise OSError("network down")

    gimbal.set_gimbal_angle = boom
    ctrl.start("roi_1")
    ctrl.step()
    assert "network down" in ctrl.status().last_error


def test_background_thread_runs_steps():
    import time

    ctrl, gimbal, vehicle, clock = make(vstate(), rate_hz=50)
    ctrl.start_background()
    try:
        ctrl.start("roi_1")
        deadline = time.monotonic() + 1.0
        while not gimbal.angles and time.monotonic() < deadline:
            time.sleep(0.01)
        assert gimbal.angles
    finally:
        ctrl.shutdown()


def test_start_unset_target_raises_value_error():
    ctrl, gimbal, _, _ = make(vstate())
    ctrl.set_targets([RoiTarget(id="roi_3", name="", lat=0.0, lon=0.0, alt_msl=0.0)])
    with pytest.raises(ValueError):
        ctrl.start("roi_3")
    assert gimbal.modes == []


def test_follow_mode_failure_reported_and_retried():
    ctrl, gimbal, _, _ = make(vstate())

    def boom(_mode):
        raise OSError("tcp down")

    gimbal.set_gimbal_motion_mode = boom
    ctrl.start("roi_1")
    ctrl.step()
    status = ctrl.status()
    assert status.active_target_id == "roi_1"
    assert "tcp down" in status.last_error
    assert gimbal.angles == []

    gimbal.set_gimbal_motion_mode = lambda mode: gimbal.modes.append(mode)
    ctrl.step()  # retried on next step
    assert gimbal.modes == [4]
    assert ctrl.status().last_error is None


def test_yaw_hysteresis_near_180_keeps_previous_side():
    # Target due south, heading jitters around north -> yaw near +/-180
    ctrl, gimbal, vehicle, _ = make(vstate(heading=0.1))
    ctrl.set_targets([RoiTarget(id="s", name="", lat=34.991, lon=139.0, alt_msl=0.0)])
    ctrl.start("s")
    ctrl.step()
    first = gimbal.angles[-1][0]
    vehicle.state = vstate(heading=-0.1)
    ctrl.step()
    second = gimbal.angles[-1][0]
    assert abs(second - first) < 1.0  # no full-turn swing
    assert abs(second) <= 190.0


def test_yaw_hysteresis_released_away_from_boundary():
    ctrl, gimbal, vehicle, _ = make(vstate(heading=0.1))
    ctrl.set_targets([RoiTarget(id="s", name="", lat=34.991, lon=139.0, alt_msl=0.0)])
    ctrl.start("s")
    ctrl.step()
    vehicle.state = vstate(heading=-30.0)
    ctrl.step()
    assert gimbal.angles[-1][0] == pytest.approx(150.0, abs=0.1)


def test_follow_mode_reasserted_periodically():
    ctrl, gimbal, _, clock = make(vstate(), follow_mode_interval_s=5.0)
    ctrl.start("roi_1")
    ctrl.step()
    clock.t = 103.0
    ctrl.step()
    assert gimbal.modes == [4]
    clock.t = 105.5
    vehicle_state = vstate(received_at=105.5)
    ctrl._vehicle.state = vehicle_state
    ctrl.step()
    assert gimbal.modes == [4, 4]


def test_stop_during_step_prevents_send():
    ctrl, gimbal, vehicle, _ = make(vstate())
    ctrl.start("roi_1")
    ctrl.step()  # sends follow mode
    sent_before = len(gimbal.angles)

    class StoppingVehicle:
        def latest(self_inner):
            ctrl.stop()  # manual override arrives mid-step
            return vstate()

    ctrl._vehicle = StoppingVehicle()
    ctrl.step()
    assert len(gimbal.angles) == sent_before
