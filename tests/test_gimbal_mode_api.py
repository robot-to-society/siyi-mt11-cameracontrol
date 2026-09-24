import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.camera_protocol import CameraState
from app.roi_controller import RoiStatus


class FakeCamera:
    def __init__(self):
        self.state = CameraState()
        self.calls = []

    def set_gimbal_mode_name(self, name):
        if name not in ("lock", "follow", "fpv"):
            raise ValueError(name)
        self.calls.append(("mode", name))

    def request_gimbal_mode(self):
        self.calls.append(("request",))


class FakeRoi:
    def __init__(self, active=None):
        self.active = active

    def status(self):
        return RoiStatus(active_target_id=self.active)


@pytest.fixture
def client(monkeypatch):
    cam = FakeCamera()
    monkeypatch.setattr(main, "camera", cam)
    monkeypatch.setattr(main, "roi", FakeRoi())
    monkeypatch.setattr(main.time, "sleep", lambda _s: None)
    tc = TestClient(main.app)
    tc.cam = cam
    return tc


@pytest.mark.parametrize("mode", ["lock", "follow", "fpv"])
def test_set_mode(client, mode):
    assert client.post("/api/gimbal/mode", json={"mode": mode}).status_code == 200
    assert client.cam.calls == [("mode", mode), ("request",)]


def test_invalid_mode(client):
    assert client.post("/api/gimbal/mode", json={"mode": "sport"}).status_code == 422
    assert client.cam.calls == []


def test_roi_active_allows_only_follow(client, monkeypatch):
    monkeypatch.setattr(main, "roi", FakeRoi(active="roi_1"))
    res = client.post("/api/gimbal/mode", json={"mode": "lock"})
    assert res.status_code == 409
    assert "Follow" in res.json()["detail"]
    assert client.post("/api/gimbal/mode", json={"mode": "follow"}).status_code == 200


def test_camera_error_is_502(client):
    def boom(name):
        raise OSError("tcp down")

    client.cam.set_gimbal_mode_name = boom
    assert client.post("/api/gimbal/mode", json={"mode": "fpv"}).status_code == 502


def test_status_reports_gimbal_mode(monkeypatch):
    monkeypatch.setattr(main.camera.state, "gimbal_mode", "fpv")
    assert TestClient(main.app).get("/api/status").json()["gimbal_mode"] == "fpv"
