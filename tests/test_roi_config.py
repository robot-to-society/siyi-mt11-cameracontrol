import json

import pytest
from pydantic import ValidationError

from app.roi_config import DEFAULT_ROI_CONFIG, RoiConfig, load_roi_config, save_roi_config


def valid_payload(**overrides):
    return {
        "mavlink_url": "udpin:127.0.0.1:15555",
        "rate_hz": 10,
        "yaw_offset_deg": 0.0,
        "targets": [{"id": "roi_1", "name": "A", "lat": 35.0, "lon": 139.0, "alt_msl": 10.0}],
        **overrides,
    }


def test_valid_config_parses():
    cfg = RoiConfig.model_validate(valid_payload())
    assert cfg.targets[0].id == "roi_1"


@pytest.mark.parametrize(
    "target",
    [
        {"id": "roi_1", "name": "", "lat": 91.0, "lon": 0.0, "alt_msl": 0.0},
        {"id": "roi_1", "name": "", "lat": 0.0, "lon": 181.0, "alt_msl": 0.0},
        {"id": "", "name": "", "lat": 0.0, "lon": 0.0, "alt_msl": 0.0},
    ],
)
def test_invalid_target_rejected(target):
    with pytest.raises(ValidationError):
        RoiConfig.model_validate(valid_payload(targets=[target]))


def test_duplicate_ids_rejected():
    t = {"id": "roi_1", "name": "", "lat": 0.0, "lon": 0.0, "alt_msl": 0.0}
    with pytest.raises(ValidationError):
        RoiConfig.model_validate(valid_payload(targets=[t, t]))


def test_rate_bounds():
    with pytest.raises(ValidationError):
        RoiConfig.model_validate(valid_payload(rate_hz=0))


def test_load_missing_returns_default(tmp_path):
    assert load_roi_config(tmp_path / "none.json") == DEFAULT_ROI_CONFIG


def test_load_corrupt_returns_default(tmp_path):
    path = tmp_path / "roi.json"
    path.write_text("{not json")
    assert load_roi_config(path) == DEFAULT_ROI_CONFIG


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "roi.json"
    cfg = RoiConfig.model_validate(valid_payload())
    save_roi_config(path, cfg)
    assert load_roi_config(path) == cfg
    assert json.loads(path.read_text())["targets"][0]["lat"] == 35.0


@pytest.mark.parametrize(
    "url",
    ["udpin:127.0.0.1:15555", "udpout:192.168.1.10:14550", "tcp:127.0.0.1:5760", "/dev/ttyAMA0,921600", "/dev/serial0"],
)
def test_mavlink_url_allowed(url):
    assert RoiConfig.model_validate(valid_payload(mavlink_url=url)).mavlink_url == url


@pytest.mark.parametrize("url", ["tcpin:0.0.0.0:5760", "http://x", "udpin:127.0.0.1", "/etc/passwd", "udpin:1.2.3.4:99999"])
def test_mavlink_url_rejected(url):
    with pytest.raises(ValidationError):
        RoiConfig.model_validate(valid_payload(mavlink_url=url))


def test_default_mavlink_url_matches_rpanion_destination():
    # Rpanion-server "Telemetry Destination" 127.0.0.1:15555 (UDP client) -> app listens there
    assert RoiConfig().mavlink_url == "udpin:127.0.0.1:15555"
    assert DEFAULT_ROI_CONFIG.mavlink_url == "udpin:127.0.0.1:15555"
