"""Geodetic helpers for pointing the gimbal at a GPS coordinate (pure functions)."""

import math
from dataclasses import dataclass

WGS84_A = 6378137.0
WGS84_E2 = 6.69437999014e-3

# MT11 pitch range for 0x0E (upright mounting)
PITCH_MIN_DEG = -90.0
PITCH_MAX_DEG = 30.0


@dataclass(frozen=True)
class GeoPoint:
    lat_deg: float
    lon_deg: float
    alt_m: float  # MSL


@dataclass(frozen=True)
class LookAngles:
    bearing_deg: float  # 0..360, clockwise from true north
    elevation_deg: float  # + up, - down
    distance_m: float  # slant range
    horizontal_m: float


@dataclass(frozen=True)
class GimbalAngles:
    yaw_deg: float  # RFU: + = left
    pitch_deg: float  # + = up


def wrap_180(deg: float) -> float:
    """Wrap an angle into (-180, 180]."""
    wrapped = math.fmod(deg + 180.0, 360.0)
    if wrapped <= 0.0:
        wrapped += 360.0
    return wrapped - 180.0


def _to_ecef(p: GeoPoint) -> tuple[float, float, float]:
    lat, lon = math.radians(p.lat_deg), math.radians(p.lon_deg)
    sin_lat = math.sin(lat)
    n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    x = (n + p.alt_m) * math.cos(lat) * math.cos(lon)
    y = (n + p.alt_m) * math.cos(lat) * math.sin(lon)
    z = (n * (1.0 - WGS84_E2) + p.alt_m) * sin_lat
    return x, y, z


def look_angles(own: GeoPoint, target: GeoPoint) -> LookAngles:
    """Bearing/elevation/range from own to target via ECEF -> local ENU."""
    ox, oy, oz = _to_ecef(own)
    tx, ty, tz = _to_ecef(target)
    dx, dy, dz = tx - ox, ty - oy, tz - oz

    lat, lon = math.radians(own.lat_deg), math.radians(own.lon_deg)
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    sin_lon, cos_lon = math.sin(lon), math.cos(lon)
    east = -sin_lon * dx + cos_lon * dy
    north = -sin_lat * cos_lon * dx - sin_lat * sin_lon * dy + cos_lat * dz
    up = cos_lat * cos_lon * dx + cos_lat * sin_lon * dy + sin_lat * dz

    horizontal = math.hypot(east, north)
    bearing = math.degrees(math.atan2(east, north)) % 360.0
    elevation = math.degrees(math.atan2(up, horizontal))
    return LookAngles(
        bearing_deg=bearing,
        elevation_deg=elevation,
        distance_m=math.sqrt(horizontal * horizontal + up * up),
        horizontal_m=horizontal,
    )


def to_gimbal_angles(look: LookAngles, heading_deg: float, yaw_offset_deg: float = 0.0) -> GimbalAngles:
    """Convert earth-frame look angles to MT11 0x0E angles (Follow mode, body-relative yaw)."""
    relative = wrap_180(look.bearing_deg - heading_deg)
    yaw = wrap_180(-relative + yaw_offset_deg)
    pitch = max(PITCH_MIN_DEG, min(PITCH_MAX_DEG, look.elevation_deg))
    return GimbalAngles(yaw_deg=yaw, pitch_deg=pitch)
