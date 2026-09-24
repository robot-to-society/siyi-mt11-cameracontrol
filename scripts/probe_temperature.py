"""Thermal temperature measurement test (documented MT11 SDK commands only).

  full   : 0x14 full-frame max / min temperature and their positions
  point  : 0x12 temperature at --point X Y
  region : 0x13 max / min inside --box X0 Y0 X1 Y1
Coordinates are video-stream pixels (SDK example: centre of 1920x1080 = 960,540).
Switch the video to Thermal first (Camera tab) so the thermal core is measuring.

Usage (on the Pi):
    python -m scripts.probe_temperature                      # full frame, once per second x 5
    python -m scripts.probe_temperature --kind point --point 960 540 --count 10
    python -m scripts.probe_temperature --kind region --box 800 400 1120 680 --continuous

--continuous uses flag 2 (camera pushes at ~5 Hz) and sends flag 0 (disable) on exit.
"""

import argparse
import socket
import struct
import time
from typing import Optional

from app.camera_protocol import make_packet

CMDS = {"point": 0x12, "region": 0x13, "full": 0x14}
FLAG_DISABLE, FLAG_ONCE, FLAG_CONTINUOUS = 0, 1, 2
# Low gain measures up to 550 C, so the field is unsigned; values above this are
# taken as negative (two's complement) temperatures - to be confirmed on hardware.
NEGATIVE_THRESHOLD_C = 600.0


def decode_temp(raw: int) -> float:
    celsius = raw / 100.0
    if celsius > NEGATIVE_THRESHOLD_C:
        celsius = (raw - 65536) / 100.0
    return round(celsius, 2)


def build_request(kind: str, flag: int, point=(0, 0), box=(0, 0, 0, 0)) -> tuple[int, bytes]:
    if kind not in CMDS:
        raise ValueError(f"unknown measurement kind: {kind}")
    if flag not in (FLAG_DISABLE, FLAG_ONCE, FLAG_CONTINUOUS):
        raise ValueError(f"invalid get_temp_flag: {flag}")
    if kind == "point":
        return 0x12, struct.pack("<HHB", point[0], point[1], flag)
    if kind == "region":
        return 0x13, struct.pack("<HHHHB", *box, flag)
    return 0x14, struct.pack("<B", flag)


def parse_full_frame(payload: bytes) -> Optional[dict]:
    if len(payload) < 12:
        return None
    tmax, tmin, max_x, max_y, min_x, min_y = struct.unpack("<6H", payload[:12])
    return {"max_c": decode_temp(tmax), "min_c": decode_temp(tmin), "max_xy": (max_x, max_y), "min_xy": (min_x, min_y)}


def parse_point(payload: bytes) -> Optional[dict]:
    if len(payload) < 6:
        return None
    temp, x, y = struct.unpack("<3H", payload[:6])
    return {"temp_c": decode_temp(temp), "xy": (x, y)}


def parse_region(payload: bytes) -> Optional[dict]:
    if len(payload) < 20:
        return None
    x0, y0, x1, y1, tmax, tmin, max_x, max_y, min_x, min_y = struct.unpack("<10H", payload[:20])
    return {
        "box": (x0, y0, x1, y1),
        "max_c": decode_temp(tmax),
        "min_c": decode_temp(tmin),
        "max_xy": (max_x, max_y),
        "min_xy": (min_x, min_y),
    }


PARSERS = {0x12: parse_point, 0x13: parse_region, 0x14: parse_full_frame}


def _read_frames(sock: socket.socket, buffer: bytearray) -> list[tuple[int, bytes]]:
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
        if len(buffer) < 10 + data_len:
            return frames
        frames.append((buffer[7], bytes(buffer[8 : 8 + data_len])))
        del buffer[: 10 + data_len]


def _print_reply(cmd: int, payload: bytes) -> None:
    parsed = PARSERS[cmd](payload)
    stamp = time.strftime("%H:%M:%S")
    print(f"{stamp} len={len(payload)} hex={payload.hex()}")
    print(f"         {parsed if parsed is not None else 'unexpected length'}")


def run(args: argparse.Namespace) -> None:
    point = tuple(args.point) if args.point else (0, 0)
    box = tuple(args.box) if args.box else (0, 0, 0, 0)
    flag = FLAG_CONTINUOUS if args.continuous else FLAG_ONCE
    cmd, data = build_request(args.kind, flag, point=point, box=box)

    sock = socket.create_connection((args.host, args.port), timeout=3.0)
    sock.settimeout(0.2)
    buffer = bytearray()
    seq = 0

    def send(payload: bytes) -> None:
        nonlocal seq
        seq = (seq + 1) & 0xFFFF
        sock.sendall(make_packet(cmd, payload, seq=seq))

    received = 0
    try:
        if args.continuous:
            send(data)
        for _ in range(args.count):
            if not args.continuous:
                send(data)
            deadline = time.monotonic() + args.interval
            while time.monotonic() < deadline:
                for got, payload in _read_frames(sock, buffer):
                    if got == cmd:
                        _print_reply(cmd, payload)
                        received += 1
                    elif args.verbose:
                        # other traffic (e.g. a reply under a different CMD_ID)
                        print(f"  other frame 0x{got:02X} len={len(payload)} hex={payload[:40].hex()}")
        if received == 0:
            print(f"no reply to 0x{cmd:02X}: is the video in Thermal mode? (Camera tab -> サーマル映像)")
            print("  try --continuous, and --verbose to see every frame the camera sends")
    except KeyboardInterrupt:
        pass
    finally:
        if args.continuous:
            send(build_request(args.kind, FLAG_DISABLE, point=point, box=box)[1])
            print("continuous measurement disabled")
        sock.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="192.168.144.25")
    parser.add_argument("--port", type=int, default=37260)
    parser.add_argument("--kind", choices=sorted(CMDS), default="full")
    parser.add_argument("--point", type=int, nargs=2, metavar=("X", "Y"))
    parser.add_argument("--box", type=int, nargs=4, metavar=("X0", "Y0", "X1", "Y1"))
    parser.add_argument("--count", type=int, default=5, help="number of cycles")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds per cycle (SDK: ~1 Hz update)")
    parser.add_argument("--continuous", action="store_true", help="flag 2: camera pushes at ~5 Hz")
    parser.add_argument("--verbose", action="store_true", help="also print frames with other CMD_IDs")
    args = parser.parse_args()
    if args.kind == "point" and not args.point:
        parser.error("--kind point needs --point X Y")
    if args.kind == "region" and not args.box:
        parser.error("--kind region needs --box X0 Y0 X1 Y1")
    run(args)


if __name__ == "__main__":
    main()
