"""MCP tool definitions for a Batocera cabinet.

How a decorated function becomes a tool the model can call:
  - ``@mcp.tool()`` registers it; it then appears in the client's ``tools/list``.
  - The function's *docstring* becomes the tool's ``description``, the model's
    only documentation, so it is written for the model.
  - The *signature* becomes the ``inputSchema``: ``Literal`` becomes an enum,
    ``Field(ge=..., le=...)`` a range, defaults make arguments optional.
  - The *return type* becomes the ``outputSchema`` when it is structured.

Safety shape (the same idea as the Shield's key and app allow-lists):
  - **Games are an allow-list.** launch_game takes a system and a game *id*,
    looks the id up in EmulationStation's own game list, and launches that
    entry's path. The model never supplies a path, so it can only start games
    EmulationStation already knows about.
  - **No ES endpoint that changes the system:** ``/quit``, ``/restart``,
    ``/addgames``, ``/removegames``, metadata and media POSTs, ``/input`` and
    ``/storage/event`` are not reachable from here.
  - **RetroArch gets constants only**, plus SHOW_MSG with validated text.
  - The destructive tools (quit_game, load_state, reset_game) lose unsaved
    progress, so they carry destructive_hint=True and clients ask first.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal, TypeVar

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field
from typing_extensions import TypedDict

from . import config as cfg
from .client import BatoceraError, EmulationStationClient, Game, GameDetails, System, to_details, to_game, to_system
from .config import load_settings
from .retroarch import RetroArchClient, RetroArchStatus

log = logging.getLogger(__name__)

LAUNCH_TIMEOUT_S = 20.0
QUIT_TIMEOUT_S = 10.0
POLL_INTERVAL_S = 0.5

_es: EmulationStationClient | None = None
_ra: RetroArchClient | None = None


def es() -> EmulationStationClient:
    assert _es is not None, "server lifespan has not started"
    return _es


def ra() -> RetroArchClient:
    assert _ra is not None, "server lifespan has not started"
    return _ra


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
    global _es, _ra
    settings = load_settings()
    _es = EmulationStationClient(settings)
    _ra = RetroArchClient(settings.host, settings.retroarch_port)
    try:
        yield
    finally:
        await _es.aclose()
        _es = _ra = None


mcp = MCPServer(
    "batocera",
    instructions=(
        "Controls a Batocera retro-gaming cabinet through its EmulationStation frontend: browse systems and "
        "games, see what is playing, launch or quit a game, and show messages on screen. While a RetroArch "
        "(libretro) game runs, it can also pause, save or load state, take a screenshot, or reset. "
        "Call get_status first. To launch a game, find its id with find_games, then call launch_game. "
        "Only one game runs at a time: quit_game ends the current one and loses unsaved progress, so confirm "
        "with the user before quitting a game someone may be playing."
    ),
    lifespan=lifespan,
)

_READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
_ACT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
_SET = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
# Quitting, loading a state and resetting all throw away unsaved progress.
_LOSES_PROGRESS = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)

T = TypeVar("T")


def _check(fn: Callable[..., T], *args: object) -> T:
    try:
        return fn(*args)
    except cfg.InvalidValue as exc:
        raise BatoceraError(str(exc)) from exc


SystemArg = Annotated[
    str, Field(min_length=1, max_length=40, description="System name from list_systems, e.g. 'snes', 'mame'.")
]
GameIdArg = Annotated[
    str, Field(min_length=32, max_length=32, description="The game's id from find_games (32 hex characters).")
]


# --- Reading -----------------------------------------------------------------
class Status(TypedDict):
    url: str
    dry_run: bool
    reachable: bool
    error: str | None
    es_version: str | None
    running_game: Game | None
    busy: bool | None
    retroarch: RetroArchStatus | None


@mcp.tool(title="Get Batocera status", annotations=_READ)
async def get_status() -> Status:
    """Report whether the cabinet's EmulationStation is reachable, which game is running (if any), and whether
    EmulationStation is busy (scraping, updating). While a RetroArch game runs, retroarch says whether it is
    playing or paused; it is null for standalone emulators or when RetroArch network commands are off.
    dry_run true means actions are reported, not sent.
    """
    c = es()
    try:
        version = await c.version()
        running = await c.running_game()
        idle = await c.is_idle()
    except BatoceraError as exc:
        return Status(
            url=c.settings.url,
            dry_run=c.settings.dry_run,
            reachable=False,
            error=str(exc),
            es_version=None,
            running_game=None,
            busy=None,
            retroarch=None,
        )
    return Status(
        url=c.settings.url,
        dry_run=c.settings.dry_run,
        reachable=True,
        error=None,
        es_version=version,
        running_game=to_game(running) if running else None,
        busy=(not idle) and running is None,
        retroarch=await ra().status() if running else None,
    )


class SystemList(TypedDict):
    systems: list[System]
    total_games: int


@mcp.tool(title="List game systems", annotations=_READ)
async def list_systems() -> SystemList:
    """List the game systems that have games, with game counts, how many have been played, favorites, and
    each system's most played game. The name field is what other tools take as system.
    """
    systems = [to_system(s) for s in await es().game_systems()]
    systems.sort(key=lambda s: s["full_name"].lower())
    return SystemList(systems=systems, total_games=sum(s["games"] for s in systems))


_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _match_score(name: str, query_words: list[str]) -> int | None:
    """0 exact name, 1 name starts with the query, 2 every query word starts some word of the name;
    None for no match. Case and punctuation are ignored: 'pac man' matches 'Pac-Man'."""
    words = _words(name)
    joined = " ".join(words)
    q = " ".join(query_words)
    if joined == q:
        return 0
    if joined.startswith(q):
        return 1
    if all(any(w.startswith(qw) for w in words) for qw in query_words):
        return 2
    return None


class GameList(TypedDict):
    matches: int
    games: list[Game]
    truncated: bool


@mcp.tool(title="Find games", annotations=_READ)
async def find_games(
    query: Annotated[
        str | None,
        Field(default=None, max_length=100, description="Words from the game's name, e.g. 'mario world'."),
    ] = None,
    system: Annotated[str | None, Field(default=None, max_length=40, description="Only this system.")] = None,
    sort: Annotated[
        Literal["relevance", "name", "most_played", "most_time", "recently_played", "random"],
        Field(
            description="relevance (best name match first), by name, play count, play time or last played, "
            "or random (a shuffled pick of the matches)."
        ),
    ] = "relevance",
    favorites_only: bool = False,
    genre: Annotated[
        str | None,
        Field(default=None, max_length=50, description="Filter by genre substring, e.g. 'Fighting', 'Platform'."),
    ] = None,
    players: Annotated[
        str | None,
        Field(default=None, max_length=10, description="Filter by player string or count, e.g. '2', '1-2'."),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    offset: Annotated[int, Field(ge=0, description="Skip the first N matching games for pagination.")] = 0,
) -> GameList:
    """Search the cabinet's games by name, optionally within one system, or list favorites, most played or
    recently played games. genre and players filter by case-insensitive substring; games without that
    metadata never match a filter. matches counts every match; use offset to page past the first limit.
    Each result's id and system are what launch_game and get_game take.
    """
    c = es()
    names = [_check(cfg.system_name, system)] if system else [str(s.get("name")) for s in await c.game_systems()]
    query_words = _words(query) if query else []
    if query and not query_words:
        raise BatoceraError("The query has no letters or digits to match.")
    genre_q = genre.strip().lower() if genre else None
    players_q = players.strip().lower() if players else None

    # ASSUMPTION B-ES-THREADS: EmulationStation's pool has 8+ threads, so 2 at a time leaves it room for other
    # clients (its web UI, Pixelcade); hardware showed 32 systems load in about 1 s at this rate.
    sem = asyncio.Semaphore(2)

    async def load(n: str) -> dict[str, dict[str, Any]]:
        async with sem:
            return await c.games(n)

    scored: list[tuple[int, Game]] = []
    for games in await asyncio.gather(*(load(n) for n in names)):
        for raw in games.values():
            game = to_game(raw)
            if favorites_only and not game["favorite"]:
                continue
            if genre_q and (game["genre"] is None or genre_q not in game["genre"].lower()):
                continue
            if players_q and (game["players"] is None or players_q not in game["players"].lower()):
                continue
            score = _match_score(game["name"], query_words) if query_words else 0
            if score is not None:
                scored.append((score, game))

    if sort == "most_played":
        scored = [x for x in scored if x[1]["play_count"] > 0]
        scored.sort(key=lambda x: (-x[1]["play_count"], -x[1]["play_time_s"]))
    elif sort == "most_time":
        scored = [x for x in scored if x[1]["play_time_s"] > 0]
        scored.sort(key=lambda x: -x[1]["play_time_s"])
    elif sort == "recently_played":
        scored = [x for x in scored if x[1]["last_played"]]
        scored.sort(key=lambda x: x[1]["last_played"] or "", reverse=True)
    elif sort == "name":
        scored.sort(key=lambda x: x[1]["name"].lower())
    elif sort == "random":
        random.shuffle(scored)
    else:
        scored.sort(key=lambda x: (x[0], x[1]["name"].lower()))
    games_out = [g for _, g in scored]
    return GameList(
        matches=len(games_out),
        games=games_out[offset : offset + limit],
        truncated=len(games_out) > (offset + limit),
    )


@mcp.tool(title="Get game details", annotations=_READ)
async def get_game(system: SystemArg, game_id: GameIdArg) -> GameDetails:
    """Full details for one game: description, developer, publisher, rating, players, play count and time."""
    s = _check(cfg.system_name, system)
    gid = _check(cfg.game_id, game_id)
    return to_details(await es().find_game(s, gid))


# --- Acting --------------------------------------------------------------------
# Every tool that changes something returns an ActionResult, the same shape as the
# sibling servers' (Shield, Onkyo, Harmony, Sofabaton): structured, so a client can
# read `outcome` and `sent` without parsing prose, with `detail` one sentence for the
# user. Failures aren't an outcome: they're isError results (BatoceraError).
Outcome = Literal["done", "unchanged", "dry_run"]


class ActionResult(TypedDict):
    cabinet: str  # BATOCERA_NAME, or the cabinet's address
    outcome: Outcome
    detail: str
    sent: list[str]  # what went to the cabinet (or, in a dry run, would have)
    warnings: list[str]


def _result(detail: str, sent: list[str], outcome: Outcome = "done", warnings: list[str] | None = None) -> ActionResult:
    settings = es().settings
    if outcome == "dry_run":
        detail = f"DRY RUN, nothing sent: would {detail}"
    return ActionResult(
        cabinet=settings.name or settings.host or "the cabinet",
        outcome=outcome,
        detail=detail,
        sent=sent,
        warnings=warnings or [],
    )


def _dry() -> bool:
    return es().settings.dry_run


async def _wait_for(check: Callable[[], Any], timeout: float) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        result = await check()
        if result:
            return result
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(POLL_INTERVAL_S)


@mcp.tool(title="Launch a game", annotations=_SET)
async def launch_game(system: SystemArg, game_id: GameIdArg) -> ActionResult:
    """Start a game on the cabinet (find its id with find_games). Refuses if a different game is already
    running: quit_game first, after checking with the user. Succeeds only once the game is confirmed running.
    """
    c = es()
    s = _check(cfg.system_name, system)
    gid = _check(cfg.game_id, game_id)
    raw = await c.find_game(s, gid)
    name = raw.get("name", gid)

    running = await c.running_game()
    if running:
        if running.get("id") == gid:
            return _result(f"{name} is already running.", [], "unchanged")
        raise BatoceraError(
            f"{running.get('name', 'Another game')} ({running.get('systemName', '?')}) is running. "
            "Quit it with quit_game first (ask the user: unsaved progress is lost)."
        )

    path = raw.get("path")
    if not isinstance(path, str) or not path:
        raise BatoceraError(f"EmulationStation's entry for {name} has no path.")
    sent = [f"POST /launch {path}"]
    if _dry():
        return _result(f"launch {name} ({s})", sent, "dry_run")
    await c.launch(path)

    # ASSUMPTION B-ES-LAUNCH: /launch answers 200 even for a path it doesn't know, and the game starts a
    # moment later; only /runningGame says whether it worked.
    async def started() -> dict[str, Any] | None:
        g = await c.running_game()
        return g if g and g.get("id") == gid else None

    if await _wait_for(started, LAUNCH_TIMEOUT_S):
        return _result(f"Launched {name} ({s}).", sent)
    other = await c.running_game()
    if other:
        raise BatoceraError(f"Something else started instead: {other.get('name')}.")
    raise BatoceraError(
        f"EmulationStation accepted the request but {name} didn't start within {LAUNCH_TIMEOUT_S:g}s. "
        "A popup (show_message style=popup), dialog or menu may be open on the cabinet: the game starts once "
        "someone closes it. Otherwise the emulator failed to start (check the screen)."
    )


@mcp.tool(title="Quit the running game", annotations=_LOSES_PROGRESS)
async def quit_game() -> ActionResult:
    """Quit the running game and return to EmulationStation. Unsaved progress is lost: ask first if someone
    may be playing (use save_state first for a RetroArch game if they want to keep it).
    """
    c = es()
    running = await c.running_game()
    if running is None:
        return _result("No game is running.", [], "unchanged")
    name = running.get("name", "the game")
    sent = ["GET /emukill"]
    if _dry():
        return _result(f"quit {name}", sent, "dry_run")
    await c.emukill()

    async def stopped() -> bool:  # ASSUMPTION B-ES-EMUKILL
        return await c.running_game() is None

    if await _wait_for(stopped, QUIT_TIMEOUT_S):
        return _result(f"Quit {name}.", sent)
    raise BatoceraError(f"Asked the emulator to stop, but {name} is still running after {QUIT_TIMEOUT_S:g}s.")


@mcp.tool(title="Show a message on screen", annotations=_ACT)
async def show_message(
    text: Annotated[str, Field(min_length=1, max_length=300)],
    style: Annotated[
        Literal["auto", "notification", "popup"],
        Field(
            description="auto: over the game if a RetroArch game is running, otherwise a frontend notification. "
            "popup: a frontend dialog that stays until someone presses a button."
        ),
    ] = "auto",
) -> ActionResult:
    """Show a message on the cabinet's screen, e.g. 'Dinner in 5 minutes!'."""
    t = _check(cfg.message_text, text)
    c = es()
    if style == "auto" and await c.running_game():
        status = await ra().status()
        if status and status["state"] != "contentless":
            one_line = " ".join(t.split())
            sent = [f"UDP SHOW_MSG {one_line}"]
            if _dry():
                return _result("show the message over the running game", sent, "dry_run")
            await ra().show_message(one_line)
            # ASSUMPTION B-RA-COMMANDS: on hardware, RetroArch shows it briefly, small, bottom-left.
            return _result("Shown over the running game (small, bottom-left, for a few seconds).", sent)
        # ASSUMPTION B-ES-MESSAGES: frontend notifications aren't visible over a running emulator.
        raise BatoceraError(
            "A game is running in an emulator that can't show messages (or RetroArch network commands are off), "
            "so the message wouldn't be visible. Try again when no game is running."
        )
    popup = style == "popup"
    sent = [f"POST {'/messagebox' if popup else '/notify'} {t}"]
    if _dry():
        return _result(f"show the message as a {'popup' if popup else 'notification'}", sent, "dry_run")
    await c.notify(t, popup=popup)
    result = _result("Shown as a popup." if popup else "Shown as a notification.", sent)
    if popup:
        # ASSUMPTION B-ES-LAUNCH: an open popup holds launches until someone presses a button.
        result["warnings"].append("Until someone presses a button on the cabinet, the popup holds back launch_game.")
    return result


