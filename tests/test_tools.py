"""MCP tools through a real in-process MCP client, against the wire-level fake cabinet.

This tests the contract the model actually sees: tools/list (names, titles, annotations, schemas),
argument validation, structured results, and what reaches the cabinet over HTTP and UDP.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re

import pytest
from mcp import Client

from batocera_mcp import server

pytestmark = pytest.mark.anyio

ALL_TOOLS = {
    "get_status",
    "list_systems",
    "find_games",
    "get_game",
    "launch_game",
    "quit_game",
    "show_message",
    "reload_games",
    "pause_game",
    "save_state",
    "load_state",
    "take_screenshot",
    "reset_game",
    "adjust_volume",
    "change_save_slot",
}
ACTIONS = {"launch_game", "quit_game", "show_message", "reload_games", "pause_game", "save_state", "load_state",
           "take_screenshot", "reset_game", "adjust_volume", "change_save_slot"}  # fmt: skip


@pytest.fixture
async def mcp_client(settings, monkeypatch):
    monkeypatch.setattr(server, "load_settings", lambda: settings)
    async with Client(server.mcp) as c:
        yield c


@pytest.fixture
async def dry_client(settings, monkeypatch):
    monkeypatch.setattr(server, "load_settings", lambda: dataclasses.replace(settings, dry_run=True))
    async with Client(server.mcp) as c:
        yield c


async def tools(c: Client) -> dict:
    return {t.name: t for t in (await c.list_tools()).tools}


def text(result) -> str:
    return result.content[0].text


async def call(c: Client, tool: str, **args):
    return await c.call_tool(tool, args)


async def act(c: Client, tool: str, **args) -> dict:
    """Call an action tool and return its ActionResult, failing on an error result."""
    r = await call(c, tool, **args)
    assert not r.is_error, text(r)
    return r.structured_content


async def ra_received(cabinet, n: int) -> list[str]:
    """Fire-and-forget datagrams can still be in flight when a tool returns."""
    for _ in range(100):
        if len(cabinet.ra_commands) >= n:
            break
        await asyncio.sleep(0.01)
    return cabinet.ra_commands


# --- tools/list ----------------------------------------------------------------
async def test_tool_names(mcp_client):
    assert set(await tools(mcp_client)) == ALL_TOOLS


async def test_every_tool_is_annotated(mcp_client):
    for t in (await tools(mcp_client)).values():
        assert t.title and t.description, t.name
        a = t.annotations
        assert a is not None and a.read_only_hint is not None and a.open_world_hint is False, t.name
        if not a.read_only_hint:
            assert a.destructive_hint is not None and a.idempotent_hint is not None, t.name


async def test_only_progress_losing_tools_are_destructive(mcp_client):
    destructive = {n for n, t in (await tools(mcp_client)).items() if t.annotations.destructive_hint}
    assert destructive == {"quit_game", "load_state", "reset_game"}


async def test_every_action_returns_an_action_result(mcp_client):
    t = await tools(mcp_client)
    for name in ACTIONS:
        assert set(t[name].output_schema["required"]) == {"cabinet", "outcome", "detail", "sent", "warnings"}, name
        assert set(t[name].output_schema["properties"]["outcome"]["enum"]) == {"done", "unchanged", "dry_run"}


async def test_launch_takes_an_id_not_a_path(mcp_client):
    props = (await tools(mcp_client))["launch_game"].input_schema["properties"]
    assert set(props) == {"system", "game_id"}
    assert props["game_id"]["minLength"] == props["game_id"]["maxLength"] == 32


@pytest.mark.parametrize("name", ["pause_game", "save_state", "load_state", "take_screenshot", "reset_game"])
async def test_retroarch_tools_take_no_free_text(mcp_client, name):
    props = (await tools(mcp_client))[name].input_schema.get("properties", {})
    assert all(p.get("type") != "string" for p in props.values())


# --- Reading -------------------------------------------------------------------
async def test_status_idle(mcp_client):
    s = (await call(mcp_client, "get_status")).structured_content
    assert (s["reachable"], s["es_version"], s["running_game"], s["busy"], s["retroarch"], s["dry_run"]) == (
        True, "43.1-sim", None, False, None, False
    )  # fmt: skip


async def test_status_while_playing(mcp_client, cabinet):
    # ASSUMPTION B-ES-DURING-GAME: the API answers while the emulator runs.
    cabinet.play("snes")
    s = (await call(mcp_client, "get_status")).structured_content
    assert s["running_game"]["name"] == "Super Mario World"
    assert s["retroarch"] == {"state": "playing", "system": "snes", "game": "Super Mario World (USA)"}


async def test_status_busy(mcp_client, cabinet):
    cabinet.busy = True
    assert (await call(mcp_client, "get_status")).structured_content["busy"] is True


async def test_status_explains_403(mcp_client, cabinet):
    # ASSUMPTION B-ES-PUBLIC-ACCESS
    cabinet.forbidden = True
    s = (await call(mcp_client, "get_status")).structured_content
    assert s["reachable"] is False and "Enable public web access" in s["error"]


async def test_list_systems_hides_collections_and_empty(mcp_client):
    # ASSUMPTION B-ES-SYSTEMS: favorites (a collection) and n64 (no games) are left out.
    r = (await call(mcp_client, "list_systems")).structured_content
    assert [s["name"] for s in r["systems"]] == ["gamecube", "mame", "snes"]
    assert r["total_games"] == 6


async def test_find_games_by_words_ignoring_punctuation(mcp_client, cabinet):
    r = (await call(mcp_client, "find_games", query="pac man")).structured_content
    assert [g["name"] for g in r["games"]] == ["Pac-Man (Midway)"]
    assert r["games"][0]["id"] == cabinet.game("mame")["id"]


async def test_find_games_relevance_order(mcp_client):
    r = (await call(mcp_client, "find_games", query="super")).structured_content
    assert [g["name"] for g in r["games"]] == ["Super Mario World", "Super Metroid", "Super Smash Bros. Melee"]


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("find_games", {"system": "imageviewer"}),
        ("get_game", {"system": "ImageViewer", "game_id": "0" * 32}),
        ("launch_game", {"system": "imageviewer", "game_id": "0" * 32}),
    ],
)
async def test_picture_viewer_is_not_a_game_system(mcp_client, cabinet, tool, args):
    # ASSUMPTION B-ES-SYSTEMS: imageviewer is flagged gamesystem "true", but holds pictures.
    r = await call(mcp_client, tool, **args)
    assert r.is_error and "picture viewer, not a game system" in text(r)
    assert cabinet.calls("/systems/imageviewer/games") == []


async def test_most_played_across_systems(mcp_client):
    r = (await call(mcp_client, "find_games", sort="most_played", limit=2)).structured_content
    assert [g["name"] for g in r["games"]] == ["Pac-Man (Midway)", "Super Mario World"]
    assert r["truncated"] is True


async def test_recently_played_and_favorites(mcp_client):
    # ASSUMPTION B-ES-METADATA: lastplayed 20261009T220000 becomes an ISO time
    r = (await call(mcp_client, "find_games", sort="recently_played")).structured_content
    assert r["games"][0]["last_played"] == "2026-10-09T22:00:00"
    r = (await call(mcp_client, "find_games", favorites_only=True)).structured_content
    assert [g["name"] for g in r["games"]] == ["Super Mario World"]


ALL_NAMES = [
    "Galaga",
    "Pac-Man (Midway)",
    "Street Fighter II Turbo",
    "Super Mario World",
    "Super Metroid",
    "Super Smash Bros. Melee",
]


async def test_find_games_schema_has_paging_and_filters(mcp_client):
    props = (await tools(mcp_client))["find_games"].input_schema["properties"]
    assert props["offset"]["minimum"] == 0 and props["offset"]["default"] == 0
    assert props["genre"]["anyOf"][0]["maxLength"] == 50
    assert props["players"]["anyOf"][0]["maxLength"] == 10
    assert "random" in props["sort"]["enum"]


@pytest.mark.parametrize(
    ("offset", "limit", "names", "truncated"),
    [
        (0, 2, ALL_NAMES[0:2], True),
        (2, 2, ALL_NAMES[2:4], True),
        (4, 2, ALL_NAMES[4:6], False),  # exactly reaches the end
        (5, 10, ALL_NAMES[5:], False),
        (6, 2, [], False),  # past the end: empty page, matches still counted
        (50, 5, [], False),
    ],
)
async def test_find_games_offset_pages(mcp_client, offset, limit, names, truncated):
    r = (await call(mcp_client, "find_games", sort="name", offset=offset, limit=limit)).structured_content
    assert [g["name"] for g in r["games"]] == names
    assert r["matches"] == 6
    assert r["truncated"] is truncated


async def test_find_games_pages_cover_every_match_once(mcp_client):
    seen = []
    for offset in range(0, 6, 4):
        r = (await call(mcp_client, "find_games", sort="name", offset=offset, limit=4)).structured_content
        seen += [g["name"] for g in r["games"]]
    assert seen == ALL_NAMES


async def test_find_games_negative_offset_is_rejected(mcp_client):
    r = await call(mcp_client, "find_games", offset=-1)
    assert r.is_error


@pytest.mark.parametrize(
    ("genre", "names"),
    [
        ("Fighting", ["Street Fighter II Turbo"]),
        ("fight", ["Street Fighter II Turbo"]),  # substring, case-insensitive
        ("PLATFORM", ["Super Mario World"]),
        ("a", ["Super Metroid", "Super Mario World"]),  # Action, Platform; games with no genre skipped
        ("Puzzle", []),
    ],
)
async def test_find_games_by_genre(mcp_client, genre, names):
    r = (await call(mcp_client, "find_games", genre=genre, sort="name")).structured_content
    assert sorted(g["name"] for g in r["games"]) == sorted(names)
    assert all(g["genre"] is not None for g in r["games"])
    assert r["matches"] == len(names)


@pytest.mark.parametrize(
    ("players", "names"),
    [
        ("1-2", ["Galaga", "Pac-Man (Midway)", "Street Fighter II Turbo", "Super Mario World"]),
        ("2", ["Galaga", "Pac-Man (Midway)", "Street Fighter II Turbo", "Super Mario World"]),
        ("4", ["Super Smash Bros. Melee"]),
        ("1", ["Galaga", "Pac-Man (Midway)", "Street Fighter II Turbo", "Super Mario World",
               "Super Smash Bros. Melee"]),  # Super Metroid has no players field, so never matches
        ("8", []),
    ],
)  # fmt: skip
async def test_find_games_by_players(mcp_client, players, names):
    r = (await call(mcp_client, "find_games", players=players, sort="name")).structured_content
    assert [g["name"] for g in r["games"]] == names
    assert r["matches"] == len(names)


async def test_find_games_filters_combine(mcp_client):
    r = (await call(mcp_client, "find_games", genre="fighting", players="2", system="snes")).structured_content
    assert [g["name"] for g in r["games"]] == ["Street Fighter II Turbo"]
    r = (await call(mcp_client, "find_games", query="super", players="1-2")).structured_content
    assert [g["name"] for g in r["games"]] == ["Super Mario World"]
    r = (await call(mcp_client, "find_games", genre="platform", system="mame")).structured_content
    assert r["games"] == [] and r["matches"] == 0


@pytest.mark.parametrize(("arg", "value"), [("genre", "x" * 51), ("players", "1" * 11)])
async def test_find_games_filter_length_limits(mcp_client, arg, value):
    r = await call(mcp_client, "find_games", **{arg: value})
    assert r.is_error


async def test_find_games_random_returns_every_match(mcp_client):
    r = (await call(mcp_client, "find_games", sort="random")).structured_content
    assert sorted(g["name"] for g in r["games"]) == ALL_NAMES
    assert (r["matches"], r["truncated"]) == (6, False)


async def test_find_games_random_shuffles_before_slicing(mcp_client, monkeypatch):
    calls = []

    def fake_shuffle(seq):
        calls.append(len(seq))
        seq.reverse()

    monkeypatch.setattr(server.random, "shuffle", fake_shuffle)
    r = (await call(mcp_client, "find_games", sort="random", limit=2, offset=1)).structured_content
    # The whole match list is shuffled (here: reversed) and only then paged.
    assert calls == [6]
    assert len(r["games"]) == 2
    assert (r["matches"], r["truncated"]) == (6, True)


async def test_find_games_random_varies(mcp_client):
    orders = set()
    for _ in range(20):
        r = (await call(mcp_client, "find_games", sort="random")).structured_content
        orders.add(tuple(g["name"] for g in r["games"]))
    assert len(orders) > 1  # 6! orders; 20 identical draws has odds of about 1e-54


async def test_game_list_is_cached(mcp_client, cabinet):
    await call(mcp_client, "find_games", query="mario", system="snes")
    await call(mcp_client, "find_games", query="metroid", system="snes")
    assert len(cabinet.calls("/systems/snes/games")) == 1


async def test_get_game(mcp_client, cabinet):
    g = (await call(mcp_client, "get_game", system="snes", game_id=cabinet.game("snes")["id"])).structured_content
    assert g["description"] == "Mario's first SNES adventure."
    assert (g["release_date"], g["play_time_s"], g["players"], g["rating"]) == ("1990-11-21", 12600, "1-2", 0.9)


@pytest.mark.parametrize("system", ["../quit", "SNES/../x", "a b"])
async def test_bad_system_names_never_reach_es(mcp_client, cabinet, system):
    r = await call(mcp_client, "find_games", query="x", system=system)
    assert r.is_error and cabinet.requests == []


# --- Launch / quit ------------------------------------------------------------------
async def test_launch(mcp_client, cabinet):
    smw = cabinet.game("snes")
    r = await act(mcp_client, "launch_game", system="snes", game_id=smw["id"])
    assert (r["outcome"], r["detail"], r["cabinet"]) == ("done", "Launched Super Mario World (snes).", "Test cabinet")
    assert r["sent"] == [f"POST /launch {smw['path']}"]
    assert cabinet.calls("/launch") == [("POST", "/launch", smw["path"])]
    assert cabinet.running["id"] == smw["id"]


async def test_launch_same_game_is_unchanged(mcp_client, cabinet):
    cabinet.play("snes")
    r = await act(mcp_client, "launch_game", system="snes", game_id=cabinet.game("snes")["id"])
    assert (r["outcome"], r["sent"]) == ("unchanged", [])
    assert cabinet.calls("/launch") == []


async def test_launch_refuses_while_another_game_runs(mcp_client, cabinet):
    cabinet.play("mame")
    r = await call(mcp_client, "launch_game", system="snes", game_id=cabinet.game("snes")["id"])
    assert r.is_error and "Pac-Man (Midway) (mame) is running" in text(r)
    assert cabinet.calls("/launch") == [] and cabinet.calls("/emukill") == []


async def test_launch_unknown_id(mcp_client, cabinet):
    r = await call(mcp_client, "launch_game", system="snes", game_id="0" * 32)
    assert r.is_error and "No game with id" in text(r)
    assert cabinet.calls("/launch") == []


async def test_launch_that_doesnt_start_says_so(mcp_client, cabinet):
    # ASSUMPTION B-ES-LAUNCH: ES answers 200 anyway, so only /runningGame tells.
    cabinet.launch_fails = True
    r = await call(mcp_client, "launch_game", system="snes", game_id=cabinet.game("snes")["id"])
    assert r.is_error and "didn't start" in text(r)


async def test_quit(mcp_client, cabinet):
    assert (await act(mcp_client, "quit_game"))["outcome"] == "unchanged"
    cabinet.play("mame")
    r = await act(mcp_client, "quit_game")
    assert (r["outcome"], r["detail"], r["sent"]) == ("done", "Quit Pac-Man (Midway).", ["GET /emukill"])
    assert cabinet.running is None


async def test_quit_that_doesnt_stop_says_so(mcp_client, cabinet):
    # ASSUMPTION B-ES-EMUKILL
    cabinet.play("mame")
    cabinet.kill_fails = True
    r = await call(mcp_client, "quit_game")
    assert r.is_error and "still running" in text(r)


# --- Messages -----------------------------------------------------------------------
async def test_message_in_frontend(mcp_client, cabinet):
    # ASSUMPTION B-ES-MESSAGES
    assert (await act(mcp_client, "show_message", text="Dinner in 5!"))["detail"] == "Shown as a notification."
    assert (await act(mcp_client, "show_message", text="Bedtime", style="popup"))["detail"] == "Shown as a popup."
    assert (cabinet.notifications, cabinet.popups) == (["Dinner in 5!"], ["Bedtime"])


async def test_open_popup_holds_back_a_launch(mcp_client, cabinet):
    # ASSUMPTION B-ES-LAUNCH: seen on hardware, a launch waited until the popup was dismissed.
    r = await act(mcp_client, "show_message", text="Bedtime", style="popup")
    assert r["warnings"] and "holds back launch_game" in r["warnings"][0]
    g = cabinet.game("snes")
    r = await call(mcp_client, "launch_game", system="snes", game_id=g["id"])
    assert r.is_error and "A popup" in text(r) and cabinet.running is None
    cabinet.dismiss()
    for _ in range(50):
        if cabinet.running is not None:
            break
        await asyncio.sleep(0.02)
    assert cabinet.running is not None and cabinet.running["id"] == g["id"]


async def test_message_over_a_retroarch_game(mcp_client, cabinet):
    cabinet.play("snes")
    r = await act(mcp_client, "show_message", text="Dinner in   5!")
    assert (r["detail"], r["sent"]) == (
        "Shown over the running game (small, bottom-left, for a few seconds).",
        ["UDP SHOW_MSG Dinner in 5!"],
    )
    assert await ra_received(cabinet, 1) == ["SHOW_MSG Dinner in 5!"]
    assert cabinet.notifications == []


async def test_message_over_a_standalone_emulator_is_refused(mcp_client, cabinet):
    cabinet.play("gamecube")  # Dolphin: RetroArch isn't running
    r = await call(mcp_client, "show_message", text="hi")
    assert r.is_error and "wouldn't be visible" in text(r)


async def test_message_rejects_control_characters(mcp_client, cabinet):
    r = await call(mcp_client, "show_message", text="a\x1b[2Jb")
    assert r.is_error and cabinet.requests == []


async def test_reload_invalidates_cache(mcp_client, cabinet):
    await call(mcp_client, "find_games", query="mario", system="snes")
    await act(mcp_client, "reload_games")
    await call(mcp_client, "find_games", query="mario", system="snes")
    assert len(cabinet.calls("/systems/snes/games")) == 2 and cabinet.reloads == 1


# --- RetroArch ------------------------------------------------------------------------
async def test_pause_and_resume(mcp_client, cabinet):
    cabinet.play("snes")
    assert (await act(mcp_client, "pause_game", paused=True))["detail"] == "Paused."
    assert (await act(mcp_client, "pause_game", paused=True))["outcome"] == "unchanged"
    assert (await act(mcp_client, "pause_game", paused=False))["detail"] == "Resumed."
    assert cabinet.ra_commands == ["PAUSE_TOGGLE", "PAUSE_TOGGLE"]


async def test_state_screenshot_reset(mcp_client, cabinet):
    # ASSUMPTION B-RA-COMMANDS
    cabinet.play("snes")
    for tool in ("save_state", "load_state", "take_screenshot", "reset_game"):
        assert (await act(mcp_client, tool))["outcome"] == "done"
    assert await ra_received(cabinet, 4) == ["SAVE_STATE", "LOAD_STATE", "SCREENSHOT", "RESET"]


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"action": "up"}, "VOLUME_UP"),
        ({"action": "down"}, "VOLUME_DOWN"),
        ({"action": "mute"}, "MUTE"),
    ],
)
async def test_adjust_volume_sends_retroarch_command(mcp_client, cabinet, args, expected):
    # ASSUMPTION B-RA-AUDIO-SLOT: RetroArch's names; VOLUME_PLUS/MINUS would be ignored silently.
    cabinet.play("snes")
    r = await act(mcp_client, "adjust_volume", **args)
    assert r["outcome"] == "done" and r["sent"] == [f"UDP {expected}"]
    assert "Super Mario World (USA)" in r["detail"]
    assert await ra_received(cabinet, 1) == [expected]
    assert cabinet.ra_received == ["GET_STATUS", expected]  # checked a game runs, then sent the command


async def test_adjust_volume_changes_the_simulated_volume(mcp_client, cabinet):
    cabinet.play("snes")
    for action in ("up", "up", "down", "mute"):
        await act(mcp_client, "adjust_volume", action=action)
    assert await ra_received(cabinet, 4) == ["VOLUME_UP", "VOLUME_UP", "VOLUME_DOWN", "MUTE"]
    assert (cabinet.ra_volume_db, cabinet.ra_muted) == (0.5, True)
    await act(mcp_client, "adjust_volume", action="mute")  # a toggle: the second one unmutes
    await ra_received(cabinet, 5)
    assert cabinet.ra_muted is False


@pytest.mark.parametrize(("direction", "expected"), [("next", "STATE_SLOT_PLUS"), ("previous", "STATE_SLOT_MINUS")])
async def test_change_save_slot_sends_retroarch_command(mcp_client, cabinet, direction, expected):
    # ASSUMPTION B-RA-AUDIO-SLOT
    cabinet.play("snes")
    r = await act(mcp_client, "change_save_slot", direction=direction)
    assert r["outcome"] == "done" and r["sent"] == [f"UDP {expected}"]
    assert await ra_received(cabinet, 1) == [expected]
    assert cabinet.ra_received == ["GET_STATUS", expected]


async def test_save_slot_moves_and_save_state_follows(mcp_client, cabinet):
    cabinet.play("snes")
    await act(mcp_client, "change_save_slot", direction="next")
    await act(mcp_client, "change_save_slot", direction="next")
    await act(mcp_client, "save_state")
    assert await ra_received(cabinet, 3) == ["STATE_SLOT_PLUS", "STATE_SLOT_PLUS", "SAVE_STATE"]
    assert cabinet.ra_slot == 2
    for _ in range(4):  # down past 0 stops at Auto (-1)
        await act(mcp_client, "change_save_slot", direction="previous")
    await ra_received(cabinet, 7)
    assert cabinet.ra_slot == -1


@pytest.mark.parametrize(
    ("tool", "args"),
    [("adjust_volume", {"action": "up"}), ("adjust_volume", {"action": "mute"}),
     ("change_save_slot", {"direction": "next"})],
)  # fmt: skip
async def test_audio_and_slot_tools_need_a_game(mcp_client, cabinet, tool, args):
    # Nothing running at all: RetroArch isn't there to answer GET_STATUS.
    r = await call(mcp_client, tool, **args)
    assert r.is_error and "isn't answering" in text(r)
    # RetroArch open with no content.
    cabinet.play("snes")
    cabinet.ra_state = "CONTENTLESS"
    r = await call(mcp_client, tool, **args)
    assert r.is_error and "no game loaded" in text(r)
    # A standalone emulator (Dolphin) doesn't take network commands either.
    cabinet.play("gamecube")
    r = await call(mcp_client, tool, **args)
    assert r.is_error and "standalone emulators" in text(r)
    assert cabinet.ra_commands == []


async def test_audio_and_slot_tools_reject_other_values(mcp_client, cabinet):
    cabinet.play("snes")
    t = await tools(mcp_client)
    assert t["adjust_volume"].input_schema["properties"]["action"]["enum"] == ["up", "down", "mute"]
    assert t["change_save_slot"].input_schema["properties"]["direction"]["enum"] == ["next", "previous"]
    for tool, args in (("adjust_volume", {"action": "VOLUME_PLUS"}), ("change_save_slot", {"direction": "QUIT"})):
        r = await call(mcp_client, tool, **args)
        assert r.is_error, tool
    assert cabinet.ra_commands == []


async def test_retroarch_not_answering_explains_how_to_enable(mcp_client, cabinet):
    # ASSUMPTION B-RA-ENABLE
    cabinet.play("snes")
    cabinet.ra_answers = False
    r = await call(mcp_client, "save_state")
    assert r.is_error and "global.retroarch.network_cmd_enable=true" in text(r)
    assert cabinet.ra_commands == []


async def test_retroarch_without_a_game(mcp_client, cabinet):
    cabinet.play("snes")
    cabinet.ra_state = "CONTENTLESS"
    r = await call(mcp_client, "reset_game")
    assert r.is_error and "no game loaded" in text(r)


# --- Dry run ----------------------------------------------------------------------------
async def test_dry_run_reads_but_sends_nothing(dry_client, cabinet):
    assert (await call(dry_client, "get_status")).structured_content["dry_run"] is True
    smw = cabinet.game("snes")
    r = await act(dry_client, "launch_game", system="snes", game_id=smw["id"])
    assert r["outcome"] == "dry_run" and r["detail"] == "DRY RUN, nothing sent: would launch Super Mario World (snes)"
    assert r["sent"] == [f"POST /launch {smw['path']}"]  # what *would* have been sent
    await act(dry_client, "show_message", text="hi")
    await act(dry_client, "reload_games")
    cabinet.play("snes")
    for tool in ("quit_game", "save_state", "load_state", "take_screenshot", "reset_game"):
        assert (await act(dry_client, tool))["outcome"] == "dry_run", tool
    assert (await act(dry_client, "pause_game", paused=True))["outcome"] == "dry_run"
    for action in ("up", "down", "mute"):
        r = await act(dry_client, "adjust_volume", action=action)
        assert r["outcome"] == "dry_run" and r["detail"].startswith("DRY RUN, nothing sent: would "), action
    for direction, cmd in (("next", "STATE_SLOT_PLUS"), ("previous", "STATE_SLOT_MINUS")):
        r = await act(dry_client, "change_save_slot", direction=direction)
        assert (r["outcome"], r["sent"]) == ("dry_run", [f"UDP {cmd}"])
    changing = [
        r for r in cabinet.requests if r[1] in ("/launch", "/emukill", "/notify", "/messagebox", "/reloadgames")
    ]
    assert changing == [] and cabinet.ra_commands == [] and cabinet.running["id"] == smw["id"]
    assert (cabinet.ra_volume_db, cabinet.ra_muted, cabinet.ra_slot) == (0.0, False, 0)


async def test_dry_run_audio_and_slot_still_need_a_game(dry_client, cabinet):
    # Dry run still reads (GET_STATUS), so it reports the same error a real call would.
    r = await call(dry_client, "adjust_volume", action="up")
    assert r.is_error and "isn't answering" in text(r)
    r = await call(dry_client, "change_save_slot", direction="next")
    assert r.is_error and "isn't answering" in text(r)
    assert cabinet.ra_commands == []


# --- Rate limits --------------------------------------------------------------------------
# launch_game and quit_game share a bucket of 3 (one back every 5 s); reload_games has its own of 2 (one back every
# 20 s). Each test runs in well under 5 s, so no token comes back mid-test.
def rate_limited(r, what: str) -> bool:
    """Whether r is ratelimit.refusal()'s message for `what` (the SDK puts "Error executing tool ...: " before it)."""
    return r.is_error and bool(
        re.search(
            rf"Rate limit on Test cabinet: refusing to {re.escape(what)} \(try again in \d+s\)\. "
            r"This guards against runaway loops; if this is deliberate, wait and retry\.$",
            text(r),
        )
    )


