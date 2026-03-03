import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional


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


class CameraClient:
    def __init__(self, host: str = "192.168.144.25", port: int = 37260):
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.lock = threading.Lock()
        self.seq = 0
        self.state = CameraState()
        self._stop_event = threading.Event()
        self._recv_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._on_state_change: Optional[Callable[[CameraState], None]] = None

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
        seq = self._next_seq()
        packet = make_packet(cmd_id=cmd_id, data=data, ctrl=ctrl, seq=seq)
        self._send_raw(packet)

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

    def ai_select_tracking(self, action: int, lx: int = 0, ly: int = 0, rx: int = 0, ry: int = 0) -> None:
        """CMD 0x56: AI Select Tracking Target (TCP)
        action: 1=Track, 0=Cancel
        lx,ly: top-left coord; rx,ry: bottom-right coord (1280x720 stream)
        """
        payload = struct.pack("<BHHHH", action, lx, ly, rx, ry)
        self.send_cmd(cmd_id=0x56, data=payload, ctrl=0x01)

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
            self._handle_frame(cmd_id, payload)

    def _handle_frame(self, cmd_id: int, payload: bytes) -> None:
        if cmd_id == 0x20 and len(payload) >= 6:
            # Camera encoding parameters: [stream, codec, width_lo, width_hi, height_lo, height_hi, ...]
            width = struct.unpack("<H", payload[2:4])[0]
            height = struct.unpack("<H", payload[4:6])[0]
            if width > 0 and height > 0:
                self.state.stream_width = width
                self.state.stream_height = height
                self._notify_state_change()
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
