"""mcp-server-batocera: an MCP server for a Batocera cabinet.

Commands:
  serve      run the MCP server: stdio (the default), or --http for a long-lived service
             behind Cloudflare Access (remote.py)
  doctor     check every layer between here and the cabinet (`check` is an alias)
  simulate   run a fake cabinet (EmulationStation + RetroArch) to try things without one
  call       call one tool exactly as the model would, and print the result
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
from pathlib import Path

from . import __version__, remote
from .config import flag, load_settings
from .logsafe import setup_logging


def _setup_logging(args: argparse.Namespace) -> None:
    """stderr only (stdout belongs to the MCP stdio transport), redacted: LAN addresses are masked
    unless --no-redact / BATOCERA_LOG_UNREDACTED=1; tokens and passwords always are."""
    debug = getattr(args, "debug", False) or flag(os.environ.get("BATOCERA_DEBUG"))
    # The server logs each request at INFO; for one-shot commands that's noise next to their output.
    quiet = args.cmd in ("doctor", "check", "call", "simulate")
    level = logging.DEBUG if debug else (logging.WARNING if quiet else logging.INFO)
    setup_logging(level, redacted=not _unredacted(args))
    if not debug:
        logging.getLogger("httpx").setLevel(logging.WARNING)


def _unredacted(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "no_redact", False)) or flag(os.environ.get("BATOCERA_LOG_UNREDACTED"))


async def _cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import render, run_doctor, to_json

    report = await run_doctor(load_settings(), timeout=args.timeout)
    redacted = not _unredacted(args)
    print(to_json(report, redacted) if args.json else render(report, redacted, verbose=args.verbose))
    return 0 if report.ok else 1


async def _cmd_simulate(args: argparse.Namespace) -> int:
    from .sim.fake_cabinet import FakeCabinet

    cabinet = FakeCabinet(es_port=args.port, ra_port=args.retroarch_port).start()
    print(f"Simulated cabinet: EmulationStation {cabinet.url}, RetroArch udp://127.0.0.1:{cabinet.ra_port}")
    for system, games in cabinet.library.items():
        print(f"  {system:<10} {', '.join(g['name'] for g in games)}")
    if args.write_config:
        d = Path(args.write_config)
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(
            json.dumps({"url": cabinet.url, "retroarch_port": cabinet.ra_port, "name": "Simulated cabinet"}, indent=2)
        )
        print(f"\nWrote {d / 'config.json'}. In another terminal:\n  export BATOCERA_CONFIG_DIR={d}")
    else:
        print(f"\nIn another terminal:\n  export BATOCERA_URL={cabinet.url} BATOCERA_RETROARCH_PORT={cabinet.ra_port}")
    print("  mcp-server-batocera doctor\n  mcp-server-batocera call find_games query=mario\n\nCtrl+C to stop.")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        cabinet.stop()


def _parse_args(raw_args: list[str]) -> dict[str, object] | None:
    out: dict[str, object] = {}
    for pair in raw_args:
        key, sep, raw = pair.partition("=")
        if not sep:
            print(f"arguments are key=value, got {pair!r}", file=sys.stderr)
            return None
        try:
            out[key] = json.loads(raw)  # paused=true -> True, limit=5 -> 5
        except json.JSONDecodeError:
            out[key] = raw  # query=mario -> a string
    return out


async def _cmd_call(tool: str, raw_args: list[str]) -> int:
    """One tool call through the real MCP layer, as the model would make it."""
    from mcp import Client

    from .server import mcp

    tool_args = _parse_args(raw_args)
    if tool_args is None:
        return 2
    async with Client(mcp) as c:
        if tool == "tools":
            for t in (await c.list_tools()).tools:
                print(f"{t.name:<16} {(t.description or '').splitlines()[0]}")
            return 0
        result = await c.call_tool(tool, tool_args)
    if result.is_error:
        print(" ".join(getattr(part, "text", "") for part in result.content) or "error", file=sys.stderr)
        return 1
    body = result.structured_content
    if isinstance(body, dict) and set(body) == {"result"}:
        body = body["result"]
    print(json.dumps(body, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp-server-batocera", description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--debug", action="store_true", help="debug logging (BATOCERA_DEBUG=1)")
    common.add_argument(
        "--no-redact",
        action="store_true",
        help="show LAN addresses in logs and doctor output (BATOCERA_LOG_UNREDACTED=1); secrets stay hidden",
    )
    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", parents=[common], help="run the MCP server (stdio; --http for a service)")
    serve.add_argument("--dry-run", action="store_true", help="read the cabinet, change nothing (BATOCERA_DRY_RUN=1)")
    remote.add_http_arguments(serve, default_port=8717, default_path="/batocera/mcp")
    for name in ("doctor", "check"):
        doc = sub.add_parser(name, parents=[common], help="check every layer between here and the cabinet")
        doc.add_argument("--json", action="store_true", help="the full report as JSON (all assumption lists)")
        doc.add_argument("-v", "--verbose", action="store_true", help="list each not-yet-confirmed assumption")
        doc.add_argument("--timeout", type=float, default=5.0)
    sim = sub.add_parser("simulate", parents=[common], help="run a fake cabinet to try things without one")
    sim.add_argument("--port", type=int, default=0, help="EmulationStation port (default: any free one)")
    sim.add_argument("--retroarch-port", type=int, default=0, help="RetroArch UDP port (default: any free one)")
    sim.add_argument("--write-config", metavar="DIR", help="write a config.json pointing at the fake to DIR")
    cal = sub.add_parser("call", parents=[common], help="call one tool as the model would; `call tools` lists them")
    cal.add_argument("tool")
    cal.add_argument("args", nargs="*", help="key=value (values are JSON when they parse: limit=5 paused=true)")
    cal.add_argument("--dry-run", action="store_true", help="change nothing")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args)
    if getattr(args, "dry_run", False):
        os.environ["BATOCERA_DRY_RUN"] = "1"  # read by load_settings() in the server's lifespan

    if args.cmd in ("doctor", "check"):
        sys.exit(asyncio.run(_cmd_doctor(args)))
    if args.cmd == "simulate":
        with contextlib.suppress(KeyboardInterrupt):
            sys.exit(asyncio.run(_cmd_simulate(args)))
        sys.exit(0)
    if args.cmd == "call":
        sys.exit(asyncio.run(_cmd_call(args.tool, args.args)))

    from .server import mcp  # imported late: doctor and simulate don't need the server code

    if getattr(args, "http", False):  # absent when no subcommand was given: plain `mcp-server-batocera` is stdio
        try:
            remote.serve_http(mcp, remote.http_config(args))
        except remote.ConfigError as exc:
            parser.exit(2, f"mcp-server-batocera: {exc}\n")
    else:
        mcp.run()  # stdio: JSON-RPC over stdin/stdout
