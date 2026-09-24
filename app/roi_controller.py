"""Keeps the gimbal pointed at a registered GPS target (ROI) using vehicle position/heading.

Two loops:
  target  (target_rate_hz, default 2 Hz): GPS + heading -> gimbal angles -> smoothed "goal"
  control (control_rate_hz, default 10 Hz):
    mode "rate":  0x0D attitude vs goal -> deadband -> PID -> 0x07 speed (smooth motion)
    mode "angle": 0x0E angle when the goal moved more than the deadband

Safety (rate mode): the gimbal keeps moving at the last 0x07 speed, so
  * every stop / error / exception / shutdown sends speed 0, repeated a few times (UDP loss)
  * the speed (UDP) is sent before any blocking TCP call (attitude request, Follow mode)
  * a watchdog thread sends 0 if the control loop stalls (e.g. blocked on a TCP reconnect)
  * results computed for an old activation (generation) are discarded
"""

import logging
import math
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Optional, Protocol

from app.geo import GeoPoint, look_angles, to_gimbal_angles
from app.gimbal_pid import (
    PidGains,
    PidState,
    deadband_with_hysteresis,
    pid_step,
    smooth_angle,
    stable_yaw_error,
)
from app.mavlink_source import VehicleState

logger = logging.getLogger(__name__)

MOTION_FOLLOW = 4
# Near +/-180 deg, keep yaw on the previous side (up to this magnitude) so heading
# jitter doesn't flip the command between +179.9 and -179.9 (a full-turn swing).
YAW_HYSTERESIS_LIMIT_DEG = 190.0
MODES = ("rate", "angle")
ZERO_RESENDS = 2  # extra speed-0 packets after the immediate one
WATCHDOG_PERIODS = 2.0  # control periods without a completed cycle before forcing a stop
DT_MAX_PERIODS = 2.0  # clamp PID dt after gaps (no integral kick)
WATCHDOG_INTERVAL_S = 0.05
ZERO = (0.0, 0.0)


def stabilize_yaw(new_deg: float, prev_deg: Optional[float]) -> float:
    if prev_deg is None or abs(new_deg - prev_deg) <= 180.0:
        return new_deg
    candidate = new_deg + (360.0 if prev_deg > 0 else -360.0)
    return candidate if abs(candidate) <= YAW_HYSTERESIS_LIMIT_DEG else new_deg


class GimbalCommander(Protocol):
    state: Any  # CameraState (gimbal_attitude from 0x0D)

    def set_gimbal_angle(self, yaw_deg: float, pitch_deg: float) -> None: ...

    def set_gimbal_speed(self, yaw: float, pitch: float) -> None: ...

    def set_gimbal_motion_mode(self, mode: int) -> None: ...

    def request_gimbal_attitude(self) -> None: ...


class VehicleSource(Protocol):
    def latest(self) -> Optional[VehicleState]: ...


@dataclass(frozen=True)
class ControlSettings:
    mode: str = "rate"
    control_rate_hz: float = 10.0
    target_rate_hz: Optional[float] = 2.0  # None = recompute every control step
    smoothing_tau_s: float = 0.8  # goal EMA time constant (0 = off)
    deadband_deg: float = 0.3
    kp: float = 2.0
    ki: float = 0.2
    kd: float = 0.0
    max_speed: float = 60.0  # 0x07 units (max 100)
    yaw_offset_deg: float = 0.0
    attitude_max_age_s: float = 0.5

    def gains(self) -> PidGains:
        return PidGains(self.kp, self.ki, self.kd, i_limit=self.max_speed / 2.0, out_limit=self.max_speed)

    @property
    def control_period_s(self) -> float:
        return 1.0 / max(self.control_rate_hz, 0.1)


@dataclass(frozen=True)
class RoiTarget:
    id: str
    name: str
    lat: float
    lon: float
    alt_msl: float


@dataclass(frozen=True)
class RoiStatus:
    active_target_id: Optional[str] = None
    mode: Optional[str] = None
    bearing_deg: Optional[float] = None
    elevation_deg: Optional[float] = None
    distance_m: Optional[float] = None
    yaw_cmd_deg: Optional[float] = None  # goal (smoothed) gimbal angles
    pitch_cmd_deg: Optional[float] = None
    gimbal_yaw_deg: Optional[float] = None  # measured (rate mode)
    gimbal_pitch_deg: Optional[float] = None
    speed_yaw: Optional[float] = None  # 0x07 output (rate mode)
    speed_pitch: Optional[float] = None
    last_error: Optional[str] = None
    updated_at: Optional[float] = None


