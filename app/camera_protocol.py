import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from app.tf_card import TfCardInfo, parse_tf_card
from app.thermal import (
    FLAG_ONCE,
    ThermalFrame,
    ThermalPoint,
    encode_full_frame_request,
    encode_point_request,
    parse_full_frame,
    parse_point,
)
from app.ai_tracking import (
    AI_MODE_RESULT,
    AI_SELECT_RESULT,
    DetectionFrame,
    EncodingParams,
    EncodingPreset,
    StreamBox,
    TrackTarget,
    encode_ai_select,
    encode_ai_select_point,
    encode_encoding_params,
    parse_candidate_frame,
    parse_encoding,
    parse_track_frame,
)


def crc16_ccitt(data: bytes, init_crc: int = 0x0000) -> int:
    crc = init_crc & 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def make_packet(cmd_id: int, data: bytes = b"", ctrl: int = 0x01, seq: int = 0) -> bytes:
    packet = bytearray()
    packet += b"\x55\x66"
    packet += struct.pack("<B", ctrl)
    packet += struct.pack("<H", len(data))
    packet += struct.pack("<H", seq)
    packet += struct.pack("<B", cmd_id)
    packet += data
    crc = crc16_ccitt(bytes(packet), init_crc=0x0000)
    packet += struct.pack("<H", crc)
    return bytes(packet)


def encode_gimbal_angle(yaw_deg: float, pitch_deg: float) -> bytes:
    """CMD 0x0E payload: int16 yaw/pitch in 0.1 deg (RFU, +yaw = left, +pitch = up)."""
    pitch = max(-90.0, min(30.0, pitch_deg))
    return struct.pack("<hh", int(round(yaw_deg * 10.0)), int(round(pitch * 10.0)))


def parse_firmware_versions(payload: bytes) -> Optional[dict]:
    """0x01 ACK: camera/gimbal/zoom uint32 (LE). Low 3 bytes = patch, minor, major; top byte = model."""
    if len(payload) < 12:
        return None
    versions = {}
    for name, offset in (("camera", 0), ("gimbal", 4), ("zoom", 8)):
        patch_v, minor, major = payload[offset], payload[offset + 1], payload[offset + 2]
        versions[name] = f"v{major}.{minor}.{patch_v}"
    return versions


@dataclass(frozen=True)
class GimbalAttitude:
    """0x0D: yaw (body-relative in Follow mode, +left), pitch (horizon, +up), roll in degrees."""

    yaw: float
    pitch: float
    roll: float
    received_at: float  # monotonic


def parse_gimbal_attitude(payload: bytes, received_at: float) -> Optional[GimbalAttitude]:
    if len(payload) < 6:
        return None
    yaw, pitch, roll = struct.unpack("<hhh", payload[:6])
    return GimbalAttitude(yaw / 10.0, pitch / 10.0, roll / 10.0, received_at)


GIMBAL_MODES = {0: "lock", 1: "follow", 2: "fpv"}  # 0x19 ACK
THERMAL_GAINS = {0: "low", 1: "high"}  # 0x37 / 0x38 Ir_gain
# Measurement range per gain (UniPod MT11 User Manual v1.0 spec table)
THERMAL_GAIN_RANGE_C = {"high": (-20, 150), "low": (0, 550)}
GIMBAL_MODE_FUNC = {"lock": 3, "follow": 4, "fpv": 5}  # 0x0C func_type


# Commands this app must never send. 0x48 formats the SD card (and the SDK text mislabels
# TF-card info as 0x48 in one place), so it is blocked at the lowest send level.
FORBIDDEN_CMD_IDS = frozenset({0x48})


def _check_allowed(cmd_id: int) -> None:
    if cmd_id in FORBIDDEN_CMD_IDS:
        raise ValueError(f"CMD 0x{cmd_id:02X} is blocked (SD card format)")


DETECTION_HISTORY_S = 1.5  # keep enough candidate frames to cover the video latency
DETECTION_HISTORY_MAX = 60


