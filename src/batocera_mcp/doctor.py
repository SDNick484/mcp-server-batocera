"""`mcp-server-batocera doctor`: check everything between this machine and the cabinet, layer by layer.

"EmulationStation isn't answering" has a handful of causes. doctor checks each
layer in order, and the first failure says what it means, what to try, and
which HARDWARE_VALIDATION.md step covers it (STEP below):

  config     - BATOCERA_URL parses; dry run on or off
  tcp        - the cabinet accepts TCP on EmulationStation's port (1234)
  api        - /caps answers; a 403 means public web access is off (B-ES-PUBLIC-ACCESS)
  library    - /systems lists systems with games (B-ES-SYSTEMS)
  running    - /runningGame and /isIdle answer, and say what's running (B-ES-RUNNINGGAME)
  retroarch  - while a game runs, RetroArch answers GET_STATUS on UDP 55355 (B-RA-ENABLE)

Nothing here changes anything: no launches, no messages, no RetroArch commands
other than GET_STATUS.
"""

from __future__ import annotations

import asyncio
import json
import platform
from dataclasses import asdict, dataclass, field
from importlib.metadata import PackageNotFoundError, version
from urllib.parse import urlparse

from .assumptions import contradicted, unverified
from .client import BatoceraError, EmulationStationClient, is_game_system
from .config import ES_PORT, Settings
from .logsafe import redact
from .retroarch import RetroArchClient

# HARDWARE_VALIDATION.md step that covers each layer, for the hints.
STEP = {"config": 1, "tcp": 1, "api": 1, "library": 2, "running": 3, "retroarch": 5}


@dataclass
class Check:
    layer: str
    ok: bool
    detail: str
    hint: str = ""


@dataclass
class Report:
    versions: dict[str, str]
    url: str
    dry_run: bool
    checks: list[Check] = field(default_factory=list)
    unverified_assumptions: list[str] = field(default_factory=list)  # status simulator-only
    contradicted_assumptions: list[str] = field(default_factory=list)  # status hardware-contradicted

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)


def versions() -> dict[str, str]:
    out = {"python": platform.python_version()}
    for pkg in ("mcp-server-batocera", "mcp", "httpx"):
        try:
            out[pkg] = version(pkg)
        except PackageNotFoundError:
            out[pkg] = "not installed"
    return out


def _hint(layer: str, text: str) -> str:
    return f"{text} (HARDWARE_VALIDATION.md step {STEP[layer]})"


async def _tcp(host: str, port: int, timeout: float) -> str | None:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, TimeoutError) as exc:
        return str(exc) or type(exc).__name__
    writer.close()
    return None


async def run_doctor(settings: Settings, timeout: float = 5.0) -> Report:
    report = Report(
        versions=versions(),
        url=settings.url,
        dry_run=settings.dry_run,
        unverified_assumptions=[f"{a.id}: {a.claim}" for a in unverified()],
        contradicted_assumptions=[f"{a.id}: {a.claim}" for a in contradicted()],
    )
    add = report.checks.append
    add(Check("config", True, f"EmulationStation at {settings.url}" + (" (dry run)" if settings.dry_run else "")))

    port = urlparse(settings.url).port or ES_PORT
    err = await _tcp(settings.host, port, timeout)
    if err:
        add(Check("tcp", False, f"no TCP connection to {settings.host}:{port}: {err}", _hint(
            "tcp",
            "Is the cabinet on? If it is, EmulationStation only listens on 127.0.0.1 until public web access "
            "is on: System Settings > Developer > Enable public web access, then reboot",
        )))  # fmt: skip
        return report
    add(Check("tcp", True, f"{settings.host}:{port} accepts connections"))

    es = EmulationStationClient(settings)
    try:
        try:
            ver = await es.version()
        except BatoceraError as exc:
            add(Check("api", False, str(exc), _hint("api", "See the message above")))
            return report
        add(Check("api", True, f"EmulationStation {ver or '(no version reported)'} answers"))

        try:
            systems = [s for s in await es.systems() if is_game_system(s)]
        except BatoceraError as exc:
            add(Check("library", False, str(exc), _hint("library", "The API answered /caps but not /systems")))
            return report
        with_games = [s for s in systems if int(s.get("totalGames", 0) or 0) > 0]
        games = sum(int(s.get("totalGames", 0) or 0) for s in with_games)
        if not with_games:
            add(Check("library", False, "no systems with games", _hint(
                "library", "EmulationStation reports no games; check /userdata/roms, or run reload_games"
            )))  # fmt: skip
        else:
            add(Check("library", True, f"{len(with_games)} systems, {games} games"))

        try:
            running = await es.running_game()
            idle = await es.is_idle()
        except BatoceraError as exc:
            add(Check("running", False, str(exc), _hint("running", "See the message above")))
            return report
        if running:
            add(Check("running", True, f"playing {running.get('name')} ({running.get('systemName')})"))
        else:
            add(Check("running", True, "no game running" + ("" if idle else "; EmulationStation is busy")))
    finally:
        await es.aclose()

    ra = await RetroArchClient(settings.host, settings.retroarch_port).status()
    if ra:
        add(Check("retroarch", True, f"RetroArch {ra['state']} {ra['game'] or ''}".rstrip()))
    elif running:
        silent = f"a game runs but RetroArch doesn't answer on UDP {settings.retroarch_port}"
        add(Check("retroarch", False, silent, _hint(
            "retroarch",
            "Normal for standalone emulators (Dolphin, PCSX2). For RetroArch games, add "
            "global.retroarch.network_cmd_enable=true to /userdata/system/batocera.conf and relaunch",
        )))  # fmt: skip
    else:
        add(Check("retroarch", True, "not checked: no game running (RetroArch only listens during a game)"))
    return report


def _assumption_lines(report: Report, verbose: bool) -> list[str]:
    """Contradicted ones in full (rare, and each one changed the code); unverified ones as a count unless
    verbose, since that list only shrinks as hardware checks are recorded and says little run to run."""
    lines = [""]
    if report.contradicted_assumptions:
        lines.append(f"Contradicted on hardware, no longer relied on ({len(report.contradicted_assumptions)}):")
        lines += [f"   {c}" for c in report.contradicted_assumptions]
    pending = report.unverified_assumptions
    if verbose and pending:
        lines.append(f"Not yet confirmed on hardware ({len(pending)}):")
        lines += [f"   {u}" for u in pending]
    else:
        more = " (doctor --verbose lists them)" if pending else ""
        lines.append(f"Not yet confirmed on hardware: {len(pending)}{more}")
    return lines


def render(report: Report, redacted: bool = True, verbose: bool = False) -> str:
    lines = ["mcp-server-batocera doctor", ""]
    lines += [f"  {k}: {v}" for k, v in report.versions.items()]
    if report.dry_run:
        lines += ["", "  note: dry run is on (BATOCERA_DRY_RUN): actions are reported, not sent"]
    lines += ["", f"Cabinet {report.url}"]
    for c in report.checks:
        lines.append(f"   {'ok' if c.ok else 'x '} {c.layer:<10} {c.detail}")
        if c.hint:
            lines.append(f"      -> {c.hint}")
    lines += _assumption_lines(report, verbose)
    lines += ["", "OK" if report.ok else "PROBLEMS FOUND"]
    text = "\n".join(lines)
    return redact(text) if redacted else text


def to_json(report: Report, redacted: bool = True) -> str:
    text = json.dumps({**asdict(report), "ok": report.ok}, indent=2)
    return redact(text) if redacted else text
