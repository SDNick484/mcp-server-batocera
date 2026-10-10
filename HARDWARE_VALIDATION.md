# Hardware validation (Batocera cabinet)

Nothing in this repo has run against a real cabinet yet. The server was built from Batocera's
EmulationStation and RetroArch sources and tested against a wire-level simulator
(`src/batocera_mcp/sim/fake_cabinet.py`: real HTTP, real UDP). playtracker was also tested against a
real Mosquitto broker. Each step below depends only on the ones before it and names the assumptions it
confirms (ids, claims and sources are in `src/batocera_mcp/assumptions.py`).

Plan on about an hour. `doctor` points at steps 1, 2, 3 and 5 by number, so keep the numbering if you
edit this file.

## What's new and unconfirmed

| Feature | Status | Step |
| --- | --- | --- |
| `doctor` (TCP, API, library, running game, RetroArch) | verified against simulator only | 1 |
| `list_systems`, `find_games`, `get_game` | verified against simulator only | 2 |
| `launch_game` (confirmed via /runningGame), `quit_game` | verified against simulator only | 3 |
| `show_message` (frontend notification, popup, over a RetroArch game) | verified against simulator only | 4 |
| RetroArch tools: pause, save/load state, screenshot, reset | verified against simulator only | 5 |
| Dry run and ActionResult | verified against simulator only | 6 |
| playtracker install, Home Assistant discovery | verified against simulator only | 7 |
| playtracker sessions and the short-session filter | verified against simulator only | 8 |
| playtracker ignores screensaver activity | verified against simulator only | 9 |
| Log redaction (shared logsafe.py) | verified against simulator only | 1 |

## Before you start

- The cabinet on, and its IP (192.168.1.20 at last check). SSH: `root` / `linux`.
- A machine on the same LAN with this repo:

```sh
git clone https://github.com/SDNick484/mcp-server-batocera.git && cd mcp-server-batocera
git checkout hardware-free
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q                          # expect: all passed
export BATOCERA_URL=192.168.1.20
mkdir -p captures
```

**Tool calls** use `mcp-server-batocera call <tool> key=value ...`. It runs the tool through the same
MCP layer the model uses and prints the structured result; `call tools` lists the tools. Add `--debug`
(and `2> captures/<name>.log`) to record the HTTP traffic.

To rehearse any step first: `mcp-server-batocera simulate --write-config /tmp/sim`, then
`export BATOCERA_CONFIG_DIR=/tmp/sim` in another terminal (and `unset BATOCERA_URL`).

## 1. Public web access and doctor

Confirms B-ES-PUBLIC-ACCESS.

1. Before changing anything: `mcp-server-batocera doctor`. Expect `x  tcp` (connection refused): the API
   only listens on 127.0.0.1. **Record** what you see.
2. On the cabinet: *Main Menu > System Settings > Developer > Enable public web access*. Reboot.
   **Record** whether it took effect without the reboot (try doctor before rebooting).
3. `mcp-server-batocera doctor`. Expect every layer `ok` and the EmulationStation version.
4. `mcp-server-batocera doctor --json > captures/doctor.json`. The address should read `x.x.x.20`
   (log redaction); `--no-redact` shows it in full.

## 2. Browsing the library

Confirms B-ES-SYSTEMS, B-ES-METADATA, B-ES-GAME-ID, B-ES-THREADS.

1. `curl -s $BATOCERA_URL:1234/systems > captures/systems.json` (or `http://192.168.1.20:1234/systems`).
   Check that collections show `"collection": "true"` and that values like `totalGames` are numbers.
2. `call list_systems`. Expect your real systems, without favorites or empty ones.
3. `curl -s http://192.168.1.20:1234/systems/mame/games > captures/mame-games.json`. **Record** one
   game's `lastplayed`, `gametime` and `playcount` as they appear (B-ES-METADATA).
4. `call find_games sort=most_played limit=5`, then `call find_games query="<a game you own>"`.
   Check that play counts and times look right against what you remember.
5. Note one game's id. Run `call reload_games`, wait for the rescan, then `call get_game system=<s>
   game_id=<id>`. The id should still work (B-ES-GAME-ID).
6. `time mcp-server-batocera call find_games query=zzz` (searches every system): **record** how long.
   B-ES-THREADS limits it to 2 requests at a time.

## 3. Launching and quitting

Confirms B-ES-LAUNCH, B-ES-RUNNINGGAME, B-ES-DURING-GAME, B-ES-EMUKILL.

1. From the frontend, with no game running: `curl -si http://192.168.1.20:1234/runningGame | head -1`.
   Expect `201` (B-ES-RUNNINGGAME). Same for `/isIdle`.
2. `call launch_game system=snes game_id=<id from find_games>` (a RetroArch system). Expect
   `"outcome": "done"` within a few seconds. **Record** how long until the game was on screen.
3. While it runs: `call get_status`. Expect `running_game` filled in (B-ES-DURING-GAME).
4. `call launch_game` with a *different* game: expect a refusal naming the running game.
5. `call quit_game`. Expect `done` and the frontend back (B-ES-EMUKILL).
6. Repeat 2-5 with a standalone emulator if you have one (GameCube/Dolphin, PS2/PCSX2). **Record**
   whether `quit_game` stops it cleanly.
