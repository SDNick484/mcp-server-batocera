"""Shared fixtures: a fake EmulationStation (httpx.MockTransport) and a fake RetroArch (real UDP socket)."""

from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest

from batocera_mcp import server
from batocera_mcp.config import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def gid(path: str) -> str:
    """EmulationStation's game id: md5 of the ROM path."""
    return hashlib.md5(path.encode()).hexdigest()


def game(system: str, rom: str, name: str, **meta: str) -> dict:
    path = f"/userdata/roms/{system}/{rom}"
    return {"id": gid(path), "path": path, "name": name, "systemName": system, **meta}


GAMES = {
    "snes": [
        game(
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
            desc="Mario's first SNES adventure.",
        ),
        game(
            "snes",
            "Super Metroid (Japan, USA).sfc",
            "Super Metroid",
            playcount="2",
            gametime="5400",
            lastplayed="20261001T210000",
            rating="0.95",
        ),
        game("snes", "Street Fighter II Turbo (USA).sfc", "Street Fighter II Turbo"),
    ],
    "mame": [
        game("mame", "pacman.zip", "Pac-Man (Midway)", playcount="12", gametime="3600", lastplayed="20261009T220000"),
        game("mame", "galaga.zip", "Galaga", playcount="3", gametime="900"),
    ],
}

SYSTEMS = [
    {
        "name": "snes",
        "fullname": "Super Nintendo",
        "manufacturer": "Nintendo",
        "visible": "true",
        "collection": "false",
        "gamesystem": "true",
        "groupsystem": "false",
        "totalGames": 3,
        "playedGames": 2,
        "favoriteGames": 1,
        "mostPlayedGame": "Super Mario World",
    },
    {
        "name": "mame",
        "fullname": "Mame",
        "manufacturer": "Arcade",
        "visible": "true",
        "collection": "false",
        "gamesystem": "true",
        "groupsystem": "false",
        "totalGames": 2,
        "playedGames": 2,
        "favoriteGames": 0,
        "mostPlayedGame": "Pac-Man (Midway)",
    },
    {
        "name": "favorites",
        "fullname": "Favorites",
        "visible": "true",
        "collection": "true",
        "gamesystem": "true",
        "groupsystem": "false",
        "totalGames": 1,
    },
    {
        "name": "n64",
        "fullname": "Nintendo 64",
        "visible": "true",
        "collection": "false",
        "gamesystem": "true",
        "groupsystem": "false",
        "totalGames": 0,
    },
]


class FakeES:
    """Mirrors HttpServerThread.cpp: 201 for no running game, /launch answers 200 even for unknown paths."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str]] = []  # method, path, body
        self.running: dict | None = None
        self.forbidden = False
        self.launch_works = True
        self.kill_works = True
        self.error: Exception | None = None
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.error is not None:
            raise self.error
        path = request.url.path
        body = request.content.decode()
        self.requests.append((request.method, path, body))
        if self.forbidden:
            return httpx.Response(403, text="403 - Forbidden")
        if path == "/caps":
            return httpx.Response(200, json={"Version": "43.1", "SortName": False})
        if path == "/runningGame":
            if self.running is None:
                return httpx.Response(201, json={"msg": "NO GAME RUNNING"})
            return httpx.Response(200, json=self.running)
        if path == "/isIdle":
            return httpx.Response(200 if self.running is None else 201, text="[ true ]")
        if path == "/systems":
            return httpx.Response(200, json=SYSTEMS)
        if path.startswith("/systems/") and path.endswith("/games"):
            system = path.split("/")[2]
            if system not in GAMES and system not in ("favorites", "n64"):
                return httpx.Response(404, text="404 system not found")
            return httpx.Response(200, text=json.dumps(GAMES.get(system, [])))
        if path == "/launch":
            if self.launch_works:
                for games in GAMES.values():
                    for g in games:
                        if g["path"] == body:
                            self.running = g
            return httpx.Response(200)
        if path == "/emukill":
            if self.kill_works:
                self.running = None
            return httpx.Response(200)
        if path in ("/notify", "/messagebox", "/reloadgames"):
            return httpx.Response(200)
        return httpx.Response(404, text="404 not found")

    def calls(self, path: str) -> list[tuple[str, str, str]]:
        return [r for r in self.requests if r[1] == path]


class FakeRetroArch(asyncio.DatagramProtocol):
    """A UDP listener that answers GET_STATUS like RetroArch and records every command."""

    def __init__(self) -> None:
        self.received: list[str] = []
        self.state = "PLAYING"  # or PAUSED, CONTENTLESS, or None for "doesn't answer"
        self.game = "snes,Super Mario World (USA),crc32=b19ed489"
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        cmd = data.decode()
        self.received.append(cmd)
        if cmd == "PAUSE_TOGGLE" and self.state in ("PLAYING", "PAUSED"):
            self.state = "PAUSED" if self.state == "PLAYING" else "PLAYING"
        if cmd == "GET_STATUS" and self.state is not None and self.transport:
            reply = "GET_STATUS CONTENTLESS" if self.state == "CONTENTLESS" else f"GET_STATUS {self.state} {self.game}"
            self.transport.sendto(reply.encode(), addr)

    @property
    def commands(self) -> list[str]:
        return [c for c in self.received if c != "GET_STATUS"]


@pytest.fixture
def fake_es() -> FakeES:
    return FakeES()


@pytest.fixture
async def fake_ra():
    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(FakeRetroArch, local_addr=("127.0.0.1", 0))
    proto.port = transport.get_extra_info("sockname")[1]
    yield proto
    transport.close()


@pytest.fixture
def settings(fake_ra) -> Settings:
    return Settings(url="http://127.0.0.1:1234", timeout_s=2.0, retroarch_port=fake_ra.port)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BATOCERA_CONFIG_DIR", str(tmp_path))
    for var in ("BATOCERA_URL", "BATOCERA_TIMEOUT", "BATOCERA_RETROARCH_PORT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(server, "LAUNCH_TIMEOUT_S", 0.3)
    monkeypatch.setattr(server, "QUIT_TIMEOUT_S", 0.3)
    monkeypatch.setattr(server, "POLL_INTERVAL_S", 0.01)
