import struct

import pytest

from app.thermal import (
    ThermalFrame,
    ThermalPoint,
    decode_temp,
    encode_full_frame_request,
    encode_point_request,
    parse_full_frame,
    parse_point,
    parse_region,
    thermal_overlay,
)


def test_point_request_matches_sdk_example():
    assert encode_point_request(960, 540, flag=1) == bytes.fromhex("c0031c0201")


def test_full_frame_request():
    assert encode_full_frame_request(flag=1) == b"\x01"
    with pytest.raises(ValueError):
        encode_full_frame_request(flag=3)


@pytest.mark.parametrize("raw,c", [(1604, 16.04), (55000, 550.0), (65536 - 1500, -15.0)])
def test_decode_temp(raw, c):
    assert decode_temp(raw) == pytest.approx(c)


def test_parse_hardware_samples():
    # captured on the MT11 (FW 0.0.9)
    frame = parse_full_frame(bytes.fromhex("6a08e6033e062000f4016203"), received_at=5.0)
    assert frame == ThermalFrame(max_c=21.54, min_c=9.98, max_xy=(1598, 32), min_xy=(500, 866), received_at=5.0)
    point = parse_point(bytes.fromhex("4406c0031c02"), received_at=6.0)
    assert point == ThermalPoint(temp_c=16.04, xy=(960, 540), received_at=6.0)


def test_parse_region():
    payload = struct.pack("<10H", 800, 400, 1120, 680, 1760, 1185, 1100, 570, 826, 654)
    assert parse_region(payload)["max_c"] == pytest.approx(17.6)


def test_short_payloads():
    assert parse_full_frame(b"\x00" * 11, 0.0) is None
    assert parse_point(b"\x00" * 5, 0.0) is None
    assert parse_region(b"\x00" * 19) is None


class TestOverlay:
    FRAME = ThermalFrame(21.5, 9.9, (1920 // 2, 108), (192, 1080 - 1), received_at=10.0)
    POINT = ThermalPoint(16.0, (960, 540), received_at=10.0)

    def test_normalized_positions(self):
        o = thermal_overlay(self.FRAME, self.POINT, 1920, 1080, now=10.5, video_mode="thermal")
        assert o["frame"]["max"] == {"c": 21.5, "x": 0.5, "y": 0.1}
        assert o["frame"]["min"]["x"] == pytest.approx(0.1)
        assert o["point"] == {"c": 16.0, "x": 0.5, "y": 0.5, "age_s": 0.5}

    def test_hidden_outside_thermal_mode(self):
        assert thermal_overlay(self.FRAME, self.POINT, 1920, 1080, now=10.5, video_mode="rgb") is None

    def test_stale_values_dropped(self):
        o = thermal_overlay(self.FRAME, self.POINT, 1920, 1080, now=40.0, video_mode="thermal")
        assert o == {"frame": None, "point": None}

    def test_unknown_resolution(self):
        assert thermal_overlay(self.FRAME, self.POINT, 0, 0, now=10.5, video_mode="thermal") is None