async def test_launch_burst_is_refused(mcp_client, cabinet):
    smw = cabinet.game("snes")
    for _ in range(3):
        cabinet.running = None  # the game ends on its own, so each launch is a real one
        assert (await act(mcp_client, "launch_game", system="snes", game_id=smw["id"]))["outcome"] == "done"
    cabinet.running = None
    r = await call(mcp_client, "launch_game", system="snes", game_id=smw["id"])
    assert rate_limited(r, "launch Super Mario World"), text(r)
    assert len(cabinet.calls("/launch")) == 3 and cabinet.running is None


async def test_quit_burst_is_refused(mcp_client, cabinet):
    for _ in range(3):
        cabinet.play("mame")
        assert (await act(mcp_client, "quit_game"))["outcome"] == "done"
    cabinet.play("mame")
    r = await call(mcp_client, "quit_game")
    assert rate_limited(r, "quit Pac-Man (Midway)"), text(r)
    assert len(cabinet.calls("/emukill")) == 3 and cabinet.running is not None  # still playing


async def test_launch_and_quit_share_a_bucket(mcp_client, cabinet):
    # A launch/quit loop is the runaway this guards against; separate buckets would let it run twice as fast.
    smw = cabinet.game("snes")
    await act(mcp_client, "launch_game", system="snes", game_id=smw["id"])
    await act(mcp_client, "quit_game")
    await act(mcp_client, "launch_game", system="snes", game_id=smw["id"])
    r = await call(mcp_client, "quit_game")
    assert rate_limited(r, "quit Super Mario World"), text(r)
    assert cabinet.running["id"] == smw["id"]


