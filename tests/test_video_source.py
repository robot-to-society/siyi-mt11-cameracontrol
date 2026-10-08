"""Video source selection: MT11 RTSP first, Android relay second, rescan after a pause."""

import http.server
import json
import socket
import threading
import time

import pytest

from app.video_probe import probe_android_status, probe_rtsp, rtsp_request_uri
from app.video_source import (
    MODES,
    SourceStatus,
    VideoSourceMonitor,
    VideoSourceSettings,
    decide_source,
    settings_from_env,
)


# ── pure decision ───────────────────────────────────────────────
@pytest.mark.parametrize(
    "mode, mt11, android, expected",
    [
        ("auto", True, True, "mt11"),
        ("auto", True, False, "mt11"),
        ("auto", False, True, "android"),
        ("auto", False, False, "none"),
        ("mt11", True, True, "mt11"),
        ("mt11", False, True, "mt11"),  # forced: honoured even when the probe fails (player retries)
        ("android", True, True, "android"),
        ("android", True, False, "android"),
    ],
)
def test_decide_source(mode, mt11, android, expected):
    assert decide_source(mode, mt11, android) == expected


def test_modes_are_the_three_known_values():
    assert MODES == ("auto", "mt11", "android")


# ── monitor ─────────────────────────────────────────────────────
SETTINGS = VideoSourceSettings(
    rtsp_url_template="rtsp://{host}:8554/video1",
    android_url="http://127.0.0.1:8080",
    android_token=None,
    rescan_s=15.0,
    check_s=3.0,
    probe_timeout_s=2.0,
)


class Probes:
    def __init__(self, mt11=False, android=None):
        self.mt11 = mt11
        self.android = android
        self.calls = []

    def probe_mt11(self):
        self.calls.append("mt11")
        return self.mt11

    def probe_android(self):
        self.calls.append("android")
        return self.android


def make_monitor(probes, clock=lambda: 100.0):
    return VideoSourceMonitor(SETTINGS, probe_mt11=probes.probe_mt11, probe_android=probes.probe_android, clock=clock)


def test_initial_status_is_none_in_auto_mode():
    status = make_monitor(Probes()).status
    assert status == SourceStatus()
    assert status.source == "none"
    assert status.mode == "auto"


def test_scan_prefers_mt11_and_returns_new_frozen_status():
    probes = Probes(mt11=True, android={"streaming": True, "width": 576})
    monitor = make_monitor(probes)
    before = monitor.status
    after = monitor.scan()
    assert after.source == "mt11"
    assert after.mt11_ok is True
    assert after.android == {"streaming": True, "width": 576}
    assert after.scanned_at == 100.0
    assert after.next_scan_at == 103.0  # check interval while a source is live
    assert after.scans == 1
    assert before.source == "none"  # previous snapshot untouched
    assert monitor.status is after
    with pytest.raises(Exception):
        after.source = "android"  # frozen


def test_scan_falls_back_to_android_then_none_with_rescan_pause():
    probes = Probes(mt11=False, android={"streaming": True})
    monitor = make_monitor(probes)
    assert monitor.scan().source == "android"
    probes.android = None
    assert monitor.scan().source == "android"  # first miss: hysteresis keeps the live source
    status = monitor.scan()
    assert status.source == "none"  # second consecutive miss: give up
    assert status.next_scan_at == 103.0  # one quick re-check right after losing a live source
    status = monitor.scan()
    assert status.next_scan_at == 115.0  # still nothing: 15 s pause before scanning both again
    assert probes.calls == ["mt11", "android"] * 4


def test_single_failed_probe_does_not_drop_a_live_source():
    probes = Probes(mt11=True)
    monitor = make_monitor(probes)
    assert monitor.scan().source == "mt11"
    probes.mt11 = False  # one slow DESCRIBE
    blip = monitor.scan()
    assert blip.source == "mt11"
    assert blip.mt11_ok is False  # raw probe result is still reported
    assert blip.fail_streak == 1
    probes.mt11 = True
    assert monitor.scan().fail_streak == 0


def test_losing_mt11_with_android_available_switches_after_threshold():
    probes = Probes(mt11=True, android={"streaming": True})
    monitor = make_monitor(probes)
    assert monitor.scan().source == "mt11"
    probes.mt11 = False
    assert monitor.scan().source == "mt11"
    assert monitor.scan().source == "android"
    probes.mt11 = True
    assert monitor.scan().source == "mt11"  # the primary wins back immediately


def test_forced_mode_ignores_probe_results():
    probes = Probes(mt11=False, android=None)
    monitor = make_monitor(probes)
    assert monitor.set_mode("mt11").source == "mt11"
    assert monitor.scan().source == "mt11"
    assert monitor.set_mode("android").source == "android"
    assert monitor.scan().source == "android"
    assert monitor.set_mode("auto").source == "none"


def test_set_mode_does_no_io_and_wakes_the_loop():
    probes = Probes(mt11=True, android={"streaming": True})
    monitor = make_monitor(probes)
    monitor.scan()
    probes.calls.clear()
    status = monitor.set_mode("android")
    assert status.source == "android"
    assert probes.calls == []  # decided from cached probe results
    assert monitor._wake.is_set()


