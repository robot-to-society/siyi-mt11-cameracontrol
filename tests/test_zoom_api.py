import pytest
from fastapi.testclient import TestClient

import app.main as main


class FakeCamera:
    def __init__(self):
        self.calls = []

    def set_absolute_zoom(self, zoom):
        self.calls.append(("zoom", zoom))

    def request_zoom_level(self):
        self.calls.append(("request",))


@pytest.fixture
def client(monkeypatch):
    cam = FakeCamera()
    monkeypatch.setattr(main, "camera", cam)
    monkeypatch.setattr(main.time, "sleep", lambda _s: None)
    tc = TestClient(main.app)
    tc.cam = cam
    return tc


def test_zoom_1x_sets_absolute_zoom(client):
    # joystick function "zoom_1x" posts {"zoom": 1.0}
    assert client.post("/api/zoom/set", json={"zoom": 1.0}).status_code == 200
    assert client.cam.calls == [("zoom", 1.0), ("request",)]


def test_zoom_below_1_rejected(client):
    assert client.post("/api/zoom/set", json={"zoom": 0.5}).status_code == 400
    assert client.cam.calls == []
