import pytest
from fastapi.testclient import TestClient

import app.main as main
import app.video_routes as video_routes
from app.ai_tracking import Detection, DetectionFrame, StreamBox, TrackTarget
from app.camera_protocol import CameraState


class FakeCamera:
    def __init__(self):
        self.state = CameraState(stream_width=1920, stream_height=1080, video_mode_name="rgb")
        self.calls = []

    def set_ai_mode(self, enable):
        self.calls.append(("ai_mode", enable))

    def ai_select_box(self, box):
        self.calls.append(("select", box))

    def ai_select_point(self, x, y):
        self.calls.append(("point", x, y))

    def ai_cancel_tracking(self):
        self.calls.append(("cancel",))

    def set_encoding_params(self, preset):
        self.calls.append(("encoding", preset.key))

    def request_encoding_params(self):
        self.calls.append(("request_encoding",))


class FakeRoi:
    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


@pytest.fixture
def client(monkeypatch):
    cam, roi = FakeCamera(), FakeRoi()
    monkeypatch.setattr(main, "camera", cam)
    monkeypatch.setattr(main, "roi", roi)
    monkeypatch.setattr(video_routes.time, "sleep", lambda _s: None)
    tc = TestClient(main.app)
    tc.cam, tc.roi = cam, roi
    return tc


class TestTrackPoint:
    def test_center_click_sends_box_and_stops_roi(self, client):
        res = client.post("/api/ai/track-point", json={"x": 0.5, "y": 0.5, "box_px": 150})
        assert res.status_code == 200
        assert res.json()["box"] == {"lx": 885, "ly": 465, "rx": 1035, "ry": 615}
        assert client.cam.calls == [("ai_mode", True), ("select", StreamBox(885, 465, 1035, 615))]
        assert client.roi.stopped == 1

    def test_default_box_px(self, client):
        res = client.post("/api/ai/track-point", json={"x": 0.5, "y": 0.5})
        assert res.json()["box"]["rx"] - res.json()["box"]["lx"] == 150

    @pytest.mark.parametrize(
        "body",
        [{"x": 1.2, "y": 0.5}, {"x": 0.5, "y": -0.1}, {"x": 0.5, "y": 0.5, "box_px": 10}, {"x": 0.5, "y": 0.5, "box_px": 900}],
    )
    def test_validation(self, client, body):
        assert client.post("/api/ai/track-point", json=body).status_code == 422
        assert client.cam.calls == []

    def test_rejected_outside_rgb_mode(self, client):
        client.cam.state.video_mode_name = "thermal"
        res = client.post("/api/ai/track-point", json={"x": 0.5, "y": 0.5})
        assert res.status_code == 409
        assert client.cam.calls == []

    def test_unknown_resolution(self, client):
        client.cam.state.stream_width = 0
        assert client.post("/api/ai/track-point", json={"x": 0.5, "y": 0.5}).status_code == 503


def test_cancel(client):
    assert client.post("/api/ai/cancel").status_code == 200
    assert client.cam.calls == [("cancel",)]


def test_legacy_center_tracking_uses_stream_resolution(client):
    client.post("/api/ai/tracking", json={"enable": True})
    assert client.cam.calls[-1] == ("select", StreamBox(860, 440, 1060, 640))
    client.post("/api/ai/tracking", json={"enable": False})
    assert client.cam.calls[-1] == ("cancel",)


class TestEncoding:
    def test_get_lists_presets(self, client):
        body = client.get("/api/video/encoding").json()
        assert [p["key"] for p in body["presets"]][:2] == ["h264_720p", "h264_1080p"]
        assert body["current"] is None

    def test_apply_preset(self, client):
        assert client.post("/api/video/encoding", json={"preset": "h264_1080p"}).status_code == 200
        assert client.cam.calls == [("encoding", "h264_1080p"), ("request_encoding",)]

    def test_unknown_preset(self, client):
        assert client.post("/api/video/encoding", json={"preset": "nope"}).status_code == 400


class TestAiSnapshot:
    def test_fresh_track_included(self):
        state = CameraState(stream_width=1920, stream_height=1080)
        state.track = TrackTarget(0.1, 0.2, 0.3, 0.4, "car", "tracking", received_at=10.0)
        snap = video_routes.ai_snapshot(state, now=10.5)
        assert snap["track"]["status"] == "tracking"
        assert snap["stream"] == {"width": 1920, "height": 1080}

    def test_stale_track_dropped(self):
        state = CameraState()
        state.track = TrackTarget(0.1, 0.2, 0.3, 0.4, "car", "tracking", received_at=10.0)
        assert video_routes.ai_snapshot(state, now=11.5)["track"] is None


