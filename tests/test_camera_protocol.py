import struct

import pytest

from app.camera_protocol import CameraClient, encode_gimbal_angle, make_packet


def sdk_bytes(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str.replace(" ", ""))


class TestEncodeGimbalAngle:
    def test_matches_sdk_example_yaw30_pitch20(self):
        packet = make_packet(0x0E, encode_gimbal_angle(30.0, 20.0), seq=0)
        assert packet == sdk_bytes("55 66 01 04 00 00 00 0E 2C 01 C8 00 59 17")

    def test_matches_sdk_example_yaw0_pitch_minus90(self):
        packet = make_packet(0x0E, encode_gimbal_angle(0.0, -90.0), seq=0)
        assert packet == sdk_bytes("55 66 01 04 00 00 00 0E 00 00 7C FC 4F A4")

    def test_rounds_to_tenth_degree(self):
        assert encode_gimbal_angle(60.54, 0.0) == (605).to_bytes(2, "little", signed=True) + b"\x00\x00"

    def test_clamps_pitch_range(self):
        assert encode_gimbal_angle(0.0, 45.0) == encode_gimbal_angle(0.0, 30.0)
        assert encode_gimbal_angle(0.0, -120.0) == encode_gimbal_angle(0.0, -90.0)


class RecordingClient(CameraClient):
    def __init__(self):
        super().__init__()
        self.sent: list[tuple[str, int, bytes]] = []

    def send_udp_cmd(self, cmd_id, data=b"", ctrl=0x01):
        self.sent.append(("udp", cmd_id, data))

    def send_cmd(self, cmd_id, data=b"", ctrl=0x01):
        self.sent.append(("tcp", cmd_id, data))


def test_set_gimbal_angle_uses_udp_0x0e():
    client = RecordingClient()
    client.set_gimbal_angle(30.0, 20.0)
    assert client.sent == [("udp", 0x0E, sdk_bytes("2C 01 C8 00"))]


def test_set_gimbal_motion_mode_follow():
    client = RecordingClient()
    client.set_gimbal_motion_mode(CameraClient.MOTION_FOLLOW)
    assert client.sent == [("tcp", 0x0C, b"\x04")]


