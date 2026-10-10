"""MCP tools through a real in-process MCP client, against the fake EmulationStation and RetroArch."""

from __future__ import annotations

import asyncio

import pytest
from mcp import Client

from batocera_mcp import server
from batocera_mcp.client import EmulationStationClient
from batocera_mcp.retroarch import RetroArchClient

from .conftest import GAMES

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
SMW = GAMES["snes"][0]
PACMAN = GAMES["mame"][0]


@pytest.fixture
async def mcp_client(settings, fake_es, monkeypatch):
    monkeypatch.setattr(server, "load_settings", lambda: settings)
    monkeypatch.setattr(server, "EmulationStationClient", lambda s: EmulationStationClient(s, fake_es.transport))
    monkeypatch.setattr(RetroArchClient, "reply_timeout_s", 0.2)
    async with Client(server.mcp) as c:
        yield c


async def tools(c: Client) -> dict:
    return {t.name: t for t in (await c.list_tools()).tools}


def text(result) -> str:
    return result.content[0].text


async def call(c: Client, tool: str, **args):
    return await c.call_tool(tool, args)


async def received(fake_ra, n: int) -> list[str]:
    """Fire-and-forget datagrams can still be in flight when a tool returns; let the loop deliver them."""
    for _ in range(100):
        if len(fake_ra.commands) >= n:
            break
        await asyncio.sleep(0.01)
    return fake_ra.commands


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
    assert (s["reachable"], s["es_version"], s["running_game"], s["busy"], s["retroarch"]) == (
        True,
        "43.1",
        None,
        False,
        None,
    )


async def test_status_while_playing(mcp_client, fake_es):
    fake_es.running = SMW
    s = (await call(mcp_client, "get_status")).structured_content
    assert s["running_game"]["name"] == "Super Mario World"
    assert s["retroarch"] == {"state": "playing", "system": "snes", "game": "Super Mario World (USA)"}


async def test_status_explains_403(mcp_client, fake_es):
    fake_es.forbidden = True
    s = (await call(mcp_client, "get_status")).structured_content
    assert s["reachable"] is False and "Enable public web access" in s["error"]


async def test_list_systems_hides_collections_and_empty(mcp_client):
    r = (await call(mcp_client, "list_systems")).structured_content
    assert [s["name"] for s in r["systems"]] == ["mame", "snes"]
    assert r["total_games"] == 5


async def test_find_games_by_words_ignoring_punctuation(mcp_client):
    r = (await call(mcp_client, "find_games", query="pac man")).structured_content
    assert [g["name"] for g in r["games"]] == ["Pac-Man (Midway)"]
    assert r["games"][0]["id"] == PACMAN["id"]


async def test_find_games_relevance_order(mcp_client):
    r = (await call(mcp_client, "find_games", query="super")).structured_content
    assert [g["name"] for g in r["games"]] == ["Super Mario World", "Super Metroid"]


async def test_most_played_across_systems(mcp_client):
    r = (await call(mcp_client, "find_games", sort="most_played", limit=2)).structured_content
    assert [g["name"] for g in r["games"]] == ["Pac-Man (Midway)", "Super Mario World"]
    assert r["truncated"] is True


async def test_recently_played_and_favorites(mcp_client):
    r = (await call(mcp_client, "find_games", sort="recently_played")).structured_content
    assert r["games"][0]["last_played"] == "2026-10-09T22:00:00"
    r = (await call(mcp_client, "find_games", favorites_only=True)).structured_content
    assert [g["name"] for g in r["games"]] == ["Super Mario World"]


async def test_game_list_is_cached(mcp_client, fake_es):
    await call(mcp_client, "find_games", query="mario", system="snes")
    await call(mcp_client, "find_games", query="metroid", system="snes")
    assert len(fake_es.calls("/systems/snes/games")) == 1


async def test_get_game(mcp_client):
    g = (await call(mcp_client, "get_game", system="snes", game_id=SMW["id"])).structured_content
    assert g["description"] == "Mario's first SNES adventure."
    assert (g["release_date"], g["play_time_s"], g["players"]) == ("1990-11-21", 12600, "1-2")


@pytest.mark.parametrize("system", ["../quit", "SNES/../x", "a b"])
async def test_bad_system_names_never_reach_es(mcp_client, fake_es, system):
    r = await call(mcp_client, "find_games", query="x", system=system)
    assert r.is_error and fake_es.requests == []


# --- Launch / quit ------------------------------------------------------------------
async def test_launch(mcp_client, fake_es):
    r = await call(mcp_client, "launch_game", system="snes", game_id=SMW["id"])
    assert text(r) == "Launched Super Mario World (snes)."
    assert fake_es.calls("/launch") == [("POST", "/launch", SMW["path"])]


