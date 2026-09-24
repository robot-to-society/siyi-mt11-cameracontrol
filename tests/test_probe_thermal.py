import struct

import pytest

from app.camera_protocol import make_packet
from scripts.probe_thermal_readonly import PROBES, build_probe_packet, decode_reply


def test_only_read_only_ids_are_probed():
    assert set(PROBES) == {0x33, 0x37, 0x39, 0x3B, 0x42, 0x44, 0x46}
    # destructive / state-changing commands must never be in the list
    for dangerous in (0x48, 0x80, 0x4F, 0x3A, 0x3C, 0x43, 0x45, 0x47, 0x34, 0x35, 0x0C, 0x21, 0x82):
        assert dangerous not in PROBES


def test_packet_has_no_payload():
    assert build_probe_packet(0x39, seq=0) == make_packet(0x39, b"", seq=0)


@pytest.mark.parametrize("cmd", [0x48, 0x3A, 0x80, 0x00])
def test_refuses_anything_else(cmd):
    with pytest.raises(ValueError):
        build_probe_packet(cmd, seq=0)


def test_decode_env_params():
    payload = struct.pack("<HHHHH", 1000, 9500, 6000, 2500, 2300)
    result = decode_reply(0x39, payload)
    assert result["length_ok"] is True
    assert result["values"] == {
        "dist_m": 10.0,
        "emissivity_raw_x100": 95.0,
        "humidity_pct": 60.0,
        "air_temp_c": 25.0,
        "reflected_temp_c": 23.0,
    }


def test_decode_threshold_params():
    region = struct.pack("<BhhBBB", 1, 50, 120, 255, 0, 0)
    payload = region + struct.pack("<BhhBBB", 0, 0, 0, 0, 0, 0) * 2
    result = decode_reply(0x44, payload)
    assert result["length_ok"] is True
    assert result["values"]["region1"] == {"on": 1, "min": 50, "max": 120, "rgb": [255, 0, 0]}


def test_decode_single_byte_and_unexpected_length():
    assert decode_reply(0x3B, b"\x01") == {"length_ok": True, "values": {"env_correct": 1}}
    bad = decode_reply(0x3B, b"\x01\x02\x03")
    assert bad["length_ok"] is False
    assert bad["values"] is None


class FakeSocket:
    """Scripted camera: replies per CMD_ID, or drops the connection."""

    def __init__(self, behaviour):
        self.behaviour = behaviour  # cmd -> bytes payload | "close" | None
        self.pending = b""
        self.closed = False

    def settimeout(self, _t):
        pass

    def sendall(self, packet):
        if self.closed:
            raise BrokenPipeError(32, "Broken pipe")
        cmd = packet[7]
        action = self.behaviour.get(cmd)
        if action == "close":
            self.closed = True
        elif action is not None:
            self.pending += make_packet(cmd, action, seq=0)

    def recv(self, _n):
        if self.closed:
            return b""
        if not self.pending:
            import socket

            raise socket.timeout()
        data, self.pending = self.pending, b""
        return data

    def close(self):
        pass


def test_probe_uses_fresh_connection_per_command_and_reports_drops(monkeypatch):
    import scripts.probe_thermal_readonly as probe_mod

    behaviour = {0x37: b"\x01", 0x39: "close", 0x3B: None}
    opened = []

    def connect(addr, timeout=None):
        sock = FakeSocket(behaviour)
        opened.append(sock)
        return sock

    monkeypatch.setattr(probe_mod.socket, "create_connection", connect)
    results, frames_seen = probe_mod.probe("cam", 37260, wait_s=0.05)
    by_cmd = {r["cmd"]: r for r in results}
    assert len(opened) == len(probe_mod.PROBES)  # one connection per command
    assert by_cmd["0x37"]["status"] == "answered" and by_cmd["0x37"]["values"] == {"ir_gain": 1}
    assert by_cmd["0x39"]["status"] == "connection closed by camera"
    assert by_cmd["0x3B"]["status"] == "no reply"
    assert frames_seen == 1
