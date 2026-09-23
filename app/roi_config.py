"""ROI settings (MAVLink endpoint + GPS target presets), validated and persisted as JSON."""

import json
import logging
import os
import re
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

# udpin/udpout/udp/tcp (client) to host:port, or a serial device (/dev/..., optional ",baud").
# Listening TCP (tcpin) is intentionally not allowed.
_NET_URL = re.compile(r"^(udpin|udpout|udp|tcp):[A-Za-z0-9.\-]+:(\d{1,5})$")
_SERIAL_URL = re.compile(r"^/dev/(tty|serial)[A-Za-z0-9_\-]*(,\d{3,7})?$")


class RoiTargetModel(BaseModel):
    id: str = Field(min_length=1, max_length=32)
    name: str = Field(default="", max_length=64)
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    alt_msl: float = Field(ge=-500.0, le=9000.0)


class RoiConfig(BaseModel):
    mavlink_url: str = Field(default="udpin:127.0.0.1:15555", min_length=1, max_length=256)
    rate_hz: float = Field(default=10.0, gt=0.0, le=50.0)
    yaw_offset_deg: float = Field(default=0.0, ge=-180.0, le=180.0)
    targets: list[RoiTargetModel] = Field(default_factory=list, max_length=20)

    @field_validator("mavlink_url")
    @classmethod
    def _allowed_url(cls, url: str) -> str:
        net = _NET_URL.match(url)
        if net and 1 <= int(net.group(2)) <= 65535:
            return url
        if _SERIAL_URL.match(url):
            return url
        raise ValueError("mavlink_url must be udpin|udpout|udp|tcp:<host>:<port> or /dev/tty*[,baud]")

    @field_validator("targets")
    @classmethod
    def _unique_ids(cls, targets: list[RoiTargetModel]) -> list[RoiTargetModel]:
        ids = [t.id for t in targets]
        if len(ids) != len(set(ids)):
            raise ValueError("target ids must be unique")
        return targets


# Preset slots roi_1..roi_N (joystick button functions use the same ids)
ROI_SLOT_COUNT = 10


def _empty_slot(index: int) -> RoiTargetModel:
    return RoiTargetModel(id=f"roi_{index}", name="", lat=0.0, lon=0.0, alt_msl=0.0)


def with_default_slots(config: RoiConfig) -> RoiConfig:
    """Append any missing roi_1..roi_N slots (e.g. files saved when there were only 4)."""
    existing = {t.id for t in config.targets}
    missing = [_empty_slot(i) for i in range(1, ROI_SLOT_COUNT + 1) if f"roi_{i}" not in existing]
    if not missing:
        return config
    return RoiConfig.model_validate({**config.model_dump(), "targets": [*config.model_dump()["targets"], *[m.model_dump() for m in missing]]})


DEFAULT_ROI_CONFIG = RoiConfig(targets=[_empty_slot(i) for i in range(1, ROI_SLOT_COUNT + 1)])


def load_roi_config(path: Path) -> RoiConfig:
    if not path.exists():
        return DEFAULT_ROI_CONFIG
    try:
        return with_default_slots(RoiConfig.model_validate(json.loads(path.read_text())))
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        logger.warning("Invalid ROI config %s, using defaults: %s", path, exc)
        return DEFAULT_ROI_CONFIG


def save_roi_config(path: Path, config: RoiConfig) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(config.model_dump(), ensure_ascii=False, indent=2))
    os.replace(tmp, path)
