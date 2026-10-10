"""A wire-level fake Batocera cabinet: EmulationStation's web API over real HTTP, RetroArch over real UDP.

Used by the tests and by ``mcp-server-batocera simulate``. It reproduces what the
server depends on, each behavior tagged with the assumption it stands for
(see assumptions.py), so a hardware result that contradicts one points here too:

  - 403 for every request while public web access is off (B-ES-PUBLIC-ACCESS)
  - ``/runningGame`` answers 201 when nothing runs (B-ES-RUNNINGGAME)
  - ``/launch`` answers 200 for any body; the game starts a moment later, on
    EmulationStation's UI thread, and an unknown path starts nothing (B-ES-LAUNCH)
  - ``/emukill`` stops the game a moment later (B-ES-EMUKILL)
  - the API keeps answering while a game runs (B-ES-DURING-GAME)
  - game ids are md5 of the path; metadata values are strings (B-ES-GAME-ID, B-ES-METADATA)
  - RetroArch answers GET_STATUS only for libretro systems (B-RA-STATUS)

Fault switches (``forbidden``, ``busy``, ``slow_s``, ``drop``, ``launch_fails``,
``kill_fails``, ``ra_answers``) let tests and you try the failure paths.
Standard library only, with threads, so it runs under any event loop or none.
"""

from __future__ import annotations

import copy
import hashlib
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# Systems whose default emulator on Batocera is a RetroArch core. GameCube runs in
# Dolphin (standalone), so RetroArch tools don't apply there.
LIBRETRO_SYSTEMS = {"snes", "nes", "mame", "megadrive", "fbneo", "psx"}


def game_id(path: str) -> str:
    """EmulationStation's id for a game: md5 of its path (B-ES-GAME-ID)."""
    return hashlib.md5(path.encode()).hexdigest()


def _game(system: str, rom: str, name: str, **meta: str) -> dict[str, Any]:
    path = f"/userdata/roms/{system}/{rom}"
    return {"id": game_id(path), "path": path, "name": name, "systemName": system, **meta}


def default_library() -> dict[str, list[dict[str, Any]]]:
    return {
        "snes": [
            _game(
                "snes",
                "Super Mario World (USA).sfc",
                "Super Mario World",
                playcount="7",
                gametime="12600",
                lastplayed="20261008T193000",
                favorite="true",
                genre="Platform",
                players="1-2",
                releasedate="19901121T000000",
                developer="Nintendo",
                publisher="Nintendo",
                desc="Mario's first SNES adventure.",
                rating="0.9",
            ),
            _game(
                "snes",
                "Super Metroid (Japan, USA).sfc",
                "Super Metroid",
                playcount="2",
                gametime="5400",
                lastplayed="20261001T210000",
                rating="0.95",
                genre="Action",
            ),
            _game(
                "snes", "Street Fighter II Turbo (USA).sfc", "Street Fighter II Turbo", genre="Fighting", players="1-2"
            ),
        ],
        "mame": [
            _game(
                "mame",
                "pacman.zip",
                "Pac-Man (Midway)",
                playcount="12",
                gametime="3600",
                lastplayed="20261009T220000",
                players="1-2",
            ),
            _game("mame", "galaga.zip", "Galaga", playcount="3", gametime="900", players="1-2"),
        ],
        "gamecube": [
            _game("gamecube", "Super Smash Bros. Melee (USA).rvz", "Super Smash Bros. Melee", players="1-4"),
        ],
    }


SYSTEM_INFO = {
    "snes": ("Super Nintendo", "Nintendo"),
    "mame": ("Mame", "Arcade"),
    "gamecube": ("GameCube", "Nintendo"),
}


class _QuietHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # the drop fault closes sockets mid-request on purpose; socketserver would print a traceback


