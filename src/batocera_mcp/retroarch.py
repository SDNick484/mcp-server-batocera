"""RetroArch network commands over UDP: pause, save/load state, screenshot, reset, on-screen text.

RetroArch listens on UDP 55355 when ``network_cmd_enable`` is on (on Batocera:
``global.retroarch.network_cmd_enable=true`` in batocera.conf). Commands are
plain ASCII datagrams such as ``PAUSE_TOGGLE``. Almost none of them reply, so
``GET_STATUS`` (which does) is used both to check RetroArch is there and to
read state:

    GET_STATUS PLAYING snes,Super Mario World (USA),crc32=b19ed489
    GET_STATUS PAUSED snes,Super Mario World (USA),crc32=b19ed489
    GET_STATUS CONTENTLESS

Only the constants in COMMANDS are ever sent, plus SHOW_MSG with validated text.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from typing_extensions import TypedDict

from .client import BatoceraError
from .config import CONTROL_CHARS

log = logging.getLogger(__name__)

COMMANDS = frozenset({"GET_STATUS", "PAUSE_TOGGLE", "SAVE_STATE", "LOAD_STATE", "SCREENSHOT", "RESET"})

# ASSUMPTION B-RA-ENABLE: batocera.conf's global.retroarch.network_cmd_enable=true turns this on.
NOT_ANSWERING = (
    "RetroArch isn't answering on UDP {port}. Either no RetroArch (libretro) game is running (standalone "
    "emulators such as Dolphin or PCSX2 don't support this), or network commands are off: add "
    "'global.retroarch.network_cmd_enable=true' to /userdata/system/batocera.conf and relaunch the game."
)


class RetroArchStatus(TypedDict):
    state: Literal["playing", "paused", "contentless"]
    system: str | None
    game: str | None


# ASSUMPTION B-RA-STATUS
def parse_status(reply: str) -> RetroArchStatus | None:
    parts = reply.strip().split(" ", 2)
    if len(parts) < 2 or parts[0] != "GET_STATUS":
        return None
    state = parts[1].lower()
    if state == "contentless":
        return RetroArchStatus(state="contentless", system=None, game=None)
    if state not in ("playing", "paused"):
        return None
    system = game = None
    if len(parts) == 3:
        fields = parts[2].split(",")
        # The game name itself can contain commas; the crc32 field is last.
        if fields and fields[-1].startswith("crc32="):
            fields = fields[:-1]
        system = fields[0] if fields else None
        game = ",".join(fields[1:]) or None
    return RetroArchStatus(state="playing" if state == "playing" else "paused", system=system, game=game)


class _Proto(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.replies: asyncio.Queue[bytes] = asyncio.Queue()

    def datagram_received(self, data: bytes, addr: tuple[str | object, int]) -> None:
        self.replies.put_nowait(data)


class RetroArchClient:
    reply_timeout_s = 1.5

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    async def _exchange(self, payload: str, want_reply: bool) -> str | None:
        loop = asyncio.get_running_loop()
        try:
            transport, proto = await loop.create_datagram_endpoint(_Proto, remote_addr=(self.host, self.port))
        except OSError as exc:
            raise BatoceraError(f"Can't reach {self.host} for RetroArch commands: {exc}") from exc
        try:
            log.info("RetroArch <- %s", payload.split(" ", 1)[0])
            transport.sendto(payload.encode())
            if not want_reply:
                return None
            try:
                data = await asyncio.wait_for(proto.replies.get(), self.reply_timeout_s)
            except TimeoutError:
                return None
            return data.decode(errors="replace")
        finally:
            transport.close()

    async def status(self) -> RetroArchStatus | None:
        """None when RetroArch doesn't answer (not running, or network commands off)."""
        try:
            reply = await self._exchange("GET_STATUS", want_reply=True)
        except BatoceraError:
            return None
        return parse_status(reply) if reply else None

    async def require_game(self) -> RetroArchStatus:
        s = await self.status()
        if s is None:
            raise BatoceraError(NOT_ANSWERING.format(port=self.port))
        if s["state"] == "contentless":
            raise BatoceraError("RetroArch is running but has no game loaded.")
        return s

    async def send(self, command: str) -> None:
        if command not in COMMANDS:  # the tools only pass constants; this is a backstop
            raise BatoceraError(f"Refusing RetroArch command {command!r}")
        await self._exchange(command, want_reply=False)

    async def show_message(self, text: str) -> None:
        if CONTROL_CHARS.search(text) or "\n" in text:
            raise BatoceraError("On-screen messages must be a single line without control characters.")
        await self._exchange(f"SHOW_MSG {text}", want_reply=False)
