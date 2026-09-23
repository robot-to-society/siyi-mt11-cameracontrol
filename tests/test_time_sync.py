import pytest

from app.camera_protocol import CameraState
from app.mavlink_source import TimeSample
from app.time_sync import TimeSync, current_unix_us

GPS_US = 1_790_000_000_000_000


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeTimeSource:
    def __init__(self, sample=None):
        self.sample = sample

    def latest_time(self):
        return self.sample


class FakeCamera:
    """Answers 0x40 after `rtt` seconds with its clock `offset_us` ahead of true time."""

    def __init__(self, clock, source, offset_us=0, rtt=0.02, answer=True):
        self.state = CameraState(connected=True)
        self.clock, self.source = clock, source
        self.offset_us, self.rtt, self.answer = offset_us, rtt, answer
        self.set_times = []

    def set_utc_time(self, unix_us):
        self.set_times.append(unix_us)
        self.offset_us = 0  # camera now follows the time we sent

    def request_system_time(self):
        if not self.answer:
            return
        mid = self.clock() + self.rtt / 2
        true_us = current_unix_us(self.source.sample, mid)
        self.clock.t += self.rtt
        self.state.camera_time = (true_us + self.offset_us, self.clock())


def make(sample=TimeSample(GPS_US, received_at=99.5), **cam_kw):
    clock = Clock()
    src = FakeTimeSource(sample)
    cam = FakeCamera(clock, src, **cam_kw)
    sync = TimeSync(cam, src, clock=clock, sleep=clock.sleep, resync_s=600.0)
    return sync, cam, src, clock


def test_current_unix_us_adds_elapsed_time():
    assert current_unix_us(TimeSample(GPS_US, received_at=10.0), now=10.25) == GPS_US + 250_000


def test_first_sync_sends_compensated_time_and_measures_offset():
    sync, cam, _, clock = make()
    sync.step()
    assert cam.set_times == [GPS_US + 500_000]  # received 0.5 s before sending
    st = sync.status()
    assert st.last_sync_ok is True
    assert st.offset_ms == pytest.approx(0.0, abs=0.01)
    assert st.rtt_ms == pytest.approx(20.0)


def test_no_gps_time_does_not_send():
    sync, cam, _, _ = make(sample=None)
    sync.step()
    assert cam.set_times == []
    assert "GPS" in sync.status().error


def test_stale_gps_time_does_not_send():
    sync, cam, _, _ = make(sample=TimeSample(GPS_US, received_at=50.0))
    sync.step()
    assert cam.set_times == []
    assert "stale" in sync.status().error


def test_resync_only_after_interval():
    sync, cam, src, clock = make()
    sync.step()
    clock.t += 300
    src.sample = TimeSample(GPS_US + 300_000_000, received_at=clock.t)
    sync.step()
    assert len(cam.set_times) == 1
    clock.t += 301
    src.sample = TimeSample(GPS_US + 601_000_000, received_at=clock.t)
    sync.step()
    assert len(cam.set_times) == 2


def test_reconnect_triggers_sync():
    sync, cam, src, clock = make()
    sync.step()
    cam.state.connected = False
    sync.step()
    cam.state.connected = True
    src.sample = TimeSample(GPS_US + 10_000_000, received_at=clock.t)
    sync.step()
    assert len(cam.set_times) == 2


def test_manual_request_triggers_sync():
    sync, cam, _, _ = make()
    sync.step()
    sync.request_sync()
    sync.step()
    assert len(cam.set_times) == 2


def test_camera_not_answering_0x40_reports_error():
    sync, cam, _, _ = make(answer=False)
    sync.step()
    st = sync.status()
    assert cam.set_times  # time was still sent
    assert st.offset_ms is None
    assert "0x40" in st.error


def test_camera_disconnected_skips():
    sync, cam, _, _ = make()
    cam.state.connected = False
    sync.step()
    assert cam.set_times == []
