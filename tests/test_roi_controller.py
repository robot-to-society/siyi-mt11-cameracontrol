from dataclasses import replace

import pytest

from app.camera_protocol import CameraState, GimbalAttitude
from app.mavlink_source import VehicleState
from app.roi_controller import ControlSettings, RoiController, RoiTarget

# Legacy angle mode without smoothing / deadband / target decimation: one 0x0E per step
ANGLE = ControlSettings(mode="angle", target_rate_hz=None, smoothing_tau_s=0.0, deadband_deg=0.0)

TARGETS = (
    RoiTarget(id="roi_1", name="North", lat=35.009, lon=139.0, alt_msl=0.0),
    RoiTarget(id="roi_2", name="Here", lat=35.0, lon=139.0, alt_msl=1.0),
)


class FakeGimbal:
    def __init__(self):
        self.angles: list[tuple[float, float]] = []
        self.modes: list[int] = []
        self.speeds: list[tuple[float, float]] = []
        self.attitude_requests = 0
        self.state = CameraState()

    def set_gimbal_angle(self, yaw_deg, pitch_deg):
        self.angles.append((yaw_deg, pitch_deg))

    def set_gimbal_speed(self, yaw, pitch):
        self.speeds.append((yaw, pitch))

    def request_gimbal_attitude(self):
        self.attitude_requests += 1

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


def make(state=None, settings=ANGLE, **kwargs):
    gimbal, vehicle, clock = FakeGimbal(), FakeVehicle(state), FakeClock()
    ctrl = RoiController(gimbal, vehicle, clock=clock, settings=settings, **kwargs)
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
    ctrl, gimbal, _, _ = make(vstate(), settings=replace(ANGLE, yaw_offset_deg=1.5))
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

    ctrl, gimbal, vehicle, clock = make(vstate(), settings=replace(ANGLE, control_rate_hz=50))
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


# ── rate mode (PID -> 0x07) ─────────────────────────────────────────
RATE = ControlSettings(
    mode="rate", target_rate_hz=2.0, smoothing_tau_s=0.0, deadband_deg=0.3,
    kp=2.0, ki=0.0, kd=0.0, max_speed=60.0,
)


def attitude(yaw, pitch, at=100.0):
    return GimbalAttitude(yaw=yaw, pitch=pitch, roll=0.0, received_at=at)


def make_rate(heading=0.0, settings=RATE):
    return make(vstate(heading=heading), settings=settings)


