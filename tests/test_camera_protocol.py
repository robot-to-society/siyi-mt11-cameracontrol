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
