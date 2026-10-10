#!/usr/bin/env python3
"""Batocera play tracker: publishes what's being played to MQTT, with Home Assistant discovery.

Install on the cabinet as
    /userdata/system/configs/emulationstation/scripts/playtracker.py   (chmod +x)
with its settings in
    /userdata/system/configs/playtracker.conf

EmulationStation runs every executable in that scripts folder on every event,
with the event name first: ``playtracker.py game-start <rom> <basename> <name>``.
It runs them asynchronously, so several copies of this script can run at once;
state changes are serialized with a lock file. Only the Python standard library
is used, because Batocera's Python has no pip packages to rely on.

Why screensaver activity can't pollute the log
----------------------------------------------
Batocera's screensaver (dim, black, random video, slideshow, suspend) never starts
a game on its own. The random-video and slideshow screensavers *select* games to
show their videos, which fires ``game-selected`` (that is what makes a Pixelcade
marquee follow the screensaver). This tracker only logs ``game-start`` /
``game-end``, which fire only when a game really launches.

The one way a game launches from the screensaver is a person pressing START on
the video being shown (Screensaver settings > "Screensaver controls"). This
script recognizes that case (the started game is the one the screensaver was
showing, and the screensaver was active within the last few seconds) and, with
IGNORE_SCREENSAVER_LAUNCHES=yes (the default), doesn't log it. Sessions shorter
than MIN_SESSION_SECONDS (default 60) aren't logged either.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import struct
import sys
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

CONF_PATH = Path(os.environ.get("PLAYTRACKER_CONF", "/userdata/system/configs/playtracker.conf"))
STATE_DIR = Path(os.environ.get("PLAYTRACKER_STATE", "/tmp/playtracker"))
SCREENSAVER_GRACE_S = 10.0
OUTBOX_MAX = 50

DEFAULTS = {
    "MQTT_HOST": "",
    "MQTT_PORT": "1883",
    "MQTT_USER": "",
    "MQTT_PASSWORD": "",
    "TOPIC_PREFIX": "batocera",
    "DISCOVERY_PREFIX": "homeassistant",
    "DEVICE_ID": "",  # default: the hostname
    "DEVICE_NAME": "Batocera",
    "MIN_SESSION_SECONDS": "60",
    "IGNORE_SCREENSAVER_LAUNCHES": "yes",
}

Message = tuple[str, str, bool]  # topic, payload, retain


# --- Config --------------------------------------------------------------------
def load_conf(path: Path = CONF_PATH) -> dict[str, str]:
    conf = dict(DEFAULTS)
    try:
        text = path.read_text()
    except OSError:
        return conf
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        conf[key.strip().upper()] = value
    if not conf["DEVICE_ID"]:
        conf["DEVICE_ID"] = socket.gethostname() or "batocera"
    conf["DEVICE_ID"] = re.sub(r"[^a-z0-9_-]", "_", conf["DEVICE_ID"].lower())
    return conf


def yes(value: str) -> bool:
    return value.strip().lower() in ("1", "yes", "true", "on")


# --- Arguments ---------------------------------------------------------------------
def unescape(path: str) -> str:
    """EmulationStation backslash-escapes shell characters in the game-start ROM path."""
    return re.sub(r"\\(.)", r"\1", path)


_ROMS = re.compile(r"/roms/([^/]+)/")


def system_of(path: str) -> str | None:
    m = _ROMS.search(path)
    return m.group(1) if m else None


def now_iso(t: float) -> str:
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


# --- State ---------------------------------------------------------------------------
class State:
    def __init__(self, root: Path) -> None:
        self.root = root

    def read(self, name: str) -> dict[str, Any]:
        try:
            data = json.loads((self.root / f"{name}.json").read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def write(self, name: str, data: dict[str, Any] | None) -> None:
        path = self.root / f"{name}.json"
        if data is None:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            return
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)  # atomic: a concurrent reader sees the old or the new file, never half

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        import fcntl  # Linux only; imported here so the module also imports on other systems for tests

        with open(self.root / "lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


# --- Topics and Home Assistant discovery ------------------------------------------------------
def base_topic(conf: dict[str, str]) -> str:
    return f"{conf['TOPIC_PREFIX'].strip('/')}/{conf['DEVICE_ID']}"


def discovery_messages(conf: dict[str, str]) -> list[Message]:
    base = base_topic(conf)
    dev_id = conf["DEVICE_ID"]
    device = {
        "identifiers": [f"batocera_{dev_id}"],
        "name": conf["DEVICE_NAME"],
        "manufacturer": "Batocera",
        "model": "EmulationStation play tracker",
    }
    common = {"availability_topic": f"{base}/status", "device": device}
    np = f"{base}/now_playing"
    last = f"{base}/last_session"
    entities: list[tuple[str, str, dict[str, Any]]] = [
        (
            "sensor",
            "now_playing",
            {
                "name": "Now playing",
                "icon": "mdi:gamepad-variant",
                "state_topic": np,
                "value_template": "{{ value_json.game if value_json.state == 'playing' else 'Idle' }}",
                "json_attributes_topic": np,
            },
        ),
        (
            "binary_sensor",
            "playing",
            {
                "name": "Playing",
                "icon": "mdi:controller",
                "state_topic": np,
                "value_template": "{{ 'ON' if value_json.state == 'playing' else 'OFF' }}",
            },
        ),
        (
            "sensor",
            "now_playing_system",
            {
                "name": "Now playing system",
                "icon": "mdi:nintendo-game-boy",
                "state_topic": np,
                "value_template": "{{ value_json.system if value_json.state == 'playing' else 'None' }}",
            },
        ),
        (
            "sensor",
            "last_session",
            {
                "name": "Last played",
                "icon": "mdi:history",
                "state_topic": last,
                "value_template": "{{ value_json.game }}",
                "json_attributes_topic": last,
            },
        ),
        (
            "sensor",
            "last_session_duration",
            {
                "name": "Last session length",
                "device_class": "duration",
                "unit_of_measurement": "min",
                "state_topic": last,
                "value_template": "{{ (value_json.duration_s / 60) | round(1) }}",
            },
        ),
        (
            "event",
            "game_session",
            {
                "name": "Game session",
                "icon": "mdi:timer-outline",
                "state_topic": f"{base}/session",
                "event_types": ["session_end"],
            },
        ),
    ]
    out: list[Message] = []
    for component, object_id, cfg in entities:
        payload = {**cfg, **common, "unique_id": f"batocera_{dev_id}_{object_id}"}
        topic = f"{conf['DISCOVERY_PREFIX']}/{component}/{dev_id}/{object_id}/config"
        out.append((topic, json.dumps(payload), True))
    return out


# --- The tracker ---------------------------------------------------------------------------
class Tracker:
    """Turns EmulationStation events into MQTT messages. Pure apart from `state`, so tests drive it directly."""

    def __init__(self, conf: dict[str, str], state: State, clock: Callable[[], float] = time.time) -> None:
        self.conf = conf
        self.state = state
        self.clock = clock
        self.base = base_topic(conf)

    def _idle(self) -> Message:
        return (f"{self.base}/now_playing", json.dumps({"state": "idle"}), True)

    def _online(self, online: bool) -> Message:
        return (f"{self.base}/status", "online" if online else "offline", True)

    def handle(self, event: str, args: list[str]) -> list[Message]:
        now = self.clock()
        out: list[Message] = []
        # Discovery once per boot (/tmp is cleared on reboot); entities stay defined in HA via retain.
        marker = self.state.root / "announced"
        announced = False
        if not marker.exists() and event not in ("game-selected", "system-selected"):
            out += discovery_messages(self.conf)
            out.append(self._online(True))
            self.state.root.mkdir(parents=True, exist_ok=True)
            marker.touch()
            announced = True

        if event in ("start", "wake"):
            out += ([] if announced else [self._online(True)]) + [self._idle()]
        elif event in ("quit", "shutdown", "reboot", "sleep"):
            out += self._end_session(now)
            out.append(self._online(False))
            if event != "sleep":
                with contextlib.suppress(FileNotFoundError):
                    marker.unlink()
        elif event == "screensaver-start":
            ss = self.state.read("screensaver")
            self.state.write("screensaver", {**ss, "active": True, "since": now})
        elif event == "screensaver-stop":
            ss = self.state.read("screensaver")
            self.state.write("screensaver", {**ss, "active": False, "stopped_at": now})
        elif event == "game-selected":
            # Only interesting while the screensaver shows games: remember which one is on screen.
            ss = self.state.read("screensaver")
            if ss.get("active") and len(args) >= 2:
                self.state.write("screensaver", {**ss, "game": unescape(args[1]), "game_at": now})
        elif event == "game-start":
            out += self._start_session(args, now)
        elif event == "game-end":
            out += self._end_session(now)
        return out

    def _launched_from_screensaver(self, rom: str, now: float) -> bool:
        ss = self.state.read("screensaver")
        recent = bool(ss.get("active")) or (now - float(ss.get("stopped_at", 0) or 0)) <= SCREENSAVER_GRACE_S
        return recent and ss.get("game") == rom

    def _start_session(self, args: list[str], now: float) -> list[Message]:
        out: list[Message] = []
        if self.state.read("session"):  # a game-end went missing (ES crash); close the old one first
            out += self._end_session(now)
        if not args:
            return out
        rom = unescape(args[0])
        name = args[2] if len(args) >= 3 and args[2] else (args[1] if len(args) >= 2 else Path(rom).stem)
        source = "screensaver" if self._launched_from_screensaver(rom, now) else "frontend"
        ignored = source == "screensaver" and yes(self.conf["IGNORE_SCREENSAVER_LAUNCHES"])
        session = {
            "game": name,
            "system": system_of(rom),
            "rom": rom,
            "start": now,
            "source": source,
            "ignored": ignored,
        }
        self.state.write("session", session)
        if ignored:
            return out
        payload = {k: session[k] for k in ("game", "system", "rom", "source")}
        payload |= {"state": "playing", "started": now_iso(now)}
        out.append((f"{self.base}/now_playing", json.dumps(payload), True))
        return out

    def _end_session(self, now: float) -> list[Message]:
        session = self.state.read("session")
        if not session:
            return []
        self.state.write("session", None)
        if session.get("ignored"):
            return []
        out = [self._idle()]
        duration = int(now - float(session.get("start", now)))
        try:
            min_s = int(self.conf["MIN_SESSION_SECONDS"])
        except ValueError:
            min_s = 60
        if duration < min_s:
            return out
        record = {
            "game": session.get("game"),
            "system": session.get("system"),
            "rom": session.get("rom"),
            "source": session.get("source"),
            "started": now_iso(float(session["start"])),
            "ended": now_iso(now),
            "duration_s": duration,
        }
        out.append((f"{self.base}/last_session", json.dumps(record), True))
        out.append((f"{self.base}/session", json.dumps({"event_type": "session_end", **record}), False))
        return out


# --- A minimal MQTT 3.1.1 publisher (QoS 1) ---------------------------------------------------
class MQTTError(Exception):
    pass


def _str(s: str) -> bytes:
    b = s.encode()
    return struct.pack("!H", len(b)) + b


def _packet(header: int, body: bytes) -> bytes:
    n = len(body)
    length = bytearray()
    while True:
        byte, n = n % 128, n // 128
        length.append(byte | (0x80 if n else 0))
        if not n:
            break
    return bytes([header]) + bytes(length) + body


def connect_packet(client_id: str, user: str, password: str, keepalive: int = 30) -> bytes:
    flags = 0x02  # clean session
    payload = _str(client_id)
    if user:
        flags |= 0x80
        payload += _str(user)
        if password:
            flags |= 0x40
            payload += _str(password)
    return _packet(0x10, _str("MQTT") + bytes([4, flags]) + struct.pack("!H", keepalive) + payload)


def publish_packet(topic: str, payload: str, retain: bool, packet_id: int) -> bytes:
    return _packet(0x32 | (0x01 if retain else 0), _str(topic) + struct.pack("!H", packet_id) + payload.encode())


def _read_packet(sock: socket.socket) -> tuple[int, bytes]:
    def recv(n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise MQTTError("broker closed the connection")
            buf += chunk
        return buf

    header = recv(1)[0]
    mult, length = 1, 0
    while True:
        b = recv(1)[0]
        length += (b & 0x7F) * mult
        if not b & 0x80:
            break
        mult *= 128
    return header, recv(length) if length else b""


CONNACK_ERRORS = {
    1: "bad protocol version",
    2: "client id rejected",
    3: "broker unavailable",
    4: "bad username or password",
    5: "not authorized",
}


def publish(conf: dict[str, str], messages: list[Message], timeout: float = 5.0) -> None:
    if not messages:
        return
    if not conf["MQTT_HOST"]:
        raise MQTTError(f"MQTT_HOST is not set in {CONF_PATH}")
    with socket.create_connection((conf["MQTT_HOST"], int(conf["MQTT_PORT"])), timeout=timeout) as sock:
        sock.settimeout(timeout)
        client_id = f"playtracker-{conf['DEVICE_ID']}-{os.getpid()}"[:23]
        sock.sendall(connect_packet(client_id, conf["MQTT_USER"], conf["MQTT_PASSWORD"]))
        header, body = _read_packet(sock)
        if header >> 4 != 2 or len(body) < 2:
            raise MQTTError("no CONNACK from the broker")
        if body[1] != 0:
            raise MQTTError(f"broker refused the connection: {CONNACK_ERRORS.get(body[1], body[1])}")
        for i, (topic, payload, retain) in enumerate(messages, start=1):
            sock.sendall(publish_packet(topic, payload, retain, i))
            header, body = _read_packet(sock)
            if header >> 4 != 4 or struct.unpack("!H", body[:2])[0] != i:
                raise MQTTError("broker didn't acknowledge a message")
        sock.sendall(b"\xe0\x00")  # DISCONNECT


# --- Outbox: messages that couldn't be sent wait for the next event ----------------------------
def deliver(conf: dict[str, str], state: State, messages: list[Message], log: Callable[[str], None]) -> None:
    pending = [tuple(m) for m in state.read("outbox").get("messages", [])]
    batch = [(str(t), str(p), bool(r)) for t, p, r in pending] + messages
    if not batch:
        return
    try:
        publish(conf, batch)
        if pending:
            state.write("outbox", None)
            log(f"flushed {len(pending)} queued message(s)")
    except (OSError, MQTTError) as exc:
        # Keep the newest retained message per topic, plus every session event.
        keep: dict[str, Message] = {}
        events: list[Message] = []
        for m in batch:
            if m[2]:
                keep[m[0]] = m
            else:
                events.append(m)
        queued = (list(keep.values()) + events)[-OUTBOX_MAX:]
        state.write("outbox", {"messages": queued})
        log(f"MQTT publish failed ({exc}); {len(queued)} message(s) queued for the next event")


def _logger(state: State) -> Callable[[str], None]:
    def log(msg: str) -> None:
        path = state.root / "playtracker.log"
        with contextlib.suppress(OSError):
            if path.exists() and path.stat().st_size > 256_000:
                path.unlink()
            with open(path, "a") as fh:
                fh.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")

    return log


HANDLED = {
    "start",
    "wake",
    "quit",
    "shutdown",
    "reboot",
    "sleep",
    "screensaver-start",
    "screensaver-stop",
    "game-selected",
    "game-start",
    "game-end",
}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in HANDLED:
        return 0  # system-selected, config-changed, ...: nothing to do, exit fast
    event, args = argv[1], argv[2:]
    state = State(STATE_DIR)
    log = _logger(state)
    # game-selected fires on every scroll. Skip the lock unless the screensaver is running.
    if event == "game-selected" and not state.read("screensaver").get("active"):
        return 0
    conf = load_conf()
    with state.locked():
        messages = Tracker(conf, state).handle(event, args)
        if event != "game-selected":
            log(f"{event} {' '.join(args)[:120]} -> {len(messages)} message(s)")
        deliver(conf, state, messages, log)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as exc:  # never let a tracking problem bother EmulationStation
        with contextlib.suppress(Exception):
            _logger(State(STATE_DIR))(f"error: {exc!r}")
        sys.exit(0)
