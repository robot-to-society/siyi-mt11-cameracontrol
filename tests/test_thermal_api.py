from fastapi.testclient import TestClient

import app.main as main


def test_status_thermal_gain_with_range(monkeypatch):
    monkeypatch.setattr(main.camera.state, "thermal_gain", "high")
    body = TestClient(main.app).get("/api/status").json()
    assert body["thermal"] == {"gain": "high", "range_min_c": -20, "range_max_c": 150}


def test_status_thermal_low_gain(monkeypatch):
    monkeypatch.setattr(main.camera.state, "thermal_gain", "low")
    body = TestClient(main.app).get("/api/status").json()
    assert body["thermal"] == {"gain": "low", "range_min_c": 0, "range_max_c": 550}


def test_status_thermal_unknown(monkeypatch):
    monkeypatch.setattr(main.camera.state, "thermal_gain", None)
    assert TestClient(main.app).get("/api/status").json()["thermal"] is None
