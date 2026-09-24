import struct

import pytest

from app.camera_protocol import make_packet
from scripts.probe_temperature import (
    build_request,
    decode_temp,
    parse_full_frame,
    parse_point,
    parse_region,
)


def sdk_bytes(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str.replace(" ", ""))


class TestRequests:
    def test_point_matches_sdk_example(self):
        payload = build_request("point", flag=1, point=(960, 540))
        assert make_packet(0x12, payload[1], seq=0) == sdk_bytes("55 66 01 05 00 00 00 12 C0 03 1C 02 01 AC D7")
        assert payload[0] == 0x12

    def test_region_matches_sdk_example(self):
        cmd, data = build_request("region", flag=1, box=(200, 200, 400, 400))
        assert make_packet(cmd, data, seq=0) == sdk_bytes(
            "55 66 01 09 00 00 00 13 C8 00 C8 00 90 01 90 01 01 7C 03"
        )

    def test_full_frame(self):
        assert build_request("full", flag=2) == (0x14, b"\x02")

    def test_disable_flag(self):
        assert build_request("full", flag=0) == (0x14, b"\x00")

    @pytest.mark.parametrize("flag", [3, -1])
    def test_invalid_flag(self, flag):
        with pytest.raises(ValueError):
            build_request("full", flag=flag)

    def test_unknown_kind(self):
        with pytest.raises(ValueError):
            build_request("format", flag=1)


class TestDecode:
    @pytest.mark.parametrize(
        "raw,celsius",
        [(2534, 25.34), (0, 0.0), (55000, 550.0), (65536 - 1500, -15.0)],
    )
    def test_decode_temp(self, raw, celsius):
        assert decode_temp(raw) == pytest.approx(celsius)

    def test_full_frame(self):
        payload = struct.pack("<HHHHHH", 4512, 1850, 320, 256, 10, 20)
        assert parse_full_frame(payload) == {
            "max_c": 45.12,
            "min_c": 18.5,
            "max_xy": (320, 256),
            "min_xy": (10, 20),
        }

    def test_point(self):
        assert parse_point(struct.pack("<HHH", 3650, 960, 540)) == {"temp_c": 36.5, "xy": (960, 540)}

    def test_region(self):
        payload = struct.pack("<10H", 200, 200, 400, 400, 6000, 2000, 300, 310, 210, 220)
        assert parse_region(payload) == {
            "box": (200, 200, 400, 400),
            "max_c": 60.0,
            "min_c": 20.0,
            "max_xy": (300, 310),
            "min_xy": (210, 220),
        }

    def test_short_payloads(self):
        assert parse_full_frame(b"\x00" * 11) is None
        assert parse_point(b"\x00" * 5) is None
        assert parse_region(b"\x00" * 19) is None
