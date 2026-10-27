"""The play tracker: session logic (screensaver and short-session filtering) and the MQTT publisher.

The tracker is a single stdlib-only script that runs on the cabinet, so it is loaded from its file path.
Events below use the argument shapes EmulationStation really sends (es-app/src/FileData.cpp,
SystemScreenSaver.cpp): game-start gets the backslash-escaped ROM path, its basename and the game name;
game-selected gets the system, the plain path and the name; game-end gets nothing.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import socket
import struct
import sys
import threading
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "playtracker", Path(__file__).parent.parent / "playtracker" / "playtracker.py"
)
assert _spec and _spec.loader
pt = importlib.util.module_from_spec(_spec)
sys.modules["playtracker"] = pt
_spec.loader.exec_module(pt)

SMW_PATH = "/userdata/roms/snes/Super Mario World (USA).sfc"
SMW_ESCAPED = r"/userdata/roms/snes/Super\ Mario\ World\ \(USA\).sfc"
PAC_PATH = "/userdata/roms/mame/pacman.zip"


class Clock:
    def __init__(self) -> None:
        self.t = 1_760_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def conf() -> dict[str, str]:
    c = dict(pt.DEFAULTS)
    c.update(MQTT_HOST="127.0.0.1", DEVICE_ID="game_room", DEVICE_NAME="Game Room Arcade")
    return c


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def tracker(conf, tmp_path, clock):
    t = pt.Tracker(conf, pt.State(tmp_path), clock)
    t.handle("start", [])  # first event publishes discovery; tests below look at what comes after
    return t


def topics(msgs) -> list[str]:
    return [m[0].rsplit("/", 1)[-1] for m in msgs]


def payload(msgs, suffix: str) -> dict:
    """The last message on a topic: what Home Assistant ends up showing."""
    return [json.loads(p) for t, p, _ in msgs if t.endswith(suffix)][-1]


PLAY_TOPICS = ("now_playing", "last_session", "session")


def play(msgs) -> list[str]:
    """Messages about play (Now playing, the log, session events), as opposed to what's on screen."""
    return [t for t in topics(msgs) if t in PLAY_TOPICS]


def activity(msgs) -> list[str]:
    return [p for t, p, _ in msgs if t.endswith("/activity")]


# --- Discovery ---------------------------------------------------------------------------
def test_first_event_announces_to_home_assistant(conf, tmp_path, clock):
    msgs = pt.Tracker(conf, pt.State(tmp_path), clock).handle("start", [])
    disc = [m for m in msgs if m[0].startswith("homeassistant/")]
    assert {m[0] for m in disc} == {
        "homeassistant/sensor/game_room/now_playing/config",
        "homeassistant/binary_sensor/game_room/playing/config",
        "homeassistant/sensor/game_room/now_playing_system/config",
        "homeassistant/sensor/game_room/last_session/config",
        "homeassistant/sensor/game_room/last_session_duration/config",
        "homeassistant/event/game_room/game_session/config",
        "homeassistant/binary_sensor/game_room/screensaver/config",
        "homeassistant/sensor/game_room/activity/config",
        "homeassistant/event/game_room/cabinet/config",
    }
    assert all(retain for _, _, retain in disc)
    assert [p for t, p, _ in msgs if t.endswith("/status")] == ["online"]  # announced once, not twice
    cfg = {t.split("/")[-2]: json.loads(p) for t, p, _ in disc}
    session = cfg["game_session"]
    assert session["event_types"] == ["session_start", "session_end"]
    assert session["state_topic"] == "batocera/game_room/session"
    assert session["device"]["name"] == "Game Room Arcade"
    assert cfg["cabinet"]["event_types"] == ["start", "wake", "sleep", "quit", "reboot", "shutdown"]
    assert cfg["activity"]["options"] == ["menu", "screensaver", "playing", "off"]
    # Activity alone has no availability topic, so "off" shows instead of "unavailable"
    assert [k for k, c in cfg.items() if "availability_topic" not in c] == ["activity"]
    # what a fresh boot shows: online, Idle, no screensaver, in the menu, and a start event
    assert payload(msgs, "/screensaver") == {"active": False}
    assert activity(msgs) == ["menu"]
    assert payload(msgs, "/cabinet")["event_type"] == "start"
    # ...and only once per boot
    assert not any(
        t.startswith("homeassistant/") for t, _, _ in pt.Tracker(conf, pt.State(tmp_path), clock).handle("start", [])
    )


