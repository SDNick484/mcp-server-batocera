# mcp-server-batocera

An [MCP](https://modelcontextprotocol.io) server for a **[Batocera](https://batocera.org)** retro-gaming
cabinet. It lets an MCP client such as Claude browse the game library, see what's playing, launch and
quit games, put messages on screen, and, while a RetroArch game runs, pause, save or load state, take
screenshots and reset.

The repo also includes **playtracker**, a small hook script for the cabinet that publishes what's being
played to MQTT with Home Assistant discovery. It doesn't log screensaver activity.

> **Status: early, partly tested on hardware.** Built from Batocera's EmulationStation and RetroArch sources
> and tested against a wire-level simulator; playtracker is also tested against a real Mosquitto broker.
> On a real cabinet (Batocera 43), every MCP tool has been checked: `doctor`, the library, launching and
> quitting, messages, RetroArch and dry run, and playtracker with Home Assistant (sessions, the short-session
> filter, broker outages, and ignoring the screensaver). See [HARDWARE_VALIDATION.md](HARDWARE_VALIDATION.md) and the
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

## Local only: stdio, on your LAN

This server is meant to run on your LAN, launched over **stdio** by Claude Code or Claude Desktop on a
machine that can reach the cabinet. Unlike its AV siblings (Onkyo, Shield TV, Harmony, Sofabaton, webOS),
it has no HTTP transport and isn't published through a Cloudflare Tunnel, so it isn't reachable from
claude.ai or the Claude mobile app. That's on purpose:

- **The cabinet's APIs have no authentication.** EmulationStation's web API and RetroArch's network
  commands trust anything that can reach them. A remotely reachable server that drives them would put
  launching and quitting games, and save-state loads, behind whatever protects the server, not the cabinet.
- **It's a single-room device.** Starting a game is only useful to someone standing at the cabinet.

So it takes the code it shares with its siblings from
[mcp-server-shared](https://github.com/SDNick484/mcp-server-shared) but excludes the `remote` module (the
Streamable HTTP transport behind Cloudflare Access) in [`shared.lock`](shared.lock). If you ever want remote
access, that's the place to start: drop the exclusion, add a `serve --http` mode like
[mcp-server-misterfpga](https://github.com/SDNick484/mcp-server-misterfpga)'s, and put a Cloudflare Access
application in front of it. Don't expose ports 1234 or 55355 directly.

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

### On Alpine (e.g. a Proxmox LXC)

`deploy/alpine/` installs it to run from the command line on an always-on Alpine machine on the
cabinet's LAN: a venv in `/opt/mcp-server-batocera`, a `mcp-batocera` system user, every setting in
`/etc/mcp-server-batocera/env`, and `mcp-server-batocera` on `PATH`, which runs as that user with those
settings. Nothing listens on a port, which keeps it [local only](#local-only-stdio-on-your-lan): you
run `doctor` and `call` over SSH. The layout matches the sibling servers' `deploy/alpine/`, so an OpenRC
service could be added later without moving anything.

```sh
git clone https://github.com/SDNick484/mcp-server-batocera.git && cd mcp-server-batocera
sh deploy/alpine/install.sh                                # as root; re-run to upgrade
vim /etc/mcp-server-batocera/env                           # BATOCERA_URL=<the cabinet's IP>
mcp-server-batocera doctor
mcp-server-batocera call find_games query=mario
```

A variable set in your shell beats the env file, so one-off overrides work as usual
(`BATOCERA_DRY_RUN=1 mcp-server-batocera call ...`).

**Both arcade servers at once.** `deploy/alpine/setup-arcade.sh` installs this server and
[mcp-server-pixelcade](https://github.com/SDNick484/mcp-server-pixelcade) from GitHub into
`/srv/src/`, points both at the cabinet, and runs both doctors. Run it as root on the Alpine machine,
or from elsewhere through the Proxmox host:

```sh
ssh root@<proxmox host> "pct exec <container id> -- env ARCADE_HOST=<cabinet IP> sh -s" < deploy/alpine/setup-arcade.sh
```

`ARCADE_REF` picks a branch, tag or commit (default `main`); re-running updates the code and keeps
the settings. CI runs `deploy/alpine/smoke-test.sh` in a stock `python:3.12-alpine` container: install,
upgrade, the test suite on musl, `doctor` and `call` through the command on `PATH` against the
simulator, and `setup-arcade.sh` itself.

## Tools

| Tool              | What it does                                                                                  |
| ----------------- | --------------------------------------------------------------------------------------------- |
| `get_status`      | Reachable, EmulationStation version, the running game, busy (scraping/updating), RetroArch state |
| `list_systems`    | Systems with games (not collections or the picture viewer): counts, played, favorites, most played |
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
{ "cabinet": "Game Room", "outcome": "done", "detail": "Launched Galaga (mame).",
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
`game-selected` event. (A Pixelcade marquee doesn't follow those: Pixelcade's own screensaver hook locks
it in attract mode until the screensaver ends.) playtracker only logs `game-start`/`game-end`, which fire
only when a game actually launches.

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
   reboot. A device named after `DEVICE_NAME` (*Batocera* unless you change it) appears in Home
   Assistant under MQTT.

Scripts directly in `.../emulationstation/scripts/` get every event, with the event name first. This
doesn't interfere with Pixelcade's per-event scripts in the subfolders. To debug, read
`/tmp/playtracker/playtracker.log` on the cabinet.

### Entities

Home Assistant names the entities after the device, so with the default `DEVICE_NAME=Batocera` they
come out as below. Rename the device and they follow: `DEVICE_NAME=Game Room Arcade` gives
`sensor.game_room_arcade_now_playing`, and so on. Check the device page for the exact IDs.

| Entity                                  | State                  | Attributes                                           |
| --------------------------------------- | ---------------------- | ---------------------------------------------------- |
| `sensor.batocera_now_playing`           | game name, or `Idle`   | system, rom, started, source                         |
| `binary_sensor.batocera_playing`        | on while a game runs   |                                                      |
| `sensor.batocera_now_playing_system`    | system name            |                                                      |
| `sensor.batocera_last_session`          | last game played       | system, rom, started, ended, duration_s              |
| `sensor.batocera_last_session_length`   | minutes                |                                                      |
| `event.batocera_game_session`           | fires `session_end`    | game, system, started, ended, duration_s             |
| availability                            | `batocera/<id>/status` | online/offline (set on start, quit, shutdown, sleep) |

Each logged session is one `session_end` event, so it shows up in the logbook, history and automations.
Daily play time:

```yaml
sensor:
  - platform: history_stats
    name: Arcade play time today
    entity_id: binary_sensor.batocera_playing
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
timing or versions; *low* means our own guess. Hardware results so far are from Batocera 43 (2026-10-21).

| Assumption | Confidence | Status |
|---|---|---|
| `B-ES-PUBLIC-ACCESS` | high | hardware-verified |
| `B-ES-DURING-GAME` | medium | hardware-verified |
| `B-ES-RUNNINGGAME` | high | hardware-verified |
| `B-ES-LAUNCH` | high | hardware-verified |
| `B-ES-EMUKILL` | medium | hardware-verified |
| `B-ES-GAME-ID` | high | hardware-verified |
| `B-ES-METADATA` | high | hardware-verified |
| `B-ES-SYSTEMS` | high | hardware-verified |
| `B-ES-THREADS` | medium | simulator-only |
| `B-ES-MESSAGES` | medium | hardware-verified |
| `B-RA-ENABLE` | medium | hardware-verified |
| `B-RA-STATUS` | medium | hardware-verified |
| `B-RA-COMMANDS` | medium | hardware-verified |
| `B-PT-SCRIPTS` | high | hardware-verified |
| `B-PT-GAME-ARGS` | high | hardware-verified |
| `B-PT-SCREENSAVER` | high | hardware-verified |
| `B-PT-SCREENSAVER-START` | medium | hardware-verified |
| `B-PT-TMP` | medium | simulator-only |
| `B-PT-HA-MQTT` | high | hardware-verified |

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
