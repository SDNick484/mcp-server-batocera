"""Config validators, ES value parsing, and RetroArch status parsing."""

from __future__ import annotations

import asyncio
import json

import anyio
import httpx
import pytest

from batocera_mcp import config as cfg
from batocera_mcp import server
from batocera_mcp.client import BatoceraError, EmulationStationClient, es_time, to_game
from batocera_mcp.retroarch import parse_status


@pytest.mark.parametrize(("value", "expected"), [("SNES", "snes"), (" mame ", "mame"), ("atari2600", "atari2600")])
def test_system_name(value, expected):
    assert cfg.system_name(value) == expected


@pytest.mark.parametrize("value", ["", "../x", "a/b", "sn es", "..", "x" * 41])
def test_system_name_refuses(value):
    with pytest.raises(cfg.InvalidValue):
        cfg.system_name(value)


def test_game_id():
    assert cfg.game_id("A" * 32) == "a" * 32
    for bad in ("a" * 31, "g" * 32, "/userdata/roms/snes/x.sfc"):
        with pytest.raises(cfg.InvalidValue):
            cfg.game_id(bad)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("20261009T201500", "2026-10-09T20:15:00"),
        ("19901121T000000", "1990-11-21"),
        ("19700101T000000", None),
        ("", None),
        ("not a date", None),
    ],
)
def test_es_time(raw, expected):
    assert es_time(raw) == expected


def test_to_game_tolerates_missing_and_odd_values():
    g = to_game({"id": "x", "name": "N", "systemName": "s", "playcount": "", "gametime": "abc"})
    assert (g["play_count"], g["play_time_s"], g["favorite"], g["genre"]) == (0, 0, False, None)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (
            "GET_STATUS PLAYING snes,Super Mario World (USA),crc32=b19ed489",
            ("playing", "snes", "Super Mario World (USA)"),
        ),
        (
            "GET_STATUS PAUSED mame,Street Fighter II, The World Warrior,crc32=1",
            ("paused", "mame", "Street Fighter II, The World Warrior"),
        ),
        ("GET_STATUS CONTENTLESS", ("contentless", None, None)),
    ],
)
def test_parse_status(reply, expected):
    s = parse_status(reply)
    assert s is not None and (s["state"], s["system"], s["game"]) == expected


@pytest.mark.parametrize("reply", ["", "VERSION 1.19.1", "GET_STATUS WHATEVER x"])
def test_parse_status_rejects(reply):
    assert parse_status(reply) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("batocera.local", "http://batocera.local:1234"),
        ("192.168.1.20", "http://192.168.1.20:1234"),
        ("http://10.0.0.2:8000/", "http://10.0.0.2:8000"),
    ],
)
def test_normalize_url(raw, expected):
    assert cfg.normalize_url(raw) == expected


def test_settings_from_env_and_file(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"url": "10.0.0.5", "retroarch_port": 55400}))
    s = cfg.load_settings()
    assert (s.url, s.host, s.retroarch_port) == ("http://10.0.0.5:1234", "10.0.0.5", 55400)
    monkeypatch.setenv("BATOCERA_URL", "192.168.1.20")
    assert cfg.load_settings().url == "http://192.168.1.20:1234"


@pytest.mark.anyio
async def test_connection_refused_mentions_public_access():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 111] Connection refused", request=request)

    c = EmulationStationClient(cfg.Settings(url="http://10.0.0.9:1234"), httpx.MockTransport(boom))
    try:
        with pytest.raises(BatoceraError, match="public web access"):
            await c.version()
    finally:
        await c.aclose()


class SlowES:
    """A MockTransport handler that holds each request open briefly and records how many overlapped."""

    def __init__(self, systems: list[str], delay_s: float = 0.05) -> None:
        self.systems = systems
        self.delay_s = delay_s
        self.in_flight = 0
        self.peak = 0
        self.game_fetches: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/systems":
            body = [{"name": s, "fullname": s, "gamesystem": "true", "totalGames": "1"} for s in self.systems]
            return httpx.Response(200, text=json.dumps(body))
        system = path.split("/")[2]
        self.game_fetches.append(system)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await anyio.sleep(self.delay_s)
        finally:
            self.in_flight -= 1
        game = {"id": f"{system}-1", "name": f"{system} game", "systemName": system}
        return httpx.Response(200, text=json.dumps([game]))


def slow_client(handler: SlowES) -> EmulationStationClient:
    return EmulationStationClient(cfg.Settings(url="http://10.0.0.9:1234"), httpx.MockTransport(handler))


@pytest.mark.anyio
async def test_games_fetches_different_systems_concurrently():
    es = SlowES(["snes", "nes", "mame"])
    c = slow_client(es)
    try:
        results = await asyncio.gather(*(c.games(s) for s in es.systems))
    finally:
        await c.aclose()
    assert [list(r) for r in results] == [["snes-1"], ["nes-1"], ["mame-1"]]
    # The cache lock isn't held across the request, so all three were on the wire at once.
    assert es.peak == 3


@pytest.mark.anyio
async def test_games_shares_one_fetch_per_system():
    es = SlowES(["snes"])
    c = slow_client(es)
    try:
        results = await asyncio.gather(*(c.games("snes") for _ in range(5)))
    finally:
        await c.aclose()
    assert es.game_fetches == ["snes"]
    assert all(r is results[0] for r in results)


@pytest.mark.anyio
async def test_invalidate_mid_fetch_drops_the_stale_list():
    es = SlowES(["snes"])
    c = slow_client(es)
    try:
        fetch = asyncio.create_task(c.games("snes"))
        await anyio.sleep(es.delay_s / 2)  # the request is in flight
        c.invalidate()
        await fetch
        assert c._games == {}
        await c.games("snes")
        assert es.game_fetches == ["snes", "snes"]
    finally:
        await c.aclose()


@pytest.mark.anyio
async def test_find_games_loads_two_systems_at_a_time(monkeypatch):
    es = SlowES(["snes", "nes", "mame", "megadrive", "n64"])
    c = slow_client(es)
    monkeypatch.setattr(server, "_es", c)
    try:
        r = await server.find_games(query="game", sort="name")
    finally:
        await c.aclose()
    assert r["matches"] == 5
    assert sorted(es.game_fetches) == sorted(es.systems)
    assert es.peak == 2


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
