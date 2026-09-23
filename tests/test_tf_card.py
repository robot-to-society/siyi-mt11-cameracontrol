import struct

import pytest

from app.tf_card import parse_tf_card


def payload(status, fs, total_x100, free_x100):
    return struct.pack("<BBHH", status, fs, total_x100, free_x100)


def test_parse_normal_card():
    info = parse_tf_card(payload(2, 2, 5820, 1234))
    assert info.status == "ok"
    assert info.filesystem == "exFAT"
    assert info.total_gb == pytest.approx(58.20)
    assert info.free_gb == pytest.approx(12.34)
    assert info.free_ratio == pytest.approx(12.34 / 58.20)


@pytest.mark.parametrize(
    "code,status",
    [(0, "not_inserted"), (1, "mount_failed"), (3, "low_space"), (4, "read_only"), (5, "read_error")],
)
def test_status_codes(code, status):
    assert parse_tf_card(payload(code, 1, 0, 0)).status == status


def test_zero_total_has_no_ratio():
    assert parse_tf_card(payload(0, 0, 0, 0)).free_ratio is None


def test_short_payload():
    assert parse_tf_card(b"\x02\x01\x00") is None
