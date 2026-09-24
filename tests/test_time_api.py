from fastapi.testclient import TestClient

import app.main as main
from app.time_sync import TimeSyncStatus


class FakeTimeSync:
    def __init__(self, status):
        self._status = status
        self.requested = 0

    def status(self):
        return self._status

    def request_sync(self):
        self.requested += 1


def test_status_includes_time_sync(monkeypatch):
    monkeypatch.setattr(main.time, "monotonic", lambda: 130.0)
    fake = FakeTimeSync(TimeSyncStatus(last_sync_at=100.0, last_sync_ok=True, offset_ms=12.3, rtt_ms=8.0))
    monkeypatch.setattr(main, "time_sync", fake)
    body = TestClient(main.app).get("/api/status").json()
    assert body["time_sync"] == {
        "synced": True,
        "age_s": 30.0,
        "offset_ms": 12.3,
        "rtt_ms": 8.0,
        "error": None,
        "camera_ack": None,
    }


def test_status_before_first_sync(monkeypatch):
    monkeypatch.setattr(main, "time_sync", FakeTimeSync(TimeSyncStatus(error="no GPS time from FC (SYSTEM_TIME)")))
    body = TestClient(main.app).get("/api/status").json()
    assert body["time_sync"]["synced"] is False
    assert body["time_sync"]["age_s"] is None
    assert "GPS" in body["time_sync"]["error"]


def test_sync_now(monkeypatch):
    fake = FakeTimeSync(TimeSyncStatus())
    monkeypatch.setattr(main, "time_sync", fake)
    assert TestClient(main.app).post("/api/time/sync").status_code == 200
    assert fake.requested == 1


def test_status_includes_camera_clock(monkeypatch):
    from app.mavlink_source import TimeSample

    monkeypatch.setattr(main.time, "monotonic", lambda: 101.0)
    monkeypatch.setattr(main.camera.state, "camera_time", (1_790_000_000_500_000, 100.0))
    monkeypatch.setattr(main.mavlink, "latest_time", lambda: TimeSample(1_790_000_000_000_000, received_at=100.0))
    monkeypatch.setattr(main, "time_sync", FakeTimeSync(TimeSyncStatus()))
    body = TestClient(main.app).get("/api/status").json()
    assert body["camera_clock"] == {"camera_unix_ms": 1_790_000_000_500, "age_s": 1.0, "gps_offset_ms": 500.0}


def test_status_camera_clock_none(monkeypatch):
    monkeypatch.setattr(main.camera.state, "camera_time", None)
    monkeypatch.setattr(main, "time_sync", FakeTimeSync(TimeSyncStatus()))
    assert TestClient(main.app).get("/api/status").json()["camera_clock"] is None
