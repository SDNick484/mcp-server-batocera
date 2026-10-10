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
    c.update(MQTT_HOST="127.0.0.1", DEVICE_ID="nicks_arcade", DEVICE_NAME="Nick's Arcade")
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


# --- Discovery ---------------------------------------------------------------------------
def test_first_event_announces_to_home_assistant(conf, tmp_path, clock):
    msgs = pt.Tracker(conf, pt.State(tmp_path), clock).handle("start", [])
    disc = [m for m in msgs if m[0].startswith("homeassistant/")]
    assert {m[0] for m in disc} == {
        "homeassistant/sensor/nicks_arcade/now_playing/config",
        "homeassistant/binary_sensor/nicks_arcade/playing/config",
        "homeassistant/sensor/nicks_arcade/now_playing_system/config",
        "homeassistant/sensor/nicks_arcade/last_session/config",
        "homeassistant/sensor/nicks_arcade/last_session_duration/config",
        "homeassistant/event/nicks_arcade/game_session/config",
    }
    assert all(retain for _, _, retain in disc)
    assert [p for t, p, _ in msgs if t.endswith("/status")] == ["online"]  # announced once, not twice
    event = json.loads(next(p for t, p, _ in disc if "/event/" in t))
    assert event["event_types"] == ["session_end"] and event["state_topic"] == "batocera/nicks_arcade/session"
    assert event["device"]["name"] == "Nick's Arcade"
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
    clock.t += 25 * 60
    msgs = tracker.handle("game-end", [])
    assert topics(msgs) == ["now_playing", "last_session", "session"]
    assert payload(msgs, "now_playing") == {"state": "idle"}
    ev = payload(msgs, "/session")
    assert (ev["event_type"], ev["game"], ev["duration_s"]) == ("session_end", "Super Mario World", 1500)
    assert [r for t, _, r in msgs if t.endswith("/session")] == [False]  # events aren't retained


def test_short_sessions_are_not_logged(tracker, clock):
    tracker.handle("game-start", [PAC_PATH, "pacman", "Pac-Man"])
    clock.t += 20
    assert topics(tracker.handle("game-end", [])) == ["now_playing"]  # back to idle, nothing logged


def test_screensaver_videos_never_log_anything(tracker, clock):
    tracker.handle("screensaver-start", ["random video"])
    for _ in range(5):  # the random-video screensaver selects game after game
        assert tracker.handle("game-selected", ["snes", SMW_PATH, "Super Mario World"]) == []
        clock.t += 30
    assert tracker.handle("screensaver-stop", []) == []


def test_start_pressed_on_a_screensaver_video_is_ignored(tracker, clock):
    tracker.handle("screensaver-start", ["random video"])
    tracker.handle("game-selected", ["snes", SMW_PATH, "Super Mario World"])
    clock.t += 3
    # ES fires screensaver-stop and game-start back to back, and runs the scripts concurrently,
    # so they can arrive in either order. Here game-start arrives first.
    assert tracker.handle("game-start", [SMW_ESCAPED, "Super Mario World (USA)", "Super Mario World"]) == []
    tracker.handle("screensaver-stop", [])
    clock.t += 600
    assert tracker.handle("game-end", []) == []


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
    assert payload(msgs, "/session")["game"] == "Pac-Man"
    assert payload(msgs, "now_playing")["game"] == "Super Mario World"


def test_shutdown_closes_the_session_and_goes_offline(tracker, clock):
    tracker.handle("game-start", [PAC_PATH, "pacman", "Pac-Man"])
    clock.t += 300
    msgs = tracker.handle("shutdown", [])
    assert topics(msgs) == ["now_playing", "last_session", "session", "status"]
    assert msgs[-1][1] == "offline"


def test_conf_parsing(tmp_path):
    p = tmp_path / "playtracker.conf"
    p.write_text('# comment\nMQTT_HOST = 192.168.1.10\nmqtt_password="p@ss=word"\nDEVICE_ID=Nick\'s Arcade!\n')
    c = pt.load_conf(p)
    assert (c["MQTT_HOST"], c["MQTT_PASSWORD"], c["DEVICE_ID"], c["MIN_SESSION_SECONDS"]) == (
        "192.168.1.10",
        "p@ss=word",
        "nick_s_arcade_",
        "60",
    )


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


def test_unreachable_broker_queues_until_the_next_event(conf, tmp_path):
    state = pt.State(tmp_path)
    logs: list[str] = []
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    conf["MQTT_PORT"] = str(dead.getsockname()[1])  # bound, not listening: connection refused
    dead.close()
    msgs = [("t/now_playing", "{}", True), ("t/now_playing", '{"state":"idle"}', True), ("t/session", "{1}", False)]
    pt.deliver(conf, state, msgs, logs.append)
    queued = state.read("outbox")["messages"]
    # only the newest retained value per topic is worth keeping; every session event is kept
    assert [tuple(m) for m in queued] == [("t/now_playing", '{"state":"idle"}', True), ("t/session", "{1}", False)]
    b = FakeBroker()
    try:
        conf["MQTT_PORT"] = str(b.port)
        pt.deliver(conf, state, [("t/status", "online", True)], logs.append)
        assert [m[0] for m in b.published] == ["t/now_playing", "t/session", "t/status"]
        assert state.read("outbox") == {}
    finally:
        b.close()
