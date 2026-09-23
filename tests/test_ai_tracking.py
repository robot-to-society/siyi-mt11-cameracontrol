import struct

import pytest

from app.ai_tracking import (
    ENCODING_PRESETS,
    StreamBox,
    click_to_stream_box,
    encode_ai_select,
    encode_encoding_params,
    find_preset,
    parse_encoding,
    parse_track_frame,
)
from app.camera_protocol import make_packet


def sdk_bytes(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str.replace(" ", ""))


class TestEncodeAiSelect:
    def test_box_matches_sdk_example(self):
        packet = make_packet(0x56, encode_ai_select(StreamBox(200, 200, 400, 400)), seq=0)
        assert packet == sdk_bytes("55 66 01 09 00 00 00 56 01 C8 00 C8 00 90 01 90 01 92 20")

    def test_cancel_matches_sdk_example(self):
        packet = make_packet(0x56, encode_ai_select(None), seq=0)
        assert packet == sdk_bytes("55 66 01 09 00 00 00 56 00 00 00 00 00 00 00 00 00 2D 94")


class TestClickToStreamBox:
    def test_center_1080p(self):
        assert click_to_stream_box(0.5, 0.5, 1920, 1080, 150) == StreamBox(885, 465, 1035, 615)

    def test_center_4k(self):
        assert click_to_stream_box(0.5, 0.5, 3840, 2160, 150) == StreamBox(1845, 1005, 1995, 1155)

    def test_center_720p(self):
        assert click_to_stream_box(0.5, 0.5, 1280, 720, 150) == StreamBox(565, 285, 715, 435)

    @pytest.mark.parametrize(
        "nx,ny,expected",
        [
            (0.0, 0.0, StreamBox(0, 0, 150, 150)),
            (1.0, 1.0, StreamBox(1769, 929, 1919, 1079)),
            (0.01, 0.99, StreamBox(0, 929, 150, 1079)),
        ],
    )
    def test_edges_shift_inward_keeping_size(self, nx, ny, expected):
        box = click_to_stream_box(nx, ny, 1920, 1080, 150)
        assert box == expected
        assert box.rx - box.lx == 150 and box.ry - box.ly == 150

    def test_box_larger_than_frame_is_clamped(self):
        box = click_to_stream_box(0.5, 0.5, 400, 300, 600)
        assert box == StreamBox(0, 0, 399, 299)

    @pytest.mark.parametrize("nx,ny", [(-0.1, 0.5), (0.5, 1.1)])
    def test_rejects_out_of_range(self, nx, ny):
        with pytest.raises(ValueError):
            click_to_stream_box(nx, ny, 1920, 1080, 150)

    def test_rejects_unknown_resolution(self):
        with pytest.raises(ValueError):
            click_to_stream_box(0.5, 0.5, 0, 0, 150)


class TestParseTrackFrame:
    def test_center_based_to_normalized_top_left(self):
        payload = struct.pack("<HHHHBB", 640, 360, 128, 72, 0, 0)
        t = parse_track_frame(payload, received_at=3.0)
        assert (t.x, t.y, t.w, t.h) == pytest.approx((0.45, 0.45, 0.1, 0.1))
        assert t.status == "tracking" and t.target_type == "person"
        assert t.received_at == 3.0

    def test_any_object_status(self):
        t = parse_track_frame(struct.pack("<HHHHBB", 100, 100, 20, 20, 255, 4), 0.0)
        assert t.target_type == "any" and t.status == "tracking_any"

    def test_clamps_to_frame(self):
        t = parse_track_frame(struct.pack("<HHHHBB", 5, 5, 40, 40, 1, 1), 0.0)
        assert t.x == 0.0 and t.y == 0.0
        assert t.status == "lost_temporarily"

    def test_short_payload_returns_none(self):
        assert parse_track_frame(b"\x00" * 9, 0.0) is None


class TestEncoding:
    def test_parse_main_stream(self):
        payload = struct.pack("<BBHHHB", 1, 2, 3840, 2160, 16000, 30)
        e = parse_encoding(payload)
        assert (e.stream_type, e.codec, e.width, e.height, e.bitrate_kbps, e.fps) == (1, "h265", 3840, 2160, 16000, 30)

    def test_parse_without_fps(self):
        e = parse_encoding(struct.pack("<BBHHH", 1, 1, 1920, 1080, 4000))
        assert e.codec == "h264" and e.fps is None

    def test_parse_short_returns_none(self):
        assert parse_encoding(b"\x01\x01\x00") is None

    def test_presets_cover_plan(self):
        keys = [p.key for p in ENCODING_PRESETS]
        assert keys == ["h264_720p", "h264_1080p", "h264_4k", "h265_1080p", "h265_4k"]

    def test_encode_h264_1080p_main(self):
        data = encode_encoding_params(find_preset("h264_1080p"))
        assert data == struct.pack("<BBHHHB", 1, 1, 1920, 1080, 0, 0)

    def test_find_unknown_preset(self):
        with pytest.raises(KeyError):
            find_preset("h266_8k")


