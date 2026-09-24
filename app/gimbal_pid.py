"""Pure helpers for ROI rate control: PID, deadband, angle smoothing (immutable state)."""

import math
from dataclasses import dataclass
from typing import Optional

from app.geo import wrap_180

# Within this distance of +/-180 deg the yaw error keeps its previous sign (no left/right dithering)
YAW_FLIP_GUARD_DEG = 170.0


@dataclass(frozen=True)
class PidGains:
    kp: float
    ki: float
    kd: float
    i_limit: float  # |integral term| clamp (output units) for anti-windup
    out_limit: float  # |output| clamp (0x07 speed units, max 100)


@dataclass(frozen=True)
class PidState:
    integral: float = 0.0  # already multiplied by ki (output units)
    prev_error: Optional[float] = None


def pid_step(gains: PidGains, state: PidState, error: float, dt: float) -> tuple[float, PidState]:
    """One PID update. Returns (output, new_state); the given state is not modified."""
    integral = state.integral + gains.ki * error * dt
    integral = max(-gains.i_limit, min(gains.i_limit, integral))
    derivative = 0.0 if state.prev_error is None or dt <= 0 else (error - state.prev_error) / dt
    output = gains.kp * error + integral + gains.kd * derivative
    output = max(-gains.out_limit, min(gains.out_limit, output))
    return output, PidState(integral=integral, prev_error=error)


def apply_deadband(error: float, deadband: float) -> float:
    return 0.0 if abs(error) < deadband else error


def smooth_angle(prev: Optional[float], new: float, dt: float, tau_s: float, wrap: bool = False) -> float:
    """Exponential moving average with time constant tau_s (0 = no smoothing).
    With wrap=True the step is taken the short way round (+/-180 deg)."""
    if prev is None or tau_s <= 0:
        return new
    alpha = 1.0 - math.exp(-max(dt, 0.0) / tau_s)
    delta = wrap_180(new - prev) if wrap else new - prev
    value = prev + alpha * delta
    return wrap_180(value) if wrap else value


def stable_yaw_error(target: float, current: float, prev_error: Optional[float]) -> float:
    """Shortest yaw error, but keep the previous turning direction near +/-180 deg."""
    error = wrap_180(target - current)
    if (
        prev_error is not None
        and abs(error) > YAW_FLIP_GUARD_DEG
        and abs(prev_error) > YAW_FLIP_GUARD_DEG
        and (error > 0) != (prev_error > 0)
    ):
        error += 360.0 if prev_error > 0 else -360.0
    return error


def deadband_with_hysteresis(error: float, deadband: float, holding: bool) -> tuple[float, bool]:
    """Stop below `deadband`; once stopped, resume only above 2 x deadband (no 0 <-> small chatter).
    Returns (error_to_use, holding)."""
    threshold = 2.0 * deadband if holding else deadband
    if abs(error) < threshold:
        return 0.0, True
    return error, False
