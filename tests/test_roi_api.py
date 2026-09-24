import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.roi_controller import RoiController


class FakeGimbal:
    def __init__(self):
        self.angles = []
        self.modes = []
        self.speeds = []

    def set_gimbal_angle(self, yaw, pitch):
        self.angles.append((yaw, pitch))

    def set_gimbal_motion_mode(self, mode):
        self.modes.append(mode)

    def set_gimbal_speed(self, yaw, pitch):
        self.speeds.append((yaw, pitch))

    def center_gimbal(self, mode=1):
        pass


class FakeMavlink:
    def __init__(self):
        self.restarted_with = None
        self.connected = False
        self.last_error = None

    def latest(self):
        return None

    def restart(self, url):
        self.restarted_with = url


CONFIG = {
    "mavlink_url": "udpin:127.0.0.1:15555",
    "rate_hz": 10,
    "yaw_offset_deg": 0,
    "targets": [
        {"id": "roi_1", "name": "Tower", "lat": 35.01, "lon": 139.0, "alt_msl": 50},
        {"id": "roi_2", "name": "", "lat": 0, "lon": 0, "alt_msl": 0},
    ],
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    gimbal, mav = FakeGimbal(), FakeMavlink()
    ctrl = RoiController(gimbal, mav)
    monkeypatch.setattr(main, "ROI_CONFIG_PATH", tmp_path / "roi_config.json")
    monkeypatch.setattr(main, "camera", gimbal)
    monkeypatch.setattr(main, "mavlink", mav)
    monkeypatch.setattr(main, "roi", ctrl)
    monkeypatch.setattr(main, "roi_config", main.RoiConfig.model_validate(CONFIG))
    main._apply_roi_targets(main.roi_config)
    tc = TestClient(main.app)  # no context manager: skip startup (no camera/MAVLink I/O)
    tc.gimbal, tc.mav, tc.ctrl, tc.cfg_path = gimbal, mav, ctrl, tmp_path / "roi_config.json"
    return tc


def test_get_config(client):
    body = client.get("/api/roi/config").json()
    assert body["targets"][0]["name"] == "Tower"


def test_save_config_persists_and_applies(client):
    new = {**CONFIG, "mavlink_url": "udpin:127.0.0.1:14999"}
    new["targets"] = [{**CONFIG["targets"][0], "id": "roi_3"}]
    assert client.post("/api/roi/config", json=new).status_code == 200
    assert client.cfg_path.exists()
    assert client.mav.restarted_with == "udpin:127.0.0.1:14999"
    assert [t.id for t in client.ctrl.targets()] == ["roi_3"]


def test_save_config_rejects_invalid(client):
    bad = {**CONFIG, "targets": [{**CONFIG["targets"][0], "lat": 123}]}
    assert client.post("/api/roi/config", json=bad).status_code == 422
    assert not client.cfg_path.exists()


def test_start_and_stop(client):
    assert client.post("/api/roi/start", json={"target_id": "roi_1"}).status_code == 200
    assert client.ctrl.status().active_target_id == "roi_1"
    assert client.post("/api/roi/stop").status_code == 200
    assert client.ctrl.status().active_target_id is None


def test_start_unknown_is_404(client):
    assert client.post("/api/roi/start", json={"target_id": "nope"}).status_code == 404


def test_start_unset_is_400(client):
    assert client.post("/api/roi/start", json={"target_id": "roi_2"}).status_code == 400


def test_manual_gimbal_speed_cancels_roi(client):
    client.post("/api/roi/start", json={"target_id": "roi_1"})
    client.post("/api/gimbal/speed", json={"yaw": 10, "pitch": 0})
    assert client.ctrl.status().active_target_id is None
    assert client.gimbal.speeds == [(10, 0)]


def test_stick_noise_does_not_cancel_roi(client):
    client.post("/api/roi/start", json={"target_id": "roi_1"})
    client.post("/api/gimbal/speed", json={"yaw": 2, "pitch": -1})
    assert client.ctrl.status().active_target_id == "roi_1"
    assert client.gimbal.speeds == []  # not forwarded while ROI is active


def test_explicit_stop_cancels_roi(client):
    client.post("/api/roi/start", json={"target_id": "roi_1"})
    client.post("/api/gimbal/speed", json={"yaw": 0, "pitch": 0})
    assert client.ctrl.status().active_target_id is None


def test_center_cancels_roi(client):
    client.post("/api/roi/start", json={"target_id": "roi_1"})
    client.post("/api/gimbal/center")
    assert client.ctrl.status().active_target_id is None


def test_app_shutdown_stops_roi_and_gimbal(monkeypatch):
    calls = []

    class Roi:
        def stop(self):
            calls.append("roi.stop")

        def shutdown(self):
            calls.append("roi.shutdown")

    class Cam:
        def set_gimbal_speed(self, yaw, pitch):
            calls.append(("speed", yaw, pitch))

    monkeypatch.setattr(main, "roi", Roi())
    monkeypatch.setattr(main, "camera", Cam())
    main.shutdown_event()
    assert calls == ["roi.stop", "roi.shutdown", ("speed", 0, 0)]
