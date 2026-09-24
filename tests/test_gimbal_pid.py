import math

import pytest

from app.gimbal_pid import (
    PidGains,
    PidState,
    apply_deadband,
    pid_step,
    smooth_angle,
    stable_yaw_error,
)

GAINS = PidGains(kp=2.0, ki=0.5, kd=0.1, i_limit=20.0, out_limit=60.0)


class TestPid:
    def test_proportional_only_first_step(self):
        out, st = pid_step(PidGains(kp=2.0, ki=0.0, kd=0.0, i_limit=0, out_limit=100), PidState(), 10.0, 0.1)
        assert out == pytest.approx(20.0)
        assert st.prev_error == 10.0

    def test_integral_accumulates_and_is_limited(self):
        st = PidState()
        g = PidGains(kp=0.0, ki=1.0, kd=0.0, i_limit=0.5, out_limit=100)
        for _ in range(20):
            out, st = pid_step(g, st, 1.0, 0.1)
        assert st.integral == pytest.approx(0.5)  # anti-windup clamp
        assert out == pytest.approx(0.5)

    def test_derivative_uses_error_change(self):
        g = PidGains(kp=0.0, ki=0.0, kd=1.0, i_limit=0, out_limit=100)
        _, st = pid_step(g, PidState(), 1.0, 0.1)  # first step: no derivative kick
        out, _ = pid_step(g, st, 2.0, 0.1)
        assert out == pytest.approx(10.0)

    def test_output_clamped(self):
        out, _ = pid_step(GAINS, PidState(), 1000.0, 0.1)
        assert out == pytest.approx(60.0)
        out, _ = pid_step(GAINS, PidState(), -1000.0, 0.1)
        assert out == pytest.approx(-60.0)

    def test_state_not_mutated(self):
        st = PidState()
        pid_step(GAINS, st, 5.0, 0.1)
        assert st == PidState()


class TestDeadband:
    def test_inside_is_zero(self):
        assert apply_deadband(0.2, 0.3) == 0.0
        assert apply_deadband(-0.29, 0.3) == 0.0

    def test_outside_passes_through(self):
        assert apply_deadband(0.5, 0.3) == 0.5
        assert apply_deadband(-2.0, 0.3) == -2.0


class TestSmoothAngle:
    def test_first_value_passes(self):
        assert smooth_angle(None, 10.0, 0.5, tau_s=1.0) == 10.0

    def test_ema_step(self):
        alpha = 1 - math.exp(-0.5 / 1.0)
        assert smooth_angle(0.0, 10.0, 0.5, tau_s=1.0) == pytest.approx(10.0 * alpha)

    def test_zero_tau_disables_smoothing(self):
        assert smooth_angle(0.0, 10.0, 0.5, tau_s=0.0) == 10.0

    def test_wraps_across_180(self):
        # 179 -> -179 is a 2 deg move, not 358
        v = smooth_angle(179.0, -179.0, 100.0, tau_s=1.0, wrap=True)
        assert v == pytest.approx(-179.0, abs=1e-6)
        mid = smooth_angle(179.0, -179.0, math.log(2), tau_s=1.0, wrap=True)  # alpha = 0.5
        assert abs(mid) == pytest.approx(180.0)


class TestStableYawError:
    def test_shortest_path(self):
        assert stable_yaw_error(10.0, 0.0, None) == pytest.approx(10.0)
        assert stable_yaw_error(-170.0, 170.0, None) == pytest.approx(20.0)

    def test_keeps_direction_near_180(self):
        # error flips from +179 to -179 when the target is behind: keep turning the same way
        assert stable_yaw_error(-1.0, 178.0, prev_error=179.0) == pytest.approx(181.0)

    def test_releases_when_not_near_180(self):
        assert stable_yaw_error(-90.0, 0.0, prev_error=179.0) == pytest.approx(-90.0)


class TestDeadbandHysteresis:
    def test_stops_inside_and_holds_until_twice_the_band(self):
        from app.gimbal_pid import deadband_with_hysteresis

        assert deadband_with_hysteresis(0.2, 0.3, holding=False) == (0.0, True)
        assert deadband_with_hysteresis(0.5, 0.3, holding=True) == (0.0, True)  # < 0.6: keep holding
        assert deadband_with_hysteresis(0.7, 0.3, holding=True) == (0.7, False)
        assert deadband_with_hysteresis(0.5, 0.3, holding=False) == (0.5, False)