def test_android_probe_with_streaming_false_counts_as_down():
    monitor = make_monitor(Probes(mt11=False, android={"streaming": False, "last_error": "no devices"}))
    status = monitor.scan()
    assert status.source == "none"
    assert status.android_ok is False
    assert status.android == {"streaming": False, "last_error": "no devices"}


def test_probe_exceptions_are_treated_as_down():
    def boom():
        raise OSError("network unreachable")

    monitor = VideoSourceMonitor(SETTINGS, probe_mt11=boom, probe_android=boom, clock=lambda: 1.0)
    status = monitor.scan()
    assert status.source == "none"
    assert status.mt11_ok is False
    assert status.android is None


def test_set_mode_validates_and_recomputes_from_cache():
    probes = Probes(mt11=True, android={"streaming": True})
    monitor = make_monitor(probes)
    assert monitor.scan().source == "mt11"
    status = monitor.set_mode("android")
    assert status.mode == "android"
    assert status.source == "android"
    assert monitor.set_mode("mt11").source == "mt11"
    assert monitor.set_mode("auto").source == "mt11"
    with pytest.raises(ValueError):
        monitor.set_mode("hdmi")


def test_as_dict_exposes_ages_relative_to_now():
    monitor = make_monitor(Probes(mt11=True), clock=lambda: 100.0)
    assert monitor.status.as_dict(now=5.0)["scan_age_s"] is None  # nothing scanned yet
    assert monitor.status.as_dict(now=5.0)["scanning"] is True
    monitor.scan()
    body = monitor.status.as_dict(now=101.5)
    assert body["source"] == "mt11"
    assert body["scanning"] is False
    assert body["mode"] == "auto"
    assert body["mt11_ok"] is True
    assert body["android_ok"] is False
    assert body["android"] is None
    assert body["scan_age_s"] == 1.5
    assert body["next_scan_in_s"] == 1.5
    assert body["scans"] == 1


def test_loop_waits_rescan_interval_when_nothing_is_available():
    probes = Probes()
    waits = []
    monitor = make_monitor(probes)

    def fake_wait(seconds):
        waits.append(seconds)
        return len(waits) >= 3  # "stop requested" after three sleeps

    monitor.run_loop(wait=fake_wait)
    assert waits == [15.0, 15.0, 15.0]
    assert monitor.status.scans == 3


def test_loop_checks_live_source_more_often_and_switches_back_to_mt11():
    probes = Probes(mt11=False, android={"streaming": True})
    waits = []
    monitor = make_monitor(probes)
    seen = []

    def fake_wait(seconds):
        waits.append(seconds)
        seen.append(monitor.status.source)
        probes.mt11 = True  # camera comes back while Android is live
        return len(waits) >= 2

    monitor.run_loop(wait=fake_wait)
    assert waits == [3.0, 3.0]
    assert seen == ["android", "mt11"]


def test_start_and_stop_thread():
    monitor = make_monitor(Probes(mt11=True))
    monitor.start()
    monitor.stop()
    assert monitor.status.scans >= 1
    assert not monitor.running


# ── settings ────────────────────────────────────────────────────
def test_settings_from_env_defaults(monkeypatch):
    for key in ("MT11_RTSP_URL", "MT11_ANDROID_URL", "MT11_ANDROID_TOKEN", "MT11_VIDEO_RESCAN_S", "MT11_VIDEO_CHECK_S", "MT11_PROBE_TIMEOUT_S"):
        monkeypatch.delenv(key, raising=False)
    s = settings_from_env()
    assert s == SETTINGS
    assert s.rtsp_url("192.168.144.25") == "rtsp://192.168.144.25:8554/video1"
    assert s.android_status_url == "http://127.0.0.1:8080/status"
    assert s.android_offer_url == "http://127.0.0.1:8080/offer"


def test_settings_validation_of_template_and_token(monkeypatch):
    for key in ("MT11_RTSP_URL", "MT11_ANDROID_URL", "MT11_ANDROID_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MT11_RTSP_URL", "rtsp://{host}:8554/{path}")
    with pytest.raises(ValueError):
        settings_from_env()
    monkeypatch.setenv("MT11_RTSP_URL", "rtsp://{host}/video1 extra")
    with pytest.raises(ValueError):
        settings_from_env()
    monkeypatch.delenv("MT11_RTSP_URL")
    monkeypatch.setenv("MT11_ANDROID_TOKEN", "bad\r\ntoken")
    with pytest.raises(ValueError):
        settings_from_env()


def test_empty_android_url_disables_the_android_source(monkeypatch):
    monkeypatch.setenv("MT11_ANDROID_URL", "")
    s = settings_from_env()
    assert s.android_enabled is False
    assert s.android_status_url == ""
    assert probe_android_status(s.android_status_url, timeout_s=0.1) is None


