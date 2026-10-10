"""MCP tools through a real in-process MCP client, against the wire-level fake cabinet.

This tests the contract the model actually sees: tools/list (names, titles, annotations, schemas),
argument validation, structured results, and what reaches the cabinet over HTTP and UDP.
"""

from __future__ import annotations

import asyncio
import dataclasses

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
}
ACTIONS = {"launch_game", "quit_game", "show_message", "reload_games", "pause_game", "save_state", "load_state",
           "take_screenshot", "reset_game"}  # fmt: skip


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


async def test_message_over_a_retroarch_game(mcp_client, cabinet):
    cabinet.play("snes")
    r = await act(mcp_client, "show_message", text="Dinner in   5!")
    assert (r["detail"], r["sent"]) == ("Shown over the running game.", ["UDP SHOW_MSG Dinner in 5!"])
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
    changing = [
        r for r in cabinet.requests if r[1] in ("/launch", "/emukill", "/notify", "/messagebox", "/reloadgames")
    ]
    assert changing == [] and cabinet.ra_commands == [] and cabinet.running["id"] == smw["id"]


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
