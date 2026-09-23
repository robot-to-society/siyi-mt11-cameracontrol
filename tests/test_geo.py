import math

import pytest

from app.geo import GeoPoint, LookAngles, look_angles, to_gimbal_angles, wrap_180

OWN = GeoPoint(lat_deg=35.0, lon_deg=139.0, alt_m=0.0)


def offset_point(north_m: float, east_m: float, up_m: float = 0.0) -> GeoPoint:
    """Small-offset helper using WGS84 local radii (accurate for a few km)."""
    a, e2 = 6378137.0, 6.69437999014e-3
    sin_lat = math.sin(math.radians(OWN.lat_deg))
    meridional = a * (1 - e2) / (1 - e2 * sin_lat**2) ** 1.5
    normal = a / math.sqrt(1 - e2 * sin_lat**2)
    dlat = math.degrees(north_m / meridional)
    dlon = math.degrees(east_m / (normal * math.cos(math.radians(OWN.lat_deg))))
    return GeoPoint(OWN.lat_deg + dlat, OWN.lon_deg + dlon, OWN.alt_m + up_m)


class TestWrap180:
    @pytest.mark.parametrize(
        "deg,expected",
        [(0, 0), (180, 180), (-180, 180), (190, -170), (-190, 170), (360, 0), (725, 5)],
    )
    def test_wrap(self, deg, expected):
        assert wrap_180(deg) == pytest.approx(expected)


class TestLookAngles:
    def test_north(self):
        look = look_angles(OWN, offset_point(1000, 0))
        assert wrap_180(look.bearing_deg) == pytest.approx(0.0, abs=0.05)
        assert look.horizontal_m == pytest.approx(1000, rel=0.01)

    def test_east(self):
        look = look_angles(OWN, offset_point(0, 1000))
        assert look.bearing_deg == pytest.approx(90.0, abs=0.05)

    def test_south_west(self):
        look = look_angles(OWN, offset_point(-500, -500))
        assert look.bearing_deg == pytest.approx(225.0, abs=0.02)

    def test_bearing_in_0_360(self):
        look = look_angles(OWN, offset_point(1000, -10))
        assert 0.0 <= look.bearing_deg < 360.0
        assert look.bearing_deg == pytest.approx(359.43, abs=0.05)

    def test_elevation_up_45(self):
        look = look_angles(OWN, offset_point(100, 0, up_m=100))
        assert look.elevation_deg == pytest.approx(45.0, abs=0.2)
        assert look.distance_m == pytest.approx(math.hypot(100, 100), rel=0.01)

    def test_elevation_down_from_altitude(self):
        own = GeoPoint(OWN.lat_deg, OWN.lon_deg, 100.0)
        look = look_angles(own, offset_point(100, 0, up_m=0))
        assert look.elevation_deg == pytest.approx(-45.0, abs=0.2)

    def test_straight_down(self):
        own = GeoPoint(OWN.lat_deg, OWN.lon_deg, 50.0)
        look = look_angles(own, OWN)
        assert look.elevation_deg == pytest.approx(-90.0, abs=0.01)
        assert look.horizontal_m == pytest.approx(0.0, abs=0.01)

    def test_earth_curvature_lowers_far_target(self):
        look = look_angles(OWN, offset_point(10_000, 0))
        # Target on the ellipsoid 10 km away sits ~7.8 m below the local horizon
        assert look.elevation_deg == pytest.approx(-0.045, abs=0.01)


class TestToGimbalAngles:
    def _look(self, bearing, elevation=0.0):
        return LookAngles(bearing_deg=bearing, elevation_deg=elevation, distance_m=100.0, horizontal_m=100.0)

    def test_target_ahead(self):
        g = to_gimbal_angles(self._look(10), heading_deg=10)
        assert g.yaw_deg == pytest.approx(0.0)

    def test_target_right_is_negative_yaw(self):
        g = to_gimbal_angles(self._look(90), heading_deg=0)
        assert g.yaw_deg == pytest.approx(-90.0)

    def test_target_left_is_positive_yaw_across_north(self):
        g = to_gimbal_angles(self._look(350), heading_deg=10)
        assert g.yaw_deg == pytest.approx(20.0)

    def test_yaw_offset(self):
        g = to_gimbal_angles(self._look(0), heading_deg=0, yaw_offset_deg=2.5)
        assert g.yaw_deg == pytest.approx(2.5)

    def test_pitch_clamped_to_upper_limit(self):
        g = to_gimbal_angles(self._look(0, elevation=60), heading_deg=0)
        assert g.pitch_deg == pytest.approx(30.0)

    def test_pitch_passthrough(self):
        g = to_gimbal_angles(self._look(0, elevation=-45), heading_deg=0)
        assert g.pitch_deg == pytest.approx(-45.0)
