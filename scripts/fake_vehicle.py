"""Bench test: pretend to be a flight controller and stream position/heading to the app.

Sends HEARTBEAT + GLOBAL_POSITION_INT at 10 Hz, so the real MT11 can be tested on a desk
without flying. Use a separate port from Rpanion's telemetry destination (15555) so the
real FC does not mix in, and temporarily set the ROI tab "MAVLink URL" to
udpin:127.0.0.1:15556.

Usage (on the Pi):
    python -m scripts.fake_vehicle --lat 35.681236 --lon 139.767125 --alt 40 --heading 0

Commands (type + Enter):
    +10 / -10        rotate heading relative (deg)
    h 90             set heading (deg, true north = 0, clockwise)
    p <lat> <lon>    set position
    a <alt_msl>      set altitude (m, MSL)
    q                quit
"""

import argparse
import threading
import time
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class FakeVehicleState:
    lat: float
    lon: float
    alt_msl: float
    heading_deg: float

    def global_position_int_fields(self, time_boot_ms: int) -> dict:
        return {
            "time_boot_ms": time_boot_ms,
            "lat": int(round(self.lat * 1e7)),
            "lon": int(round(self.lon * 1e7)),
            "alt": int(round(self.alt_msl * 1000)),
            "relative_alt": 0,
            "vx": 0,
            "vy": 0,
            "vz": 0,
            "hdg": int(round(self.heading_deg * 100)) % 36000,
        }


def _float(text: str, name: str) -> float:
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number: {text!r}") from exc


def apply_command(state: FakeVehicleState, line: str) -> FakeVehicleState:
    """Return a new state for one command line. Raises ValueError on bad input."""
    parts = line.split()
    if not parts:
        raise ValueError("empty command")
    head, args = parts[0], parts[1:]

    if head[0] in "+-" and not args:
        delta = _float(head, "heading delta")
        return replace(state, heading_deg=(state.heading_deg + delta) % 360.0)
    if head == "h" and len(args) == 1:
        return replace(state, heading_deg=_float(args[0], "heading") % 360.0)
    if head == "p" and len(args) == 2:
        lat, lon = _float(args[0], "lat"), _float(args[1], "lon")
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            raise ValueError("lat/lon out of range")
        return replace(state, lat=lat, lon=lon)
    if head == "a" and len(args) == 1:
        return replace(state, alt_msl=_float(args[0], "alt"))
    raise ValueError(f"unknown command: {line!r}")


class Streamer:
    def __init__(self, url: str, state: FakeVehicleState, rate_hz: float = 10.0):
        from pymavlink import mavutil

        self._mavutil = mavutil
        self._conn = mavutil.mavlink_connection(url, source_system=1, source_component=1)
        self._state = state
        self._period = 1.0 / rate_hz
        self._stop = threading.Event()
        self._boot = time.monotonic()

    def set_state(self, state: FakeVehicleState) -> None:
        self._state = state  # replaced atomically, never mutated

    def run(self) -> None:
        mav = self._conn.mav
        ml = self._mavutil.mavlink
        while not self._stop.is_set():
            mav.heartbeat_send(ml.MAV_TYPE_QUADROTOR, ml.MAV_AUTOPILOT_ARDUPILOTMEGA, 0, 0, ml.MAV_STATE_STANDBY)
            ms = int((time.monotonic() - self._boot) * 1000)
            mav.global_position_int_send(**self._state.global_position_int_fields(ms))
            self._stop.wait(self._period)

    def stop(self) -> None:
        self._stop.set()


def describe(state: FakeVehicleState) -> str:
    return f"lat={state.lat:.7f} lon={state.lon:.7f} alt={state.alt_msl:.1f}m heading={state.heading_deg:.1f}°"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--alt", type=float, required=True, help="MSL altitude of the camera (m)")
    parser.add_argument("--heading", type=float, default=0.0, help="direction the camera base front faces (deg)")
    parser.add_argument("--url", default="udpout:127.0.0.1:15556")
    args = parser.parse_args()

    state = FakeVehicleState(args.lat, args.lon, args.alt, args.heading % 360.0)
    streamer = Streamer(args.url, state)
    thread = threading.Thread(target=streamer.run, daemon=True)
    thread.start()
    print(f"streaming to {args.url}: {describe(state)}")
    print("commands: +N / -N / h DEG / p LAT LON / a ALT / q")

    try:
        while True:
            line = input("> ").strip()
            if line == "q":
                break
            try:
                state = apply_command(state, line)
            except ValueError as exc:
                print(f"  error: {exc}")
                continue
            streamer.set_state(state)
            print(f"  {describe(state)}")
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        streamer.stop()
        thread.join(timeout=1.0)


if __name__ == "__main__":
    main()
