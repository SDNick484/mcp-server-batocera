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

SEEN = "Seen 2026-10-21 on Batocera 43 (ES 43couv.1) from a Proxmox LXC: "
SEEN_PT = "Seen 2026-10-22 on Batocera 43 with Home Assistant's Mosquitto add-on: "
SEEN_24 = "Seen 2026-10-24 on Batocera 43, on the cabinet over SSH: "


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
        "Unless 'Enable public web access' is on, EmulationStation listens on 127.0.0.1 only, so other hosts' "
        "connections are refused (its 403 for non-local clients is a second guard they can't reach); with it on "
        "(after a reboot), it listens on 0.0.0.0:1234.",
        f"{HTTP} isAllowed() and listen()",
        "high",
        status="hardware-verified",
        note=SEEN + "connection refused with the setting off; doctor all ok after turning it on and rebooting. "
        "The claim used to say other hosts get 403; they get a refused connection. Not yet known: whether "
        "the reboot is needed.",
    ),
    Assumption(
        "B-ES-DURING-GAME",
        "The API keeps answering while a game runs (it is a thread inside EmulationStation, which waits on "
        "the emulator process).",
        f"{HTTP}; {ES} es-app/src/FileData.cpp launchGame()",
        "medium",
        status="hardware-verified",
        note=SEEN + "/runningGame and /isIdle answered while a SNES (RetroArch) game ran, and get_status "
        "worked during a GameCube game in Dolphin.",
    ),
    Assumption(
        "B-ES-RUNNINGGAME",
        '/runningGame answers 201 {"msg":"NO GAME RUNNING"} when idle and 200 with the game\'s JSON otherwise; '
        "/isIdle answers 200 [ true ] or 201 [ false ].",
        HTTP,
        "high",
        status="hardware-verified",
        note=SEEN + 'idle: /runningGame 201 {"msg":"NO GAME RUNNING"}, /isIdle 200 [ true ]; during a game: '
        "/runningGame 200, /isIdle 201 [ false ].",
    ),
    Assumption(
        "B-ES-LAUNCH",
        "POST /launch takes the ROM path as the raw body, answers 200 whether or not the path is a known game, "
        "cancels the screensaver, and starts the game a moment later on the UI thread, which an open popup "
        "(/messagebox) holds until someone dismisses it.",
        f"{HTTP} /launch",
        "high",
        status="hardware-verified",
        note=SEEN + "SNES and GameCube paths with spaces, commas, '&', an apostrophe and parentheses launched, "
        "confirmed in /runningGame within 2.4 s; POST /launch of a missing path answered 200 and nothing started. "
        "With a show_message popup open, a launch waited until the popup was dismissed (launch_game timed out).",
    ),
    Assumption(
        "B-ES-EMUKILL",
        "GET /emukill stops the running emulator; /runningGame goes back to 201 within a few seconds.",
        f"{HTTP}; {ES} ApiSystem::emuKill()",
        "medium",
        status="hardware-verified",
        note=SEEN + "quit_game was confirmed in 7.9 s for a SNES game (RetroArch) and 3.9 s for a GameCube game "
        "(Dolphin), against the server's 10 s wait. PCSX2 not checked.",
    ),
    Assumption(
        "B-ES-GAME-ID",
        "A game's id is the md5 hex of its path, stable across reloads.",
        f"{ES} es-app/src/services/HttpApi.cpp getFileDataId()",
        "high",
        status="hardware-verified",
        note=SEEN + "id == md5(path) for all 512 mame games, and an id still found its game after reload_games.",
    ),
    Assumption(
        "B-ES-METADATA",
        "Game metadata values are strings: playcount and gametime (seconds) as integers, lastplayed and "
        'releasedate as 20261009T201500, favorite as "true"; EmulationStation records play count and time itself.',
        f"{ES} es-app/src/MetaData.cpp, HttpApi.cpp getFileDataJson()",
        "high",
        status="hardware-verified",
        note=SEEN + "playcount '2', gametime '210', lastplayed '20250629T112833', releasedate '19860101T000000', "
        "favorite 'false', all strings. Some gametime totals run to hundreds of hours: ES counts time a game "
        "is left running. playcount and lastplayed changed at launch, and gametime was already 22 s higher "
        "20 s into a game.",
    ),
    Assumption(
        "B-ES-SYSTEMS",
        "/systems lists collections (favorites, last played) and group systems too, flagged with "
        '"collection", "gamesystem" and "groupsystem" as the strings "true"/"false".',
        f"{ES} HttpApi.cpp getSystemDataJson()",
        "high",
        status="hardware-verified",
        note=SEEN + "collection 'true' on custom collections, 'all' and 'favorites'; groupsystem 'true' on ports; "
        "member systems carry a 'group' string. imageviewer (the picture viewer) is flagged gamesystem 'true' "
        "too, so the server hides it by name (config.NON_GAME_SYSTEMS).",
    ),
    Assumption(
        "B-ES-THREADS",
        "The HTTP server hands each connection to a fixed pool of max(8, cores - 1) threads (cpp-httplib's "
        "default), and a connection that sends nothing holds its thread for the 5 s read timeout. The server "
        "loads at most 2 game lists at a time, a courtesy that leaves most of the pool to other clients.",
        f"{HTTP} at batocera.linux 43's pinned commit 68f45afb (no new_task_queue, so httplib.h's "
        "ThreadPool(CPPHTTPLIB_THREAD_POOL_COUNT)); ES master since 7cc47355 (2026-08-19) uses ThreadPool(2, "
        "CPPHTTPLIB_THREAD_POOL_COUNT): 2 threads growing to the same maximum when busy",
        "high",
        status="hardware-verified",
        note=SEEN_24 + "nproc 4, and batocera_mcp.probe answered at once with up to 7 idle connections held and "
        "stalled 4.7 s (the read timeout, less the probe's 0.3 s settle) with 8: a pool of 8, max(8, 4 - 1). "
        "Corrected 2026-10-23 from a 2-thread pool, read from ES master's ThreadPool(2, ...), which Batocera 43 "
        "doesn't have. find_games across 32 systems (3,000+ games, cold cache) took about 1 s at 2 at a time.",
    ),
    Assumption(
        "B-ES-MESSAGES",
        "POST /notify shows the body as a transient notification; POST /messagebox shows a dialog that stays "
        "until someone presses a button. Neither is visible over a running emulator.",
        HTTP,
        "medium",
        status="hardware-verified",
        note=SEEN + "a notification showed in the frontend; a popup stayed until a button was pressed; /notify "
        "during a Dolphin game answered 200 but nothing appeared, then or later.",
    ),
    # --- RetroArch ----------------------------------------------------------------------
    Assumption(
        "B-RA-ENABLE",
        "Adding global.retroarch.network_cmd_enable=true to batocera.conf makes RetroArch listen on UDP 55355 "
        "from the next game launch.",
        "batocera.linux python-src/batocera-launch-libretro emulator.py (retroarch.* keys copied to the config)",
        "medium",
        status="hardware-verified",
        note=SEEN + "with the line in batocera.conf, Batocera wrote network_cmd_enable = true and network_cmd_port = "
        '"55355" into retroarchcustom.cfg, and RetroArch 1.22.2 answered from the LAN. The line was already there '
        "(set before testing); RetroArch's own default is off.",
    ),
    Assumption(
        "B-RA-STATUS",
        "GET_STATUS answers 'GET_STATUS PLAYING|PAUSED <system>,<game>,crc32=<hex>' or 'GET_STATUS CONTENTLESS'; "
        "other commands don't answer, and nothing answers while a standalone emulator runs.",
        "RetroArch command.c",
        "medium",
        status="hardware-verified",
        note=SEEN + "\"GET_STATUS PLAYING super_nes,Super Mario World 2 - Yoshi's Island (USA) (Rev 1),"
        "crc32=cf98ddaa\\n\", and PAUSED while paused. The system is RetroArch's name (super_nes), not Batocera's "
        "(snes); the game is the ROM file name.",
    ),
    Assumption(
        "B-RA-COMMANDS",
        "PAUSE_TOGGLE, SAVE_STATE, LOAD_STATE (current slot), SCREENSHOT (to /userdata/screenshots), RESET and "
        "SHOW_MSG <text> do what their names say.",
        "RetroArch command.h",
        "medium",
        status="hardware-verified",
        note=SEEN + "all six on RetroArch 1.22.2 (snes9x): pause and resume, save and load (jumped back), a "
        "screenshot named '<ROM>-261021-233238.png' in /userdata/screenshots, reset to the title screen, and "
        "SHOW_MSG as small text in the bottom-left corner for a few seconds (easy to miss).",
    ),
    # --- playtracker (EmulationStation event scripts) -----------------------------------------
    Assumption(
        "B-PT-SCRIPTS",
        "EmulationStation runs every executable directly in /userdata/system/configs/emulationstation/scripts/ "
        "on every event, event name first, without waiting (asynchronously), with python3 available.",
        f"{ES} es-core/src/Scripting.cpp fireEvent()",
        "high",
        status="hardware-verified",
        note=SEEN_PT + "playtracker.py (chmod +x) directly in scripts/ received start, game-start, game-end, "
        "screensaver-start/stop, wake and game-selected with the event name first; python3 was on the PATH. "
        "Pixelcade's per-event subfolders kept working alongside.",
    ),
    Assumption(
        "B-PT-GAME-ARGS",
        "game-start passes the backslash-escaped ROM path, its basename and the game's name; game-end passes "
        "nothing; game-selected passes system, plain path and name.",
        f"{ES} es-app/src/FileData.cpp, utils/FileSystemUtil.cpp getEscapedPath()",
        "high",
        status="hardware-verified",
        note=SEEN_PT + "game-start passed '/userdata/roms/snes/Super\\ Mario\\ World\\ 2\\ -\\ Yoshi\\'s\\ "
        "Island\\ \\(USA\\)\\ \\(Rev\\ 1\\).zip', which playtracker unescaped to the real path; game-end "
        "passed nothing.",
    ),
    Assumption(
        "B-PT-SCREENSAVER",
        "No screensaver type (dim, black, random video, slideshow, suspend) launches a game by itself; random "
        "video and slideshow fire game-selected for each game they show.",
        f"{ES} es-app/src/SystemScreenSaver.cpp, guis/GuiGeneralScreensaverOptions.cpp",
        "high",
        status="hardware-verified",
        note=SEEN_PT + "ten minutes of random video logged no game-start, and Home Assistant stayed Idle; "
        "game-selected fired for the videos (playtracker matched the one START launched).",
    ),
    Assumption(
        "B-PT-SCREENSAVER-START",
        "With screensaver controls on, START on a screensaver video fires screensaver-stop then game-start for "
        "the game shown; the scripts can finish in either order.",
        f"{ES} es-core/src/Window.cpp input(), SystemScreenSaver::launchGame()",
        "medium",
        status="hardware-verified",
        note=SEEN_PT + "START on a video logged screensaver-stop then game-start in the same second; with "
        "IGNORE_SCREENSAVER_LAUNCHES=yes the 11-minute session sent nothing, and a game picked from the list "
        "after waking was logged. playtracker allows 10 s.",
    ),
    Assumption(
        "B-PT-TMP",
        "/tmp is a tmpfs, emptied by any restart, so after a power cut or crash (when no quit, reboot or shutdown "
        "event ran to delete it) playtracker's announced marker is gone and it re-announces to Home Assistant.",
        "Batocera mounts /tmp as tmpfs",
        "medium",
        status="hardware-verified",
        note=SEEN_24 + "stat -f -c %T /tmp printed tmpfs. A clean reboot wouldn't have shown this: playtracker "
        "deletes the marker itself on reboot, shutdown and quit, so the tmpfs only matters after a power cut.",
    ),
    Assumption(
        "B-PT-HA-MQTT",
        "Home Assistant's Mosquitto add-on accepts a Home Assistant user's login, and MQTT discovery creates the "
        "sensor, binary_sensor and event entities from the retained config messages.",
        "Home Assistant Mosquitto add-on docs; MQTT discovery docs",
        "high",
        status="hardware-verified",
        note=SEEN_PT + "a Home Assistant user's login worked; discovery created the device and six entities "
        "(entity ids come from the display names: Last played is sensor.<device>_last_session, Last session "
        "length sensor.<device>_last_session_length). A session played while the add-on was stopped was queued "
        "and arrived with the next event.",
    ),
)

BY_ID = {a.id: a for a in ASSUMPTIONS}


def unverified() -> list[Assumption]:
    """Not checked on hardware yet: the code relies on them on the strength of docs or source alone."""
    return [a for a in ASSUMPTIONS if a.status == "simulator-only"]


def contradicted() -> list[Assumption]:
    """Checked on hardware and found wrong: the code no longer relies on them (see each ``note``)."""
    return [a for a in ASSUMPTIONS if a.status == "hardware-contradicted"]
