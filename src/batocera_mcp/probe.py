"""Measure how many connections EmulationStation's web API serves at once (ASSUMPTION B-ES-THREADS).

    python -m batocera_mcp.probe [HOST] [--port 1234] [--max 16] [--read-timeout 5]

HOST defaults to BATOCERA_URL. Standard library only, so the file also runs on its own with any python3.

How it works: EmulationStation's HTTP server (cpp-httplib) gives each *connection* to a worker thread from
a fixed pool, and a worker that has accepted a connection waits up to its read timeout (5 s) for the
request line. So the probe opens k connections that never send anything, which pins k workers, and then
times one ordinary request. While k is below the pool size a free worker answers it at once; once k
reaches the pool size the request waits in the queue until a silent connection times out (about 5 s).
The smallest k that stalls is the pool size.

It sends nothing that changes the cabinet (GET /runningGame), and every silent connection is closed
before the next round, which frees its worker straight away. While a round is stalled, the API is busy
for up to the read timeout; nothing else is affected.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import socket
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TextIO
from urllib.parse import urlsplit

DEFAULT_KS = (0, 1, 2, 3, 4, 6, 7, 8, 9, 12, 16, 24, 32)


@dataclass(frozen=True)
class Round:
    held: int  # silent connections open during the timed request
    seconds: float  # how long the timed request took
    status: str  # its status line, or "error: ..."


@dataclass(frozen=True)
class Result:
    rounds: list[Round]
    pool: int | None  # smallest held count that stalled; None when nothing stalled up to the last round

    def summary(self) -> str:
        if self.pool is None:
            most = max(r.held for r in self.rounds)
            return f"no stall with {most} connections held: the pool is larger than {most}"
        return f"pool size {self.pool}: requests stalled once {self.pool} connections were held"


def parse_host(text: str, port: int) -> tuple[str, int]:
    """'192.168.1.20', 'http://cab.lan:1234' or 'cab.lan:1234' -> (host, port)."""
    parts = urlsplit(text if "//" in text else f"//{text}")
    if not parts.hostname:
        raise ValueError(f"not a host: {text!r}")
    return parts.hostname, parts.port or port


def timed_request(host: str, port: int, timeout: float) -> tuple[float, str]:
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(f"GET /runningGame HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
            line = s.makefile("rb").readline().decode(errors="replace").strip()
    except OSError as exc:
        return time.monotonic() - start, f"error: {exc}"
    return time.monotonic() - start, line or "error: empty reply"


def run(
    host: str,
    port: int,
    ks: Iterable[int] = DEFAULT_KS,
    read_timeout: float = 5.0,
    settle: float = 0.3,
    out: TextIO | None = None,  # where to print each round, if anywhere
) -> Result:
    """Run rounds in increasing k; stop after the first stall (larger k would only stall too)."""
    stall = read_timeout * 0.5  # a free worker answers in milliseconds; a stall lasts about read_timeout
    # Connecting is the kernel's job, not a worker's, but a burst can overflow a small listen backlog and the
    # SYN is then resent after a second; don't mistake that for anything.
    patience = max(3.0, read_timeout * 3)
    rounds: list[Round] = []
    pool = None
    for k in sorted(set(ks)):
        held: list[socket.socket] = []
        try:
            for _ in range(k):
                held.append(socket.create_connection((host, port), timeout=patience))
            time.sleep(settle)  # let the server's accept loop hand each one to a worker
            seconds, status = timed_request(host, port, timeout=patience)
        finally:
            for s in held:
                with contextlib.suppress(OSError):
                    s.close()
        r = Round(k, round(seconds, 3), status)
        rounds.append(r)
        if out:
            print(f"  {k:3d} held: {r.seconds:6.3f} s  {r.status}", file=out, flush=True)
        if status.startswith("error"):
            raise OSError(f"request failed with {k} connections held: {status}")
        if seconds >= stall:
            pool = k
            break
        time.sleep(settle)  # closed sockets: give the workers a moment to notice and return to the pool
    return Result(rounds, pool)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m batocera_mcp.probe", description=__doc__.splitlines()[0])
    p.add_argument("host", nargs="?", default=os.environ.get("BATOCERA_URL"), help="default: $BATOCERA_URL")
    p.add_argument("--port", type=int, default=1234)
    p.add_argument("--max", type=int, default=16, help="most connections to hold (default 16)")
    p.add_argument("--read-timeout", type=float, default=5.0, help="the server's read timeout, seconds")
    a = p.parse_args(argv)
    if not a.host:
        p.error("give the cabinet's address, or set BATOCERA_URL")
    host, port = parse_host(a.host, a.port)
    print(f"EmulationStation at {host}:{port}: timing GET /runningGame with k silent connections held")
    try:
        result = run(host, port, [k for k in DEFAULT_KS if k <= a.max], a.read_timeout, out=sys.stdout)
    except OSError as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        return 1
    print(result.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