@dataclass
class CameraState:
    record_sta: int = 0
    zoom_current: float = 1.0
    zoom_max: float = 165.0
    video_mode_main: int = 0
    video_mode_sub: int = 2
    video_mode_name: str = "rgb"
    stream_width: int = 0   # 0 = unknown (not yet queried)
    stream_height: int = 0
    last_feedback: Optional[int] = None
    last_error: Optional[str] = None
    connected: bool = False
    updated_at: float = 0.0
    encoding: Optional[EncodingParams] = None  # main stream (0x20)
    encoding_set_ok: Optional[bool] = None  # last 0x21 ACK
    ai_mode_result: Optional[str] = None  # last 0x55 ACK
    ai_select_result: Optional[str] = None  # last 0x56 ACK
    ai_select_at: float = 0.0
    track: Optional[TrackTarget] = None  # last 0x50 frame
    detection_history: tuple[DetectionFrame, ...] = ()  # recent 0x5F frames (oldest first)
    firmware: Optional[dict] = None  # 0x01 (camera/gimbal/zoom versions)
    utc_set_ok: Optional[bool] = None  # last 0x30 ACK
    camera_time: Optional[tuple[int, float]] = None  # last 0x40: (camera unix us, monotonic received)
    tf_card: Optional[TfCardInfo] = None  # last 0x49
    gimbal_attitude: Optional[GimbalAttitude] = None  # last 0x0D
    gimbal_mode: Optional[str] = None  # last 0x19: lock / follow / fpv
    thermal_gain: Optional[str] = None  # last 0x37 / 0x38: low / high
    thermal_frame: Optional[ThermalFrame] = None  # last 0x14 (full-frame max/min)
    thermal_point: Optional[ThermalPoint] = None  # last 0x12 (point temperature)


