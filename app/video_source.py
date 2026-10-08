"""Pick which live video source the UI should play.

Order of preference: the MT11 camera over RTSP (relayed by MediaMTX), then an
Android phone screen served by the ``android-streaming`` relay. When neither
answers, the monitor pauses ``rescan_s`` (15 s by default) before probing both
again; while a source is live it is re-checked every ``check_s`` so a lost
camera is noticed quickly and the camera wins back as soon as it returns.

A live source is only abandoned after ``FAIL_THRESHOLD`` consecutive failed
probes, so one slow DESCRIBE does not tear down a working picture. A forced
mode (``mt11`` / ``android``) always proxies to that source regardless of the
probe: the operator asked for it, and the player's own retry handles outages.

State is an immutable ``SourceStatus`` snapshot replaced on every scan. Probes
run outside the lock; only the publish step is serialised.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal
from urllib.parse import urlsplit

from app.video_probe import probe_android_status, probe_rtsp, rtsp_request_uri

logger = logging.getLogger(__name__)

Source = Literal["mt11", "android", "none"]
Mode = Literal["auto", "mt11", "android"]
MODES: tuple[Mode, ...] = ("auto", "mt11", "android")

DEFAULT_RTSP_URL_TEMPLATE = "rtsp://{host}:8554/video1"
DEFAULT_ANDROID_URL = "http://127.0.0.1:8080"
DEFAULT_RESCAN_S = 15.0
DEFAULT_CHECK_S = 3.0
DEFAULT_PROBE_TIMEOUT_S = 2.0
# consecutive failed probes of the live source before switching away (auto mode)
FAIL_THRESHOLD = 2
THREAD_JOIN_S = 1.0


@dataclass(frozen=True)
class VideoSourceSettings:
    rtsp_url_template: str
    android_url: str  # "" disables the Android source
    android_token: str | None
    rescan_s: float
    check_s: float
    probe_timeout_s: float

    def rtsp_url(self, camera_host: str) -> str:
        return self.rtsp_url_template.format(host=camera_host)

    @property
    def android_enabled(self) -> bool:
        return bool(self.android_url)

    @property
    def android_status_url(self) -> str:
        return f"{self.android_url}/status" if self.android_url else ""

    @property
    def android_offer_url(self) -> str:
        return f"{self.android_url}/offer" if self.android_url else ""


def settings_from_env(env: dict[str, str] | None = None) -> VideoSourceSettings:
    """Read ``MT11_*`` environment overrides; invalid values raise ``ValueError`` at startup."""
    env = os.environ if env is None else env
    android_url = env.get("MT11_ANDROID_URL", DEFAULT_ANDROID_URL).strip().rstrip("/")
    if android_url and urlsplit(android_url).scheme not in ("http", "https"):
        raise ValueError(f"MT11_ANDROID_URL must be http(s) or empty to disable: {android_url!r}")
    token = env.get("MT11_ANDROID_TOKEN") or None
    if token is not None and (not token.isprintable() or any(ch in token for ch in " \r\n\t")):
        raise ValueError("MT11_ANDROID_TOKEN must be a single printable token")
    rtsp_template = env.get("MT11_RTSP_URL", DEFAULT_RTSP_URL_TEMPLATE)
    try:
        rtsp_request_uri(rtsp_template.format(host="192.0.2.1"))
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"MT11_RTSP_URL is not a usable rtsp:// template: {rtsp_template!r} ({exc})") from exc
    return VideoSourceSettings(
        rtsp_url_template=rtsp_template,
        android_url=android_url,
        android_token=token,
        rescan_s=_positive_float(env, "MT11_VIDEO_RESCAN_S", DEFAULT_RESCAN_S),
        check_s=_positive_float(env, "MT11_VIDEO_CHECK_S", DEFAULT_CHECK_S),
        probe_timeout_s=_positive_float(env, "MT11_PROBE_TIMEOUT_S", DEFAULT_PROBE_TIMEOUT_S),
    )


def _positive_float(env: dict[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{key} must be positive, got {raw!r}")
    return value


@dataclass(frozen=True)
class SourceStatus:
    source: Source = "none"
    mode: Mode = "auto"
    mt11_ok: bool = False
    android: dict[str, Any] | None = None  # relay /status payload (None when unreachable)
    fail_streak: int = 0  # consecutive probes in which the live source was down
    scanned_at: float = 0.0
    next_scan_at: float = 0.0
    scans: int = 0

    @property
    def android_ok(self) -> bool:
        return bool(self.android and self.android.get("streaming") is True)

    @property
    def scanning(self) -> bool:
        return self.scans == 0

    def probe_ok(self, source: str) -> bool:
        return self.mt11_ok if source == "mt11" else self.android_ok if source == "android" else False

    def as_dict(self, now: float) -> dict[str, Any]:
        return {
            **asdict(self),
            "android_ok": self.android_ok,
            "scanning": self.scanning,
            "scan_age_s": round(now - self.scanned_at, 1) if self.scans else None,
            "next_scan_in_s": round(max(0.0, self.next_scan_at - now), 1),
        }


def decide_source(mode: Mode, mt11_ok: bool, android_ok: bool) -> Source:
    """Auto prefers MT11, then Android; a forced mode is honoured as is (the player retries on its own)."""
    if mode in ("mt11", "android"):
        return mode
    if mt11_ok:
        return "mt11"
    return "android" if android_ok else "none"


def next_status(previous: SourceStatus, *, mt11_ok: bool, android: dict[str, Any] | None, now: float,
                rescan_s: float, check_s: float) -> SourceStatus:
    """Pure transition: apply fresh probe results with hysteresis and schedule the next scan."""
    probed = replace(previous, mt11_ok=mt11_ok, android=android)
    candidate = decide_source(previous.mode, mt11_ok, probed.android_ok)
    live = previous.source != "none"
    losing_live = previous.mode == "auto" and live and candidate != previous.source and not probed.probe_ok(previous.source)
    streak = previous.fail_streak + 1 if losing_live else 0
    source = previous.source if losing_live and streak < FAIL_THRESHOLD else candidate
    # a source just lost gets one quick re-check; a confirmed outage waits the full rescan pause
    pause = check_s if source != "none" or live else rescan_s
    return replace(probed, source=source, fail_streak=streak, scanned_at=now, next_scan_at=now + pause,
                   scans=previous.scans + 1)


class VideoSourceMonitor:
    """Background scanner; ``status`` is always a complete, immutable snapshot."""

    def __init__(
        self,
        settings: VideoSourceSettings,
        *,
        probe_mt11: Callable[[], bool],
        probe_android: Callable[[], dict[str, Any] | None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._probe_mt11 = probe_mt11
        self._probe_android = probe_android
        self._clock = clock
        self._status = SourceStatus()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def status(self) -> SourceStatus:
        return self._status

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def scan(self) -> SourceStatus:
        """Probe both sources now (outside the lock) and publish the resulting snapshot."""
        mt11_ok = _safe_probe(self._probe_mt11, False, "MT11 RTSP")
        android = _safe_probe(self._probe_android, None, "Android relay")
        with self._lock:
            previous = self._status
            status = next_status(previous, mt11_ok=mt11_ok, android=android, now=self._clock(),
                                 rescan_s=self.settings.rescan_s, check_s=self.settings.check_s)
            self._status = status
        if status.source != previous.source:
            logger.info("video source: %s -> %s (mt11=%s android=%s)", previous.source, status.source, mt11_ok,
                        status.android_ok)
        return status

    def set_mode(self, mode: str) -> SourceStatus:
        """Change the mode using the cached probe results (no I/O); the loop re-probes right after."""
        if mode not in MODES:
            raise ValueError(f"unknown video source mode: {mode!r}")
        with self._lock:
            previous = self._status
            source = decide_source(mode, previous.mt11_ok, previous.android_ok)
            self._status = replace(previous, mode=mode, source=source, fail_streak=0)
            status = self._status
        if status.source != previous.source:
            logger.info("video source: %s -> %s (mode %s)", previous.source, status.source, mode)
        self._wake.set()
        return status

    def run_loop(self, wait: Callable[[float], bool]) -> None:
        """Scan, then wait ``check_s`` or ``rescan_s``; ``wait`` returns True to stop."""
        while True:
            status = self.scan()
            if wait(max(0.0, status.next_scan_at - self._clock())):
                return

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, name="video-source-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=THREAD_JOIN_S)  # daemon thread; a probe may still be mid-timeout
            if not thread.is_alive():
                self._thread = None

    def _thread_main(self) -> None:
        def wait(seconds: float) -> bool:
            self._wake.wait(seconds)
            self._wake.clear()
            return self._stop.is_set()

        self.run_loop(wait)


def _safe_probe(probe: Callable[[], Any], down: Any, label: str) -> Any:
    try:
        return probe()
    except Exception as exc:  # noqa: BLE001 - a probe crash must read as "down", not kill the loop
        logger.warning("%s probe failed: %s", label, exc)
        return down


def build_monitor(settings: VideoSourceSettings, get_camera_host: Callable[[], str]) -> VideoSourceMonitor:
    """Wire the real probes; the RTSP host follows the camera IP set in the UI."""
    return VideoSourceMonitor(
        settings,
        probe_mt11=lambda: probe_rtsp(settings.rtsp_url(get_camera_host()), settings.probe_timeout_s),
        probe_android=lambda: probe_android_status(settings.android_status_url, settings.probe_timeout_s),
    )