def test_send_cmd_when_disconnected_does_not_deadlock():
    import socket
    import threading

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()  # nothing listens here -> connection refused

    client = CameraClient(host="127.0.0.1", port=free_port)
    outcome = {}

    def run():
        try:
            client.send_cmd(0x0C, b"\x04")
        except OSError as exc:
            outcome["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=5.0)
    assert not t.is_alive(), "send_cmd deadlocked on camera lock"
    assert "error" in outcome


class TestAiTrackingCommands:
    def test_ai_select_box_and_cancel(self):
        from app.ai_tracking import StreamBox

        client = RecordingClient()
        client.ai_select_box(StreamBox(200, 200, 400, 400))
        client.ai_cancel_tracking()
        assert client.sent == [
            ("tcp", 0x56, sdk_bytes("01 C8 00 C8 00 90 01 90 01")),
            ("tcp", 0x56, b"\x00" * 9),
        ]

    def test_set_track_stream(self):
        client = RecordingClient()
        client.set_track_stream(True)
        client.set_track_stream(False)
        assert client.sent == [("tcp", 0x51, b"\x01"), ("tcp", 0x51, b"\x00")]

    def test_set_encoding_params(self):
        from app.ai_tracking import find_preset

        client = RecordingClient()
        client.set_encoding_params(find_preset("h264_720p"))
        assert client.sent == [("tcp", 0x21, struct.pack("<BBHHHB", 1, 1, 1280, 720, 0, 0))]


class TestHandleFrame:
    def test_0x20_main_stream_encoding(self):
        client = CameraClient()
        client._handle_frame(0x20, struct.pack("<BBHHHB", 1, 1, 1920, 1080, 6000, 30))
        assert client.state.encoding.codec == "h264"
        assert (client.state.stream_width, client.state.stream_height) == (1920, 1080)

    def test_0x20_other_stream_ignored(self):
        client = CameraClient()
        client._handle_frame(0x20, struct.pack("<BBHHHB", 0, 2, 3840, 2160, 0, 30))
        assert client.state.encoding is None
        assert client.state.stream_width == 0

    def test_0x21_ack(self):
        client = CameraClient()
        client._handle_frame(0x21, b"\x01\x01")
        assert client.state.encoding_set_ok is True
        client._handle_frame(0x21, b"\x01\x00")
        assert client.state.encoding_set_ok is False

    def test_0x55_and_0x56_ack(self):
        client = CameraClient()
        client._handle_frame(0x55, b"\x01\x03")
        client._handle_frame(0x56, b"\x04")
        assert client.state.ai_mode_result == "video stabilization enabled: AI recognition unavailable"
        assert client.state.ai_select_result == "selected area has insufficient texture"

    def test_0x50_track(self):
        client = CameraClient()
        client._handle_frame(0x50, struct.pack("<HHHHBB", 640, 360, 128, 72, 1, 0))
        assert client.state.track.target_type == "car"
        assert client.state.track.x == pytest.approx(0.45)


def test_set_encoding_clears_stale_resolution():
    from app.ai_tracking import find_preset

    client = RecordingClient()
    client._handle_frame(0x20, struct.pack("<BBHHHB", 1, 1, 1920, 1080, 6000, 30))
    client.set_encoding_params(find_preset("h264_4k"))
    assert (client.state.stream_width, client.state.stream_height) == (0, 0)
    assert client.state.encoding is None
    assert client.state.encoding_set_ok is None


class TestDetections:
    def test_set_candidate_push_and_point(self):
        client = RecordingClient()
        client.set_candidate_push(True)
        client.set_candidate_push(False)
        client.ai_select_point(960, 540)
        assert client.sent == [
            ("tcp", 0x5F, b"\x05"),
            ("tcp", 0x5F, b"\x04"),
            ("tcp", 0x56, sdk_bytes("01 C0 03 1C 02 00 00 00 00")),
        ]

    def test_0x5f_frames_kept_as_recent_history(self, monkeypatch):
        import app.camera_protocol as cp
        from tests.test_ai_tracking import candidate_payload

        clock = {"t": 100.0}
        monkeypatch.setattr(cp.time, "monotonic", lambda: clock["t"])
        client = CameraClient()
        client._handle_frame(0x5F, b"\x05\x00")  # enable ACK: ignored
        assert client.state.detection_history == ()
        client._handle_frame(0x5F, candidate_payload([(0.1, 0.1, 0.2, 0.2, 0.9, 0)]))
        clock["t"] = 101.0
        client._handle_frame(0x5F, candidate_payload([(0.2, 0.1, 0.3, 0.2, 0.9, 0)]))
        assert len(client.state.detection_history) == 2
        clock["t"] = 103.0
        client._handle_frame(0x5F, candidate_payload([]))
        assert len(client.state.detection_history) == 1  # older than 1.5 s pruned


def test_rx_debug_records_counts_and_last_payload():
    client = CameraClient()
    frame = make_packet(0x5F, b"\x05\x00\x01\x02", seq=1)
    buf = bytearray(frame + make_packet(0x5F, b"\x05\x00", seq=2))
    client._parse_buffer(buf)
    info = client.rx_debug()
    assert info["counts"]["0x5F"] == 2
    assert info["last"]["0x5F"]["len"] == 2
    assert info["last"]["0x5F"]["hex"] == "0500"


class TestFirmware:
    def test_parse_sdk_examples(self):
        from app.camera_protocol import parse_firmware_versions

        # camera 0x89 01 00 01 -> v1.0.1, gimbal 0x8A 00 01 0A -> v0.1.10 (bytes little-endian on the wire)
        payload = bytes([0x01, 0x00, 0x01, 0x89, 0x0A, 0x01, 0x00, 0x8A, 0, 0, 0, 0])
        assert parse_firmware_versions(payload) == {"camera": "v1.0.1", "gimbal": "v0.1.10", "zoom": "v0.0.0"}

    def test_short_payload(self):
        from app.camera_protocol import parse_firmware_versions

        assert parse_firmware_versions(b"\x01\x02") is None

    def test_handle_and_request(self):
        client = RecordingClient()
        client.request_firmware_version()
        assert client.sent == [("tcp", 0x01, b"")]
        client._handle_frame(0x01, bytes([1, 0, 1, 0x89, 0x0A, 1, 0, 0x8A, 0, 0, 0, 0]))
        assert client.state.firmware["gimbal"] == "v0.1.10"


class TestTimeCommands:
    def test_set_utc_time_and_request(self):
        client = RecordingClient()
        client.set_utc_time(1_790_000_000_123_456)
        client.request_system_time()
        assert client.sent == [
            ("tcp", 0x30, struct.pack("<Q", 1_790_000_000_123_456)),
            ("tcp", 0x40, b""),
        ]

    def test_acks(self, monkeypatch):
        import app.camera_protocol as cp

        monkeypatch.setattr(cp.time, "monotonic", lambda: 7.5)
        client = CameraClient()
        client._handle_frame(0x30, b"\x01")
        assert client.state.utc_set_ok is True
        client._handle_frame(0x30, b"\x00")
        assert client.state.utc_set_ok is False
        client._handle_frame(0x40, struct.pack("<QI", 1_790_000_000_000_000, 5000))
        assert client.state.camera_time == (1_790_000_000_000_000, 7.5)
