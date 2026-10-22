"""EmulationStationClient: one HTTP client for Batocera's EmulationStation web API.

Facts from the EmulationStation source (es-app/src/services/HttpServerThread.cpp
and HttpApi.cpp) that this module depends on:

* Every request from a host other than 127.0.0.1 gets ``403 - Forbidden``
  unless *Enable public web access* is on.
* ``GET /runningGame`` answers **201** with ``{"msg":"NO GAME RUNNING"}`` when
  idle, 200 with the game's JSON otherwise.
* ``POST /launch`` takes a ROM path as the raw body and answers 200 whether or
  not it found the game, so a launch is only confirmed by ``/runningGame``.
* Game ids are the md5 of the ROM path. Metadata values are all strings:
  ``"playcount": "3"``, ``"gametime": "5400"`` (seconds),
  ``"lastplayed": "20261009T201500"``, ``"favorite": "true"``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError
from typing_extensions import TypedDict

from .config import NON_GAME_SYSTEMS, Settings

log = logging.getLogger(__name__)

GAMES_CACHE_TTL_S = 300.0


class BatoceraError(ToolError):
    """An error the model should see verbatim.

    Subclassing the SDK's ToolError matters: only ToolError messages reach the
    model; other exceptions surface only as "Error executing tool <name>".
    """


# --- Shapes returned to the model -----------------------------------------
class Game(TypedDict):
    id: str
    system: str
    name: str
    genre: str | None
    players: str | None
    release_date: str | None
    developer: str | None
    favorite: bool
    play_count: int
    play_time_s: int
    last_played: str | None


class GameDetails(Game):
    publisher: str | None
    rating: float | None
    description: str | None


class System(TypedDict):
    name: str
    full_name: str
    manufacturer: str | None
    games: int
    played_games: int
    favorites: int
    most_played: str | None


# ASSUMPTION B-ES-METADATA: metadata values are strings; times look like 20261009T201500.
def es_time(value: Any) -> str | None:
    """'20261009T201500' -> '2026-10-09T20:15:00'; dates without a time -> '2026-10-09'."""
    if not isinstance(value, str) or len(value) < 8 or not value[:8].isdigit():
        return None
    date = f"{value[0:4]}-{value[4:6]}-{value[6:8]}"
    if value[:8] in ("19700101", "00000000"):
        return None
    t = value[9:15]
    if len(value) >= 15 and value[8] == "T" and t.isdigit() and t != "000000":
        return f"{date}T{t[0:2]}:{t[2:4]}:{t[4:6]}"
    return date


def _int(value: Any) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _opt(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def to_game(raw: dict[str, Any]) -> Game:
    return Game(
        id=str(raw.get("id", "")),
        system=str(raw.get("systemName", "")),
        name=str(raw.get("name", "")),
        genre=_opt(raw.get("genre")),
        players=_opt(raw.get("players")),
        release_date=es_time(raw.get("releasedate")),
        developer=_opt(raw.get("developer")),
        favorite=raw.get("favorite") == "true",
        play_count=_int(raw.get("playcount")),
        play_time_s=_int(raw.get("gametime")),
        last_played=es_time(raw.get("lastplayed")),
    )


def to_details(raw: dict[str, Any]) -> GameDetails:
    g = to_game(raw)
    rating: float | None = None
    try:
        rating = round(float(raw["rating"]), 2) if raw.get("rating") else None
    except (TypeError, ValueError):
        rating = None
    desc = _opt(raw.get("desc"))
    return GameDetails(
        **g,
        publisher=_opt(raw.get("publisher")),
        rating=rating,
        description=desc[:2000] if desc else None,
    )


def to_system(raw: dict[str, Any]) -> System:
    return System(
        name=str(raw.get("name", "")),
        full_name=str(raw.get("fullname", "")),
        manufacturer=_opt(raw.get("manufacturer")),
        games=_int(raw.get("totalGames")),
        played_games=_int(raw.get("playedGames")),
        favorites=_int(raw.get("favoriteGames")),
        most_played=_opt(raw.get("mostPlayedGame")),
    )


# ASSUMPTION B-ES-SYSTEMS: collections and groups are flagged with the strings "true"/"false".
def is_game_system(raw: dict[str, Any]) -> bool:
    """Real ROM systems only: not collections (favorites, last played), groups, or ones without games."""
    return (
        raw.get("gamesystem") == "true"
        and raw.get("name") not in NON_GAME_SYSTEMS
        and raw.get("collection") != "true"
        and raw.get("groupsystem") != "true"
        and raw.get("visible", "true") == "true"
    )


# --- The client ------------------------------------------------------------
class EmulationStationClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.url,
            timeout=httpx.Timeout(settings.timeout_s, connect=min(settings.timeout_s, 5.0)),
            transport=transport,
            follow_redirects=False,
        )
        # system -> (fetched_at, {game id: raw game JSON})
        self._games: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}
        self._systems: tuple[float, list[dict[str, Any]]] | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    def invalidate(self) -> None:
        self._games.clear()
        self._systems = None

    async def request(self, method: str, path: str, body: str | None = None) -> httpx.Response:
        url = self.settings.url
        log.info("%s %s", method, path)
        try:
            resp = await self._http.request(
                method,
                path,
                content=body.encode() if body is not None else None,
                headers={"Content-Type": "text/plain; charset=utf-8"} if body is not None else None,
            )
        except httpx.ConnectError as exc:
            raise BatoceraError(_connect_advice(url, exc)) from exc
        except httpx.TimeoutException as exc:
            raise BatoceraError(
                f"EmulationStation at {url} didn't answer within {self.settings.timeout_s:g}s. "
                "It may be busy (scraping, updating) or the cabinet may be asleep."
            ) from exc
        except httpx.HTTPError as exc:
            raise BatoceraError(f"Talking to EmulationStation at {url} failed: {exc}") from exc
        if resp.status_code == 403:  # ASSUMPTION B-ES-PUBLIC-ACCESS
            raise BatoceraError(
                "EmulationStation refused the request (403). Turn on public web access: in Batocera's "
                "Main Menu > System Settings > Developer, enable 'Enable public web access', then reboot."
            )
        if resp.status_code >= 400 and resp.status_code != 404:
            raise BatoceraError(f"EmulationStation answered {resp.status_code}: {resp.text.strip()[:200]}")
        return resp

    async def get_json(self, path: str) -> Any:
        resp = await self.request("GET", path)
        if resp.status_code == 404:
            raise BatoceraError(f"EmulationStation has no {path}: {resp.text.strip()[:100]}")
        try:
            return json.loads(resp.text)
        except json.JSONDecodeError as exc:
            raise BatoceraError(f"EmulationStation sent something that isn't JSON: {resp.text[:200]!r}") from exc

    # --- Reads -------------------------------------------------------------
    async def version(self) -> str | None:
        caps = await self.get_json("/caps")
        return _opt(caps.get("Version")) if isinstance(caps, dict) else None

    async def running_game(self) -> dict[str, Any] | None:
        # ASSUMPTION B-ES-DURING-GAME: this keeps answering while the emulator runs.
        resp = await self.request("GET", "/runningGame")
        if resp.status_code == 201:  # ASSUMPTION B-ES-RUNNINGGAME: {"msg":"NO GAME RUNNING"}
            return None
        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) and data.get("id") else None

    async def is_idle(self) -> bool:
        resp = await self.request("GET", "/isIdle")
        return resp.status_code == 200

    async def systems(self) -> list[dict[str, Any]]:
        if self._systems and time.monotonic() - self._systems[0] < GAMES_CACHE_TTL_S:
            return self._systems[1]
        data = await self.get_json("/systems")
        systems = [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []
        self._systems = (time.monotonic(), systems)
        return systems

    async def game_systems(self) -> list[dict[str, Any]]:
        return [s for s in await self.systems() if is_game_system(s) and _int(s.get("totalGames")) > 0]

    async def games(self, system: str, fresh: bool = False) -> dict[str, dict[str, Any]]:
        """All games in one system, by id. Cached: MAME's list can be thousands of entries."""
        async with self._lock:
            cached = self._games.get(system)
            if cached and not fresh and time.monotonic() - cached[0] < GAMES_CACHE_TTL_S:
                return cached[1]
            resp = await self.request("GET", f"/systems/{system}/games")
            if resp.status_code == 404:
                raise BatoceraError(f"No system named {system!r}. Use list_systems to see the names.")
            try:
                data = json.loads(resp.text)
            except json.JSONDecodeError as exc:
                raise BatoceraError(f"EmulationStation sent a game list that isn't JSON for {system}") from exc
            games = {g["id"]: g for g in data if isinstance(g, dict) and isinstance(g.get("id"), str)}
            self._games[system] = (time.monotonic(), games)
            return games

    async def find_game(self, system: str, gid: str) -> dict[str, Any]:
        # ASSUMPTION B-ES-GAME-ID: ids are md5(path), so they survive a reload; a miss refetches once.
        games = await self.games(system)
        if gid not in games:
            games = await self.games(system, fresh=True)
        if gid not in games:
            raise BatoceraError(f"No game with id {gid} in {system}. Use find_games to look it up again.")
        return games[gid]

    # --- Actions -----------------------------------------------------------
    async def launch(self, path: str) -> None:
        await self.request("POST", "/launch", body=path)

    async def emukill(self) -> None:
        await self.request("GET", "/emukill")

    async def notify(self, text: str, popup: bool) -> None:
        await self.request("POST", "/messagebox" if popup else "/notify", body=text)

    async def reload_games(self) -> None:
        await self.request("GET", "/reloadgames")
        self.invalidate()


def _connect_advice(url: str, exc: Exception) -> str:
    host = httpx.URL(url).host
    msg = str(exc).lower()
    if "name or service not known" in msg or "nodename nor servname" in msg or "getaddrinfo" in msg:
        return (
            f"Can't resolve {host}. mDNS (.local names) often doesn't cross VLANs or work under WSL2; "
            "set BATOCERA_URL to the cabinet's IP address, e.g. http://192.168.1.20:1234."
        )
    return (
        f"EmulationStation isn't answering at {url}. Is the cabinet on? If it is, public web access is "
        "probably off: EmulationStation only listens on 127.0.0.1 until 'Enable public web access' "
        "(System Settings > Developer) is turned on and the cabinet rebooted."
    )
