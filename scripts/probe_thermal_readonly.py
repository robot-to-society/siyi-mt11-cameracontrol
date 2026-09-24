"""Read-only probe: does the MT11 answer SIYI (ZT30/ZT6) thermal commands missing from its SDK doc?

Sends ONLY payload-less "request" commands and logs the replies. Nothing is set.
  0x37  thermal gain (documented in the MT11 SDK -> control / connection check)
  0x39  env correction params     0x3B  env correction switch
  0x42  threshold switch          0x44  threshold params
  0x46  threshold precision       0x33  thermal output mode
Any other CMD_ID (e.g. 0x48 = format SD card) is refused before a packet is built.

Usage (on the Pi). The camera answers only one TCP client, so stop the UI service first:
    sudo systemctl stop mt11-camera-controller
    python -m scripts.probe_thermal_readonly --host 192.168.144.25 [--json out.json]  # one TCP connection per command
    sudo systemctl start mt11-camera-controller

A reply with the expected length strongly suggests the command exists on this firmware.
Back up the SD card first anyway: MT11 reuses some SIYI IDs with other meanings (e.g. 0x49).
"""

import argparse
import json
import socket
import struct
import time
from typing import Callable, Optional

from app.camera_protocol import make_packet

STOP_HINT = "the camera answers only one TCP client: stop the UI service first (sudo systemctl stop mt11-camera-controller)"


def _u8(name: str) -> tuple[int, Callable[[bytes], dict]]:
    return 1, lambda p: {name: p[0]}


def _env_params(p: bytes) -> dict:
    dist, ems, hum, ta, tu = struct.unpack("<HHHHH", p[:10])
    return {
        "dist_m": dist / 100,
        "emissivity_raw_x100": ems / 100,  # SIYI doc: "(%) * 100" -> unit to be confirmed
        "humidity_pct": hum / 100,
        "air_temp_c": ta / 100,
        "reflected_temp_c": tu / 100,
    }


def _threshold_params(p: bytes) -> dict:
    values = {}
    for i in range(3):
        on, tmin, tmax, r, g, b = struct.unpack_from("<BhhBBB", p, i * 8)
        values[f"region{i + 1}"] = {"on": on, "min": tmin, "max": tmax, "rgb": [r, g, b]}
    return values


# CMD_ID -> (name, expected payload length, decoder). Keep this list read-only.
PROBES: dict[int, tuple[str, int, Callable[[bytes], dict]]] = {
    0x37: ("thermal gain (documented)", *_u8("ir_gain")),
    0x39: ("env correction params", 10, _env_params),
    0x3B: ("env correction switch", *_u8("env_correct")),
    0x42: ("threshold switch", *_u8("ir_thresh_sta")),
    0x44: ("threshold params", 24, _threshold_params),
    0x46: ("threshold precision", *_u8("precision")),
    0x33: ("thermal output mode", *_u8("mode")),
}


def build_probe_packet(cmd_id: int, seq: int) -> bytes:
    """Only allow-listed request commands, always with an empty payload."""
    if cmd_id not in PROBES:
        raise ValueError(f"CMD 0x{cmd_id:02X} is not an allowed read-only probe")
    return make_packet(cmd_id, b"", seq=seq)


def decode_reply(cmd_id: int, payload: bytes) -> dict:
    _name, expected_len, decoder = PROBES[cmd_id]
    length_ok = len(payload) == expected_len
    return {"length_ok": length_ok, "values": decoder(payload) if length_ok else None}


def _read_frames(sock: socket.socket, buffer: bytearray) -> list[tuple[int, bytes]]:
    try:
        chunk = sock.recv(4096)
    except socket.timeout:
        return []
    if not chunk:
        raise ConnectionResetError("connection closed by camera")
    buffer.extend(chunk)
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


def _probe_one(host: str, port: int, cmd_id: int, wait_s: float) -> tuple[dict, int]:
    """One fresh TCP connection per command: the MT11 may drop the link on unknown commands."""
    entry = {"cmd": f"0x{cmd_id:02X}", "name": PROBES[cmd_id][0], "status": "no reply"}
    frames_seen = 0
    sock = socket.create_connection((host, port), timeout=3.0)
    sock.settimeout(0.2)
    buffer = bytearray()
    try:
        sock.sendall(build_probe_packet(cmd_id, seq=1))
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            for got_id, payload in _read_frames(sock, buffer):
                frames_seen += 1
                if got_id == cmd_id:
                    entry.update(
                        {"status": "answered", "len": len(payload), "hex": payload.hex(), **decode_reply(cmd_id, payload)}
                    )
                    return entry, frames_seen
    except ConnectionError:  # BrokenPipe / ConnectionReset / closed by peer
        entry["status"] = "connection closed by camera"
    finally:
        sock.close()
    return entry, frames_seen


def probe(host: str, port: int, wait_s: float) -> tuple[list[dict], int]:
    """Returns (results, number of frames of any kind received)."""
    results, frames_seen = [], 0
    for cmd_id in PROBES:
        entry, seen = _probe_one(host, port, cmd_id, wait_s)
        results.append(entry)
        frames_seen += seen
        time.sleep(0.3)  # let the camera settle before reconnecting
    return results, frames_seen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="192.168.144.25")
    parser.add_argument("--port", type=int, default=37260)
    parser.add_argument("--wait", type=float, default=1.5, help="seconds to wait for each reply")
    parser.add_argument("--json", help="save results to this file")
    args = parser.parse_args()

    results, frames_seen = probe(args.host, args.port, args.wait)
    if frames_seen == 0:
        print("camera sent nothing on this connection: " + STOP_HINT)
    for r in results:
        if r["status"] != "answered":
            print(f"{r['cmd']} {r['name']:<28} {r['status']}")
            continue
        mark = "OK " if r["length_ok"] else "LEN?"
        print(f"{r['cmd']} {r['name']:<28} {mark} len={r['len']} hex={r['hex']}")
        if r["values"] is not None:
            print(f"      {r['values']}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"saved: {args.json}")


if __name__ == "__main__":
    main()