async def test_reload_burst_is_refused(mcp_client, cabinet):
    for _ in range(2):
        assert (await act(mcp_client, "reload_games"))["outcome"] == "done"
    r = await call(mcp_client, "reload_games")
    assert rate_limited(r, "rescan the game lists"), text(r)
    assert cabinet.reloads == 2
    # Its own bucket: the launch/quit budget is untouched.
    assert server._launch_bucket.tokens == 3


async def test_dry_runs_spend_no_tokens(dry_client, cabinet):
    smw = cabinet.game("snes")
    for _ in range(5):
        assert (await act(dry_client, "launch_game", system="snes", game_id=smw["id"]))["outcome"] == "dry_run"
        assert (await act(dry_client, "reload_games"))["outcome"] == "dry_run"
    cabinet.play("snes")
    for _ in range(5):
        assert (await act(dry_client, "quit_game"))["outcome"] == "dry_run"
    assert (server._launch_bucket.tokens, server._reload_bucket.tokens) == (3, 2)


async def test_no_ops_and_refusals_spend_no_tokens(mcp_client, cabinet):
    smw = cabinet.game("snes")
    for _ in range(5):
        assert (await act(mcp_client, "quit_game"))["outcome"] == "unchanged"  # nothing running
        assert (await call(mcp_client, "launch_game", system="snes", game_id="0" * 32)).is_error  # unknown id
    cabinet.play("snes")
    for _ in range(5):
        r = await act(mcp_client, "launch_game", system="snes", game_id=smw["id"])
        assert r["outcome"] == "unchanged"  # already running
    cabinet.play("mame")
    for _ in range(5):
        r = await call(mcp_client, "launch_game", system="snes", game_id=smw["id"])
        assert r.is_error and "is running" in text(r)  # refused before the bucket
    assert server._launch_bucket.tokens == 3
    assert (await act(mcp_client, "quit_game"))["outcome"] == "done"  # the full budget is still there


