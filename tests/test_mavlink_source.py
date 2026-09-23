from types import SimpleNamespace

import pytest

from app.mavlink_source import vehicle_state_from_global_position


def msg(**kwargs):
    base = dict(lat=350000000, lon=1390000000, alt=12345, relative_alt=0, hdg=9000)
    return SimpleNamespace(**{**base, **kwargs})


def test_converts_units():
    state = vehicle_state_from_global_position(msg(), received_at=5.0)
    assert state.lat == pytest.approx(35.0)
    assert state.lon == pytest.approx(139.0)
    assert state.alt_msl == pytest.approx(12.345)
    assert state.heading_deg == pytest.approx(90.0)
    assert state.received_at == 5.0


def test_unknown_heading_is_none():
    state = vehicle_state_from_global_position(msg(hdg=65535), received_at=0.0)
    assert state.heading_deg is None


def test_no_fix_returns_none():
    assert vehicle_state_from_global_position(msg(lat=0, lon=0), received_at=0.0) is None


class FakeMsg(SimpleNamespace):
    def get_type(self):
        return self.type

    def get_srcSystem(self):
        return 1

    def get_srcComponent(self):
        return 1


class FakeConn:
    def __init__(self, messages, source):
        self.messages = list(messages)
        self.source = source
        self.commands = []
        self.mav = SimpleNamespace(command_long_send=lambda *a: self.commands.append(a))
        self.closed = False

    def recv_match(self, **_):
        if not self.messages:
            self.source._stop_event.set()
            return None
        item = self.messages.pop(0)
        if isinstance(item, Advance):
            self.clock.t += item.seconds
            return None
        return item

    def close(self):
        self.closed = True


class Advance:
    """Test-only item: advance the fake clock instead of delivering a message."""

    def __init__(self, seconds):
        self.seconds = seconds


class StepClock:
    def __init__(self, t=10.0):
        self.t = t

    def __call__(self):
        return self.t


def heartbeat(autopilot=3):
    return FakeMsg(type="HEARTBEAT", autopilot=autopilot)


def position(**kw):
    return FakeMsg(type="GLOBAL_POSITION_INT", **{**vars(msg()), **kw})


def run_with(messages, clock=None):
    from app.mavlink_source import MavlinkSource

    clock = clock if clock is not None else StepClock()
    holder = {}

    def connect(url):
        conn = FakeConn(messages, source)
        conn.clock = clock
        holder["conn"] = conn
        return conn

    source = MavlinkSource("udpin:127.0.0.1:15555", clock=clock, connect=connect)
    source._run()
    return source, holder["conn"]


def test_no_request_during_grace_period_after_first_heartbeat():
    _, conn = run_with([heartbeat(), Advance(5), heartbeat(), Advance(4.9), heartbeat()])
    assert conn.commands == []


def test_requests_missing_streams_after_10s():
    source, conn = run_with([heartbeat(), Advance(10.1), heartbeat(), position()])
    assert all(c[2] == 511 for c in conn.commands)  # MAV_CMD_SET_MESSAGE_INTERVAL
    requested = {c[4]: c[5] for c in conn.commands}
    assert requested == {33: 100000, 2: 1000000}  # GLOBAL_POSITION_INT 10 Hz, SYSTEM_TIME 1 Hz
    assert source.latest().lat == pytest.approx(35.0)
    assert conn.closed


def test_streams_already_flowing_are_left_alone():
    # the FC already streams both (e.g. for Mission Planner): never touch its settings
    msgs = [heartbeat()]
    for _ in range(11):
        msgs += [Advance(1.0), position(), system_time(GPS_US), heartbeat()]
    _, conn = run_with(msgs)
    assert conn.commands == []


def test_only_the_missing_stream_is_requested():
    msgs = [heartbeat()]
    for _ in range(11):
        msgs += [Advance(1.0), position(), heartbeat()]
    _, conn = run_with(msgs)
    assert [c[4] for c in conn.commands] == [2]  # SYSTEM_TIME only


def test_loop_ignores_gcs_heartbeat():
    _, conn = run_with([heartbeat(autopilot=8)])
    assert conn.commands == []




def test_loop_ignores_position_without_fix():
    source, _ = run_with([position(lat=0, lon=0)])
    assert source.latest() is None


def test_connect_error_is_recorded():
    from app.mavlink_source import MavlinkSource

    import time

    def connect(url):
        raise OSError("port busy")

    source = MavlinkSource("udpin:127.0.0.1:15555", connect=connect)
    source.start()
    try:
        deadline = time.monotonic() + 2.0
        while source.last_error is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "port busy" in source.last_error
        assert not source.connected
    finally:
        source.stop()


class OtherSysMsg(FakeMsg):
    def get_srcSystem(self):
        return 42


def test_position_from_other_system_ignored_after_fc_known():
    other = OtherSysMsg(type="GLOBAL_POSITION_INT", **{**vars(msg()), "lat": 10 * 10**7})
    source, _ = run_with([heartbeat(), other, position()])
    assert source.latest().lat == pytest.approx(35.0)


def test_restart_uses_fresh_stop_event():
    import threading

    from app.mavlink_source import MavlinkSource

    release = threading.Event()
    urls = []

    def connect(url):
        urls.append(url)
        release.wait(5.0)  # simulate a connect that hangs past stop()'s join timeout
        raise OSError("gone")

    source = MavlinkSource("udpin:127.0.0.1:1", connect=connect, join_timeout_s=0.1)
    source.start()
    old_event = source._stop_event
    source.restart("udpin:127.0.0.1:2")
    assert old_event.is_set()  # the stuck old thread will exit once unblocked
    assert source._stop_event is not old_event
    release.set()
    source.stop()


def system_time(unix_us, **kw):
    return FakeMsg(type="SYSTEM_TIME", time_unix_usec=unix_us, time_boot_ms=1000, **kw)


GPS_US = 1_790_000_000_000_000  # 2026-09


def test_system_time_sample_stored_with_receive_time():
    source, conn = run_with([heartbeat(), system_time(GPS_US)], clock=StepClock(42.0))
    sample = source.latest_time()
    assert sample.unix_us == GPS_US and sample.received_at == 42.0


def test_system_time_without_gps_ignored():
    source, _ = run_with([system_time(0), system_time(1_000_000)])
    assert source.latest_time() is None


def test_time_sample_parse():
    from app.mavlink_source import time_sample_from_system_time

    assert time_sample_from_system_time(SimpleNamespace(time_unix_usec=GPS_US), 1.0).unix_us == GPS_US
    assert time_sample_from_system_time(SimpleNamespace(time_unix_usec=1_000), 1.0) is None
