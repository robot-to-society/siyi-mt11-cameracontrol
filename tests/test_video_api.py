import json

import pytest
from fastapi.testclient import TestClient

import app.main as main
import app.video_routes as video_routes
from app.ai_tracking import Detection, DetectionFrame, StreamBox, TrackTarget
from app.camera_protocol import CameraState
from app.video_source import MODES, SourceStatus, VideoSourceSettings

SRC = {"source": "mt11", "mode": "auto"}


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

    def request_point_temperature(self, x, y):
        self.calls.append(("temp_point", x, y))

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


class FakeVideoSource:
    """Stands in for VideoSourceMonitor: fixed source, records mode changes."""

    def __init__(self, source="mt11", mode="auto"):
        self.settings = VideoSourceSettings(
            rtsp_url_template="rtsp://{host}:8554/video1",
            android_url="http://127.0.0.1:8080",
            android_token="tok",
            rescan_s=15.0,
            check_s=3.0,
            probe_timeout_s=2.0,
        )
        self._status = SourceStatus(source=source, mode=mode, mt11_ok=source == "mt11", scanned_at=1.0, next_scan_at=4.0, scans=1)
        self.modes = []

    @property
    def status(self):
        return self._status

    def set_mode(self, mode):
        if mode not in MODES:
            raise ValueError(mode)
        self.modes.append(mode)
        self._status = SourceStatus(source=mode if mode != "auto" else "mt11", mode=mode)
        return self._status


@pytest.fixture
def client(monkeypatch):
    cam, roi, source = FakeCamera(), FakeRoi(), FakeVideoSource()
    monkeypatch.setattr(main, "camera", cam)
    monkeypatch.setattr(main, "roi", roi)
    monkeypatch.setattr(main, "video_source", source)
    monkeypatch.setattr(video_routes.time, "sleep", lambda _s: None)
    tc = TestClient(main.app)
    tc.cam, tc.roi, tc.source = cam, roi, source
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
        assert res.json()["box"]["rx"] - res.json()["box"]["lx"] == 100

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
        snap = video_routes.ai_snapshot(state, now=10.5, video_source=SRC)
        assert snap["track"]["status"] == "tracking"
        assert snap["stream"] == {"width": 1920, "height": 1080}

    def test_stale_track_dropped(self):
        state = CameraState()
        state.track = TrackTarget(0.1, 0.2, 0.3, 0.4, "car", "tracking", received_at=10.0)
        assert video_routes.ai_snapshot(state, now=11.5, video_source=SRC)["track"] is None


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
        assert body["mode"] == "detection"
        assert body["detection"]["class_name"] == "person"
        assert body["point"] == {"x": 960, "y": 648}
        assert client.cam.calls == [("ai_mode", True), ("point", 960, 648)]
        assert client.roi.stopped == 1

    def test_miss_is_404(self, client, monkeypatch):
        monkeypatch.setattr(video_routes.time, "monotonic", lambda: 10.2)
        client.cam.state.detection_history = (frame_at(10.0, PERSON),)
        assert client.post("/api/ai/track-detection", json={"x": 0.1, "y": 0.1}).status_code == 404
        assert client.cam.calls == []

    def test_no_detection_data_falls_back_to_point_at_click(self, client, monkeypatch):
        # firmware without 0x5F: let the camera pick the detected object at the click
        monkeypatch.setattr(video_routes.time, "monotonic", lambda: 20.0)
        client.cam.state.detection_history = (frame_at(10.0, PERSON),)  # stale only
        res = client.post("/api/ai/track-detection", json={"x": 0.25, "y": 0.5})
        assert res.status_code == 200
        assert res.json()["mode"] == "point_at_click"
        assert res.json()["detection"] is None
        assert client.cam.calls == [("ai_mode", True), ("point", 480, 540)]

    def test_rgb_only(self, client):
        client.cam.state.video_mode_name = "thermal"
        assert client.post("/api/ai/track-detection", json={"x": 0.45, "y": 0.5}).status_code == 409


def test_snapshot_includes_recent_detections():
    state = CameraState()
    state.detection_history = (frame_at(10.0, PERSON),)
    snap = video_routes.ai_snapshot(state, now=10.3, video_source=SRC)
    assert snap["detections"][0]["class_name"] == "person"
    assert video_routes.ai_snapshot(state, now=11.0, video_source=SRC)["detections"] == []


def test_debug_rx_endpoint(client, monkeypatch):
    monkeypatch.setattr(client.cam, "rx_debug", lambda: {"counts": {"0x5F": 3}, "last": {}}, raising=False)
    body = client.get("/api/debug/rx").json()
    assert body["counts"]["0x5F"] == 3
    assert "firmware" in body
    assert body["detection_frames"] == 0


class TestThermalPoint:
    def test_alt_click_requests_point_temperature(self, client):
        client.cam.state.video_mode_name = "thermal"
        res = client.post("/api/thermal/point", json={"x": 0.25, "y": 0.5})
        assert res.status_code == 200
        assert res.json()["point"] == {"x": 480, "y": 540}
        assert client.cam.calls == [("temp_point", 480, 540)]

    def test_only_in_thermal_mode(self, client):
        assert client.post("/api/thermal/point", json={"x": 0.5, "y": 0.5}).status_code == 409
        assert client.cam.calls == []

    def test_validation(self, client):
        client.cam.state.video_mode_name = "thermal"
        assert client.post("/api/thermal/point", json={"x": 1.5, "y": 0.5}).status_code == 422