# --- Sessions --------------------------------------------------------------------------------
def test_a_played_game_is_logged(tracker, clock):
    msgs = tracker.handle("game-start", [SMW_ESCAPED, "Super Mario World (USA)", "Super Mario World"])
    now = payload(msgs, "now_playing")
    assert (now["state"], now["game"], now["system"], now["rom"], now["source"]) == (
        "playing",
        "Super Mario World",
        "snes",
        SMW_PATH,
        "frontend",
    )
    start = payload(msgs, "/session")
    assert (start["event_type"], start["game"], start["source"]) == ("session_start", "Super Mario World", "frontend")
    assert activity(msgs) == ["playing"]
    clock.t += 25 * 60
    msgs = tracker.handle("game-end", [])
    assert topics(msgs) == ["now_playing", "last_session", "session", "activity"]
    assert payload(msgs, "now_playing") == {"state": "idle"}
    assert activity(msgs) == ["menu"]
    ev = payload(msgs, "/session")
    assert (ev["event_type"], ev["game"], ev["duration_s"]) == ("session_end", "Super Mario World", 1500)
    assert [r for t, _, r in msgs if t.endswith("/session")] == [False]  # events aren't retained


def test_short_sessions_are_not_logged(tracker, clock):
    tracker.handle("game-start", [PAC_PATH, "pacman", "Pac-Man"])
    clock.t += 20
    assert play(tracker.handle("game-end", [])) == ["now_playing"]  # back to idle, nothing logged


def test_screensaver_videos_never_log_anything(tracker, clock):
    msgs = tracker.handle("screensaver-start", ["random video"])
    assert play(msgs) == []
    assert payload(msgs, "/screensaver") == {"active": True, "type": "random video"}
    assert activity(msgs) == ["screensaver"]
    for _ in range(5):  # the random-video screensaver selects game after game
        assert tracker.handle("game-selected", ["snes", SMW_PATH, "Super Mario World"]) == []
        clock.t += 30
    msgs = tracker.handle("screensaver-stop", [])
    assert play(msgs) == []
    assert payload(msgs, "/screensaver") == {"active": False}
    assert activity(msgs) == ["menu"]


@pytest.mark.parametrize("game_start_first", [True, False])
def test_start_pressed_on_a_screensaver_video_is_ignored(tracker, clock, game_start_first):
    tracker.handle("screensaver-start", ["random video"])
    tracker.handle("game-selected", ["snes", SMW_PATH, "Super Mario World"])
    clock.t += 3
    # ES fires screensaver-stop and game-start back to back, and runs the scripts concurrently,
    # so they can arrive in either order.
    order = [
        ("game-start", [SMW_ESCAPED, "Super Mario World (USA)", "Super Mario World"]),
        ("screensaver-stop", []),
    ]
    msgs = [m for event, args in (order if game_start_first else order[::-1]) for m in tracker.handle(event, args)]
    assert play(msgs) == []  # not logged...
    assert activity(msgs)[-1] == "playing"  # ...but someone is playing, whichever event came last
    clock.t += 600
    msgs = tracker.handle("game-end", [])
    assert play(msgs) == []
    assert activity(msgs) == ["menu"]


def test_screensaver_launch_counts_when_configured(tracker, clock, conf):
    conf["IGNORE_SCREENSAVER_LAUNCHES"] = "no"
    tracker.handle("screensaver-start", ["slideshow"])
    tracker.handle("game-selected", ["snes", SMW_PATH, "Super Mario World"])
    tracker.handle("screensaver-stop", [])
    msgs = tracker.handle("game-start", [SMW_ESCAPED, "x", "Super Mario World"])
    assert payload(msgs, "now_playing")["source"] == "screensaver"


