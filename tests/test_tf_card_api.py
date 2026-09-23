import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.tf_card import TfCardInfo


def test_status_includes_tf_card(monkeypatch):
    monkeypatch.setattr(main.camera.state, "tf_card", TfCardInfo("ok", "exFAT", 58.2, 12.34))
    body = TestClient(main.app).get("/api/status").json()
    assert body["tf_card"] == {
        "status": "ok",
        "filesystem": "exFAT",
        "total_gb": 58.2,
        "free_gb": 12.34,
        "free_percent": pytest.approx(21.2, abs=0.05),
    }


def test_status_tf_card_unknown(monkeypatch):
    monkeypatch.setattr(main.camera.state, "tf_card", None)
    assert TestClient(main.app).get("/api/status").json()["tf_card"] is None