def test_snapshot_includes_thermal_overlay():
    from app.thermal import ThermalFrame

    state = CameraState(stream_width=1920, stream_height=1080, video_mode_name="thermal")
    state.thermal_frame = ThermalFrame(21.5, 9.9, (960, 108), (192, 1079), received_at=10.0)
    snap = video_routes.ai_snapshot(state, now=10.5, video_source=SRC)
    assert snap["thermal"]["frame"]["max"]["c"] == 21.5
    assert video_routes.ai_snapshot(CameraState(), now=10.5, video_source=SRC)["thermal"] is None


def test_snapshot_includes_video_source():
    snap = video_routes.ai_snapshot(CameraState(), now=1.0, video_source={"source": "android", "mode": "auto"})
    assert snap["video_source"] == {"source": "android", "mode": "auto"}


class TestVideoSourceApi:
    def test_get_status(self, client):
        body = client.get("/api/video/source").json()
        assert body["source"] == "mt11"
        assert body["mode"] == "auto"
        assert body["mt11_ok"] is True
        assert "next_scan_in_s" in body

    def test_set_mode(self, client):
        res = client.post("/api/video/source", json={"mode": "android"})
        assert res.status_code == 200
        assert res.json()["mode"] == "android"
        assert client.source.modes == ["android"]

    def test_invalid_mode(self, client):
        assert client.post("/api/video/source", json={"mode": "hdmi"}).status_code == 422
        assert client.source.modes == []


class TestWhepSourceRouting:
    def test_android_source_wraps_offer_as_json(self, client, monkeypatch):
        client.source._status = SourceStatus(source="android", mode="auto")
        seen = {}

        def fake_android(offer, *, url, token):
            seen.update(offer=offer, url=url, token=token)
            return 201, b"v=0 android answer"

        monkeypatch.setattr(video_routes, "_post_sdp_android", fake_android)
        res = client.post("/api/video/whep", content=b"v=0 offer", headers={"Content-Type": "application/sdp"})
        assert res.status_code == 201
        assert res.text == "v=0 android answer"
        assert seen == {"offer": b"v=0 offer", "url": "http://127.0.0.1:8080/offer", "token": "tok"}

    def test_no_source_is_503(self, client, monkeypatch):
        client.source._status = SourceStatus(source="none", mode="auto")
        called = []
        monkeypatch.setattr(video_routes, "_post_sdp", lambda offer: called.append(offer))
        res = client.post("/api/video/whep", content=b"v=0", headers={"Content-Type": "application/sdp"})
        assert res.status_code == 503
        assert called == []


class TestPostSdpAndroid:
    def test_json_round_trip(self, monkeypatch):
        import io
        import urllib.request

        seen = {}

        class FakeResponse(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        def fake_urlopen(req, timeout):
            seen["url"] = req.full_url
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            seen["body"] = json.loads(req.data)
            return FakeResponse(json.dumps({"type": "answer", "sdp": "v=0 answer"}).encode())

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        status, answer = video_routes._post_sdp_android(b"v=0 offer", url="http://127.0.0.1:8080/offer", token="tok")
        assert (status, answer) == (201, b"v=0 answer")
        assert seen["body"] == {"type": "offer", "sdp": "v=0 offer"}
        assert seen["headers"]["content-type"] == "application/json"
        assert seen["headers"]["x-stream-token"] == "tok"

    def test_error_status_is_passed_through(self, monkeypatch):
        import urllib.error
        import urllib.request

        def fake_urlopen(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, io.BytesIO(b'{"error": "no device streaming"}'))

        import io

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        status, answer = video_routes._post_sdp_android(b"v=0", url="http://127.0.0.1:8080/offer", token=None)
        assert status == 503
        assert b"no device streaming" in answer

    def test_malformed_answer_is_502(self, monkeypatch):
        import io
        import urllib.request

        class FakeResponse(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: FakeResponse(b"not json"))
        status, _ = video_routes._post_sdp_android(b"v=0", url="http://127.0.0.1:8080/offer", token=None)
        assert status == 502


class TestCameraIpValidation:
    def test_accepts_ip_addresses(self, client, monkeypatch):
        seen = []
        monkeypatch.setattr(client.cam, "configure_host", lambda ip: seen.append(ip), raising=False)
        assert client.post("/api/camera/ip", json={"ip": " 192.168.144.26 "}).status_code == 200
        assert seen == ["192.168.144.26"]

    @pytest.mark.parametrize("ip", ["camera.local", "127.0.0.1:6379/\r\nFLUSHALL", "", "192.168.1"])
    def test_rejects_non_ip_values(self, client, monkeypatch, ip):
        seen = []
        monkeypatch.setattr(client.cam, "configure_host", lambda ip: seen.append(ip), raising=False)
        assert client.post("/api/camera/ip", json={"ip": ip}).status_code == 400
        assert seen == []


def test_whep_error_bodies_are_not_labelled_as_sdp(client, monkeypatch):
    monkeypatch.setattr(video_routes, "_post_sdp", lambda offer: (503, b'{"error": "busy"}'))
    res = client.post("/api/video/whep", content=b"v=0", headers={"Content-Type": "application/sdp"})
    assert res.status_code == 503
    assert res.headers["content-type"].startswith("text/plain")


def test_whep_http_protocol_error_is_502(client, monkeypatch):
    import http.client

    def bad(_offer):
        raise http.client.BadStatusLine("garbage")

    monkeypatch.setattr(video_routes, "_post_sdp", bad)
    res = client.post("/api/video/whep", content=b"v=0", headers={"Content-Type": "application/sdp"})
    assert res.status_code == 502
