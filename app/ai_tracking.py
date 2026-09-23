"""MT11 AI tracking / video encoding helpers (pure functions, immutable data)."""

import math
import struct
from dataclasses import dataclass
from typing import Optional

# 0x50 tracking coordinates are reported on a fixed 1280x720 grid
TRACK_BASE_W = 1280
TRACK_BASE_H = 720

BOX_PX_DEFAULT = 150
BOX_PX_MIN = 32
BOX_PX_MAX = 600

TRACK_STATUS = {0: "tracking", 1: "lost_temporarily", 2: "lost", 3: "cancelled", 4: "tracking_any"}
TARGET_TYPES = {0: "person", 1: "car", 2: "bus", 3: "truck", 255: "any"}

# 0x56 ACK sta
AI_SELECT_RESULT = {
    0: "setting failed",
    1: "ok",
    2: "not in AI tracking mode",
    3: "current stream does not support AI tracking",
    4: "selected area has insufficient texture",
    5: "video stabilization is enabled",
}
# 0x55 ACK Sta
AI_MODE_RESULT = {
    0: "ok",
    1: "night vision enabled: box selection only",
    2: "AI super-resolution enabled: box selection only",
    3: "video stabilization enabled: AI recognition unavailable",
    4: "night vision + stabilization enabled: AI unavailable",
    5: "super-resolution + stabilization enabled: AI unavailable",
}
CODECS = {1: "h264", 2: "h265"}


@dataclass(frozen=True)
class StreamBox:
    lx: int
    ly: int
    rx: int
    ry: int


@dataclass(frozen=True)
class TrackTarget:
    x: float  # normalized top-left
    y: float
    w: float
    h: float
    target_type: str
    status: str
    received_at: float


@dataclass(frozen=True)
class EncodingParams:
    stream_type: int
    codec: str
    width: int
    height: int
    bitrate_kbps: int
    fps: Optional[int]


@dataclass(frozen=True)
class EncodingPreset:
    key: str
    label: str
    codec_id: int
    width: int
    height: int


ENCODING_PRESETS: tuple[EncodingPreset, ...] = (
    EncodingPreset("h264_720p", "H.264 1280×720", 1, 1280, 720),
    EncodingPreset("h264_1080p", "H.264 1920×1080", 1, 1920, 1080),
    EncodingPreset("h264_4k", "H.264 3840×2160", 1, 3840, 2160),
    EncodingPreset("h265_1080p", "H.265 1920×1080", 2, 1920, 1080),
    EncodingPreset("h265_4k", "H.265 3840×2160", 2, 3840, 2160),
)


def _axis_span(center: float, size: int, limit: int) -> tuple[int, int]:
    """Place a span of `size` around `center`, shifted inward to stay within [0, limit-1]."""
    size = min(size, limit - 1)
    # floor(x + 0.5) = JS Math.round, so the browser preview matches exactly (round() is banker's)
    start = math.floor(center - size / 2.0 + 0.5)
    start = max(0, min(start, limit - 1 - size))
    return start, start + size


def click_to_stream_box(nx: float, ny: float, width: int, height: int, box_px: int = BOX_PX_DEFAULT) -> StreamBox:
    """Square of `box_px` stream pixels centred on a normalized click, kept inside the frame."""
    if not (0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0):
        raise ValueError("click position must be within 0..1")
    if width <= 1 or height <= 1:
        raise ValueError("stream resolution unknown")
    lx, rx = _axis_span(nx * width, box_px, width)
    ly, ry = _axis_span(ny * height, box_px, height)
    return StreamBox(lx, ly, rx, ry)


def encode_ai_select(box: Optional[StreamBox]) -> bytes:
    """0x56 payload: box selection, or cancel when box is None."""
    if box is None:
        return struct.pack("<BHHHH", 0, 0, 0, 0, 0)
    return struct.pack("<BHHHH", 1, box.lx, box.ly, box.rx, box.ry)


def parse_track_frame(payload: bytes, received_at: float) -> Optional[TrackTarget]:
    """0x50: centre-based box on 1280x720 -> normalized top-left box."""
    if len(payload) < 10:
        return None
    cx, cy, w, h, target_id, status = struct.unpack("<HHHHBB", payload[:10])
    left = max(0.0, (cx - w / 2.0) / TRACK_BASE_W)
    top = max(0.0, (cy - h / 2.0) / TRACK_BASE_H)
    return TrackTarget(
        x=left,
        y=top,
        w=w / TRACK_BASE_W,
        h=h / TRACK_BASE_H,
        target_type=TARGET_TYPES.get(target_id, f"type_{target_id}"),
        status=TRACK_STATUS.get(status, f"status_{status}"),
        received_at=received_at,
    )


def parse_encoding(payload: bytes) -> Optional[EncodingParams]:
    """0x20 ACK: stream_type, VideoEncType, width, height, bitrate(kbps), [fps]."""
    if len(payload) < 8:
        return None
    stream_type, codec_id, width, height, bitrate = struct.unpack("<BBHHH", payload[:8])
    return EncodingParams(
        stream_type=stream_type,
        codec=CODECS.get(codec_id, "unknown"),
        width=width,
        height=height,
        bitrate_kbps=bitrate,
        fps=payload[8] if len(payload) >= 9 else None,
    )


def find_preset(key: str) -> EncodingPreset:
    for preset in ENCODING_PRESETS:
        if preset.key == key:
            return preset
    raise KeyError(key)


def encode_encoding_params(preset: EncodingPreset, stream_type: int = 1) -> bytes:
    """0x21 payload (bitrate is not supported by MT11 yet -> 0)."""
    return struct.pack("<BBHHHB", stream_type, preset.codec_id, preset.width, preset.height, 0, 0)
