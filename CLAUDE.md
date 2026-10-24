# CLAUDE.md

## What this is

`mcp-server-batocera`: an MCP server for a Batocera cabinet, through EmulationStation's web API (TCP 1234)
and RetroArch network commands (UDP 55355), plus `playtracker/`, a hook script for the cabinet that
publishes play sessions to Home Assistant over MQTT. Sibling of `mcp-server-shieldtv`, `mcp-server-onkyo`
and `mcp-server-pixelcade`, and follows their conventions.

**This is a learning project.** The owner wants to understand how MCP servers are built, so explain the
reasoning behind non-obvious changes instead of only making them.

## Layout

- `src/batocera_mcp/config.py`: settings (`BATOCERA_URL`, RetroArch port, dry run, name) and validators
  (system name, game id, message text)
- `src/batocera_mcp/client.py`: `EmulationStationClient` (httpx, game-list cache), value parsers, `BatoceraError`
- `src/batocera_mcp/retroarch.py`: UDP client, `COMMANDS` allow-list, `GET_STATUS` parser
- `src/batocera_mcp/server.py`: MCP tools (`MCPServer` from `mcp` 2.x); actions return `ActionResult`
- `src/batocera_mcp/assumptions.py`: every Batocera behavior relied on, by id, with source and status
- `src/batocera_mcp/doctor.py`: layer-by-layer checks; `STEP` maps layers to HARDWARE_VALIDATION.md steps
- `src/batocera_mcp/sim/fake_cabinet.py`: wire-level fake (ES over real HTTP, RetroArch over real UDP), with
  fault switches; used by the tests and `simulate`
- `src/batocera_mcp/logsafe.py`, `tasks.py`, `ratelimit.py` (and their tests): shared with the sibling
  servers, synced from `mcp-server-shared` by `scripts/sync_shared.py` at the commit in `shared.lock`.
  `tasks.py` and `ratelimit.py` aren't used here yet. `remote.py` is excluded: this server is LAN-only.
- `src/batocera_mcp/cli.py`: `serve` (default), `doctor` (`check`), `simulate`, `call`
- `deploy/alpine/`: command-line install on Alpine (venv, `mcp-batocera` user, `/etc/.../env`, a `PATH`
  command that drops to the user; caller's environment beats the file), plus `setup-arcade.sh`, which
  installs this and mcp-server-pixelcade from GitHub (e.g. through `pct exec`). No service: nothing listens.
  Keep it in step with mcp-server-pixelcade's. CI runs setup-arcade.sh with this repo at the commit
  under test and mcp-server-pixelcade at its same-named branch (put changes to both kits on one), else
  main, each pinned to a commit up front (`ARCADE_REF_BATOCERA`, `ARCADE_REF_PIXELCADE`) so a twin
  PR merging mid-run, which deletes its branch, can't break the job.
- `playtracker/playtracker.py`: runs **on the cabinet**, stdlib only (no pip on Batocera), single file.
  `Tracker` (pure session logic), a minimal MQTT 3.1.1 QoS 1 publisher, and an outbox for broker outages
- `tests/`: `conftest.py` (a `FakeCabinet` per test, on free ports), `test_tools` (in-process MCP `Client`,
  dry run, faults), `test_cli`, `test_assumptions` (registry vs code vs docs), `test_units`, `test_logsafe`,
  `test_playtracker` (loaded from its file path; `FakeBroker` speaks enough MQTT), `test_stdio`.
  Async tests use anyio's plugin.

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
- **Actions honor dry run** (`if _dry(): return _result(..., "dry_run")` before anything is sent) and return
  an `ActionResult` (`cabinet`, `outcome` done/unchanged/dry_run, `detail`, `sent`, `warnings`).
- **Don't invent Batocera behavior.** Anything not seen on hardware goes in `assumptions.py` (status
  simulator-only), is cited where the code relies on it (`# ASSUMPTION B-...`), is implemented the same way
  in `fake_cabinet.py`, gets a test, and gets a step in HARDWARE_VALIDATION.md and a README table row.
  `tests/test_assumptions.py` fails if these drift.
- **Shared files are changed in `mcp-server-shared`, never here**: the `shared-drift` CI job fails on an
  edited copy. Then `python scripts/sync_shared.py --update` here, and commit the files with `shared.lock`.

## Facts the code depends on

The authoritative list, with ids and status, is `assumptions.py`; this is the readable summary.
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
mcp-server-batocera doctor [--json | --verbose] | simulate [--write-config DIR] | call <tool> k=v [--dry-run] | (serve [--dry-run])
```

Real hardware: `doctor`, then HARDWARE_VALIDATION.md. Record results in `assumptions.py` (status + note)
and the README table.