class CameraClient:
    MOTION_LOCK = 3
    MOTION_FOLLOW = 4
    MOTION_FPV = 5

    def __init__(self, host: str = "192.168.144.25", port: int = 37260):
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.lock = threading.RLock()  # re-entrant: _send_raw() holds it while calling connect()
        self.seq = 0
        self.state = CameraState()
        self._stop_event = threading.Event()
        self._recv_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._on_state_change: Optional[Callable[[CameraState], None]] = None
        # Diagnostics: frames received per CMD_ID and the last payload of each (for /api/debug/rx)
        self._rx_counts: dict[int, int] = {}
        self._rx_last: dict[int, tuple[float, bytes]] = {}

    def set_on_state_change(self, callback: Callable[[CameraState], None]) -> None:
        self._on_state_change = callback

    def _notify_state_change(self) -> None:
        self.state.updated_at = time.time()
        if self._on_state_change:
            self._on_state_change(self.state)

    def _next_seq(self) -> int:
        self.seq = (self.seq + 1) & 0xFFFF
        return self.seq

    def configure_host(self, host: str) -> None:
        self.host = host.strip()
        self.reconnect()

    def connect(self) -> None:
        with self.lock:
            if self.sock:
                return
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3.0)
            s.connect((self.host, self.port))
            s.settimeout(1.0)
            self.sock = s
            self.state.connected = True
            self.state.last_error = None
            self._notify_state_change()

        self._stop_event.clear()
        if not self._recv_thread or not self._recv_thread.is_alive():
            self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
            self._recv_thread.start()
        if not self._heartbeat_thread or not self._heartbeat_thread.is_alive():
            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._heartbeat_thread.start()

    def disconnect(self) -> None:
        self._stop_event.set()
        with self.lock:
            if self.sock:
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None
        self.state.connected = False
        self._notify_state_change()

    def reconnect(self) -> None:
        self.disconnect()
        try:
            self.connect()
        except OSError as exc:
            self.state.last_error = str(exc)
            self._notify_state_change()

    def _send_raw(self, packet: bytes) -> None:
        with self.lock:
            if not self.sock:
                self.connect()
            if not self.sock:
                raise ConnectionError("camera socket is unavailable")
            self.sock.sendall(packet)

    def send_cmd(self, cmd_id: int, data: bytes = b"", ctrl: int = 0x01) -> None:
        _check_allowed(cmd_id)
        seq = self._next_seq()
        packet = make_packet(cmd_id=cmd_id, data=data, ctrl=ctrl, seq=seq)
        self._send_raw(packet)

    def request_firmware_version(self) -> None:
        """CMD 0x01: Request Firmware Version (TCP)"""
        self.send_cmd(cmd_id=0x01, data=b"", ctrl=0x01)

    def set_utc_time(self, unix_us: int) -> None:
        """CMD 0x30: Set UTC Time, UNIX epoch microseconds (TCP)"""
        self.send_cmd(cmd_id=0x30, data=struct.pack("<Q", unix_us), ctrl=0x01)

    def request_system_time(self) -> None:
        """CMD 0x40: Request System Time (TCP)"""
        self.send_cmd(cmd_id=0x40, data=b"", ctrl=0x01)

    def request_gimbal_mode(self) -> None:
        """CMD 0x19: Request Current Gimbal Mode (TCP)"""
        self.send_cmd(cmd_id=0x19, data=b"", ctrl=0x01)

    def set_gimbal_mode_name(self, name: str) -> None:
        """Lock / Follow / FPV via CMD 0x0C (func 3/4/5)."""
        if name not in GIMBAL_MODE_FUNC:
            raise ValueError(f"unsupported gimbal mode: {name}")
        self.set_gimbal_motion_mode(GIMBAL_MODE_FUNC[name])

    def request_gimbal_attitude(self) -> None:
        """CMD 0x0D: Request Gimbal Attitude Data (TCP)"""
        self.send_cmd(cmd_id=0x0D, data=b"", ctrl=0x01)

    def request_tf_card_info(self) -> None:
        """CMD 0x49: Request TF Card Information (TCP). NOT 0x48, which formats the card."""
        self.send_cmd(cmd_id=0x49, data=b"", ctrl=0x01)

    def request_status(self) -> None:
        self.send_cmd(cmd_id=0x0A, data=b"", ctrl=0x01)

    def request_zoom_level(self) -> None:
        self.send_cmd(cmd_id=0x18, data=b"", ctrl=0x01)

    def request_video_mode(self) -> None:
        self.send_cmd(cmd_id=0x10, data=b"", ctrl=0x01)

    def request_encoding_params(self) -> None:
        """CMD 0x20: Request Camera Encoding Parameters (main stream)"""
        self.send_cmd(cmd_id=0x20, data=b"\x01", ctrl=0x01)

    def set_absolute_zoom(self, zoom: float) -> None:
        clamped = max(1.0, min(zoom, self.state.zoom_max))
        int_part = int(clamped)
        float_part = int(round((clamped - int_part) * 10.0))
        if float_part > 9:
            int_part += 1
            float_part = 0
        payload = struct.pack("<BB", int_part, float_part)
        self.send_cmd(cmd_id=0x0F, data=payload, ctrl=0x01)

    def zoom_in_step(self, step: float = 1.0) -> None:
        self.set_absolute_zoom(self.state.zoom_current + step)

    def zoom_out_step(self, step: float = 1.0) -> None:
        self.set_absolute_zoom(self.state.zoom_current - step)

    def set_video_mode(self, main_stream: int, sub_stream: int) -> None:
        payload = struct.pack("<BB", main_stream & 0xFF, sub_stream & 0xFF)
        self.send_cmd(cmd_id=0x11, data=payload, ctrl=0x01)

    def set_video_mode_preset(self, preset: str) -> None:
        key = preset.strip().lower()
        # The spec text has minor inconsistencies; these pairs follow Chapter 4 examples.
        presets = {
            "rgb": (0x00, 0x02),   # Main: wide-angle (RGB), Sub: thermal
            "thermal": (0x02, 0x00),  # Main: thermal, Sub: wide-angle
            "side_by_side": (0x03, 0x02),  # Main: wide-angle + thermal
        }
        if key not in presets:
            raise ValueError(f"unsupported video mode preset: {preset}")
        main_stream, sub_stream = presets[key]
        self.set_video_mode(main_stream, sub_stream)

    def trigger_photo(self) -> None:
        self.send_cmd(cmd_id=0x0C, data=b"\x00", ctrl=0x01)

    def toggle_record(self) -> None:
        self.send_cmd(cmd_id=0x0C, data=b"\x02", ctrl=0x01)

    def start_record(self) -> None:
        if self.state.record_sta != 1:
            self.toggle_record()

    def stop_record(self) -> None:
        if self.state.record_sta == 1:
            self.toggle_record()

    def _ensure_udp_socket(self) -> None:
        """UDP ソケットを遅延初期化 (ジンバル速度コマンド用)"""
        if not hasattr(self, "_udp_sock") or self._udp_sock is None:
            self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send_udp_cmd(self, cmd_id: int, data: bytes = b"", ctrl: int = 0x01) -> None:
        """UDP経由でコマンドを送信 (ジンバル速度制御など)"""
        _check_allowed(cmd_id)
        self._ensure_udp_socket()
        seq = self._next_seq()
        packet = make_packet(cmd_id=cmd_id, data=data, ctrl=ctrl, seq=seq)
        self._udp_sock.sendto(packet, (self.host, self.port))

    def set_gimbal_speed(self, yaw: float, pitch: float) -> None:
        """CMD 0x07: ジンバル速度コマンド (UDP)
        yaw, pitch: -100.0〜+100.0 (int8に変換)
        yaw>0=右, pitch>0=上
        """
        yaw_i = int(max(-100, min(100, round(yaw))))
        pitch_i = int(max(-100, min(100, round(pitch))))
        payload = struct.pack("<bb", yaw_i, pitch_i)
        self.send_udp_cmd(cmd_id=0x07, data=payload)

    def set_gimbal_angle(self, yaw_deg: float, pitch_deg: float) -> None:
        """CMD 0x0E: ジンバル角度指令 (UDP)
        yaw: +=左, pitch: +=上 (pitch は -90〜+30 にクランプ)
        """
        self.send_udp_cmd(cmd_id=0x0E, data=encode_gimbal_angle(yaw_deg, pitch_deg))

    def set_gimbal_motion_mode(self, mode: int) -> None:
        """CMD 0x0C: モーションモード (TCP)
        mode: 3=Lock, 4=Follow, 5=FPV
        """
        if mode not in (self.MOTION_LOCK, self.MOTION_FOLLOW, self.MOTION_FPV):
            raise ValueError(f"unsupported motion mode: {mode}")
        self.send_cmd(cmd_id=0x0C, data=struct.pack("<B", mode), ctrl=0x01)

    def center_gimbal(self, mode: int = 1) -> None:
        """CMD 0x08: センターコマンド (UDP)
        mode: 1=One-Key Reset, 2=Center+face down, 3=Center, 4=Face downward
        """
        payload = struct.pack("<B", mode)
        self.send_udp_cmd(cmd_id=0x08, data=payload)

    def zoom_speed(self, direction: int) -> None:
        """CMD 0x05: Manual Zoom (TCP)
        direction: 1=ズームイン, 0=停止, -1=ズームアウト
        """
        val = max(-1, min(1, direction))
        payload = struct.pack("<b", val)
        self.send_cmd(cmd_id=0x05, data=payload, ctrl=0x01)

    def manual_focus(self, direction: int) -> None:
        """CMD 0x06: Manual Focus (TCP)
        direction: 1=far, 0=stop, -1=near
        """
        val = max(-1, min(1, direction))
        self.send_cmd(cmd_id=0x06, data=struct.pack("<b", val), ctrl=0x01)

    def request_full_frame_temperature(self, flag: int = FLAG_ONCE) -> None:
        """CMD 0x14: Full-Frame Temperature Measurement (TCP, ~1 Hz update)"""
        self.send_cmd(cmd_id=0x14, data=encode_full_frame_request(flag), ctrl=0x01)

    def request_point_temperature(self, x: int, y: int, flag: int = FLAG_ONCE) -> None:
        """CMD 0x12: temperature at a point in video-stream pixels (TCP)"""
        self.send_cmd(cmd_id=0x12, data=encode_point_request(x, y, flag), ctrl=0x01)

    def request_thermal_gain(self) -> None:
        """CMD 0x37: Request Thermal Imaging Gain Mode (TCP)"""
        self.send_cmd(cmd_id=0x37, data=b"", ctrl=0x01)

    def set_thermal_gain(self, gain: int) -> None:
        """CMD 0x38: Set Thermal Imaging Gain Mode (TCP)
        gain: 0=Low Gain, 1=High Gain
        """
        self.send_cmd(cmd_id=0x38, data=struct.pack("<B", gain & 0x01), ctrl=0x01)

    def set_thermal_palette(self, palette: int) -> None:
        """CMD 0x1B: Set Thermal Palette (TCP)
        palette: 0=White_Hot, 2=Sepia, 3=Ironbow, 4=Rainbow, 5=Night,
                 6=Aurora, 7=Red_Hot, 8=Jungle, 9=Medical, 10=Black_Hot, 11=Glory_Hot
        """
        self.send_cmd(cmd_id=0x1B, data=struct.pack("<B", palette & 0xFF), ctrl=0x01)

    def set_ai_mode(self, enable: bool) -> None:
        """CMD 0x55: Enable/Disable AI Tracking Mode (TCP)"""
        self.send_cmd(cmd_id=0x55, data=struct.pack("<B", 1 if enable else 0), ctrl=0x01)

    def ai_select_box(self, box: StreamBox) -> None:
        """CMD 0x56: AI Select Tracking Target, box selection in main-stream pixels (TCP)"""
        self.send_cmd(cmd_id=0x56, data=encode_ai_select(box), ctrl=0x01)

    def ai_cancel_tracking(self) -> None:
        """CMD 0x56: Cancel tracking (TCP)"""
        self.send_cmd(cmd_id=0x56, data=encode_ai_select(None), ctrl=0x01)

    def ai_select_point(self, x: int, y: int) -> None:
        """CMD 0x56: point selection (camera picks the detected object at x,y) (TCP)"""
        self.send_cmd(cmd_id=0x56, data=encode_ai_select_point(x, y), ctrl=0x01)

    def set_candidate_push(self, enable: bool) -> None:
        """CMD 0x5F: enable/disable AI candidate bounding box push (TCP)"""
        self.send_cmd(cmd_id=0x5F, data=b"\x05" if enable else b"\x04", ctrl=0x01)

    def set_track_stream(self, enable: bool) -> None:
        """CMD 0x51: enable/disable 0x50 tracking box push to this connection (TCP)"""
        self.send_cmd(cmd_id=0x51, data=b"\x01" if enable else b"\x00", ctrl=0x01)

    def set_encoding_params(self, preset: EncodingPreset) -> None:
        """CMD 0x21: Set main stream encoding (TCP). Recording stream is separate.
        The known resolution is cleared so click-to-track waits for a fresh 0x20
        instead of mapping clicks onto the old resolution."""
        self.state.stream_width = 0
        self.state.stream_height = 0
        self.state.encoding = None
        self.state.encoding_set_ok = None
        self.send_cmd(cmd_id=0x21, data=encode_encoding_params(preset), ctrl=0x01)

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.send_cmd(cmd_id=0x00, data=b"\x00", ctrl=0x01)
            except Exception as exc:  # noqa: BLE001
                self.state.connected = False
                self.state.last_error = str(exc)
                self._notify_state_change()
                try:
                    self.reconnect()
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(1.0)

    def _recv_loop(self) -> None:
        buffer = bytearray()
        while not self._stop_event.is_set():
            try:
                if not self.sock:
                    time.sleep(0.1)
                    continue
                data = self.sock.recv(4096)
                if not data:
                    raise ConnectionError("camera disconnected")
                buffer.extend(data)
                self._parse_buffer(buffer)
            except socket.timeout:
                continue
            except Exception as exc:  # noqa: BLE001
                self.state.connected = False
                self.state.last_error = str(exc)
                self._notify_state_change()
                try:
                    self.reconnect()
                except Exception:  # noqa: BLE001
                    time.sleep(1.0)

    def _parse_buffer(self, buffer: bytearray) -> None:
        while True:
            start = buffer.find(b"\x55\x66")
            if start < 0:
                buffer.clear()
                return
            if start > 0:
                del buffer[:start]

            if len(buffer) < 10:
                return

            data_len = struct.unpack("<H", buffer[3:5])[0]
            frame_len = 2 + 1 + 2 + 2 + 1 + data_len + 2
            if len(buffer) < frame_len:
                return

            frame = bytes(buffer[:frame_len])
            del buffer[:frame_len]

            expected_crc = struct.unpack("<H", frame[-2:])[0]
            actual_crc = crc16_ccitt(frame[:-2], init_crc=0x0000)
            if expected_crc != actual_crc:
                continue

            cmd_id = frame[7]
            payload = frame[8:-2]
            self._rx_counts[cmd_id] = self._rx_counts.get(cmd_id, 0) + 1
            self._rx_last[cmd_id] = (time.monotonic(), payload)
            self._handle_frame(cmd_id, payload)

    def rx_debug(self, max_hex_bytes: int = 160) -> dict:
        """Counts and last payload (hex) per received CMD_ID."""
        now = time.monotonic()
        return {
            "counts": {f"0x{cmd:02X}": n for cmd, n in sorted(self._rx_counts.items())},
            "last": {
                f"0x{cmd:02X}": {
                    "age_s": round(now - at, 1),
                    "len": len(payload),
                    "hex": payload[:max_hex_bytes].hex(),
                }
                for cmd, (at, payload) in sorted(self._rx_last.items())
            },
        }

    def _handle_frame(self, cmd_id: int, payload: bytes) -> None:
        if cmd_id == 0x20:
            encoding = parse_encoding(payload)
            if encoding and encoding.stream_type == 1 and encoding.width > 0 and encoding.height > 0:
                self.state.encoding = encoding
                self.state.stream_width = encoding.width
                self.state.stream_height = encoding.height
                self._notify_state_change()
        elif cmd_id == 0x50:
            track = parse_track_frame(payload, received_at=time.monotonic())
            if track is not None:
                self.state.track = track
        elif cmd_id == 0x14:
            frame = parse_full_frame(payload, received_at=time.monotonic())
            if frame is not None:
                self.state.thermal_frame = frame
        elif cmd_id == 0x12:
            point = parse_point(payload, received_at=time.monotonic())
            if point is not None:
                self.state.thermal_point = point
        elif cmd_id in (0x37, 0x38) and len(payload) >= 1:
            self.state.thermal_gain = THERMAL_GAINS.get(payload[0], f"gain_{payload[0]}")
        elif cmd_id == 0x19 and len(payload) >= 1:
            self.state.gimbal_mode = GIMBAL_MODES.get(payload[0], f"mode_{payload[0]}")
            self._notify_state_change()
        elif cmd_id == 0x0D:
            attitude = parse_gimbal_attitude(payload, received_at=time.monotonic())
            if attitude is not None:
                self.state.gimbal_attitude = attitude
        elif cmd_id == 0x49:
            info = parse_tf_card(payload)
            if info is not None:
                self.state.tf_card = info
        elif cmd_id == 0x30 and len(payload) >= 1:
            self.state.utc_set_ok = payload[0] == 1
        elif cmd_id == 0x40 and len(payload) >= 8:
            self.state.camera_time = (struct.unpack("<Q", payload[:8])[0], time.monotonic())
        elif cmd_id == 0x01:
            versions = parse_firmware_versions(payload)
            if versions is not None:
                self.state.firmware = versions
        elif cmd_id == 0x5F:
            frame = parse_candidate_frame(payload, received_at=time.monotonic())
            if frame is not None:
                recent = tuple(
                    f for f in self.state.detection_history
                    if frame.received_at - f.received_at <= DETECTION_HISTORY_S
                )
                self.state.detection_history = (*recent[-(DETECTION_HISTORY_MAX - 1):], frame)
        elif cmd_id == 0x21 and len(payload) >= 2:
            self.state.encoding_set_ok = payload[1] == 1
            self._notify_state_change()
        elif cmd_id == 0x55 and len(payload) >= 2:
            self.state.ai_mode_result = AI_MODE_RESULT.get(payload[1], f"status {payload[1]}")
        elif cmd_id == 0x56 and len(payload) >= 1:
            self.state.ai_select_result = AI_SELECT_RESULT.get(payload[0], f"status {payload[0]}")
            self.state.ai_select_at = time.monotonic()
        elif cmd_id == 0x0A and len(payload) >= 4:
            self.state.record_sta = payload[3]
            self._notify_state_change()
        elif cmd_id == 0x0B and len(payload) >= 1:
            self.state.last_feedback = payload[0]
            if payload[0] == 5:
                self.state.record_sta = 1
            elif payload[0] == 6:
                self.state.record_sta = 0
            self._notify_state_change()
        elif cmd_id == 0x18 and len(payload) >= 2:
            self.state.zoom_current = float(payload[0]) + (float(payload[1]) / 10.0)
            self._notify_state_change()
        elif cmd_id in (0x10, 0x11) and len(payload) >= 2:
            self.state.video_mode_main = payload[0]
            self.state.video_mode_sub = payload[1]
            mode_map = {
                (0, 2): "rgb",
                (2, 0): "thermal",
                (3, 2): "side_by_side",
                (1, 2): "rgb",
                (2, 1): "thermal",
                (4, 2): "side_by_side",
            }
            self.state.video_mode_name = mode_map.get(
                (self.state.video_mode_main, self.state.video_mode_sub),
                "custom",
            )
            self._notify_state_change()
