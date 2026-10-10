"""Shared fixtures. Tests talk to the wire-level fake cabinet (sim/fake_cabinet.py): real HTTP, real UDP."""

from __future__ import annotations

import pytest

from batocera_mcp import server
from batocera_mcp.config import Settings
from batocera_mcp.retroarch import RetroArchClient
from batocera_mcp.sim.fake_cabinet import FakeCabinet


# Async tests use anyio's pytest plugin (pytest.mark.anyio) rather than pytest-asyncio: the MCP SDK's
# in-process Client needs a fixture's setup and teardown in the same task, which anyio guarantees.
@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def cabinet():
    c = FakeCabinet()
    c.launch_delay_s = 0.05
    c.kill_delay_s = 0.05
    with c:
        yield c


@pytest.fixture
def settings(cabinet) -> Settings:
    return Settings(url=cabinet.url, timeout_s=2.0, retroarch_port=cabinet.ra_port, name="Test cabinet")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Never read the user's real config or environment; keep the waits short."""
    monkeypatch.setenv("BATOCERA_CONFIG_DIR", str(tmp_path))
    for var in ("BATOCERA_URL", "BATOCERA_TIMEOUT", "BATOCERA_RETROARCH_PORT", "BATOCERA_DRY_RUN", "BATOCERA_NAME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(server, "LAUNCH_TIMEOUT_S", 0.6)
    monkeypatch.setattr(server, "QUIT_TIMEOUT_S", 0.6)
    monkeypatch.setattr(server, "POLL_INTERVAL_S", 0.02)
    monkeypatch.setattr(RetroArchClient, "reply_timeout_s", 0.3)
