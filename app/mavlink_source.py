"""Vehicle position/heading from a flight controller via MAVLink (e.g. via Rpanion-server telemetry destination)."""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

MSG_ID_GLOBAL_POSITION_INT = 33
MAV_CMD_SET_MESSAGE_INTERVAL = 511
MAV_AUTOPILOT_INVALID = 8
HDG_UNKNOWN = 65535

POSITION_INTERVAL_US = 100_000  # 10 Hz
REQUEST_RETRY_S = 5.0
STALE_REQUEST_S = 3.0


@dataclass(frozen=True)
class VehicleState:
    lat: float
    lon: float
    alt_msl: float
    heading_deg: Optional[float]
    received_at: float  # monotonic seconds


def vehicle_state_from_global_position(msg: Any, received_at: float) -> Optional[VehicleState]:
    """GLOBAL_POSITION_INT -> VehicleState. Returns None when there is no position fix."""
    if msg.lat == 0 and msg.lon == 0:
        return None
    heading = None if msg.hdg == HDG_UNKNOWN else msg.hdg / 100.0
    return VehicleState(
        lat=msg.lat / 1e7,
        lon=msg.lon / 1e7,
        alt_msl=msg.alt / 1000.0,
        heading_deg=heading,
        received_at=received_at,
    )


def _default_connect(url: str) -> Any:
    from pymavlink import mavutil  # imported lazily so tests don't need pymavlink

    return mavutil.mavlink_connection(url, source_system=1, source_component=191)


class MavlinkSource:
    def __init__(
        self,
        url: str,
        clock: Callable[[], float] = time.monotonic,
        connect: Callable[[str], Any] = _default_connect,
        join_timeout_s: float = 1.0,
    ):
        self._url = url
        self._clock = clock
        self._connect = connect
        self._join_timeout_s = join_timeout_s
        self._state: Optional[VehicleState] = None
        self._connected = False
        self._last_error: Optional[str] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._target: Optional[tuple[int, int]] = None
        self._last_request = -1e9

    @property
    def url(self) -> str:
        return self._url

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def latest(self) -> Optional[VehicleState]:
        return self._state

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        # A fresh event per thread: a thread stuck past stop()'s join timeout keeps
        # its own (set) event and exits later instead of being revived by clear().
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop_event, self._url), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=self._join_timeout_s)
        self._thread = None
        self._connected = False

    def restart(self, url: str) -> None:
        self.stop()
        self._url = url
        self._state = None
        self._target = None
        self.start()

    def _run(self, stop_event: Optional[threading.Event] = None, url: Optional[str] = None) -> None:
        stop_event = stop_event or self._stop_event
        url = url or self._url
        while not stop_event.is_set():
            conn = None
            try:
                conn = self._connect(url)
                if stop_event.is_set():
                    return
                self._connected = True
                self._last_error = None
                self._receive_loop(conn, stop_event)
            except Exception as exc:  # noqa: BLE001
                if stop_event.is_set():
                    return
                self._last_error = f"mavlink: {exc}"
                logger.warning("MAVLink connection error (%s): %s", url, exc)
                stop_event.wait(2.0)
            finally:
                if not stop_event.is_set():
                    self._connected = False
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass

    def _receive_loop(self, conn: Any, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            msg = conn.recv_match(type=["HEARTBEAT", "GLOBAL_POSITION_INT"], blocking=True, timeout=1.0)
            if msg is None:
                continue
            if msg.get_type() == "HEARTBEAT":
                self._on_heartbeat(conn, msg)
            elif self._target is None or msg.get_srcSystem() == self._target[0]:
                state = vehicle_state_from_global_position(msg, self._clock())
                if state is not None and not stop_event.is_set():
                    self._state = state

    def _on_heartbeat(self, conn: Any, msg: Any) -> None:
        if msg.autopilot == MAV_AUTOPILOT_INVALID:
            return  # GCS / companion, not the flight controller
        self._target = (msg.get_srcSystem(), msg.get_srcComponent())
        now = self._clock()
        state = self._state
        is_stale = state is None or now - state.received_at > STALE_REQUEST_S
        if is_stale and now - self._last_request >= REQUEST_RETRY_S:
            self._request_position_stream(conn)
            self._last_request = now

    def _request_position_stream(self, conn: Any) -> None:
        target_system, target_component = self._target
        conn.mav.command_long_send(
            target_system,
            target_component,
            MAV_CMD_SET_MESSAGE_INTERVAL,
            0,
            MSG_ID_GLOBAL_POSITION_INT,
            POSITION_INTERVAL_US,
            0, 0, 0, 0, 0,
        )
