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
  - The destructive tools (batocera_quit_game, batocera_load_state, batocera_reset_game) lose unsaved
    progress, so they carry destructive_hint=True and clients ask first.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal, TypeVar

from mcp.server import MCPServer
from pydantic import Field
from typing_extensions import TypedDict

from . import config as cfg
from .client import BatoceraError, EmulationStationClient, Game, GameDetails, System, to_details, to_game, to_system
from .config import load_settings
from .logsafe import mask_url
from .ratelimit import TokenBucket, refusal
from .retroarch import RetroArchClient, RetroArchStatus
from .tools import ACT, ACT_IDEMPOTENT, DESTRUCTIVE, READ_ONLY

log = logging.getLogger(__name__)

LAUNCH_TIMEOUT_S = 20.0
QUIT_TIMEOUT_S = 10.0
POLL_INTERVAL_S = 0.5

# Hardware rate limiters:
# 1. Rescan game lists: hammering disk / metadata reload across ROM folders
_RELOAD_BUCKET = TokenBucket(capacity=1.0, rate=1.0 / 60.0)
# 2. Launch / quit game: emulator process fork / kill storms (/emukill), display/audio driver lockup
_LAUNCH_BUCKET = TokenBucket(capacity=2.0, rate=1.0 / 15.0)
# 3. Save / load state / reset: rapid UDP 55355 state writes to flash/storage
_STATE_BUCKET = TokenBucket(capacity=3.0, rate=1.0 / 5.0)


def reset_rate_limiters() -> None:
    """Reset token buckets to full capacity (used in test fixtures)."""
    _RELOAD_BUCKET.tokens = _RELOAD_BUCKET.capacity
    _LAUNCH_BUCKET.tokens = _LAUNCH_BUCKET.capacity
    _STATE_BUCKET.tokens = _STATE_BUCKET.capacity


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
        "In the cabinet signal chain, the Batocera PC acts as the Video and Audio Source feeding HDMI into "
        "the AV Receiver (Onkyo) or TV (LG webOS). Game launch events automatically trigger dynamic marquee "
        "artwork updates on Pixelcade LED marquees. Play session tracking automatically publishes MQTT events "
        "to Home Assistant ('batocera/now_playing'). "
        "Call batocera_get_status first. To launch a game, find its id with batocera_find_games, then call "
        "batocera_launch_game. Only one game runs at a time: batocera_quit_game ends the current one and loses "
        "unsaved progress, so confirm with the user before quitting a game someone may be playing."
    ),
    lifespan=lifespan,
)

T = TypeVar("T")


def _check(fn: Callable[..., T], *args: object) -> T:
    try:
        return fn(*args)
    except cfg.InvalidValue as exc:
        raise BatoceraError(str(exc)) from exc