@mcp.tool(title="Rescan game lists", annotations=_SET)
async def reload_games() -> ActionResult:
    """Make EmulationStation rescan its game lists, e.g. after ROMs were added to the cabinet or its share."""
    sent = ["GET /reloadgames"]
    if _dry():
        return _result("rescan the game lists", sent, "dry_run")
    await es().reload_games()
    return _result("EmulationStation is reloading its game lists.", sent)


# --- RetroArch -------------------------------------------------------------------
# ASSUMPTION B-RA-COMMANDS: each command does what its name says; only GET_STATUS answers.
async def _retroarch(command: str, verb: str, done: str) -> ActionResult:
    s = await ra().require_game()
    game = s["game"] or "the game"
    sent = [f"UDP {command}"]
    if _dry():
        return _result(f"{verb} {game}", sent, "dry_run")
    await ra().send(command)
    return _result(done.format(game=game), sent)


@mcp.tool(title="Pause or resume", annotations=_SET)
async def pause_game(paused: bool) -> ActionResult:
    """Pause (paused=true) or resume (paused=false) the running RetroArch game."""
    r = ra()
    s = await r.require_game()
    if (s["state"] == "paused") == paused:
        return _result("Already paused." if paused else "Already running.", [], "unchanged")
    sent = ["UDP PAUSE_TOGGLE"]
    if _dry():
        return _result("pause the game" if paused else "resume the game", sent, "dry_run")
    await r.send("PAUSE_TOGGLE")
    await asyncio.sleep(0.2)
    after = await r.status()
    if after and (after["state"] == "paused") == paused:
        return _result("Paused." if paused else "Resumed.", sent)
    raise BatoceraError("Sent pause/resume, but RetroArch's state didn't change.")


@mcp.tool(title="Save state", annotations=_ACT)
async def save_state() -> ActionResult:
    """Save the running RetroArch game's state to its current save slot (overwrites that slot)."""
    return await _retroarch("SAVE_STATE", "save state for", "Saved state for {game} (current slot).")


@mcp.tool(title="Load state", annotations=_LOSES_PROGRESS)
async def load_state() -> ActionResult:
    """Load the running RetroArch game's state from its current slot. Progress since that save is lost."""
    return await _retroarch("LOAD_STATE", "load the saved state for", "Loaded the saved state for {game}.")


@mcp.tool(title="Take a screenshot", annotations=_ACT)
async def take_screenshot() -> ActionResult:
    """Save a screenshot of the running RetroArch game to the cabinet's screenshots folder."""
    return await _retroarch(
        "SCREENSHOT", "take a screenshot of", "Screenshot of {game} saved in /userdata/screenshots."
    )


@mcp.tool(title="Reset the game", annotations=_LOSES_PROGRESS)
async def reset_game() -> ActionResult:
    """Reset the running RetroArch game, like pressing the console's reset button. Unsaved progress is lost."""
    return await _retroarch("RESET", "reset", "Reset {game}.")
