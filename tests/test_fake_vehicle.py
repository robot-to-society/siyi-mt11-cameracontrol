import pytest

from scripts.fake_vehicle import FakeVehicleState, apply_command


BASE = FakeVehicleState(lat=35.0, lon=139.0, alt_msl=20.0, heading_deg=0.0)


def test_relative_heading_wraps():
    assert apply_command(BASE, "-10").heading_deg == pytest.approx(350.0)
    assert apply_command(BASE, "+370").heading_deg == pytest.approx(10.0)


def test_absolute_heading():
    assert apply_command(BASE, "h 90").heading_deg == pytest.approx(90.0)


def test_position_and_altitude():
    moved = apply_command(BASE, "p 35.1 139.2")
    assert (moved.lat, moved.lon) == (35.1, 139.2)
    assert apply_command(BASE, "a 55.5").alt_msl == pytest.approx(55.5)


def test_original_state_not_mutated():
    apply_command(BASE, "h 45")
    assert BASE.heading_deg == 0.0


@pytest.mark.parametrize("line", ["", "x", "h", "h abc", "p 91 0", "p 1", "+abc"])
def test_invalid_commands_raise(line):
    with pytest.raises(ValueError):
        apply_command(BASE, line)


def test_global_position_fields():
    fields = BASE.global_position_int_fields(time_boot_ms=1234)
    assert fields == {
        "time_boot_ms": 1234,
        "lat": 350000000,
        "lon": 1390000000,
        "alt": 20000,
        "relative_alt": 0,
        "vx": 0,
        "vy": 0,
        "vz": 0,
        "hdg": 0,
    }
    assert apply_command(BASE, "h 359.99").global_position_int_fields(0)["hdg"] == 35999