def test_a_game_started_later_is_a_normal_session(tracker, clock):
    tracker.handle("screensaver-start", ["random video"])
    tracker.handle("game-selected", ["snes", SMW_PATH, "Super Mario World"])
    tracker.handle("screensaver-stop", [])
    clock.t += 120  # well after the screensaver ended, someone picks the same game from the list
    msgs = tracker.handle("game-start", [SMW_ESCAPED, "x", "Super Mario World"])
    assert payload(msgs, "now_playing")["source"] == "frontend"


def test_a_different_game_launched_out_of_the_screensaver_counts(tracker, clock):
    # e.g. launch_game from the MCP server while the screensaver shows something else
    tracker.handle("screensaver-start", ["random video"])
    tracker.handle("game-selected", ["snes", SMW_PATH, "Super Mario World"])
    msgs = tracker.handle("game-start", [PAC_PATH, "pacman", "Pac-Man"])
    assert payload(msgs, "now_playing")["source"] == "frontend"


def test_missing_game_end_is_recovered(tracker, clock):
    tracker.handle("game-start", [PAC_PATH, "pacman", "Pac-Man"])
    clock.t += 300
    msgs = tracker.handle("game-start", [SMW_ESCAPED, "x", "Super Mario World"])
    events = [json.loads(p) for t, p, _ in msgs if t.endswith("/session")]
    assert [(e["event_type"], e["game"]) for e in events] == [
        ("session_end", "Pac-Man"),
        ("session_start", "Super Mario World"),
    ]
    assert payload(msgs, "now_playing")["game"] == "Super Mario World"


@pytest.mark.parametrize("quit_first", [True, False])
def test_a_reboot_is_reported_once(tracker, clock, quit_first):
    # ASSUMPTION B-PT-LIFECYCLE: a reboot fires `quit reboot` and then `reboot`, run concurrently.
    tracker.handle("game-start", [PAC_PATH, "pacman", "Pac-Man"])
    clock.t += 300
    order = [("quit", ["reboot"]), ("reboot", [])]
    msgs = [m for event, args in (order if quit_first else order[::-1]) for m in tracker.handle(event, args)]
    assert [json.loads(p)["event_type"] for t, p, _ in msgs if t.endswith("/cabinet")] == ["reboot"]
    assert play(msgs) == ["now_playing", "last_session", "session"]  # the session is closed once
    assert activity(msgs) == ["off"]
    assert not any(t.startswith("homeassistant/") for t, _, _ in msgs)  # no re-announce on the way down
    assert [p for t, p, _ in msgs if t.endswith("/status")][-1] == "offline"


def test_a_plain_quit_is_a_quit_event(tracker):
    msgs = tracker.handle("quit", [])
    assert payload(msgs, "/cabinet")["event_type"] == "quit"
    assert topics(msgs) == ["cabinet", "activity", "status"]


def test_sleep_and_wake_keep_the_cabinet_online(tracker, clock):
    # ASSUMPTION B-PT-LIFECYCLE: they're EmulationStation's idle sleep during a dim or black screensaver.
    tracker.handle("screensaver-start", ["dim"])
    msgs = tracker.handle("sleep", [])
    assert topics(msgs) == ["cabinet"]  # just the event: not offline, and Activity stays "screensaver"
    assert payload(msgs, "/cabinet")["event_type"] == "sleep"
    msgs = tracker.handle("wake", [])
    assert play(msgs) == [] and payload(msgs, "/cabinet")["event_type"] == "wake"
    assert activity(tracker.handle("screensaver-stop", [])) == ["menu"]


def test_wake_after_a_launch_doesnt_reset_now_playing(tracker):
    # An MCP launch during a dim screensaver: ES wakes, then fires game-start, and the wake script can
    # finish second. It mustn't publish Idle over the game.
    tracker.handle("game-start", [PAC_PATH, "pacman", "Pac-Man"])
    msgs = tracker.handle("wake", [])
    assert play(msgs) == [] and activity(msgs) == []