@pytest.mark.parametrize("box_px,expected_lx", [(151, 885), (149, 886)])
def test_half_pixel_rounds_up_like_js_math_round(box_px, expected_lx):
    # JS Math.round rounds .5 up; Python round() is banker's rounding -> must match the preview
    assert click_to_stream_box(0.5, 0.5, 1920, 1080, box_px).lx == expected_lx


# ── Detection candidates (0x5F) / point selection ─────────────────
from app.ai_tracking import (  # noqa: E402
    Detection,
    DetectionFrame,
    encode_ai_select_point,
    normalized_to_stream_point,
    parse_candidate_frame,
    pick_detection,
)


def candidate_payload(boxes, model=0, pts=123456):
    """boxes: list of (lx, ly, rx, ry, score, class_id) in 0..1 floats."""
    n = len(boxes)
    q = lambda v: int(round(v * 65535))  # noqa: E731
    head = struct.pack("<BBQB", 5, model, pts, n)
    arrays = b"".join(
        struct.pack(f"<{n}H", *[q(b[i]) for b in boxes]) for i in range(5)
    )
    return head + arrays + bytes(b[5] for b in boxes)


def det(x0, y0, x1, y1, class_id=0, score=0.9):
    return Detection(x0, y0, x1, y1, score, class_id, "person" if class_id == 0 else "car")


class TestCandidateFrame:
    def test_parse_struct_of_arrays(self):
        payload = candidate_payload([(0.1, 0.2, 0.3, 0.4, 0.9, 0), (0.5, 0.5, 0.7, 0.9, 0.5, 1)])
        f = parse_candidate_frame(payload, received_at=2.0)
        assert f.model == 0 and f.pts_us == 123456 and f.received_at == 2.0
        assert len(f.detections) == 2
        d0, d1 = f.detections
        assert (d0.x0, d0.y0, d0.x1, d0.y1) == pytest.approx((0.1, 0.2, 0.3, 0.4), abs=1e-4)
        assert d0.class_name == "person" and d1.class_name == "car"
        assert d1.score == pytest.approx(0.5, abs=1e-4)

    def test_empty_frame(self):
        f = parse_candidate_frame(candidate_payload([]), received_at=0.0)
        assert f.detections == ()

    def test_truncated_returns_none(self):
        payload = candidate_payload([(0.1, 0.2, 0.3, 0.4, 0.9, 0)])
        assert parse_candidate_frame(payload[:-1], 0.0) is None
        assert parse_candidate_frame(b"\x05\x00", 0.0) is None  # enable ACK only


class TestPointSelect:
    def test_point_matches_sdk_example(self):
        packet = make_packet(0x56, encode_ai_select_point(960, 540), seq=0)
        assert packet == sdk_bytes("55 66 01 09 00 00 00 56 01 C0 03 1C 02 00 00 00 00 39 F9")

    def test_normalized_to_stream_point(self):
        assert normalized_to_stream_point(0.5, 0.5, 1920, 1080) == (960, 540)
        assert normalized_to_stream_point(1.0, 1.0, 1920, 1080) == (1919, 1079)
        assert normalized_to_stream_point(0.0, 0.0, 1920, 1080) == (0, 0)


class TestPickDetection:
    def frame(self, dets, t):
        return DetectionFrame(model=0, pts_us=0, detections=tuple(dets), received_at=t)

    def test_no_hit(self):
        assert pick_detection([self.frame([det(0.1, 0.1, 0.2, 0.2)], 1.0)], 0.5, 0.5) is None
        assert pick_detection([], 0.5, 0.5) is None

    def test_smallest_containing_box_wins(self):
        big, small = det(0.0, 0.0, 0.8, 0.8), det(0.4, 0.4, 0.6, 0.6)
        assert pick_detection([self.frame([big, small], 1.0)], 0.5, 0.5) == small

    def test_hit_in_older_frame_follows_object_to_newest_frame(self):
        # object moved right between frames; click matches where it was (delayed video)
        old = self.frame([det(0.40, 0.40, 0.50, 0.50)], 1.0)
        new = self.frame([det(0.45, 0.40, 0.55, 0.50), det(0.80, 0.1, 0.9, 0.2)], 1.5)
        assert pick_detection([old, new], 0.42, 0.45) == det(0.45, 0.40, 0.55, 0.50)

    def test_other_class_not_followed(self):
        old = self.frame([det(0.40, 0.40, 0.50, 0.50, class_id=0)], 1.0)
        new = self.frame([det(0.45, 0.40, 0.55, 0.50, class_id=1)], 1.5)
        assert pick_detection([old, new], 0.42, 0.45) == det(0.40, 0.40, 0.50, 0.50, class_id=0)