@dataclass(frozen=True)
class _Loop:
    """Per-activation control state (replaced, never mutated; written under the lock)."""

    goal: Optional[tuple[float, float]] = None
    goal_error: Optional[str] = None
    target_at: Optional[float] = None
    control_at: Optional[float] = None
    pid_yaw: PidState = PidState()
    pid_pitch: PidState = PidState()
    hold_yaw: bool = False
    hold_pitch: bool = False
    yaw_error: Optional[float] = None
    last_sent_angle: Optional[tuple[float, float]] = None
    last_speed: tuple[float, float] = ZERO


class RoiController:
    def __init__(
        self,
        gimbal: GimbalCommander,
        vehicle: VehicleSource,
        clock: Callable[[], float] = time.monotonic,
        settings: ControlSettings = ControlSettings(),
        max_age_s: float = 2.0,
        min_distance_m: float = 3.0,
        follow_mode_interval_s: float = 5.0,
    ):
        self._gimbal = gimbal
        self._vehicle = vehicle
        self._clock = clock
        self._settings = settings
        self._max_age_s = max_age_s
        self._min_distance_m = min_distance_m
        self._follow_mode_interval_s = follow_mode_interval_s
        self._last_follow_sent: Optional[float] = None
        self._loop = _Loop()
        self._generation = 0  # bumped on every start / stop: stale results are discarded
        self._zero_resends = 0
        self._targets: dict[str, RoiTarget] = {}
        self._status = RoiStatus()
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._threads: list[threading.Thread] = []

    # ── configuration ────────────────────────────────────────────
    def set_targets(self, targets: Iterable[RoiTarget]) -> None:
        with self._lock:
            self._targets = {t.id: t for t in targets}
            if self._status.active_target_id not in self._targets:
                self._deactivate_locked()

    def configure(self, settings: ControlSettings) -> None:
        if settings.mode not in MODES:
            raise ValueError(f"unknown control mode: {settings.mode}")
        with self._lock:
            if self._settings.mode == "rate" and settings.mode != "rate":
                self._halt_locked()
            self._settings = settings
            self._loop = replace(self._loop, pid_yaw=PidState(), pid_pitch=PidState(), control_at=None)

    def settings(self) -> ControlSettings:
        return self._settings

    # ── control ──────────────────────────────────────────────────
    def start(self, target_id: str) -> None:
        """Raises KeyError for unknown ids, ValueError for targets whose coordinates are unset."""
        with self._lock:
            target = self._targets.get(target_id)
            if target is None:
                raise KeyError(target_id)
            if target.lat == 0.0 and target.lon == 0.0:
                raise ValueError(f"ROI target {target_id} has no coordinates")
            self._halt_locked()
            self._generation += 1
            self._status = RoiStatus(active_target_id=target_id, mode=self._settings.mode)
            # Follow mode is sent from the loop thread: camera TCP I/O may block when disconnected
            self._last_follow_sent = None
            self._loop = _Loop()

    def stop(self) -> None:
        with self._lock:
            self._deactivate_locked()

    def status(self) -> RoiStatus:
        return self._status

    def targets(self) -> tuple[RoiTarget, ...]:
        return tuple(self._targets.values())

    def _deactivate_locked(self) -> None:
        self._halt_locked()
        self._generation += 1
        self._status = RoiStatus()
        self._loop = replace(_Loop(), last_speed=self._loop.last_speed)

    def _halt_locked(self, force: bool = False) -> None:
        """Stop a moving gimbal (rate mode): speed 0 now plus ZERO_RESENDS more on later steps."""
        if self._loop.last_speed == ZERO and not force:
            return
        self._zero_resends = ZERO_RESENDS
        self._loop = replace(
            self._loop,
            last_speed=ZERO,
            control_at=None,
            pid_yaw=PidState(),
            pid_pitch=PidState(),
            hold_yaw=False,
            hold_pitch=False,
        )
        if self._status.active_target_id is not None:
            self._status = replace(self._status, speed_yaw=0.0, speed_pitch=0.0)
        self._send_zero_locked()

    def _send_zero_locked(self) -> None:
        try:
            self._gimbal.set_gimbal_speed(0, 0)
        except Exception as exc:  # noqa: BLE001 - retried by the pending resends
            logger.warning("ROI speed-0 send failed: %s", exc)

    def _flush_zero_resends_locked(self) -> None:
        if self._zero_resends > 0:
            self._zero_resends -= 1
            self._send_zero_locked()

    # ── loop ─────────────────────────────────────────────────────
    def step(self) -> None:
        with self._lock:
            self._flush_zero_resends_locked()
            target = self._targets.get(self._status.active_target_id or "")
            gen = self._generation
        if target is None:
            return
        try:
            error = self._cycle(target, gen)
        except Exception as exc:  # noqa: BLE001 - never leave the gimbal moving on a bug
            logger.exception("ROI control cycle failed")
            with self._lock:
                if self._generation == gen:
                    self._halt_locked(force=True)
            error = f"control error: {exc}"
        with self._lock:
            if self._generation == gen and self._status.active_target_id == target.id:
                self._status = replace(self._status, last_error=error, updated_at=self._clock())

    def watchdog_check(self) -> None:
        """Force speed 0 if the gimbal is moving but no control cycle completed recently."""
        with self._lock:
            loop = self._loop
            if loop.last_speed == ZERO or loop.control_at is None:
                return
            if self._clock() - loop.control_at <= WATCHDOG_PERIODS * self._settings.control_period_s:
                return
            self._halt_locked(force=True)
            if self._status.active_target_id is not None:
                self._status = replace(self._status, last_error="control loop stalled: gimbal stopped")

    def _cycle(self, target: RoiTarget, gen: int) -> Optional[str]:
        if self._last_follow_sent is None:  # first cycle: nothing is moving yet
            mode_error = self._ensure_follow_mode()
            if mode_error:
                return self._halt_with(gen, mode_error)
        now = self._clock()
        if self._target_due(now):
            self._update_goal(target, gen, now)
        loop = self._loop
        if loop.goal_error or loop.goal is None:
            return self._halt_with(gen, loop.goal_error or "waiting for target")
        rate_mode = self._settings.mode == "rate"
        error = self._rate_cycle(target, gen, now) if rate_mode else self._angle_cycle(target, gen)

        # Blocking TCP work only after this cycle's speed/angle went out over UDP
        mode_error = self._ensure_follow_mode()
        if mode_error:
            return self._halt_with(gen, mode_error)
        if rate_mode:
            try:
                self._gimbal.request_gimbal_attitude()  # reply is used on the next cycle
            except Exception as exc:  # noqa: BLE001
                return self._halt_with(gen, f"attitude request failed: {exc}")
        return error

    def _halt_with(self, gen: int, error: str) -> str:
        with self._lock:
            if self._generation == gen:
                self._halt_locked()
        return error

    def _ensure_follow_mode(self) -> Optional[str]:
        """Send Follow mode on start and re-assert it periodically (camera reboot / other clients)."""
        now = self._clock()
        last = self._last_follow_sent
        if last is not None and now - last < self._follow_mode_interval_s:
            return None
        try:
            self._gimbal.set_gimbal_motion_mode(MOTION_FOLLOW)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to set Follow mode: %s", exc)
            return f"follow mode failed: {exc}"
        self._last_follow_sent = now
        return None

    def _target_due(self, now: float) -> bool:
        rate = self._settings.target_rate_hz
        last = self._loop.target_at
        return rate is None or last is None or now - last >= 1.0 / rate

    def _update_goal(self, target: RoiTarget, gen: int, now: float) -> None:
        """Target loop: vehicle state -> raw gimbal angles -> smoothed goal (or goal_error)."""
        raw, look, error = self._raw_angles(target, now)  # outside the lock (may call other code)
        with self._lock:
            if self._generation != gen:
                return  # restarted / stopped meanwhile: discard
            loop = self._loop
            if error:
                self._loop = replace(loop, target_at=now, goal_error=error)
                return
            dt = now - loop.target_at if loop.target_at is not None else 0.0
            tau = self._settings.smoothing_tau_s
            prev = loop.goal if loop.goal_error is None else None
            yaw = smooth_angle(prev[0] if prev else None, raw[0], dt, tau, wrap=True)
            pitch = smooth_angle(prev[1] if prev else None, raw[1], dt, tau)
            self._loop = replace(loop, goal=(yaw, pitch), goal_error=None, target_at=now)
            self._status = replace(
                self._status,
                bearing_deg=look.bearing_deg,
                elevation_deg=look.elevation_deg,
                distance_m=look.distance_m,
                yaw_cmd_deg=yaw,
                pitch_cmd_deg=pitch,
            )

    def _raw_angles(self, target: RoiTarget, now: float):
        state = self._vehicle.latest()
        if state is None:
            return None, None, "no vehicle position (MAVLink)"
        if now - state.received_at > self._max_age_s:
            return None, None, "vehicle position stale"
        if state.heading_deg is None:
            return None, None, "vehicle heading unavailable"
        own = GeoPoint(state.lat, state.lon, state.alt_msl)
        look = look_angles(own, GeoPoint(target.lat, target.lon, target.alt_msl))
        if look.distance_m < self._min_distance_m:
            return None, None, "target too close"
        cmd = to_gimbal_angles(look, state.heading_deg, self._settings.yaw_offset_deg)
        if not (math.isfinite(cmd.yaw_deg) and math.isfinite(cmd.pitch_deg)):
            return None, None, "invalid target angles"
        return (cmd.yaw_deg, cmd.pitch_deg), look, None

    def _angle_cycle(self, target: RoiTarget, gen: int) -> Optional[str]:
        loop = self._loop
        yaw = stabilize_yaw(loop.goal[0], loop.last_sent_angle[0] if loop.last_sent_angle else None)
        pitch = loop.goal[1]
        last = loop.last_sent_angle
        if last is not None and max(abs(yaw - last[0]), abs(pitch - last[1])) < self._settings.deadband_deg:
            return None
        # Re-check and send under the lock so a concurrent stop() (manual override)
        # can never be followed by a stale command. UDP send is non-blocking.
        with self._lock:
            if self._generation != gen or self._settings.mode != "angle":
                return None
            try:
                self._gimbal.set_gimbal_angle(yaw, pitch)
            except Exception as exc:  # noqa: BLE001
                logger.warning("ROI gimbal command failed: %s", exc)
                return f"gimbal command failed: {exc}"
            self._loop = replace(self._loop, last_sent_angle=(yaw, pitch))
            self._status = replace(self._status, yaw_cmd_deg=yaw)
        return None

    def _rate_cycle(self, target: RoiTarget, gen: int, now: float) -> Optional[str]:
        s = self._settings
        att = self._gimbal.state.gimbal_attitude
        if (
            att is None
            or now - att.received_at > s.attitude_max_age_s
            or not (math.isfinite(att.yaw) and math.isfinite(att.pitch))
        ):
            return self._halt_with(gen, "no gimbal attitude (0x0D)")

        loop = self._loop
        period = s.control_period_s
        dt = min(now - loop.control_at, DT_MAX_PERIODS * period) if loop.control_at is not None else period
        yaw_error = stable_yaw_error(loop.goal[0], att.yaw, loop.yaw_error)
        pitch_error = loop.goal[1] - att.pitch
        e_yaw, hold_yaw = deadband_with_hysteresis(yaw_error, s.deadband_deg, loop.hold_yaw)
        e_pitch, hold_pitch = deadband_with_hysteresis(pitch_error, s.deadband_deg, loop.hold_pitch)
        gains = s.gains()
        out_yaw, pid_yaw = self._axis(gains, loop.pid_yaw, e_yaw, dt)
        out_pitch, pid_pitch = self._axis(gains, loop.pid_pitch, e_pitch, dt)
        # 0x07: +yaw = right, +pitch = up; attitude/goal use RFU (+yaw = left)
        speed = (round(-out_yaw, 1) + 0.0, round(out_pitch, 1) + 0.0)

        with self._lock:
            if self._generation != gen or self._settings.mode != "rate":
                return None
            try:
                if speed != ZERO or self._loop.last_speed != ZERO:
                    self._gimbal.set_gimbal_speed(speed[0], speed[1])
                    if speed != ZERO:
                        self._zero_resends = 0  # moving again: drop pending stop packets
            except Exception as exc:  # noqa: BLE001
                logger.warning("ROI speed command failed: %s", exc)
                self._halt_locked(force=True)
                return f"gimbal command failed: {exc}"
            self._loop = replace(
                self._loop,
                control_at=now,
                pid_yaw=pid_yaw,
                pid_pitch=pid_pitch,
                hold_yaw=hold_yaw,
                hold_pitch=hold_pitch,
                yaw_error=yaw_error,
                last_speed=speed,
            )
            self._status = replace(
                self._status,
                gimbal_yaw_deg=att.yaw,
                gimbal_pitch_deg=att.pitch,
                speed_yaw=speed[0],
                speed_pitch=speed[1],
            )
        return None

    @staticmethod
    def _axis(gains: PidGains, state: PidState, error: float, dt: float) -> tuple[float, PidState]:
        if error == 0.0:
            return 0.0, PidState()  # inside the deadband: stop and forget the integral
        return pid_step(gains, state, error, dt)

    # ── threads ──────────────────────────────────────────────────
    def start_background(self) -> None:
        if any(t.is_alive() for t in self._threads):
            return
        self._shutdown.clear()
        self._threads = [
            threading.Thread(target=self._run, daemon=True),
            threading.Thread(target=self._watchdog_run, daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def shutdown(self) -> None:
        """Stop the threads and make sure the gimbal is not left moving."""
        self._shutdown.set()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads = []
        with self._lock:
            self._halt_locked(force=True)

    def _run(self) -> None:
        while not self._shutdown.is_set():
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001
                logger.exception("ROI step failed: %s", exc)
                with self._lock:
                    self._halt_locked(force=True)
            self._shutdown.wait(self._settings.control_period_s)

    def _watchdog_run(self) -> None:
        while not self._shutdown.is_set():
            try:
                self.watchdog_check()
            except Exception as exc:  # noqa: BLE001
                logger.exception("ROI watchdog failed: %s", exc)
            self._shutdown.wait(WATCHDOG_INTERVAL_S)
