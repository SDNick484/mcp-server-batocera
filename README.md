# mcp-server-batocera

An [MCP](https://modelcontextprotocol.io) server for a **[Batocera](https://batocera.org)** retro-gaming
cabinet. It lets an MCP client such as Claude browse the game library, see what's playing, launch and
quit games, put messages on screen, and, while a RetroArch game runs, pause, save or load state, take
screenshots and reset.

The repo also includes **playtracker**, a small hook script for the cabinet that publishes what's being
played to MQTT with Home Assistant discovery. It doesn't log screensaver activity.

> **Status: early / untested on hardware.** Built from Batocera's EmulationStation and RetroArch sources
> and tested against a wire-level simulator; playtracker is also tested against a real Mosquitto broker.
> Not yet run against a real cabinet: see [HARDWARE_VALIDATION.md](HARDWARE_VALIDATION.md) and the
> [verification table](#verification-status).

This is not an official Batocera project.

## How it works

```
MCP client ──stdio──▶ mcp-server-batocera ──HTTP :1234──▶ EmulationStation web API ─┐
                                          └──UDP :55355──▶ RetroArch (while a game runs) ├─ Batocera cabinet
Home Assistant ◀──MQTT :1883── playtracker.py ◀── EmulationStation event scripts ──────┘
```

- **EmulationStation web API** (TCP 1234) runs inside the frontend, so it keeps answering during a game.
  It covers the game library, the running game, launching, quitting and on-screen notifications.
- **RetroArch network commands** (UDP 55355) work only while a libretro core is running. They cover
  pause, save/load state, screenshots, reset and in-game messages.
- **playtracker** runs on the cabinet on every EmulationStation event and publishes to Home Assistant's
  Mosquitto broker. Nothing extra has to run on Batocera; it uses only Python's standard library.

## Cabinet setup

1. **Turn on public web access.** By default the API only answers the cabinet itself. Go to *Main Menu >
   System Settings > Developer > Enable public web access*, then reboot.
2. **Optional, for the RetroArch tools:** SSH in (`root` / `linux`) and add this line to
   `/userdata/system/batocera.conf`. It takes effect on the next game launch.
   ```
   global.retroarch.network_cmd_enable=true
   ```
3. Give the cabinet a fixed IP (a DHCP reservation) and use the IP rather than `batocera.local`.

Neither the API nor RetroArch's port has authentication; anything on the LAN can already use them. Keep
them off the internet.

## Install the MCP server

```sh
git clone https://github.com/SDNick484/mcp-server-batocera.git
cd mcp-server-batocera
python -m venv .venv && . .venv/bin/activate
pip install -e .
BATOCERA_URL=192.168.1.20 mcp-server-batocera doctor
```

`doctor` checks each layer between this machine and the cabinet (TCP, the API, the library, the
running game, RetroArch), and when one fails, says what it means and which
[HARDWARE_VALIDATION.md](HARDWARE_VALIDATION.md) step covers it. It changes nothing.

```json
{ "mcpServers": { "batocera": {
    "command": "/path/to/mcp-server-batocera/.venv/bin/mcp-server-batocera",
    "env": { "BATOCERA_URL": "192.168.1.20" } } } }
```

For Claude Code: `claude mcp add batocera -e BATOCERA_URL=192.168.1.20 -- /path/to/.venv/bin/mcp-server-batocera`.

| Setting                   | Default                                | What it does                                   |
| ------------------------- | -------------------------------------- | ---------------------------------------------- |
| `BATOCERA_URL`            | `http://batocera.local:1234`           | EmulationStation's API; a bare host gets `:1234` |
| `BATOCERA_RETROARCH_PORT` | `55355`                                | RetroArch's network command port               |
| `BATOCERA_TIMEOUT`        | `10`                                   | Seconds to wait for the API (1-60)             |
| `BATOCERA_CONFIG_DIR`     | `$XDG_CONFIG_HOME/mcp-server-batocera` | Where an optional `config.json` lives (`url`, `retroarch_port`, `timeout_s`) |
| `BATOCERA_NAME`          | the cabinet's address                  | How results name the cabinet                   |
| `BATOCERA_DRY_RUN`        | off                                    | Read the cabinet, change nothing (`serve --dry-run`) |
| `BATOCERA_LOG_UNREDACTED` | off                                    | Show LAN addresses in logs (`--no-redact`); secrets stay hidden |
| `BATOCERA_DEBUG`          | off                                    | Debug logging (`--debug`)                       |

`config.json` takes `url`, `retroarch_port`, `timeout_s`, `name` and `dry_run`; environment variables win.

### Trying it without the cabinet

```sh
mcp-server-batocera simulate --write-config /tmp/sim     # a fake cabinet: EmulationStation over HTTP, RetroArch over UDP
export BATOCERA_CONFIG_DIR=/tmp/sim                      # in another terminal
mcp-server-batocera doctor
mcp-server-batocera call find_games query=mario          # one tool call, exactly as the model makes it
mcp-server-batocera call launch_game system=snes game_id=<id from the result above>
mcp-server-batocera call --dry-run quit_game             # reports what it would send
```

The simulator has a small library across a RetroArch system and a standalone one (GameCube), so both
paths are there to try. The tests use the same simulator.

## Tools

| Tool              | What it does                                                                                  |
| ----------------- | --------------------------------------------------------------------------------------------- |
| `get_status`      | Reachable, EmulationStation version, the running game, busy (scraping/updating), RetroArch state |
| `list_systems`    | Systems with games: counts, played, favorites, most played                                    |
| `find_games`      | Search by name (punctuation-insensitive), or list favorites / most played / most time / recent |
| `get_game`        | Description, developer, publisher, rating, players, play count and time                        |
| `launch_game`     | Start a game by system + id; refuses while another game runs; confirms it started             |
| `quit_game`       | Quit the running game (**destructive**: unsaved progress is lost)                             |
| `show_message`    | Over the game (RetroArch), or a frontend notification or popup                                |
| `reload_games`    | Rescan game lists after adding ROMs                                                           |
| `pause_game`      | Pause or resume (RetroArch)                                                                   |
| `save_state`      | Save to the current slot (RetroArch)                                                          |
| `load_state`      | Load from the current slot (RetroArch, **destructive**)                                       |
| `take_screenshot` | Save a screenshot on the cabinet (RetroArch)                                                  |
| `reset_game`      | Reset the game (RetroArch, **destructive**)                                                   |

Play counts and play time come from EmulationStation itself (it records them in its game lists), so
"what have we played most?" works without Home Assistant.

Every tool that changes something returns the same structured result as the sibling servers:

```json
{ "cabinet": "Nick's Arcade", "outcome": "done", "detail": "Launched Galaga (mame).",
  "sent": ["POST /launch /userdata/roms/mame/galaga.zip"], "warnings": [] }
```

`outcome` is `done`, `unchanged` (already the case, so nothing was sent) or `dry_run`; `sent` is what
went to the cabinet, or in a dry run what would have. Failures are MCP error results with a message
saying what to fix.

## Safety design

- **Games are an allow-list.** `launch_game` takes a system and EmulationStation's game id. The server
  looks the id up in EmulationStation's own list and sends that entry's path; the model never supplies a
  path.
- **Endpoints that change the system aren't reachable:** `/quit`, `/restart`, `/addgames`,
  `/removegames`, metadata and media uploads, `/input` (simulated key presses) and `/storage/event`.
- **One game at a time, on purpose.** `launch_game` won't replace a running game; quitting is a separate
  tool marked destructive, so MCP clients ask before ending someone's game.
- **RetroArch gets constants only** (`PAUSE_TOGGLE`, `SAVE_STATE`, `LOAD_STATE`, `SCREENSHOT`, `RESET`),
  plus `SHOW_MSG` with validated single-line text.
- Every tool has a title and MCP annotations. Errors say what to do ("turn on public web access",
  "add `global.retroarch.network_cmd_enable=true`"), and logs go to stderr.

## Play tracking in Home Assistant (playtracker)

### What gets logged, and what doesn't

Batocera's screensaver (dim, black, random video, slideshow, suspend) **never starts a game**. The
random-video and slideshow screensavers *select* games to show their videos, which fires EmulationStation's
`game-selected` event; that's what makes the Pixelcade marquee follow the screensaver. playtracker only
logs `game-start`/`game-end`, which fire only when a game actually launches.

Two filters on top of that:
- **Screensaver launches.** With *Screensaver controls* on, pressing START on a screensaver video
  launches that game. playtracker recognizes this case: the launched game is the one the screensaver was
  showing, and the screensaver was active within the last 10 seconds. With `IGNORE_SCREENSAVER_LAUNCHES=yes`
  (the default) it logs nothing for that session.
- **Short sessions.** Sessions under `MIN_SESSION_SECONDS` (default 60) aren't logged.

### Install

1. In Home Assistant, create a user for the cabinet (*Settings > People > Users*, e.g. `batocera`). The
   Mosquitto add-on accepts Home Assistant users.
2. Copy the files to the cabinet:
   ```sh
   scp playtracker/playtracker.py root@192.168.1.20:/userdata/system/configs/emulationstation/scripts/
   scp playtracker/playtracker.conf.example root@192.168.1.20:/userdata/system/configs/playtracker.conf
   ssh root@192.168.1.20 'chmod +x /userdata/system/configs/emulationstation/scripts/playtracker.py;
                          chmod 600 /userdata/system/configs/playtracker.conf'
   ```
3. Edit `playtracker.conf` (broker host, user, password, device name), then restart EmulationStation or
   reboot. The *Nick's Arcade* device appears in Home Assistant under MQTT.

Scripts directly in `.../emulationstation/scripts/` get every event, with the event name first. This
doesn't interfere with Pixelcade's per-event scripts in the subfolders. To debug, read
`/tmp/playtracker/playtracker.log` on the cabinet.

### Entities

Home Assistant names the entities after the device, so with `DEVICE_NAME=Nick's Arcade` they come out
as below (check the device page for the exact IDs).

| Entity                                  | State                     | Attributes                                   |
| --------------------------------------- | ------------------------- | -------------------------------------------- |
| `sensor.nick_s_arcade_now_playing`       | game name, or `Idle`      | system, rom, started, source                 |
| `binary_sensor.nick_s_arcade_playing`    | on while a game runs      |                                              |
| `sensor.nick_s_arcade_now_playing_system`| system name               |                                              |
| `sensor.nick_s_arcade_last_session`      | last game played          | system, rom, started, ended, duration_s      |
| `sensor.nick_s_arcade_last_session_duration` | minutes               |                                              |
| `event.nick_s_arcade_game_session`       | fires `session_end`       | game, system, started, ended, duration_s     |
| availability                            | `batocera/<id>/status`    | online/offline (set on start, quit, shutdown, sleep) |

Each logged session is one `session_end` event, so it shows up in the logbook, history and automations.
Daily play time:

```yaml
sensor:
  - platform: history_stats
    name: Arcade play time today
    entity_id: binary_sensor.nick_s_arcade_playing
    state: "on"
    type: time
    start: "{{ today_at() }}"
    end: "{{ now() }}"
```

If the broker is unreachable (Home Assistant restarting, say), messages are queued in `/tmp/playtracker`
and sent with the next event. `/tmp` is cleared on reboot. If the cabinet loses power, `status` stays
`online` until it next boots, because the script doesn't keep a connection open for an MQTT last will.

## Verification status

Every Batocera behavior the code relies on is listed in `src/batocera_mcp/assumptions.py`, with its
source and how sure we are; the code cites each id where it depends on it, and
[HARDWARE_VALIDATION.md](HARDWARE_VALIDATION.md) has the step that confirms it. *high* means read
directly in the code that implements it; *medium* means read in source but dependent on settings,
timing or versions; *low* means our own guess. Nothing has been confirmed on hardware yet.

| Assumption | Confidence | Status |
|---|---|---|
| `B-ES-PUBLIC-ACCESS` | high | simulator-only |
| `B-ES-DURING-GAME` | medium | simulator-only |
| `B-ES-RUNNINGGAME` | high | simulator-only |
| `B-ES-LAUNCH` | high | simulator-only |
| `B-ES-EMUKILL` | medium | simulator-only |
| `B-ES-GAME-ID` | high | simulator-only |
| `B-ES-METADATA` | high | simulator-only |
| `B-ES-SYSTEMS` | high | simulator-only |
| `B-ES-THREADS` | medium | simulator-only |
| `B-ES-MESSAGES` | medium | simulator-only |
| `B-RA-ENABLE` | medium | simulator-only |
| `B-RA-STATUS` | medium | simulator-only |
| `B-RA-COMMANDS` | medium | simulator-only |
| `B-PT-SCRIPTS` | high | simulator-only |
| `B-PT-GAME-ARGS` | high | simulator-only |
| `B-PT-SCREENSAVER` | high | simulator-only |
| `B-PT-SCREENSAVER-START` | medium | simulator-only |
| `B-PT-TMP` | medium | simulator-only |
| `B-PT-HA-MQTT` | high | simulator-only |

## Not yet included

- Fixed-command SSH tools (CPU temperature, disk space, restart EmulationStation). These need SSH
  credentials, so they'd be opt-in.
- Adding ROMs (`/addgames`), or editing metadata and media.
- Choosing the save-state slot. RetroArch's network commands only step it up or down.

## Development

```sh
pip install -e ".[dev]"
pytest              # tools via an in-process MCP client against the simulator, faults, CLI, playtracker, stdio
ruff check . && ruff format --check .
mypy                # strict, src/ and playtracker/
```

## License

MIT. See [LICENSE](LICENSE).
