import json

import pytest
from pydantic import ValidationError

from app.roi_config import DEFAULT_ROI_CONFIG, RoiConfig, load_roi_config, save_roi_config, with_default_slots


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
    loaded = load_roi_config(path)
    assert loaded.targets[0] == cfg.targets[0]  # saved preset kept as-is
    assert loaded == with_default_slots(cfg)  # empty roi_2..roi_10 slots appended
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


def test_default_has_ten_slots():
    assert [t.id for t in DEFAULT_ROI_CONFIG.targets] == [f"roi_{i}" for i in range(1, 11)]


def test_load_pads_old_four_slot_file_keeping_saved_targets(tmp_path):
    path = tmp_path / "roi.json"
    saved = valid_payload(
        targets=[
            {"id": f"roi_{i}", "name": f"T{i}", "lat": 35.0 + i / 100, "lon": 139.0, "alt_msl": 10.0}
            for i in range(1, 5)
        ]
    )
    path.write_text(json.dumps(saved))
    cfg = load_roi_config(path)
    assert [t.id for t in cfg.targets] == [f"roi_{i}" for i in range(1, 11)]
    assert cfg.targets[0].name == "T1" and cfg.targets[3].lat == pytest.approx(35.04)
    assert cfg.targets[9].lat == 0.0 and cfg.targets[9].lon == 0.0
    assert cfg.mavlink_url == saved["mavlink_url"]


def test_load_keeps_custom_ids_and_order(tmp_path):
    path = tmp_path / "roi.json"
    t = {"name": "", "lat": 1.0, "lon": 1.0, "alt_msl": 0.0}
    path.write_text(json.dumps(valid_payload(targets=[{**t, "id": "roi_2"}, {**t, "id": "tower"}])))
    ids = [x.id for x in load_roi_config(path).targets]
    assert ids[:2] == ["roi_2", "tower"]
    assert set(ids) >= {f"roi_{i}" for i in range(1, 11)}
    assert len(ids) == 11
