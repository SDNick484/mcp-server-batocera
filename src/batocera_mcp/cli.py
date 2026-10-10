"""Entry point: `mcp-server-batocera` (serve over stdio) and `mcp-server-batocera check`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import __version__
from .client import BatoceraError, EmulationStationClient, is_game_system
from .config import load_settings
from .retroarch import RetroArchClient


def _setup_logging() -> None:
    # stdout belongs to the MCP stdio transport; logs must go to stderr.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _cmd_check() -> int:
    """Check the API before an MCP client is involved, so setup problems are easy to see."""
    settings = load_settings()
    es = EmulationStationClient(settings)
    print(f"EmulationStation: {settings.url}")
    try:
        print(f"  version:        {await es.version() or '?'}")
        systems = [s for s in await es.systems() if is_game_system(s)]
        total = sum(int(s.get("totalGames", 0) or 0) for s in systems)
        print(f"  systems:        {len([s for s in systems if int(s.get('totalGames', 0) or 0) > 0])} with games")
        print(f"  games:          {total}")
        running = await es.running_game()
        label = f"{running.get('name')} ({running.get('systemName', '?')})" if running else "-"
        print(f"  running:        {label}")
    except BatoceraError as exc:
        print(f"  error:          {exc}")
        return 1
    finally:
        await es.aclose()
    ra = RetroArchClient(settings.host, settings.retroarch_port)
    status = await ra.status()
    if status:
        print(f"RetroArch:        {status['state']} {status['game'] or ''}".rstrip())
    else:
        print(f"RetroArch:        not answering on UDP {settings.retroarch_port} (normal unless a RetroArch game runs)")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="mcp-server-batocera", description=__doc__)
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the MCP server over stdio (default)")
    sub.add_parser("check", help="check that EmulationStation answers, and print what it reports")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args()

    if args.cmd == "check":
        sys.exit(asyncio.run(_cmd_check()))

    _setup_logging()
    from .server import mcp  # imported late: `check` doesn't need the server code

    mcp.run()
