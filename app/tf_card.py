"""MT11 TF (microSD) card information, CMD 0x49 ACK."""

import struct
from dataclasses import dataclass
from typing import Optional

TF_STATUS = {
    0: "not_inserted",
    1: "mount_failed",
    2: "ok",
    3: "low_space",
    4: "read_only",
    5: "read_error",
}
FILESYSTEMS = {0: "unknown", 1: "FAT32", 2: "exFAT"}


@dataclass(frozen=True)
class TfCardInfo:
    status: str
    filesystem: str
    total_gb: float
    free_gb: float

    @property
    def free_ratio(self) -> Optional[float]:
        return self.free_gb / self.total_gb if self.total_gb > 0 else None


def parse_tf_card(payload: bytes) -> Optional[TfCardInfo]:
    """Status(u8), File system(u8), Total capacity(u16, GB x100), Available capacity(u16, GB x100)."""
    if len(payload) < 6:
        return None
    status, fs, total, free = struct.unpack("<BBHH", payload[:6])
    return TfCardInfo(
        status=TF_STATUS.get(status, f"status_{status}"),
        filesystem=FILESYSTEMS.get(fs, f"fs_{fs}"),
        total_gb=total / 100.0,
        free_gb=free / 100.0,
    )