async def test_each_session_starts_with_full_buckets(settings, monkeypatch, cabinet):
    # lifespan() rebuilds the buckets, so one session's spent tokens never carry into the next.
    monkeypatch.setattr(server, "load_settings", lambda: settings)
    for _ in range(2):
        async with Client(server.mcp) as c:
            for _ in range(2):
                assert (await act(c, "reload_games"))["outcome"] == "done"
            assert rate_limited(await call(c, "reload_games"), "rescan the game lists")
    assert cabinet.reloads == 4


# --- Faults -------------------------------------------------------------------------------
async def test_slow_cabinet_times_out_with_advice(mcp_client, cabinet):
    cabinet.slow_s = 2.5  # longer than the 2 s test timeout
    r = await call(mcp_client, "list_systems")
    assert r.is_error and "didn't answer within 2s" in text(r)


async def test_dropped_connection_is_an_error_not_a_crash(mcp_client, cabinet):
    cabinet.drop = True
    r = await call(mcp_client, "list_systems")
    assert r.is_error and "EmulationStation" in text(r)


async def test_unreachable_cabinet(settings, monkeypatch):
    import dataclasses as dc

    monkeypatch.setattr(server, "load_settings", lambda: dc.replace(settings, url="http://127.0.0.1:9"))
    async with Client(server.mcp) as c:
        s = (await c.call_tool("get_status", {})).structured_content
        assert s["reachable"] is False and "public web access" in s["error"]
        r = await c.call_tool("quit_game", {})
        assert r.is_error