def test_a_restart_closes_a_session_left_open(tracker, clock):
    tracker.handle("game-start", [PAC_PATH, "pacman", "Pac-Man"])
    tracker.handle("screensaver-start", ["dim"])  # stale state from before the crash
    clock.t += 300
    msgs = tracker.handle("start", [])
    assert payload(msgs, "/session")["event_type"] == "session_end"
    assert payload(msgs, "now_playing") == {"state": "idle"}
    assert payload(msgs, "/screensaver") == {"active": False}
    assert activity(msgs) == ["menu"]


def test_shutdown_closes_the_session_and_goes_offline(tracker, clock):
    tracker.handle("game-start", [PAC_PATH, "pacman", "Pac-Man"])
    clock.t += 300
    msgs = tracker.handle("shutdown", [])
    assert topics(msgs) == ["now_playing", "last_session", "session", "activity", "status"]
    assert msgs[-1][1] == "offline"


def test_conf_parsing(tmp_path):
    p = tmp_path / "playtracker.conf"
    p.write_text('# comment\nMQTT_HOST = 192.168.1.10\nmqtt_password="p@ss=word"\nDEVICE_ID=Game Room\'s Arcade!\n')
    c = pt.load_conf(p)
    assert (c["MQTT_HOST"], c["MQTT_PASSWORD"], c["DEVICE_ID"], c["MIN_SESSION_SECONDS"]) == (
        "192.168.1.10",
        "p@ss=word",
        "game_room_s_arcade_",
        "60",
    )


def test_taking_the_lock_doesnt_rewrite_the_lock_file(tmp_path):
    # open(..., "w") would truncate (O_TRUNC) on every event: a needless write to the cabinet's storage each
    # time. Append mode creates the file once and then leaves it alone; flock doesn't care about its contents.
    lock = tmp_path / "lock"
    lock.write_text("left by an earlier run\n")
    with pt.State(tmp_path).locked():
        assert lock.read_text() == "left by an earlier run\n"
    assert lock.read_text() == "left by an earlier run\n"


def test_state_writes_use_a_temp_file_per_process(tmp_path, monkeypatch):
    # ES runs this script concurrently. With a shared "session.tmp", two runs writing the same name could
    # interleave: one renames the other's half-written file into place. A PID in the name keeps them apart.
    state = pt.State(tmp_path)
    other = tmp_path / "session.99.tmp"  # another run, mid-write
    other.write_text('{"half": ')
    renamed: list[str] = []
    original = pt.Path.replace

    def spy(self, target):
        renamed.append(self.name)
        return original(self, target)

    monkeypatch.setattr(pt.os, "getpid", lambda: 4242)
    monkeypatch.setattr(pt.Path, "replace", spy)
    state.write("session", {"game": "Pac-Man"})
    assert renamed == ["session.4242.tmp"]
    assert state.read("session") == {"game": "Pac-Man"}
    assert other.read_text() == '{"half": '  # the other run's file is untouched
    assert sorted(p.name for p in tmp_path.iterdir()) == ["session.99.tmp", "session.json"]


def test_main_ignores_unhandled_events_without_touching_state(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "STATE_DIR", tmp_path / "state")
    assert pt.main(["playtracker.py", "system-selected", "snes"]) == 0
    assert pt.main(["playtracker.py", "game-selected", "snes", SMW_PATH, "x"]) == 0
    assert not (tmp_path / "state").exists()


