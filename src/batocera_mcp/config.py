"""Settings and the small validators that bound what the model can send.

There are two things on the cabinet this server talks to:

* **EmulationStation's web API** (TCP 1234). Batocera's frontend runs it as a
  thread inside EmulationStation, so it keeps answering while a game runs.
  By default it binds 127.0.0.1 and refuses other hosts; turning on
  *Developer > Enable public web access* (then rebooting) binds 0.0.0.0.
  It has no authentication, so keep it on the LAN.
* **RetroArch network commands** (UDP 55355), only while a libretro core is
  running, and only after ``global.retroarch.network_cmd_enable=true`` is
  added to ``batocera.conf``. Batocera's launcher copies any
  ``retroarch.<key>`` setting straight into RetroArch's config.

Settings come from, most specific first: environment variables, then
``config.json`` in the config directory, then the defaults below.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

DEFAULT_URL = "http://batocera.local:1234"
ES_PORT = 1234
RETROARCH_PORT = 55355
DEFAULT_TIMEOUT_S = 10.0

CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # allows \t and \n
# EmulationStation's game id: md5 of the ROM path, as lowercase hex.
GAME_ID = re.compile(r"[0-9a-f]{32}")
# ES system names are short folder names: snes, mame, fbneo, atari2600, 3do, ...
SYSTEM_NAME = re.compile(r"[a-z0-9][a-z0-9_\-+.]{0,39}")


class InvalidValue(ValueError):
    """A model-supplied value we refuse. The message is shown to the model."""


def system_name(value: str) -> str:
    v = value.strip().lower()
    if not SYSTEM_NAME.fullmatch(v) or ".." in v:
        raise InvalidValue(f"{value!r} isn't a system name; see list_systems (e.g. 'snes', 'mame').")
    return v


def game_id(value: str) -> str:
    v = value.strip().lower()
    if not GAME_ID.fullmatch(v):
        raise InvalidValue(f"{value!r} isn't a game id; use the id find_games returns (32 hex characters).")
    return v


def message_text(value: str, max_len: int = 300) -> str:
    v = value.strip()
    if not v:
        raise InvalidValue("The message is empty.")
    if len(v) > max_len:
        raise InvalidValue(f"The message is longer than {max_len} characters.")
    if CONTROL_CHARS.search(v):
        raise InvalidValue("The message contains control characters.")
    return v


def config_dir() -> Path:
    override = os.environ.get("BATOCERA_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "mcp-server-batocera"


@dataclass(frozen=True)
class Settings:
    url: str
    timeout_s: float = DEFAULT_TIMEOUT_S
    retroarch_port: int = RETROARCH_PORT
    # Read everything, change nothing: actions report what they would send.
    dry_run: bool = False
    # How results and messages name the cabinet; defaults to its address.
    name: str | None = None

    @property
    def host(self) -> str:
        return urlparse(self.url).hostname or ""


def normalize_url(raw: str) -> str:
    """'batocera.local' or '192.168.1.20' -> 'http://...:1234' (no trailing slash)."""
    v = raw.strip().rstrip("/")
    if "://" not in v:
        v = "http://" + v
    parsed = urlparse(v)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"BATOCERA_URL {raw!r} isn't an http URL or host name")
    if parsed.port is None:
        v = f"{parsed.scheme}://{parsed.hostname}:{ES_PORT}{parsed.path}"
    return v


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def flag(value: object) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def load_settings(dry_run: bool | None = None) -> Settings:
    data = _read_json(config_dir() / "config.json")
    url = os.environ.get("BATOCERA_URL") or data.get("url") or DEFAULT_URL
    timeout = DEFAULT_TIMEOUT_S
    raw_timeout = os.environ.get("BATOCERA_TIMEOUT", data.get("timeout_s"))
    if raw_timeout is not None:
        try:
            timeout = min(max(float(raw_timeout), 1.0), 60.0)
        except (TypeError, ValueError):
            log.warning("Ignoring timeout %r: not a number", raw_timeout)
    port = RETROARCH_PORT
    raw_port = os.environ.get("BATOCERA_RETROARCH_PORT", data.get("retroarch_port"))
    if raw_port is not None:
        try:
            port = int(raw_port)
            if not 1 <= port <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            log.warning("Ignoring retroarch_port %r", raw_port)
            port = RETROARCH_PORT
    if dry_run is None:
        dry_run = flag(os.environ.get("BATOCERA_DRY_RUN", data.get("dry_run", False)))
    name = os.environ.get("BATOCERA_NAME") or data.get("name") or None
    return Settings(
        url=normalize_url(str(url)),
        timeout_s=timeout,
        retroarch_port=port,
        dry_run=dry_run,
        name=str(name) if name else None,
    )