class TestRateMode:
    def test_target_left_turns_left_with_negative_0x07_yaw(self):
        # target north, nose east -> goal yaw +90 (left); gimbal at 0 -> error +90
        ctrl, gimbal, _, _ = make_rate(heading=90.0)
        gimbal.state.gimbal_attitude = attitude(0.0, 0.0)
        ctrl.start("roi_1")
        ctrl.step()
        yaw_speed, pitch_speed = gimbal.speeds[-1]
        assert yaw_speed == pytest.approx(-60.0)  # 2 * 90 clamped to 60, sign flipped for 0x07
        assert pitch_speed == 0.0  # pitch error ~0 is inside the deadband
        assert gimbal.angles == []  # no 0x0E in rate mode
        assert gimbal.attitude_requests == 1

    def test_proportional_speed_and_deadband(self):
        ctrl, gimbal, _, _ = make_rate(heading=-10.0)  # target north, nose 10 deg left -> goal yaw -10
        gimbal.state.gimbal_attitude = attitude(-5.0, 0.0)
        ctrl.start("roi_1")
        ctrl.step()
        assert gimbal.speeds[-1][0] == pytest.approx(10.0, abs=0.2)  # error -5 -> kp 2 -> turn right
        gimbal.state.gimbal_attitude = attitude(-9.8, 0.0)  # within 0.3 deg
        ctrl.step()
        assert gimbal.speeds[-1] == (0.0, 0.0)

    def test_zero_speed_not_sent_when_already_still(self):
        ctrl, gimbal, _, _ = make_rate(heading=0.0)
        gimbal.state.gimbal_attitude = attitude(0.0, 0.0)
        ctrl.start("roi_1")
        ctrl.step()
        ctrl.step()
        assert gimbal.speeds == []

    def test_goal_recomputed_only_at_target_rate(self):
        ctrl, gimbal, vehicle, clock = make_rate(heading=0.0)
        gimbal.state.gimbal_attitude = attitude(0.0, 0.0)
        ctrl.start("roi_1")
        ctrl.step()
        clock.t = 100.2
        vehicle.state = vstate(heading=-90.0, received_at=100.2)  # nose west: goal would jump to -90 (right)
        gimbal.state.gimbal_attitude = attitude(0.0, 0.0, at=100.2)
        ctrl.step()
        assert ctrl.status().yaw_cmd_deg == pytest.approx(0.0, abs=0.1)  # 2 Hz: not yet
        clock.t = 100.5
        vehicle.state = vstate(heading=-90.0, received_at=100.5)
        gimbal.state.gimbal_attitude = attitude(0.0, 0.0, at=100.5)
        ctrl.step()
        assert ctrl.status().yaw_cmd_deg == pytest.approx(-90.0, abs=0.1)

    def test_smoothing_moves_goal_gradually(self):
        ctrl, gimbal, vehicle, clock = make_rate(heading=0.0, settings=replace(RATE, smoothing_tau_s=1.0))
        gimbal.state.gimbal_attitude = attitude(0.0, 0.0)
        ctrl.start("roi_1")
        ctrl.step()
        clock.t = 100.5
        vehicle.state = vstate(heading=90.0, received_at=100.5)  # raw goal +90
        gimbal.state.gimbal_attitude = attitude(0.0, 0.0, at=100.5)
        ctrl.step()
        assert 20.0 < ctrl.status().yaw_cmd_deg < 90.0  # EMA, not a jump

    def test_missing_attitude_halts(self):
        ctrl, gimbal, _, clock = make_rate(heading=90.0)
        gimbal.state.gimbal_attitude = attitude(0.0, 0.0)
        ctrl.start("roi_1")
        ctrl.step()
        assert gimbal.speeds[-1] != (0.0, 0.0)
        clock.t = 101.0  # attitude now 1 s old
        ctrl.step()
        assert gimbal.speeds[-1] == (0.0, 0.0)
        assert "attitude" in ctrl.status().last_error

    def test_stale_vehicle_halts(self):
        ctrl, gimbal, _, clock = make_rate(heading=90.0)
        gimbal.state.gimbal_attitude = attitude(0.0, 0.0)
        ctrl.start("roi_1")
        ctrl.step()
        clock.t = 103.0
        gimbal.state.gimbal_attitude = attitude(0.0, 0.0, at=103.0)
        ctrl.step()
        assert gimbal.speeds[-1] == (0.0, 0.0)
        assert "stale" in ctrl.status().last_error

    def test_stop_sends_zero_speed(self):
        ctrl, gimbal, _, _ = make_rate(heading=90.0)
        gimbal.state.gimbal_attitude = attitude(0.0, 0.0)
        ctrl.start("roi_1")
        ctrl.step()
        ctrl.stop()
        assert gimbal.speeds[-1] == (0.0, 0.0)
        ctrl.step()
        assert all(s == (0.0, 0.0) for s in gimbal.speeds[1:])  # inactive: only zero resends

    def test_switching_to_angle_mode_halts(self):
        ctrl, gimbal, _, _ = make_rate(heading=90.0)
        gimbal.state.gimbal_attitude = attitude(0.0, 0.0)
        ctrl.start("roi_1")
        ctrl.step()
        ctrl.configure(replace(RATE, mode="angle"))
        assert gimbal.speeds[-1] == (0.0, 0.0)

    def test_unknown_mode_rejected(self):
        ctrl, _, _, _ = make_rate()
        with pytest.raises(ValueError):
            ctrl.configure(replace(RATE, mode="warp"))