# --- MQTT -----------------------------------------------------------------------------------------
class FakeBroker:
    """Speaks just enough MQTT 3.1.1 to accept CONNECT and QoS 1 PUBLISH, like Mosquitto does."""

    def __init__(self, refuse: int = 0) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.refuse = refuse
        self.connects: list[dict] = []
        self.published: list[tuple[str, str, bool]] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn, contextlib.suppress(OSError, pt.MQTTError):
                self._session(conn)

    def _session(self, conn: socket.socket) -> None:
        while True:
            header, body = pt._read_packet(conn)
            kind = header >> 4
            if kind == 1:  # CONNECT
                n = struct.unpack("!H", body[:2])[0]
                assert body[2 : 2 + n] == b"MQTT" and body[2 + n] == 4
                flags = body[3 + n]
                rest = body[6 + n :]
                fields = []
                while rest:
                    ln = struct.unpack("!H", rest[:2])[0]
                    fields.append(rest[2 : 2 + ln].decode())
                    rest = rest[2 + ln :]
                self.connects.append({"flags": flags, "fields": fields})
                conn.sendall(bytes([0x20, 2, 0, self.refuse]))
                if self.refuse:
                    return
            elif kind == 3:  # PUBLISH, QoS 1
                n = struct.unpack("!H", body[:2])[0]
                topic = body[2 : 2 + n].decode()
                pid = body[2 + n : 4 + n]
                self.published.append((topic, body[4 + n :].decode(), bool(header & 1)))
                conn.sendall(b"\x40\x02" + pid)
            elif kind == 14:  # DISCONNECT
                return

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def broker():
    b = FakeBroker()
    yield b
    b.close()


def test_publish(conf, broker):
    conf.update(MQTT_PORT=str(broker.port), MQTT_USER="batocera", MQTT_PASSWORD="secret")
    big = "x" * 300  # needs a two-byte remaining length
    pt.publish(conf, [("a/b", "online", True), ("a/c", big, False)])
    assert broker.published == [("a/b", "online", True), ("a/c", big, False)]
    c = broker.connects[0]
    assert c["flags"] == 0xC2 and c["fields"][1:] == ["batocera", "secret"]


def test_bad_credentials_are_reported(conf):
    b = FakeBroker(refuse=4)
    try:
        conf["MQTT_PORT"] = str(b.port)
        with pytest.raises(pt.MQTTError, match="bad username or password"):
            pt.publish(conf, [("a", "b", False)])
    finally:
        b.close()


class FakeTLS:
    """Stands in for ssl.create_default_context(): records the wrap and hands back a socket that notes its close.

    The handshake itself is the ssl module's job; what playtracker owns is *when* it wraps, with which
    server_hostname (that's what certificate checking matches against), and that the wrapped socket is closed.
    """

    def __init__(self) -> None:
        self.wrapped: list[dict] = []
        self.sockets: list[TLSSocket] = []

    def wrap_socket(self, sock: socket.socket, server_hostname: str | None = None) -> TLSSocket:
        self.wrapped.append({"fd": sock.fileno(), "server_hostname": server_hostname})
        s = TLSSocket(sock)
        self.sockets.append(s)
        return s


class TLSSocket:
    """Passes everything through to the plain socket (the fake broker speaks plain MQTT), and records close()."""

    def __init__(self, raw: socket.socket) -> None:
        self.raw = raw
        self.closed = False

    def __getattr__(self, name: str):
        return getattr(self.raw, name)

    def close(self) -> None:
        self.closed = True
        self.raw.close()

    def __enter__(self) -> TLSSocket:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@pytest.fixture
def tls(monkeypatch) -> FakeTLS:
    fake = FakeTLS()
    monkeypatch.setattr(pt.ssl, "create_default_context", lambda: fake)
    return fake


def test_mqtt_tls_yes_wraps_the_socket(conf, broker, tls):
    conf.update(MQTT_PORT=str(broker.port), MQTT_TLS="yes", MQTT_HOST="127.0.0.1")
    pt.publish(conf, [("a/b", "online", True)])
    assert [w["server_hostname"] for w in tls.wrapped] == ["127.0.0.1"]
    assert broker.published == [("a/b", "online", True)]  # MQTT went through the wrapped socket
    assert [s.closed for s in tls.sockets] == [True]