7. Unknown path: `curl -si -X POST --data '/userdata/roms/snes/nope.sfc' http://192.168.1.20:1234/launch |
   head -1`. Expect `200` and nothing launching (B-ES-LAUNCH).

## 4. Messages

Confirms B-ES-MESSAGES.

1. In the frontend: `call show_message text="Hello from Claude"`. **Record** what the notification looks
   like and how long it stays.
2. `call show_message text="Popup test" style=popup`. Expect a dialog that waits for a button.
3. While a standalone-emulator game runs (if you have one): `call show_message text=hi`. Expect a
   refusal. Optionally `curl -X POST --data hi http://192.168.1.20:1234/notify` and **record** whether
   anything shows over the game.

## 5. RetroArch

Confirms B-RA-ENABLE, B-RA-STATUS, B-RA-COMMANDS.

1. With a RetroArch game running and nothing configured yet: `call get_status`. Expect
   `"retroarch": null`, and doctor's `x  retroarch` hint.
2. `ssh root@192.168.1.20`, add `global.retroarch.network_cmd_enable=true` to
   `/userdata/system/batocera.conf`, then quit and relaunch the game (B-RA-ENABLE).
3. `call get_status`. Expect `retroarch.state` `playing`, with the system and game. **Record** the raw
   reply: `printf GET_STATUS | nc -u -w1 192.168.1.20 55355` (B-RA-STATUS).
4. `call pause_game paused=true`, then `paused=false`. Watch the screen.
5. `call show_message text="Dinner in 5"`. Expect it over the game.
6. `call save_state`, play on a little, `call load_state`: expect to jump back. `call take_screenshot`,
   then check `/userdata/screenshots` over SSH. `call reset_game`. **Record** anything that differs
   (B-RA-COMMANDS).

## 6. Dry run

1. `export BATOCERA_DRY_RUN=1`. `call get_status` shows `"dry_run": true`.
2. `call launch_game ...`: expect `"outcome": "dry_run"` with the POST it would send, and nothing on
   screen. Try `quit_game` during a game: the game keeps running.
3. `unset BATOCERA_DRY_RUN`.

## 7. playtracker install

Confirms B-PT-SCRIPTS, B-PT-HA-MQTT, B-PT-TMP.

1. Create the Home Assistant user and copy the files as in the README's *Play tracking* section. Set
   `MIN_SESSION_SECONDS=60`.
2. Restart EmulationStation (*Quit > Restart EmulationStation*). Over SSH:
   `cat /tmp/playtracker/playtracker.log`. Expect `start -> 9 message(s)` and no `publish failed`
   (B-PT-SCRIPTS). If nothing is logged, check `ls -l` shows `x`, and `which python3`.
3. In Home Assistant: *Settings > Devices > MQTT*: a device with six entities (B-PT-HA-MQTT).
   **Record** the entity ids it chose.
4. Reboot the cabinet; the log should show the announcement again after boot (B-PT-TMP).

## 8. playtracker sessions

Confirms B-PT-GAME-ARGS.

1. Launch a game whose file name has spaces and parentheses, e.g. `Super Mario World (USA)`. In Home
   Assistant, *Now playing* shows the name and *Playing* turns on. **Record** the `rom` attribute: it
   should be the plain path, without backslashes (B-PT-GAME-ARGS).
2. Play for over a minute, quit. *Last played* and *Last session length* update; the logbook shows a
   *Game session* event.
3. Launch something and quit within 20 seconds: *Now playing* goes back to Idle, nothing is logged.
4. Stop the Mosquitto add-on, play a short session (over a minute), start Mosquitto again, then launch
   any game: the queued session arrives (the log says `flushed`).

## 9. Screensaver

Confirms B-PT-SCREENSAVER, B-PT-SCREENSAVER-START.

1. Set the screensaver to *random video* with a 1-minute delay, and wait. As videos change, the
   Pixelcade marquee may follow; Home Assistant's *Now playing* must stay Idle, and the playtracker log
   must show no `game-start` (B-PT-SCREENSAVER).
2. With *Screensaver controls* on, press START during a video. The game launches, and with
   `IGNORE_SCREENSAVER_LAUNCHES=yes` nothing reaches Home Assistant. **Record** the order of
   `screensaver-stop` and `game-start` in the log (B-PT-SCREENSAVER-START).
3. Quit, wait for the screensaver again, wake it with a button, then pick the same game from the list
   after 10+ seconds: that session *is* logged.

## 10. Recording results

For each assumption a step confirmed, set `status="hardware-verified"` in
`src/batocera_mcp/assumptions.py` with what you saw in `note`, and update its row in the README's
verification table (`pytest tests/test_assumptions.py` checks they match). For anything that came out
differently, use `"hardware-contradicted"`, fix the code and the simulator, and add a test. Update this
file's *What's new* table (`verified on hardware`) as you go.
