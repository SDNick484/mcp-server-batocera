"""Every Batocera behavior this server and playtracker rely on, and whether hardware has confirmed it.

Why a registry instead of comments (the same scheme as the sibling servers): a
comment saying "unverified" is easy to miss and never goes away. Each claim has
an id that
  - the code cites where it depends on it (``# ASSUMPTION B-ES-LAUNCH``),
  - a test cites to document it,
  - HARDWARE_VALIDATION.md cites in the step that confirms it,
  - the README's verification table lists, with its status,
  - ``mcp-server-batocera doctor`` prints while it is unconfirmed.
tests/test_assumptions.py fails if they drift apart.

Everything here was read from source in 2026-10 (batocera-emulationstation,
batocera.linux, RetroArch) and nothing has been seen on the owner's cabinet yet.
To record a hardware result, set ``status`` to "hardware-verified" (or
"hardware-contradicted"), say what you saw in ``note``, and commit it.

Confidence is about the *claim*, judged from its source:
  high   - read directly in the code that implements it
  medium - read in source, but depends on settings, timing or versions we can't see
  low    - our own guess; the simulator implements it but nothing confirms it
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Confidence = Literal["high", "medium", "low"]
Status = Literal["simulator-only", "hardware-verified", "hardware-contradicted"]

ES = "batocera-emulationstation"
HTTP = f"{ES} es-app/src/services/HttpServerThread.cpp"


@dataclass(frozen=True)
class Assumption:
    id: str
    claim: str
    source: str
    confidence: Confidence
    status: Status = "simulator-only"
    note: str = ""


ASSUMPTIONS: tuple[Assumption, ...] = (
    # --- EmulationStation web API ---------------------------------------------------------
    Assumption(
        "B-ES-PUBLIC-ACCESS",
        "Requests from other hosts get 403 unless 'Enable public web access' is on; with it (after a reboot), "
        "EmulationStation listens on 0.0.0.0:1234.",
        f"{HTTP} isAllowed() and listen()",
        "high",
        note="Read in source. Whether the setting needs a reboot or only an ES restart isn't confirmed.",
    ),
    Assumption(
        "B-ES-DURING-GAME",
        "The API keeps answering while a game runs (it is a thread inside EmulationStation, which waits on "
        "the emulator process).",
        f"{HTTP}; {ES} es-app/src/FileData.cpp launchGame()",
        "medium",
        note="Follows from the threading, but some emulators or a low-memory board could stall it.",
    ),
    Assumption(
        "B-ES-RUNNINGGAME",
        '/runningGame answers 201 {"msg":"NO GAME RUNNING"} when idle and 200 with the game\'s JSON otherwise; '
        "/isIdle answers 200 [ true ] or 201 [ false ].",
        HTTP,
        "high",
        note="Read in source; status codes not yet seen on the wire.",
    ),
    Assumption(
        "B-ES-LAUNCH",
        "POST /launch takes the ROM path as the raw body, answers 200 whether or not the path is a known game, "
        "cancels the screensaver, and starts the game a moment later on the UI thread.",
        f"{HTTP} /launch",
        "high",
        note="Read in source. How long until /runningGame shows the game is a guess (the server waits 20 s).",
    ),
    Assumption(
        "B-ES-EMUKILL",
        "GET /emukill stops the running emulator; /runningGame goes back to 201 within a few seconds.",
        f"{HTTP}; {ES} ApiSystem::emuKill()",
        "medium",
        note="emuKill kills the emulator process; standalone emulators (Dolphin, PCSX2) not checked. "
        "The server waits 10 s.",
    ),
    Assumption(
        "B-ES-GAME-ID",
        "A game's id is the md5 hex of its path, stable across reloads.",
        f"{ES} es-app/src/services/HttpApi.cpp getFileDataId()",
        "high",
        note="Read in source.",
    ),
    Assumption(
        "B-ES-METADATA",
        "Game metadata values are strings: playcount and gametime (seconds) as integers, lastplayed and "
        'releasedate as 20261009T201500, favorite as "true"; EmulationStation records play count and time itself.',
        f"{ES} es-app/src/MetaData.cpp, HttpApi.cpp getFileDataJson()",
        "high",
        note="Read in source; the time format is from gamelist.xml conventions, not seen in an API reply.",
    ),
    Assumption(
        "B-ES-SYSTEMS",
        "/systems lists collections (favorites, last played) and group systems too, flagged with "
        '"collection", "gamesystem" and "groupsystem" as the strings "true"/"false".',
        f"{ES} HttpApi.cpp getSystemDataJson()",
        "high",
        note="Read in source.",
    ),
    Assumption(
        "B-ES-THREADS",
        "The HTTP server runs a 2-thread pool, so the server loads at most 2 game lists at a time.",
        f"{HTTP} new_task_queue",
        "medium",
        note="A courtesy limit: more parallel requests would queue, not fail.",
    ),
    Assumption(
        "B-ES-MESSAGES",
        "POST /notify shows the body as a transient notification; POST /messagebox shows a dialog that stays "
        "until someone presses a button. Neither is visible over a running emulator.",
        HTTP,
        "medium",
        note="Endpoints read in source; what they look like on screen, and over a game, not checked.",
    ),
    # --- RetroArch ----------------------------------------------------------------------
    Assumption(
        "B-RA-ENABLE",
        "Adding global.retroarch.network_cmd_enable=true to batocera.conf makes RetroArch listen on UDP 55355 "
        "from the next game launch.",
        "batocera.linux python-src/batocera-launch-libretro emulator.py (retroarch.* keys copied to the config)",
        "medium",
        note="The pass-through is read in source; that RetroArch then binds the LAN interface is not checked.",
    ),
    Assumption(
        "B-RA-STATUS",
        "GET_STATUS answers 'GET_STATUS PLAYING|PAUSED <system>,<game>,crc32=<hex>' or 'GET_STATUS CONTENTLESS'; "
        "other commands don't answer, and nothing answers while a standalone emulator runs.",
        "RetroArch command.c",
        "medium",
        note="From RetroArch's source; the system/game fields' exact contents on Batocera aren't seen yet.",
    ),
    Assumption(
        "B-RA-COMMANDS",
        "PAUSE_TOGGLE, SAVE_STATE, LOAD_STATE (current slot), SCREENSHOT (to /userdata/screenshots), RESET and "
        "SHOW_MSG <text> do what their names say.",
        "RetroArch command.h",
        "medium",
        note="Standard RetroArch commands; the screenshot folder is Batocera's default, not checked.",
    ),
    # --- playtracker (EmulationStation event scripts) -----------------------------------------
    Assumption(
        "B-PT-SCRIPTS",
        "EmulationStation runs every executable directly in /userdata/system/configs/emulationstation/scripts/ "
        "on every event, event name first, without waiting (asynchronously), with python3 available.",
        f"{ES} es-core/src/Scripting.cpp fireEvent()",
        "high",
        note="Read in source. The +x requirement and python3's path on the cabinet aren't checked.",
    ),
    Assumption(
        "B-PT-GAME-ARGS",
        "game-start passes the backslash-escaped ROM path, its basename and the game's name; game-end passes "
        "nothing; game-selected passes system, plain path and name.",
        f"{ES} es-app/src/FileData.cpp, utils/FileSystemUtil.cpp getEscapedPath()",
        "high",
        note="Read in source; quoting of names with spaces comes from Scripting.cpp and isn't seen yet.",
    ),
    Assumption(
        "B-PT-SCREENSAVER",
        "No screensaver type (dim, black, random video, slideshow, suspend) launches a game by itself; random "
        "video and slideshow fire game-selected for each game they show.",
        f"{ES} es-app/src/SystemScreenSaver.cpp, guis/GuiGeneralScreensaverOptions.cpp",
        "high",
        note="Read in source. The core claim behind 'screensaver activity isn't logged'.",
    ),
    Assumption(
        "B-PT-SCREENSAVER-START",
        "With screensaver controls on, START on a screensaver video fires screensaver-stop then game-start for "
        "the game shown; the scripts can finish in either order.",
        f"{ES} es-core/src/Window.cpp input(), SystemScreenSaver::launchGame()",
        "medium",
        note="Order read in source; script timing on the cabinet isn't seen. playtracker allows 10 s.",
    ),
    Assumption(
        "B-PT-TMP",
        "/tmp is cleared on reboot, so playtracker re-announces itself to Home Assistant once per boot.",
        "Batocera mounts /tmp as tmpfs",
        "medium",
        note="Standard on Batocera images; not checked on this cabinet.",
    ),
    Assumption(
        "B-PT-HA-MQTT",
        "Home Assistant's Mosquitto add-on accepts a Home Assistant user's login, and MQTT discovery creates the "
        "sensor, binary_sensor and event entities from the retained config messages.",
        "Home Assistant Mosquitto add-on docs; MQTT discovery docs",
        "high",
        note="Discovery and the MQTT client were tested against a real Mosquitto 2.0 broker; not yet against "
        "the owner's Home Assistant.",
    ),
)

BY_ID = {a.id: a for a in ASSUMPTIONS}


def unverified() -> list[Assumption]:
    return [a for a in ASSUMPTIONS if a.status != "hardware-verified"]