SystemArg = Annotated[
    str, Field(min_length=1, max_length=40, description="System name from batocera_list_systems, e.g. 'snes', 'mame'.")
]
GameIdArg = Annotated[
    str, Field(min_length=32, max_length=32, description="The game's id from batocera_find_games (32 hex characters).")
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


@mcp.tool(title="Get Batocera status", annotations=READ_ONLY)
async def batocera_get_status() -> Status:
    """
    <use_case>
    Report whether the cabinet's EmulationStation is reachable, which game is running (if any),
    and whether EmulationStation is busy (scraping, updating).
    </use_case>
    <important_notes>
    - Start here. Reports network URL, reachability, ES version, and RetroArch play/pause state.
    - While a RetroArch game runs, retroarch reports whether it is playing or paused; null for standalone emulators.
    - dry_run true indicates actions are simulated and not sent to the cabinet.
    </important_notes>
    """
    c = es()
    try:
        version = await c.version()
        running = await c.running_game()
        idle = await c.is_idle()
    except BatoceraError as exc:
        return Status(
            url=mask_url(c.settings.url),
            dry_run=c.settings.dry_run,
            reachable=False,
            error=str(exc),
            es_version=None,
            running_game=None,
            busy=None,
            retroarch=None,
        )
    return Status(
        url=mask_url(c.settings.url),
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


@mcp.tool(title="List game systems", annotations=READ_ONLY)
async def batocera_list_systems() -> SystemList:
    """
    <use_case>
    List the game systems configured on the cabinet with ROMs.
    </use_case>
    <important_notes>
    - Returns game counts, favorites, and most played game for each system.
    - The name field (e.g. 'snes', 'mame') is used as system argument in other tools.
    </important_notes>
    """
    raw = await es().game_systems()
    systems = [to_system(s) for s in raw]
    systems.sort(key=lambda s: s["full_name"].lower())
    total = sum(s["games"] for s in systems)
    return SystemList(systems=systems, total_games=total)


_WORD = re.compile(r"[\w]+", re.UNICODE)


def _words(q: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(q)]


def _match_score(name: str, query_words: list[str]) -> int | None:
    lower = name.lower()
    score = 0
    for w in query_words:
        pos = lower.find(w)
        if pos == -1:
            return None
        score += pos + 1
    return score


class GameList(TypedDict):
    matches: int
    games: list[Game]
    truncated: bool


@mcp.tool(title="Find games", annotations=READ_ONLY)
async def batocera_find_games(
    query: Annotated[
        str | None,
        Field(default=None, max_length=100, description="Words from the game's name, e.g. 'mario world'."),
    ] = None,
    system: Annotated[str | None, Field(default=None, max_length=40, description="Only this system.")] = None,
    sort: Annotated[
        Literal["relevance", "name", "most_played", "most_time", "recently_played"],
        Field(description="relevance (best name match first), or by play count, play time or last played."),
    ] = "relevance",
    favorites_only: bool = False,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> GameList:
    """
    <use_case>
    Search the cabinet's game catalog by title, system, or play history.
    </use_case>
    <important_notes>
    - Returns matches with 32-character game_id needed for batocera_launch_game and batocera_get_game.
    - Supports sorting by relevance, name, most_played, most_time, or recently_played.
    </important_notes>
    """
    c = es()
    names = [_check(cfg.system_name, system)] if system else [str(s.get("name")) for s in await c.game_systems()]
    query_words = _words(query) if query else []
    if query and not query_words:
        raise BatoceraError("The query has no letters or digits to match.")

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
    else:
        scored.sort(key=lambda x: (x[0], x[1]["name"].lower()))
    games_out = [g for _, g in scored]
    return GameList(matches=len(games_out), games=games_out[:limit], truncated=len(games_out) > limit)


@mcp.tool(title="Get game details", annotations=READ_ONLY)
async def batocera_get_game(system: SystemArg, game_id: GameIdArg) -> GameDetails:
    """
    <use_case>
    Retrieve full metadata for a specific game.
    </use_case>
    <important_notes>
    - Returns developer, publisher, rating, players, play count, and play time.
    - Requires system name and 32-hex game_id from batocera_find_games.
    </important_notes>
    """
    s = _check(cfg.system_name, system)
    gid = _check(cfg.game_id, game_id)
    return to_details(await es().find_game(s, gid))


# --- Acting --------------------------------------------------------------------
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


@mcp.tool(title="Launch a game", annotations=ACT_IDEMPOTENT)
async def batocera_launch_game(system: SystemArg, game_id: GameIdArg) -> ActionResult:
    """
    <use_case>
    Launch a game on the arcade cabinet screen.
    </use_case>
    <important_notes>
    - Refuses if another game is already running; call batocera_quit_game first.
    - Rate-limited to prevent emulator process fork storms and audio lockups.
    - Automatically triggers Pixelcade marquee updates if connected.
    </important_notes>
    """
    c = es()
    s = _check(cfg.system_name, system)
    gid = _check(cfg.game_id, game_id)
    raw = await c.find_game(s, gid)
    name = raw.get("name", gid)

    wait = _LAUNCH_BUCKET.take(1.0)
    if wait > 0:
        raise BatoceraError(refusal(f"launch {name}", wait, c.settings.name or "Batocera cabinet"))

    running = await c.running_game()
    if running:
        if running.get("id") == gid:
            return _result(f"{name} is already running.", [], "unchanged")
        raise BatoceraError(
            f"{running.get('name', 'Another game')} ({running.get('systemName', '?')}) is running. "
            "Quit it with batocera_quit_game first (ask the user: unsaved progress is lost)."
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
        "A popup (batocera_show_message style=popup), dialog or menu may be open on the cabinet: the game starts once "
        "someone closes it. Otherwise the emulator failed to start (check the screen)."
    )


@mcp.tool(title="Quit the running game", annotations=DESTRUCTIVE)
async def batocera_quit_game() -> ActionResult:
    """
    <use_case>
    Terminate the currently running game emulator and return to EmulationStation menu.
    </use_case>
    <important_notes>
    - DESTRUCTIVE: Unsaved game progress will be lost. Ask user confirmation before calling.
    - Rate-limited to protect emulator process management and display drivers.
    </important_notes>
    """
    c = es()
    wait = _LAUNCH_BUCKET.take(1.0)
    if wait > 0:
        raise BatoceraError(refusal("quit running game", wait, c.settings.name or "Batocera cabinet"))

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


@mcp.tool(title="Show a message on screen", annotations=ACT)
async def batocera_show_message(
    text: Annotated[str, Field(min_length=1, max_length=300)],
    style: Annotated[
        Literal["auto", "notification", "popup"],
        Field(
            description="auto: over the game if a RetroArch game is running, otherwise a frontend notification. "
            "popup: a frontend dialog that stays until someone presses a button."
        ),
    ] = "auto",
) -> ActionResult:
    """
    <use_case>
    Display an on-screen notification or popup dialog on the cabinet screen.
    </use_case>
    <important_notes>
    - style='auto' displays an in-game HUD overlay if RetroArch is active, or frontend banner otherwise.
    - style='popup' halts game launch until acknowledged by a button press on the cabinet.
    </important_notes>
    """
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
        result["warnings"].append("Until someone presses a button on the cabinet, the popup holds back batocera_launch_game.")
    return result


@mcp.tool(title="Rescan game lists", annotations=ACT_IDEMPOTENT)
async def batocera_reload_games() -> ActionResult:
    """
    <use_case>
    Instruct EmulationStation to rescan storage and reload game lists.
    </use_case>
    <important_notes>
    - Use after adding or removing ROM files.
    - Strictly rate-limited (1 per 60s) to prevent disk I/O thrashing and UI freezes.
    </important_notes>
    """
    c = es()
    wait = _RELOAD_BUCKET.take(1.0)
    if wait > 0:
        raise BatoceraError(refusal("rescan game lists", wait, c.settings.name or "Batocera cabinet"))

    sent = ["GET /reloadgames"]
    if _dry():
        return _result("rescan the game lists", sent, "dry_run")
    await c.reload_games()
    return _result("EmulationStation is reloading its game lists.", sent)


# --- RetroArch -------------------------------------------------------------------
# ASSUMPTION B-RA-COMMANDS: each command does what its name says; only GET_STATUS answers.
async def _retroarch(
    command: str, verb: str, done: str, bucket: TokenBucket | None = _STATE_BUCKET
) -> ActionResult:
    if bucket is not None:
        wait = bucket.take(1.0)
        if wait > 0:
            raise BatoceraError(refusal(verb, wait, "Batocera cabinet"))

    s = await ra().require_game()
    game = s["game"] or "the game"
    sent = [f"UDP {command}"]
    if _dry():
        return _result(f"{verb} {game}", sent, "dry_run")
    await ra().send(command)
    return _result(done.format(game=game), sent)


@mcp.tool(title="Pause or resume", annotations=ACT_IDEMPOTENT)
async def batocera_pause_game(paused: bool) -> ActionResult:
    """
    <use_case>
    Pause or resume the active RetroArch game.
    </use_case>
    <important_notes>
    - Only operates when a libretro/RetroArch game core is currently running.
    - Idempotent: reports unchanged if already in the target state.
    </important_notes>
    """
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


@mcp.tool(title="Save state", annotations=ACT)
async def batocera_save_state() -> ActionResult:
    """
    <use_case>
    Save game progress to the current RetroArch save slot.
    </use_case>
    <important_notes>
    - Overwrites the active save slot on the cabinet storage.
    - Rate-limited to prevent UDP packet storms and file corruption.
    </important_notes>
    """
    return await _retroarch("SAVE_STATE", "save state for", "Saved state for {game} (current slot).")


@mcp.tool(title="Load state", annotations=DESTRUCTIVE)
async def batocera_load_state() -> ActionResult:
    """
    <use_case>
    Load saved state from the current RetroArch save slot.
    </use_case>
    <important_notes>
    - DESTRUCTIVE: Unsaved progress since the last save will be lost.
    - Rate-limited to prevent storage and UDP packet collision.
    </important_notes>
    """
    return await _retroarch("LOAD_STATE", "load the saved state for", "Loaded the saved state for {game}.")


@mcp.tool(title="Take a screenshot", annotations=ACT)
async def batocera_take_screenshot() -> ActionResult:
    """
    <use_case>
    Capture a screenshot of the currently running RetroArch game.
    </use_case>
    <important_notes>
    - Saved directly to /userdata/screenshots on the cabinet.
    - Only works while a RetroArch core is actively running.
    </important_notes>
    """
    return await _retroarch(
        "SCREENSHOT", "take a screenshot of", "Screenshot of {game} saved in /userdata/screenshots.", bucket=None
    )


@mcp.tool(title="Reset the game", annotations=DESTRUCTIVE)
async def batocera_reset_game() -> ActionResult:
    """
    <use_case>
    Soft-reset the running RetroArch game emulator.
    </use_case>
    <important_notes>
    - DESTRUCTIVE: Like pressing the physical console reset button; unsaved progress is lost.
    - Rate-limited to avoid hardware and display strain.
    </important_notes>
    """
    return await _retroarch("RESET", "reset", "Reset {game}.")