class TestWhepProxy:
    def test_forwards_offer(self, client, monkeypatch):
        seen = {}

        def fake_post(offer):
            seen["offer"] = offer
            return 201, b"v=0 answer"

        monkeypatch.setattr(video_routes, "_post_sdp", fake_post)
        res = client.post("/api/video/whep", content=b"v=0 offer", headers={"Content-Type": "application/sdp"})
        assert res.status_code == 201
        assert res.text == "v=0 answer"
        assert res.headers["content-type"].startswith("application/sdp")
        assert seen["offer"] == b"v=0 offer"

    def test_rejects_wrong_content_type(self, client):
        res = client.post("/api/video/whep", content=b"{}", headers={"Content-Type": "application/json"})
        assert res.status_code == 415

    def test_rejects_huge_body(self, client):
        res = client.post("/api/video/whep", content=b"x" * 70_000, headers={"Content-Type": "application/sdp"})
        assert res.status_code == 413

    def test_rejects_huge_declared_length_before_reading(self, client, monkeypatch):
        called = []
        monkeypatch.setattr(video_routes, "_post_sdp", lambda offer: called.append(offer))
        res = client.post(
            "/api/video/whep",
            content=b"v=0",
            headers={"Content-Type": "application/sdp", "Content-Length": "999999"},
        )
        assert res.status_code == 413
        assert called == []

    def test_upstream_timeout_is_504(self, client, monkeypatch):
        import socket

        def slow(_offer):
            raise socket.timeout("timed out")

        monkeypatch.setattr(video_routes, "_post_sdp", slow)
        res = client.post("/api/video/whep", content=b"v=0", headers={"Content-Type": "application/sdp"})
        assert res.status_code == 504

    def test_upstream_down_is_502(self, client, monkeypatch):
        monkeypatch.setattr(video_routes, "WHEP_UPSTREAM", "http://127.0.0.1:1/mt11/whep")
        res = client.post("/api/video/whep", content=b"v=0", headers={"Content-Type": "application/sdp"})
        assert res.status_code == 502


def test_roi_start_cancels_ai_tracking(client, monkeypatch):
    cancelled = []
    monkeypatch.setattr(main, "cancel_ai_tracking_async", lambda cam: cancelled.append(cam))

    class Roi(FakeRoi):
        def start(self, target_id):
            self.started = target_id

    monkeypatch.setattr(main, "roi", Roi())
    assert client.post("/api/roi/start", json={"target_id": "roi_1"}).status_code == 200
    assert cancelled == [client.cam]


def frame_at(t, *dets):
    return DetectionFrame(model=0, pts_us=0, detections=tuple(dets), received_at=t)


PERSON = Detection(0.4, 0.4, 0.6, 0.8, 0.88, 0, "person")


class TestTrackDetection:
    def test_hit_sends_point_at_detection_centre(self, client, monkeypatch):
        monkeypatch.setattr(video_routes.time, "monotonic", lambda: 10.2)
        client.cam.state.detection_history = (frame_at(10.0, PERSON),)
        res = client.post("/api/ai/track-detection", json={"x": 0.45, "y": 0.5})
        assert res.status_code == 200
        body = res.json()
        assert body["detection"]["class_name"] == "person"
        assert body["point"] == {"x": 960, "y": 648}
        assert client.cam.calls == [("ai_mode", True), ("point", 960, 648)]
        assert client.roi.stopped == 1

    def test_miss_is_404(self, client, monkeypatch):
        monkeypatch.setattr(video_routes.time, "monotonic", lambda: 10.2)
        client.cam.state.detection_history = (frame_at(10.0, PERSON),)
        assert client.post("/api/ai/track-detection", json={"x": 0.1, "y": 0.1}).status_code == 404
        assert client.cam.calls == []

    def test_old_frames_ignored(self, client, monkeypatch):
        monkeypatch.setattr(video_routes.time, "monotonic", lambda: 20.0)
        client.cam.state.detection_history = (frame_at(10.0, PERSON),)
        assert client.post("/api/ai/track-detection", json={"x": 0.45, "y": 0.5}).status_code == 404

    def test_rgb_only(self, client):
        client.cam.state.video_mode_name = "thermal"
        assert client.post("/api/ai/track-detection", json={"x": 0.45, "y": 0.5}).status_code == 409


def test_snapshot_includes_recent_detections():
    state = CameraState()
    state.detection_history = (frame_at(10.0, PERSON),)
    snap = video_routes.ai_snapshot(state, now=10.3)
    assert snap["detections"][0]["class_name"] == "person"
    assert video_routes.ai_snapshot(state, now=11.0)["detections"] == []


def test_debug_rx_endpoint(client, monkeypatch):
    monkeypatch.setattr(client.cam, "rx_debug", lambda: {"counts": {"0x5F": 3}, "last": {}}, raising=False)
    body = client.get("/api/debug/rx").json()
    assert body["counts"]["0x5F"] == 3
    assert body["detection_frames"] == 0