def test_settings_from_env_overrides_and_validation(monkeypatch):
    monkeypatch.setenv("MT11_RTSP_URL", "rtsp://10.0.0.5:554/live")
    monkeypatch.setenv("MT11_ANDROID_URL", "http://127.0.0.1:9090/")
    monkeypatch.setenv("MT11_ANDROID_TOKEN", "abc")
    monkeypatch.setenv("MT11_VIDEO_RESCAN_S", "30")
    s = settings_from_env()
    assert s.rtsp_url("ignored") == "rtsp://10.0.0.5:554/live"
    assert s.android_offer_url == "http://127.0.0.1:9090/offer"
    assert s.android_token == "abc"
    assert s.rescan_s == 30.0
    monkeypatch.setenv("MT11_VIDEO_RESCAN_S", "-1")
    with pytest.raises(ValueError):
        settings_from_env()
    monkeypatch.setenv("MT11_VIDEO_RESCAN_S", "15")
    monkeypatch.setenv("MT11_ANDROID_URL", "ftp://x")
    with pytest.raises(ValueError):
        settings_from_env()


# ── probes against local fake servers ───────────────────────────
def rtsp_server(reply: bytes):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    seen = {}

    def serve():
        conn, _ = srv.accept()
        with conn:
            seen["request"] = conn.recv(4096)
            conn.sendall(reply)

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1], seen, srv


def test_probe_rtsp_describe_ok():
    port, seen, srv = rtsp_server(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Length: 0\r\n\r\n")
    url = f"rtsp://127.0.0.1:{port}/video1"
    assert probe_rtsp(url, timeout_s=2.0) is True
    srv.close()
    assert seen["request"].startswith(f"DESCRIBE {url} RTSP/1.0\r\n".encode())
    assert b"CSeq: 1" in seen["request"]


def test_probe_rtsp_error_status_is_down():
    port, _, srv = rtsp_server(b"RTSP/1.0 404 Not Found\r\nCSeq: 1\r\n\r\n")
    assert probe_rtsp(f"rtsp://127.0.0.1:{port}/nope", timeout_s=2.0) is False
    srv.close()


def test_probe_rtsp_refused_and_garbage_are_down():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert probe_rtsp(f"rtsp://127.0.0.1:{port}/video1", timeout_s=0.5) is False
    port, _, srv = rtsp_server(b"HTTP/1.1 200 OK\r\n\r\n")
    assert probe_rtsp(f"rtsp://127.0.0.1:{port}/video1", timeout_s=2.0) is False
    srv.close()


def test_probe_rtsp_rejects_non_rtsp_url():
    with pytest.raises(ValueError):
        probe_rtsp("http://127.0.0.1/video1")


def test_probe_rtsp_rejects_control_characters_in_url():
    with pytest.raises(ValueError):
        probe_rtsp("rtsp://127.0.0.1:6379/\r\nFLUSHALL\r\nX:8554/video1")
    with pytest.raises(ValueError):
        probe_rtsp("rtsp://127.0.0.1:8554/video1 RTSP/1.0\r\nX: y")


def test_request_uri_is_rebuilt_from_parsed_parts():
    assert rtsp_request_uri("rtsp://10.0.0.5/video1") == ("10.0.0.5", 554, "rtsp://10.0.0.5:554/video1")
    assert rtsp_request_uri("rtsp://[fe80::1]:8554/a?b=1") == ("fe80::1", 8554, "rtsp://[fe80::1]:8554/a?b=1")
    assert rtsp_request_uri("rtsp://h:8554") == ("h", 8554, "rtsp://h:8554/")


def test_probe_rtsp_401_counts_as_reachable():
    port, _, srv = rtsp_server(b"RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\n\r\n")
    assert probe_rtsp(f"rtsp://127.0.0.1:{port}/video1", timeout_s=2.0) is True
    srv.close()


def test_probe_rtsp_total_time_is_bounded_against_a_trickling_server():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def trickle():
        conn, _ = srv.accept()
        with conn:
            conn.recv(4096)
            for _ in range(20):
                try:
                    conn.sendall(b"R")
                except OSError:
                    return
                time.sleep(0.1)

    threading.Thread(target=trickle, daemon=True).start()
    started = time.monotonic()
    assert probe_rtsp(f"rtsp://127.0.0.1:{srv.getsockname()[1]}/video1", timeout_s=0.5) is False
    assert time.monotonic() - started < 1.5
    srv.close()


def http_server(status: int, body: bytes):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_probe_android_status_returns_payload():
    srv = http_server(200, json.dumps({"streaming": True, "width": 576, "height": 1280}).encode())
    try:
        assert probe_android_status(f"http://127.0.0.1:{srv.server_port}/status", timeout_s=2.0) == {
            "streaming": True,
            "width": 576,
            "height": 1280,
        }
    finally:
        srv.shutdown()


def test_probe_android_status_down_cases():
    srv = http_server(200, b"not json")
    try:
        assert probe_android_status(f"http://127.0.0.1:{srv.server_port}/status", timeout_s=2.0) is None
    finally:
        srv.shutdown()
    srv = http_server(500, b"{}")
    try:
        assert probe_android_status(f"http://127.0.0.1:{srv.server_port}/status", timeout_s=2.0) is None
    finally:
        srv.shutdown()
    assert probe_android_status("http://127.0.0.1:1/status", timeout_s=0.5) is None
