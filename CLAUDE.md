# CLAUDE.md

## What this is

`mcp-server-batocera`: an MCP server for a Batocera cabinet, through EmulationStation's web API (TCP 1234)
and RetroArch network commands (UDP 55355), plus `playtracker/`, a hook script for the cabinet that
publishes play sessions to Home Assistant over MQTT. Sibling of `mcp-server-shieldtv`, `mcp-server-onkyo`
and `mcp-server-pixelcade`, and follows their conventions.

**This is a learning project.** The owner wants to understand how MCP servers are built, so explain the
reasoning behind non-obvious changes instead of only making them.

## Layout

- `src/batocera_mcp/config.py`: settings (`BATOCERA_URL`, RetroArch port) and validators (system name,
  game id, message text)
- `src/batocera_mcp/client.py`: `EmulationStationClient` (httpx, game-list cache), value parsers, `BatoceraError`
- `src/batocera_mcp/retroarch.py`: UDP client, `COMMANDS` allow-list, `GET_STATUS` parser
- `src/batocera_mcp/server.py`: MCP tools (`MCPServer` from `mcp` 2.x)
- `src/batocera_mcp/cli.py`: `serve` (default) and `check`
- `playtracker/playtracker.py`: runs **on the cabinet**, stdlib only (no pip on Batocera), single file.
  `Tracker` (pure session logic), a minimal MQTT 3.1.1 QoS 1 publisher, and an outbox for broker outages
- `tests/`: `conftest.py` (`FakeES` on `httpx.MockTransport`, `FakeRetroArch` on a real UDP socket),
  `test_units`, `test_tools` (in-process MCP `Client`), `test_playtracker` (loaded from its file path;
  `FakeBroker` speaks enough MQTT), `test_stdio`. Async tests use anyio's plugin.

## Rules for changes

- **The model never supplies a path or a raw command.** Games launch by system + id, resolved through
  EmulationStation's own list. RetroArch receives only `COMMANDS` constants plus validated `SHOW_MSG` text.
- ES endpoints that change the system stay unreachable: `/quit`, `/restart`, `/addgames`, `/removegames`,
  POSTs to `/systems/...`, `/input`, `/storage/event`. Update README and tests together if that changes.
- Destructive (`destructive_hint=True`) = loses unsaved progress: `quit_game`, `load_state`, `reset_game`.
  `launch_game` refuses to replace a running game rather than killing it.
- playtracker must stay stdlib-only, single-file, and must never raise into EmulationStation (exit 0).
- Raise `BatoceraError` (a `ToolError`) for anything the model or user can act on. Log to stderr only.
- Every tool has a `title`, explicit `ToolAnnotations`, and constrained args. `test_tools.py` enforces this.

## Facts the code depends on

From batocera-emulationstation (`es-app/src/services/HttpServerThread.cpp`, `HttpApi.cpp`,
`SystemScreenSaver.cpp`, `FileData.cpp`, `es-core/src/Scripting.cpp`) and batocera.linux
(`python-src/batocera-launch-libretro/.../emulator.py`), read 2026-10. **Not yet verified on hardware.**
- Non-127.0.0.1 clients get 403 unless `PublicWebAccess`; with it, ES listens on 0.0.0.0:1234.
- `/runningGame` answers 201 `{"msg":"NO GAME RUNNING"}` when idle. `/isIdle` 200 `[ true ]` / 201.
- `POST /launch` body = ROM path; it answers 200 even for unknown paths, so confirm via `/runningGame`.
  It calls `cancelScreenSaver()` first.
- Game id = md5(path). Metadata values are strings; times look like `20261009T201500`; `gametime` is
  seconds. `/systems` includes collections and group systems (filtered by `is_game_system`).
- The HTTP server uses a 2-thread pool; `find_games` loads systems with concurrency 2.
- Screensaver types: dim, black, random video, slideshow, suspend. None launch games. Random video and
  slideshow fire `game-selected` (system, path, name) per item. With `ScreenSaverControls`, START launches
  the shown game: `cancelScreenSaver()` (fires `screensaver-stop`) then `game-start`. Scripts run async,
  so they can arrive in either order; playtracker allows a 10 s grace window.
- Script events: start, quit (+reboot/shutdown), sleep, wake, screensaver-start/stop, game-selected,
  system-selected, game-start (escaped ROM path, basename, name), game-end (no args), and others.
  Executables directly in `.../emulationstation/scripts/` get the event name as their first argument.
- `game-start`'s ROM path is backslash-escaped (`\ `, `\(`...); `game-selected`'s path is not.
- Batocera copies `<system|global>.retroarch.<key>=value` from batocera.conf into RetroArch's config;
  network commands are off unless `global.retroarch.network_cmd_enable=true`.
- Batocera's build selects the Mosquitto package (`batocera-system/Config.in`); playtracker doesn't rely on
  it and speaks MQTT itself, so it works either way.

## Commands

```sh
pip install -e ".[dev]" && pytest && ruff check . && ruff format --check . && mypy
mcp-server-batocera check | (serve)
```