class FakeCabinet:
    def __init__(self, host: str = "127.0.0.1", es_port: int = 0, ra_port: int = 0) -> None:
        self.library = default_library()
        self.version = "43.1-sim"
        self.running: dict[str, Any] | None = None
        self.ra_state = "PLAYING"  # or "PAUSED", "CONTENTLESS"
        # Faults
        self.forbidden = False  # public web access off
        self.busy = False  # scraping/updating: /isIdle false with no game
        self.slow_s = 0.0  # delay before every HTTP answer
        self.drop = False  # close HTTP connections without answering
        self.launch_fails = False  # /launch accepted, but nothing starts
        self.kill_fails = False
        self.ra_answers = True  # False: RetroArch network commands off
        self.launch_delay_s = 0.4
        self.kill_delay_s = 0.3
        # What happened
        self.requests: list[tuple[str, str, str]] = []
        self.ra_received: list[str] = []
        self.notifications: list[str] = []
        self.popups: list[str] = []
        self.reloads = 0
        self._lock = threading.RLock()
        self._http = _QuietHTTPServer((host, es_port), self._handler())
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.bind((host, ra_port))
        self._threads: list[threading.Thread] = []
        self.host = host

    # --- lifecycle ---------------------------------------------------------------
    @property
    def url(self) -> str:
        return f"http://{self.host}:{self._http.server_address[1]}"

    @property
    def es_port(self) -> int:
        return int(self._http.server_address[1])

    @property
    def ra_port(self) -> int:
        return int(self._udp.getsockname()[1])

    def start(self) -> FakeCabinet:
        # serve_forever polls for shutdown every poll_interval (default 0.5 s); stop() waits for it.
        http = threading.Thread(target=self._http.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        for t in (http, threading.Thread(target=self._serve_udp, daemon=True)):
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        self._http.shutdown()
        self._http.server_close()
        self._udp.close()

    def __enter__(self) -> FakeCabinet:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # --- helpers for tests -----------------------------------------------------------
    def game(self, system: str, index: int = 0) -> dict[str, Any]:
        return self.library[system][index]

    def play(self, system: str, index: int = 0) -> None:
        with self._lock:
            self.running = copy.deepcopy(self.library[system][index])
            self.ra_state = "PLAYING"

    def calls(self, path: str) -> list[tuple[str, str, str]]:
        return [r for r in self.requests if r[1] == path]

    @property
    def ra_commands(self) -> list[str]:
        return [c for c in self.ra_received if c != "GET_STATUS"]

    def _later(self, delay: float, fn: Any) -> None:
        t = threading.Timer(delay, fn)
        t.daemon = True
        t.start()

    # --- EmulationStation -------------------------------------------------------------
    def _systems(self) -> list[dict[str, Any]]:
        out = []
        for name, games in self.library.items():
            full, maker = SYSTEM_INFO.get(name, (name, ""))
            played = [g for g in games if int(g.get("playcount", "0") or 0) > 0]
            top = max(played, key=lambda g: int(g.get("playcount", "0")), default=None)
            out.append({
                "name": name, "fullname": full, "manufacturer": maker, "theme": name, "visible": "true",
                "collection": "false", "gamesystem": "true", "groupsystem": "false", "totalGames": len(games),
                "visibleGames": len(games), "favoriteGames": sum(g.get("favorite") == "true" for g in games),
                "playedGames": len(played), "hiddenGames": 0, "mostPlayedGame": top["name"] if top else "",
            })  # fmt: skip
        out.append({"name": "favorites", "fullname": "Favorites", "visible": "true", "collection": "true",
                    "gamesystem": "true", "groupsystem": "false", "totalGames": 1})  # fmt: skip
        out.append({"name": "n64", "fullname": "Nintendo 64", "visible": "true", "collection": "false",
                    "gamesystem": "true", "groupsystem": "false", "totalGames": 0})  # fmt: skip
        return out

    def _launch(self, path: str) -> None:
        if self.launch_fails:
            return
        for games in self.library.values():
            for g in games:
                if g["path"] == path:

                    def start(g: dict[str, Any] = g) -> None:
                        with self._lock:
                            self.running = copy.deepcopy(g)
                            self.ra_state = "PLAYING"

                    self._later(self.launch_delay_s, start)
                    return

    def _kill(self) -> None:
        if self.kill_fails:
            return

        def stop() -> None:
            with self._lock:
                self.running = None

        self._later(self.kill_delay_s, stop)

    def handle(self, method: str, path: str, body: str) -> tuple[int, str, str]:
        """(status, content type, body) for one request, as EmulationStation answers it."""
        with self._lock:
            self.requests.append((method, path, body))
        if self.forbidden:
            return 403, "text/html", "403 - Forbidden"
        js = "application/json"
        if method == "GET" and path == "/caps":
            return 200, js, json.dumps({"Version": self.version, "SortName": False})
        if method == "GET" and path == "/runningGame":
            if self.running is None:
                return 201, js, '{"msg":"NO GAME RUNNING"}'
            return 200, js, json.dumps(self.running)
        if method == "GET" and path == "/isIdle":
            idle = self.running is None and not self.busy
            return (200, js, "[ true ]") if idle else (201, js, "[ false ]")
        if method == "GET" and path == "/systems":
            return 200, js, json.dumps(self._systems(), indent=1)
        if method == "GET" and path.startswith("/systems/") and path.endswith("/games"):
            name = path.split("/")[2]
            if name in self.library:
                return 200, js, json.dumps(self.library[name], indent=1)
            if name in ("favorites", "n64"):
                return 200, js, "[]"
            return 404, "text/html", "404 system not found"
        if method == "POST" and path == "/launch":
            if not body:
                return 400, "text/html", "400 bad request - body is missing"
            self._launch(body)
            return 200, "text/plain", ""
        if method == "GET" and path == "/emukill":
            self._kill()
            return 200, "text/plain", ""
        if method == "POST" and path in ("/notify", "/messagebox"):
            if not body:
                return 400, "text/html", "400 bad request - body is missing"
            (self.notifications if path == "/notify" else self.popups).append(body)
            return 200, "text/plain", ""
        if method == "GET" and path == "/reloadgames":
            self.reloads += 1
            return 200, "text/plain", ""
        return 404, "text/html", "404 not found"

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        cabinet = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _do(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode() if length else ""
                if cabinet.slow_s:
                    time.sleep(cabinet.slow_s)
                if cabinet.drop:
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                status, ctype, text = cabinet.handle(method, self.path.split("?")[0], body)
                data = text.encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802 (http.server's naming)
                self._do("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._do("POST")

            def log_message(self, format: str, *args: Any) -> None:
                pass

        return Handler

    # --- RetroArch ------------------------------------------------------------------
    def _serve_udp(self) -> None:
        while True:
            try:
                data, addr = self._udp.recvfrom(4096)
            except OSError:
                return
            cmd = data.decode(errors="replace")
            with self._lock:
                self.ra_received.append(cmd)
                game = self.running
                on_retroarch = game is not None and game.get("systemName") in LIBRETRO_SYSTEMS
                if not (self.ra_answers and on_retroarch):
                    continue  # standalone emulator, or network commands off: nobody is listening
                if cmd == "PAUSE_TOGGLE":
                    self.ra_state = "PAUSED" if self.ra_state == "PLAYING" else "PLAYING"
                elif cmd == "RESET":
                    self.ra_state = "PLAYING"
                if cmd == "GET_STATUS" and game is not None:
                    rom = game["path"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
                    reply = f"GET_STATUS {self.ra_state} {game['systemName']},{rom},crc32=1a2b3c4d"
                    if self.ra_state == "CONTENTLESS":  # RetroArch open with no game loaded
                        reply = "GET_STATUS CONTENTLESS"
                    try:
                        self._udp.sendto(reply.encode(), addr)
                    except OSError:
                        return
