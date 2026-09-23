"""Keep the MT11 clock on GPS time (from the FC via MAVLink SYSTEM_TIME) for photo timestamps.

FC -> Pi adds a few ms to tens of ms; the Pi-side wait is compensated by adding the time
elapsed since the SYSTEM_TIME sample arrived. After setting (0x30) the camera clock is read
back (0x40) and the offset is measured using the round-trip midpoint.
"""

import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional

from app.mavlink_source import TimeSample

logger = logging.getLogger(__name__)

RESYNC_S = 600.0  # re-set every 10 min to bound camera clock drift
SAMPLE_MAX_AGE_S = 5.0  # SYSTEM_TIME arrives at 1 Hz
READBACK_TIMEOUT_S = 1.0
POLL_S = 0.02
LOOP_S = 1.0


def current_unix_us(sample: TimeSample, now: float) -> int:
    """GPS time now = sample + time elapsed on the Pi since it was received."""
    return sample.unix_us + int(round((now - sample.received_at) * 1_000_000))


@dataclass(frozen=True)
class TimeSyncStatus:
    last_sync_at: Optional[float] = None  # monotonic
    last_sync_ok: Optional[bool] = None
    offset_ms: Optional[float] = None  # camera - GPS, after sync
    rtt_ms: Optional[float] = None
    error: Optional[str] = None


class TimeSync:
    def __init__(
        self,
        camera: Any,
        time_source: Any,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        resync_s: float = RESYNC_S,
    ):
        self._camera = camera
        self._source = time_source
        self._clock = clock
        self._sleep = sleep
        self._resync_s = resync_s
        self._status = TimeSyncStatus()
        self._pending = True  # sync as soon as GPS time is available
        self._was_connected = True
        self._shutdown = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def status(self) -> TimeSyncStatus:
        return self._status

    def request_sync(self) -> None:
        self._pending = True

    def step(self) -> None:
        connected = bool(self._camera.state.connected)
        if connected and not self._was_connected:
            self._pending = True  # camera (re)connected / rebooted
        self._was_connected = connected
        if not connected:
            return
        last = self._status.last_sync_at
        due = self._pending or last is None or self._clock() - last >= self._resync_s
        if due:
            self._sync_once()

    def _sync_once(self) -> None:
        sample = self._source.latest_time()
        now = self._clock()
        if sample is None:
            self._status = replace(self._status, error="no GPS time from FC (SYSTEM_TIME)")
            return
        if now - sample.received_at > SAMPLE_MAX_AGE_S:
            self._status = replace(self._status, error="GPS time from FC is stale")
            return
        try:
            self._camera.set_utc_time(current_unix_us(sample, now))
            offset_ms, rtt_ms = self._measure_offset(sample)
        except OSError as exc:
            logger.warning("Time sync failed: %s", exc)
            self._status = replace(self._status, last_sync_ok=False, error=f"camera command failed: {exc}")
            return
        self._pending = False
        self._status = TimeSyncStatus(
            last_sync_at=now,
            last_sync_ok=True,
            offset_ms=offset_ms,
            rtt_ms=rtt_ms,
            error=None if offset_ms is not None else "camera did not answer 0x40 (time read-back)",
        )

    def _measure_offset(self, sample: TimeSample) -> tuple[Optional[float], Optional[float]]:
        sent_at = self._clock()
        self._camera.request_system_time()
        deadline = sent_at + READBACK_TIMEOUT_S
        while True:
            reply = self._camera.state.camera_time
            if reply is not None and reply[1] >= sent_at:
                camera_us, received_at = reply
                midpoint = (sent_at + received_at) / 2.0
                offset_ms = (camera_us - current_unix_us(sample, midpoint)) / 1000.0
                return round(offset_ms, 1), round((received_at - sent_at) * 1000.0, 1)
            if self._clock() >= deadline:
                return None, None
            self._sleep(POLL_S)

    def start_background(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._shutdown.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._shutdown.is_set():
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001
                logger.exception("time sync step failed: %s", exc)
            self._shutdown.wait(LOOP_S)
