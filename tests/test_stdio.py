"""End to end: the installed entry point over stdio, pointed at a port nothing listens on."""

from __future__ import annotations

import os
import shutil
import sys

import pytest
from mcp import Client, StdioServerParameters

from .test_tools import ALL_TOOLS

pytestmark = pytest.mark.anyio


async def test_serve_over_stdio(tmp_path):
    exe = shutil.which("mcp-server-batocera", path=os.path.dirname(sys.executable))
    if exe is None:
        pytest.skip("entry point not installed (pip install -e .)")
    env = {**os.environ, "BATOCERA_CONFIG_DIR": str(tmp_path), "BATOCERA_URL": "http://127.0.0.1:9"}
    async with Client(StdioServerParameters(command=exe, args=[], env=env)) as c:
        assert {t.name for t in (await c.list_tools()).tools} == ALL_TOOLS
        status = (await c.call_tool("get_status", {})).structured_content
        assert status["reachable"] is False and "isn't answering" in status["error"]
