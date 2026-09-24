"""MT11 thermal temperature measurement (0x12 point, 0x13 region, 0x14 full frame).

Verified on FW 0.0.9: coordinates are video-stream pixels (1920x1080), temperatures uint16 / 100.
"""

import struct
from dataclasses import dataclass
from typing import Optional

FLAG_DISABLE, FLAG_ONCE, FLAG_CONTINUOUS = 0, 1, 2
# Low gain measures up to 550 C, so the field is unsigned; values above this are
# taken as negative (two's complement) temperatures - to be confirmed below 0 C.
NEGATIVE_THRESHOLD_C = 600.0
FRAME_MAX_AGE_S = 3.0  # full-frame is polled at ~1 Hz
POINT_MAX_AGE_S = 15.0  # an Alt+click reading stays on screen this long


@dataclass(frozen=True)
class ThermalFrame:
    max_c: float
    min_c: float
    max_xy: tuple[int, int]
    min_xy: tuple[int, int]
    received_at: float


@dataclass(frozen=True)
class ThermalPoint:
    temp_c: float
    xy: tuple[int, int]
    received_at: float


def decode_temp(raw: int) -> float:
    celsius = raw / 100.0
    if celsius > NEGATIVE_THRESHOLD_C:
        celsius = (raw - 65536) / 100.0
    return round(celsius, 2)


def _check_flag(flag: int) -> None:
    if flag not in (FLAG_DISABLE, FLAG_ONCE, FLAG_CONTINUOUS):
        raise ValueError(f"invalid get_temp_flag: {flag}")


def encode_point_request(x: int, y: int, flag: int = FLAG_ONCE) -> bytes:
    _check_flag(flag)
    return struct.pack("<HHB", x, y, flag)


def encode_region_request(box: tuple[int, int, int, int], flag: int = FLAG_ONCE) -> bytes:
    _check_flag(flag)
    return struct.pack("<HHHHB", *box, flag)


def encode_full_frame_request(flag: int = FLAG_ONCE) -> bytes:
    _check_flag(flag)
    return struct.pack("<B", flag)


def parse_full_frame(payload: bytes, received_at: float) -> Optional[ThermalFrame]:
    if len(payload) < 12:
        return None
    tmax, tmin, max_x, max_y, min_x, min_y = struct.unpack("<6H", payload[:12])
    return ThermalFrame(decode_temp(tmax), decode_temp(tmin), (max_x, max_y), (min_x, min_y), received_at)


def parse_point(payload: bytes, received_at: float) -> Optional[ThermalPoint]:
    if len(payload) < 6:
        return None
    temp, x, y = struct.unpack("<3H", payload[:6])
    return ThermalPoint(decode_temp(temp), (x, y), received_at)


def parse_region(payload: bytes) -> Optional[dict]:
    if len(payload) < 20:
        return None
    x0, y0, x1, y1, tmax, tmin, max_x, max_y, min_x, min_y = struct.unpack("<10H", payload[:20])
    return {
        "box": (x0, y0, x1, y1),
        "max_c": decode_temp(tmax),
        "min_c": decode_temp(tmin),
        "max_xy": (max_x, max_y),
        "min_xy": (min_x, min_y),
    }


def _norm(xy: tuple[int, int], width: int, height: int) -> dict:
    return {"x": round(xy[0] / width, 4), "y": round(xy[1] / height, 4)}


def thermal_overlay(
    frame: Optional[ThermalFrame],
    point: Optional[ThermalPoint],
    width: int,
    height: int,
    now: float,
    video_mode: str,
) -> Optional[dict]:
    """Overlay data with positions normalized to 0..1; None when not in thermal view."""
    if video_mode != "thermal" or width <= 1 or height <= 1:
        return None
    frame_fresh = frame is not None and now - frame.received_at <= FRAME_MAX_AGE_S
    point_fresh = point is not None and now - point.received_at <= POINT_MAX_AGE_S
    return {
        "frame": (
            {
                "max": {"c": frame.max_c, **_norm(frame.max_xy, width, height)},
                "min": {"c": frame.min_c, **_norm(frame.min_xy, width, height)},
            }
            if frame_fresh
            else None
        ),
        "point": (
            {"c": point.temp_c, **_norm(point.xy, width, height), "age_s": round(now - point.received_at, 1)}
            if point_fresh
            else None
        ),
    }