async def test_launch_same_game_is_a_no_op(mcp_client, fake_es):
    fake_es.running = SMW
    assert text(await call(mcp_client, "launch_game", system="snes", game_id=SMW["id"])).endswith("already running.")
    assert fake_es.calls("/launch") == []


async def test_launch_refuses_while_another_game_runs(mcp_client, fake_es):
    fake_es.running = PACMAN
    r = await call(mcp_client, "launch_game", system="snes", game_id=SMW["id"])
    assert r.is_error and "Pac-Man (Midway) (mame) is running" in text(r)
    assert fake_es.calls("/launch") == [] and fake_es.calls("/emukill") == []


async def test_launch_unknown_id(mcp_client, fake_es):
    r = await call(mcp_client, "launch_game", system="snes", game_id="0" * 32)
    assert r.is_error and "No game with id" in text(r)
    assert fake_es.calls("/launch") == []


async def test_launch_that_doesnt_start_says_so(mcp_client, fake_es):
    fake_es.launch_works = False
    r = await call(mcp_client, "launch_game", system="snes", game_id=SMW["id"])
    assert r.is_error and "didn't start" in text(r)


async def test_quit(mcp_client, fake_es):
    assert text(await call(mcp_client, "quit_game")) == "No game is running."
    fake_es.running = PACMAN
    assert text(await call(mcp_client, "quit_game")) == "Quit Pac-Man (Midway)."
    fake_es.running = PACMAN
    fake_es.kill_works = False
    r = await call(mcp_client, "quit_game")
    assert r.is_error and "still running" in text(r)


# --- Messages -----------------------------------------------------------------------
async def test_message_in_frontend(mcp_client, fake_es):
    assert text(await call(mcp_client, "show_message", text="Dinner in 5!")) == "Shown as a notification."
    assert text(await call(mcp_client, "show_message", text="Bedtime", style="popup")) == "Shown as a popup."
    assert fake_es.calls("/notify")[0][2] == "Dinner in 5!"
    assert fake_es.calls("/messagebox")[0][2] == "Bedtime"


async def test_message_over_a_retroarch_game(mcp_client, fake_es, fake_ra):
    fake_es.running = SMW
    r = await call(mcp_client, "show_message", text="Dinner in   5!")
    assert text(r) == "Shown over the running game."
    assert await received(fake_ra, 1) == ["SHOW_MSG Dinner in 5!"]
    assert fake_es.calls("/notify") == []


async def test_message_over_a_standalone_emulator_is_refused(mcp_client, fake_es, fake_ra):
    fake_es.running = SMW
    fake_ra.state = None  # RetroArch doesn't answer
    r = await call(mcp_client, "show_message", text="hi")
    assert r.is_error and "wouldn't be visible" in text(r)


async def test_message_rejects_control_characters(mcp_client, fake_es):
    r = await call(mcp_client, "show_message", text="a\x1b[2Jb")
    assert r.is_error and fake_es.requests == []


async def test_reload_invalidates_cache(mcp_client, fake_es):
    await call(mcp_client, "find_games", query="mario", system="snes")
    await call(mcp_client, "reload_games")
    await call(mcp_client, "find_games", query="mario", system="snes")
    assert len(fake_es.calls("/systems/snes/games")) == 2


# --- RetroArch ------------------------------------------------------------------------
async def test_pause_and_resume(mcp_client, fake_ra):
    assert text(await call(mcp_client, "pause_game", paused=True)) == "Paused."
    assert text(await call(mcp_client, "pause_game", paused=True)) == "Already paused."
    assert text(await call(mcp_client, "pause_game", paused=False)) == "Resumed."
    assert fake_ra.commands == ["PAUSE_TOGGLE", "PAUSE_TOGGLE"]


async def test_state_screenshot_reset(mcp_client, fake_ra):
    for tool in ("save_state", "load_state", "take_screenshot", "reset_game"):
        assert not (await call(mcp_client, tool)).is_error
    assert await received(fake_ra, 4) == ["SAVE_STATE", "LOAD_STATE", "SCREENSHOT", "RESET"]


async def test_retroarch_not_answering_explains_how_to_enable(mcp_client, fake_ra):
    fake_ra.state = None
    r = await call(mcp_client, "save_state")
    assert r.is_error and "global.retroarch.network_cmd_enable=true" in text(r)
    assert fake_ra.commands == []


async def test_retroarch_without_a_game(mcp_client, fake_ra):
    fake_ra.state = "CONTENTLESS"
    r = await call(mcp_client, "reset_game")
    assert r.is_error and "no game loaded" in text(r)
