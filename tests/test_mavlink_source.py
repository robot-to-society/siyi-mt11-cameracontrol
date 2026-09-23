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
        return self.messages.pop(0)

    def close(self):
        self.closed = True


def heartbeat(autopilot=3):
    return FakeMsg(type="HEARTBEAT", autopilot=autopilot)


def position(**kw):
    return FakeMsg(type="GLOBAL_POSITION_INT", **{**vars(msg()), **kw})


def run_with(messages, clock=lambda: 10.0):
    from app.mavlink_source import MavlinkSource

    holder = {}

    def connect(url):
        holder["conn"] = FakeConn(messages, source)
        return holder["conn"]

    source = MavlinkSource("udpin:127.0.0.1:15555", clock=clock, connect=connect)
    source._run()
    return source, holder["conn"]


def test_loop_requests_stream_on_fc_heartbeat_and_stores_position():
    source, conn = run_with([heartbeat(), position()])
    assert len(conn.commands) == 1
    assert conn.commands[0][2] == 511  # MAV_CMD_SET_MESSAGE_INTERVAL
    assert conn.commands[0][4] == 33  # GLOBAL_POSITION_INT
    assert source.latest().lat == pytest.approx(35.0)
    assert conn.closed


def test_loop_ignores_gcs_heartbeat():
    _, conn = run_with([heartbeat(autopilot=8)])
    assert conn.commands == []


def test_loop_does_not_rerequest_when_fresh():
    _, conn = run_with([position(), heartbeat()])
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
