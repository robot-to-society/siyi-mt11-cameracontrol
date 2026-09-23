"""Keeps the gimbal pointed at a registered GPS target (ROI) using vehicle position/heading."""

import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Optional, Protocol

from app.geo import GeoPoint, look_angles, to_gimbal_angles
from app.mavlink_source import VehicleState

logger = logging.getLogger(__name__)

MOTION_FOLLOW = 4
# Near +/-180 deg, keep yaw on the previous side (up to this magnitude) so heading
# jitter doesn't flip the command between +179.9 and -179.9 (a full-turn swing).
YAW_HYSTERESIS_LIMIT_DEG = 190.0


def stabilize_yaw(new_deg: float, prev_deg: Optional[float]) -> float:
    if prev_deg is None or abs(new_deg - prev_deg) <= 180.0:
        return new_deg
    candidate = new_deg + (360.0 if prev_deg > 0 else -360.0)
    return candidate if abs(candidate) <= YAW_HYSTERESIS_LIMIT_DEG else new_deg


class GimbalCommander(Protocol):
    def set_gimbal_angle(self, yaw_deg: float, pitch_deg: float) -> None: ...

    def set_gimbal_motion_mode(self, mode: int) -> None: ...


class VehicleSource(Protocol):
    def latest(self) -> Optional[VehicleState]: ...


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
    bearing_deg: Optional[float] = None
    elevation_deg: Optional[float] = None
    distance_m: Optional[float] = None
    yaw_cmd_deg: Optional[float] = None
    pitch_cmd_deg: Optional[float] = None
    last_error: Optional[str] = None
    updated_at: Optional[float] = None


class RoiController:
    def __init__(
        self,
        gimbal: GimbalCommander,
        vehicle: VehicleSource,
        clock: Callable[[], float] = time.monotonic,
        rate_hz: float = 10.0,
        max_age_s: float = 2.0,
        min_distance_m: float = 3.0,
        yaw_offset_deg: float = 0.0,
        follow_mode_interval_s: float = 5.0,
    ):
        self._gimbal = gimbal
        self._vehicle = vehicle
        self._clock = clock
        self._rate_hz = rate_hz
        self._max_age_s = max_age_s
        self._min_distance_m = min_distance_m
        self._yaw_offset_deg = yaw_offset_deg
        self._follow_mode_interval_s = follow_mode_interval_s
        self._last_follow_sent: Optional[float] = None
        self._last_yaw: Optional[float] = None
        self._targets: dict[str, RoiTarget] = {}
        self._status = RoiStatus()
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── configuration ────────────────────────────────────────────
    def set_targets(self, targets: Iterable[RoiTarget]) -> None:
        with self._lock:
            self._targets = {t.id: t for t in targets}
            if self._status.active_target_id not in self._targets:
                self._status = RoiStatus()

    def configure(self, rate_hz: float, yaw_offset_deg: float) -> None:
        self._rate_hz = rate_hz
        self._yaw_offset_deg = yaw_offset_deg

    # ── control ──────────────────────────────────────────────────
    def start(self, target_id: str) -> None:
        """Raises KeyError for unknown ids, ValueError for targets whose coordinates are unset."""
        with self._lock:
            target = self._targets.get(target_id)
            if target is None:
                raise KeyError(target_id)
            if target.lat == 0.0 and target.lon == 0.0:
                raise ValueError(f"ROI target {target_id} has no coordinates")
            self._status = RoiStatus(active_target_id=target_id)
            # Follow mode is sent from the loop thread: camera TCP I/O may block when disconnected
            self._last_follow_sent = None
            self._last_yaw = None

    def stop(self) -> None:
        with self._lock:
            self._status = RoiStatus()

    def status(self) -> RoiStatus:
        return self._status

    def targets(self) -> tuple[RoiTarget, ...]:
        return tuple(self._targets.values())

    # ── loop ─────────────────────────────────────────────────────
    def step(self) -> None:
        with self._lock:
            target = self._targets.get(self._status.active_target_id or "")
        if target is None:
            return
        error = self._compute_and_send(target)
        with self._lock:
            if self._status.active_target_id == target.id:
                self._status = replace(self._status, last_error=error, updated_at=self._clock())

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

    def _compute_and_send(self, target: RoiTarget) -> Optional[str]:
        mode_error = self._ensure_follow_mode()
        if mode_error:
            return mode_error
        state = self._vehicle.latest()
        if state is None:
            return "no vehicle position (MAVLink)"
        if self._clock() - state.received_at > self._max_age_s:
            return "vehicle position stale"
        if state.heading_deg is None:
            return "vehicle heading unavailable"

        own = GeoPoint(state.lat, state.lon, state.alt_msl)
        look = look_angles(own, GeoPoint(target.lat, target.lon, target.alt_msl))
        if look.distance_m < self._min_distance_m:
            return "target too close"

        cmd = to_gimbal_angles(look, state.heading_deg, self._yaw_offset_deg)
        # Re-check and send under the lock so a concurrent stop() (manual override)
        # can never be followed by a stale 0x0E. UDP send is non-blocking.
        with self._lock:
            if self._status.active_target_id != target.id:
                return None
            yaw = stabilize_yaw(cmd.yaw_deg, self._last_yaw)
            self._status = replace(
                self._status,
                bearing_deg=look.bearing_deg,
                elevation_deg=look.elevation_deg,
                distance_m=look.distance_m,
                yaw_cmd_deg=yaw,
                pitch_cmd_deg=cmd.pitch_deg,
            )
            try:
                self._gimbal.set_gimbal_angle(yaw, cmd.pitch_deg)
            except Exception as exc:  # noqa: BLE001
                logger.warning("ROI gimbal command failed: %s", exc)
                return f"gimbal command failed: {exc}"
            self._last_yaw = yaw
        return None

    def start_background(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._shutdown.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._shutdown.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        while not self._shutdown.is_set():
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001
                logger.exception("ROI step failed: %s", exc)
            self._shutdown.wait(1.0 / max(self._rate_hz, 0.1))