class TestAngleModeFiltering:
    def test_deadband_suppresses_small_changes(self):
        ctrl, gimbal, vehicle, _ = make(vstate(heading=0.0), settings=replace(ANGLE, deadband_deg=0.5))
        ctrl.start("roi_1")
        ctrl.step()
        vehicle.state = vstate(heading=0.3)
        ctrl.step()
        assert len(gimbal.angles) == 1
        vehicle.state = vstate(heading=1.0)
        ctrl.step()
        assert len(gimbal.angles) == 2


class TestRateSafety:
    """The gimbal keeps its last 0x07 speed, so every exit path must stop it."""

    def moving(self, heading=90.0):
        ctrl, gimbal, vehicle, clock = make_rate(heading=heading)
        gimbal.state.gimbal_attitude = attitude(0.0, 0.0)
        ctrl.start("roi_1")
        ctrl.step()
        assert gimbal.speeds[-1] != (0.0, 0.0)
        return ctrl, gimbal, vehicle, clock

    def test_nonzero_speed_resent_every_cycle(self):
        ctrl, gimbal, _, clock = self.moving()
        clock.t = 100.1
        gimbal.state.gimbal_attitude = attitude(0.0, 0.0, at=100.1)
        ctrl.step()
        assert len(gimbal.speeds) == 2 and gimbal.speeds[-1] != (0.0, 0.0)

    def test_stop_zero_is_repeated_in_case_a_packet_is_lost(self):
        ctrl, gimbal, _, _ = self.moving()
        ctrl.stop()
        for _ in range(5):
            ctrl.step()
        zeros = [s for s in gimbal.speeds if s == (0.0, 0.0)]
        assert len(zeros) == 3  # immediate + 2 resends, then quiet

    def test_failed_zero_send_is_retried(self):
        ctrl, gimbal, _, _ = self.moving()
        real = gimbal.set_gimbal_speed
        gimbal.set_gimbal_speed = lambda y, p: (_ for _ in ()).throw(OSError("udp"))
        ctrl.stop()
        gimbal.set_gimbal_speed = real
        ctrl.step()
        assert gimbal.speeds[-1] == (0.0, 0.0)

    def test_exception_in_cycle_halts(self):
        ctrl, gimbal, vehicle, clock = self.moving()

        class Boom:
            def latest(self):
                raise RuntimeError("bad data")

        ctrl._vehicle = Boom()
        clock.t = 100.5
        gimbal.state.gimbal_attitude = attitude(0.0, 0.0, at=100.5)
        ctrl.step()
        assert gimbal.speeds[-1] == (0.0, 0.0)
        assert "bad data" in ctrl.status().last_error

    def test_shutdown_halts(self):
        ctrl, gimbal, _, _ = self.moving()
        ctrl.shutdown()
        assert gimbal.speeds[-1] == (0.0, 0.0)

    def test_watchdog_stops_when_loop_stalls(self):
        ctrl, gimbal, _, clock = self.moving()
        clock.t = 100.25  # > 2 control periods without a completed cycle
        ctrl.watchdog_check()
        assert gimbal.speeds[-1] == (0.0, 0.0)
        assert "stalled" in ctrl.status().last_error

    def test_watchdog_quiet_when_loop_runs(self):
        ctrl, gimbal, _, clock = self.moving()
        clock.t = 100.1
        n = len(gimbal.speeds)
        ctrl.watchdog_check()
        assert len(gimbal.speeds) == n

    def test_speed_sent_before_blocking_attitude_request(self):
        ctrl, gimbal, _, clock = make_rate(heading=90.0)
        order = []
        gimbal.set_gimbal_speed = lambda y, p: order.append(("speed", y))
        gimbal.request_gimbal_attitude = lambda: order.append(("attitude",))
        gimbal.state.gimbal_attitude = attitude(0.0, 0.0)
        ctrl.start("roi_1")
        ctrl.step()
        assert order[0][0] == "speed" and order[-1] == ("attitude",)

    def test_set_targets_removing_active_halts(self):
        ctrl, gimbal, _, _ = self.moving()
        ctrl.set_targets(TARGETS[1:])
        assert gimbal.speeds[-1] == (0.0, 0.0)

    def test_starting_another_target_halts_first(self):
        ctrl, gimbal, _, _ = self.moving()
        ctrl.set_targets([*TARGETS, RoiTarget("roi_3", "E", 35.0, 139.011, 0.0)])
        ctrl.start("roi_3")
        assert gimbal.speeds[-1] == (0.0, 0.0)

    def test_follow_mode_failure_halts(self):
        ctrl, gimbal, _, clock = self.moving()

        def boom(_mode):
            raise OSError("tcp down")

        gimbal.set_gimbal_motion_mode = boom
        clock.t = 106.0  # follow mode due again
        gimbal.state.gimbal_attitude = attitude(0.0, 0.0, at=106.0)
        ctrl._vehicle.state = vstate(heading=90.0, received_at=106.0)
        ctrl.step()
        assert gimbal.speeds[-1] == (0.0, 0.0)

    def test_restart_during_goal_update_discards_old_goal(self):
        ctrl, gimbal, vehicle, _ = make_rate(heading=90.0)
        gimbal.state.gimbal_attitude = attitude(0.0, 0.0)
        ctrl.set_targets([*TARGETS, RoiTarget("roi_3", "E", 35.0, 139.011, 0.0)])
        ctrl.start("roi_1")
        real_latest = vehicle.latest

        def latest_then_restart():
            ctrl.start("roi_3")  # operator switches target while roi_1's goal is computed
            return real_latest()

        vehicle.latest = latest_then_restart
        ctrl.step()
        assert ctrl._loop.goal is None  # roi_1's goal must not leak into roi_3
        assert gimbal.speeds == []

    def test_mode_switch_during_cycle_prevents_speed(self):
        ctrl, gimbal, vehicle, _ = make_rate(heading=90.0)
        gimbal.state.gimbal_attitude = attitude(0.0, 0.0)
        ctrl.start("roi_1")
        real_latest = vehicle.latest

        def latest_then_switch():
            ctrl.configure(replace(RATE, mode="angle"))
            return real_latest()

        vehicle.latest = latest_then_switch
        ctrl.step()
        assert gimbal.speeds == []

    def test_dt_clamped_after_gap(self):
        settings = replace(RATE, kp=0.0, ki=1.0)
        ctrl, gimbal, _, clock = make_rate(heading=90.0, settings=settings)
        gimbal.state.gimbal_attitude = attitude(0.0, 0.0)
        ctrl.start("roi_1")
        ctrl.step()
        first = abs(gimbal.speeds[-1][0])
        clock.t = 105.0  # 5 s gap
        gimbal.state.gimbal_attitude = attitude(0.0, 0.0, at=105.0)
        ctrl._vehicle.state = vstate(heading=90.0, received_at=105.0)
        ctrl.step()
        # integral grows by at most ki * err * 2 control periods, not * 5 s
        assert abs(gimbal.speeds[-1][0]) <= first + 90.0 * 0.2 + 1e-6

    def test_non_finite_attitude_halts(self):
        ctrl, gimbal, _, clock = self.moving()
        clock.t = 100.1
        gimbal.state.gimbal_attitude = attitude(float("nan"), 0.0, at=100.1)
        ctrl.step()
        assert gimbal.speeds[-1] == (0.0, 0.0)

    def test_halt_clears_displayed_speed(self):
        ctrl, gimbal, _, clock = self.moving()
        clock.t = 101.0  # attitude stale -> halt
        ctrl.step()
        assert ctrl.status().speed_yaw == 0.0 and ctrl.status().speed_pitch == 0.0
