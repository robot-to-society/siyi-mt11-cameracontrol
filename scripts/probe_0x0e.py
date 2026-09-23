"""Step 0 hardware probe: how does MT11 interpret 0x0E yaw/pitch in Follow mode?

Usage (on the Pi, with the camera UI service stopped):
    python -m scripts.probe_0x0e --host 192.168.144.25 --yaw 90 --pitch -30

1. Sets Follow mode (0x0C/4) and commands the given angle (0x0E).
2. Prints gimbal attitude (0x0D) and encoder angle (0x26) once per second.
While it runs, rotate / tilt the vehicle by hand and watch:
  - yaw:   camera keeps the same angle *relative to the nose*  -> body-relative (expected)
  - pitch: camera keeps pointing at the horizon while tilting   -> earth-relative (expected)
"""

import argparse
import socket
import struct
import time

from app.camera_protocol import encode_gimbal_angle, make_packet


def read_frames(sock: socket.socket, buffer: bytearray) -> list[tuple[int, bytes]]:
    try:
        buffer.extend(sock.recv(4096))
    except socket.timeout:
        return []
    frames = []
    while True:
        start = buffer.find(b"\x55\x66")
        if start < 0 or len(buffer) - start < 10:
            return frames
        del buffer[:start]
        data_len = struct.unpack("<H", buffer[3:5])[0]
        frame_len = 10 + data_len
        if len(buffer) < frame_len:
            return frames
        frames.append((buffer[7], bytes(buffer[8 : 8 + data_len])))
        del buffer[:frame_len]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="192.168.144.25")
    parser.add_argument("--port", type=int, default=37260)
    parser.add_argument("--yaw", type=float, default=90.0, help="deg, + = left")
    parser.add_argument("--pitch", type=float, default=-30.0, help="deg, + = up")
    parser.add_argument("--seconds", type=float, default=60.0)
    args = parser.parse_args()

    sock = socket.create_connection((args.host, args.port), timeout=3.0)
    sock.settimeout(0.3)
    seq = 0

    def send(cmd_id: int, data: bytes = b"") -> None:
        nonlocal seq
        seq = (seq + 1) & 0xFFFF
        sock.sendall(make_packet(cmd_id, data, seq=seq))

    send(0x0C, b"\x04")
    time.sleep(0.3)
    send(0x0E, encode_gimbal_angle(args.yaw, args.pitch))
    print(f"commanded yaw={args.yaw} pitch={args.pitch} (Follow mode)")

    buffer = bytearray()
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        send(0x00, b"\x00")  # heartbeat
        send(0x0D)
        send(0x26)
        end = time.monotonic() + 1.0
        while time.monotonic() < end:
            for cmd_id, payload in read_frames(sock, buffer):
                if cmd_id == 0x0D and len(payload) >= 6:
                    yaw, pitch, roll = (v / 10.0 for v in struct.unpack("<hhh", payload[:6]))
                    print(f"  attitude(0x0D) yaw={yaw:7.1f} pitch={pitch:6.1f} roll={roll:6.1f}")
                elif cmd_id == 0x26 and len(payload) >= 6:
                    yaw, pitch, roll = (v / 10.0 for v in struct.unpack("<hhh", payload[:6]))
                    print(f"  encoder (0x26) yaw={yaw:7.1f} pitch={pitch:6.1f} roll={roll:6.1f}")
                elif cmd_id == 0x0E and len(payload) >= 4:
                    print(f"  0x0E ack: {struct.unpack('<hh', payload[:4])}")
    sock.close()


if __name__ == "__main__":
    main()