def test_port_8883_implies_tls(conf, tls, monkeypatch):
    # 8883 is MQTT-over-TLS's registered port: a plain CONNECT sent there would just be dropped by the broker.
    # The broker here listens on a random port, so the connection is redirected to it.
    b = FakeBroker()
    real = pt.socket.create_connection

    def connect(address, timeout=None):
        assert address == ("broker.lan", 8883)
        return real(("127.0.0.1", b.port), timeout=timeout)

    monkeypatch.setattr(pt.socket, "create_connection", connect)
    try:
        conf.update(MQTT_HOST="broker.lan", MQTT_PORT="8883")  # MQTT_TLS left at its default, "no"
        assert conf["MQTT_TLS"] == "no"
        pt.publish(conf, [("a/b", "online", True)])
        assert [w["server_hostname"] for w in tls.wrapped] == ["broker.lan"]
        assert b.published == [("a/b", "online", True)]
        assert [s.closed for s in tls.sockets] == [True]
    finally:
        b.close()


def test_plain_mqtt_by_default(conf, broker, tls):
    conf["MQTT_PORT"] = str(broker.port)
    pt.publish(conf, [("a/b", "online", True)])
    assert tls.wrapped == [] and broker.published == [("a/b", "online", True)]


def test_tls_socket_is_closed_when_the_broker_refuses(conf, tls):
    b = FakeBroker(refuse=5)
    try:
        conf.update(MQTT_PORT=str(b.port), MQTT_TLS="on")
        with pytest.raises(pt.MQTTError, match="not authorized"):
            pt.publish(conf, [("a", "b", False)])
        assert [s.closed for s in tls.sockets] == [True]
    finally:
        b.close()


def test_a_failed_tls_handshake_closes_the_connection_and_queues(conf, tmp_path, monkeypatch):
    # ssl.SSLError (bad certificate, a broker that doesn't speak TLS) is an OSError, so deliver() queues
    # the messages like any other outage, and the plain socket it was wrapping must not leak.
    opened: list[socket.socket] = []
    real = pt.socket.create_connection

    def connect(address, timeout=None):
        s = real(address, timeout=timeout)
        opened.append(s)
        return s

    class Refusing:
        def wrap_socket(self, sock: socket.socket, server_hostname: str | None = None) -> socket.socket:
            raise pt.ssl.SSLError("certificate verify failed")

    b = FakeBroker()
    monkeypatch.setattr(pt.socket, "create_connection", connect)
    monkeypatch.setattr(pt.ssl, "create_default_context", Refusing)
    try:
        conf.update(MQTT_PORT=str(b.port), MQTT_TLS="yes")
        logs: list[str] = []
        pt.deliver(conf, pt.State(tmp_path), [("t/status", "online", True)], logs.append)
        assert "certificate verify failed" in logs[-1]
        assert pt.State(tmp_path).read("outbox")["messages"] == [["t/status", "online", True]]
        assert [s.fileno() for s in opened] == [-1]  # closed
    finally:
        b.close()


def test_unreachable_broker_queues_until_the_next_event(conf, tmp_path):
    state = pt.State(tmp_path)
    logs: list[str] = []
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    conf["MQTT_PORT"] = str(dead.getsockname()[1])  # bound, not listening: connection refused
    dead.close()
    end = json.dumps({"event_type": "session_end", "game": "Pac-Man"})
    msgs = [
        ("t/now_playing", "{}", True),
        ("t/session", json.dumps({"event_type": "session_start", "game": "Pac-Man"}), False),
        ("t/now_playing", '{"state":"idle"}', True),
        ("t/session", end, False),
        ("t/cabinet", json.dumps({"event_type": "shutdown"}), False),
    ]
    pt.deliver(conf, state, msgs, logs.append)
    queued = state.read("outbox")["messages"]
    # only the newest retained value per topic is worth keeping, and of the events only the logged session:
    # a shutdown event delivered at the next boot would run the shutdown automation then
    assert [tuple(m) for m in queued] == [("t/now_playing", '{"state":"idle"}', True), ("t/session", end, False)]
    assert "2 event(s) dropped" in logs[-1]
    b = FakeBroker()
    try:
        conf["MQTT_PORT"] = str(b.port)
        pt.deliver(conf, state, [("t/status", "online", True)], logs.append)
        assert [m[0] for m in b.published] == ["t/now_playing", "t/session", "t/status"]
        assert state.read("outbox") == {}
    finally:
        b.close()
