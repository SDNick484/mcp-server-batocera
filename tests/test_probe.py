"""The pool probe (batocera_mcp.probe) against fakes whose pool size we set (ASSUMPTION B-ES-THREADS)."""

from __future__ import annotations

import pytest

from batocera_mcp import probe
from batocera_mcp.sim.fake_cabinet import FakeCabinet

READ_TIMEOUT = 0.6  # the fake's read timeout, shortened from ES's 5 s to keep the tests quick


def fake(workers: int | None) -> FakeCabinet:
    c = FakeCabinet(http_workers=workers)
    c.read_timeout_s = READ_TIMEOUT
    return c


@pytest.mark.parametrize("workers", [2, 3, 8])
def test_finds_the_pool_size(workers):
    with fake(workers) as c:
        result = probe.run(c.host, c.es_port, read_timeout=READ_TIMEOUT, settle=0.05)
    assert result.pool == workers
    assert all(r.status.endswith("201 Created") for r in result.rounds)  # GET /runningGame, nothing running
    assert result.rounds[-1].held == workers  # stops at the first stall
    assert "pool size" in result.summary()


def test_no_stall_without_a_pool_limit():
    with fake(None) as c:
        result = probe.run(c.host, c.es_port, ks=[0, 4, 12], read_timeout=READ_TIMEOUT, settle=0.05)
    assert result.pool is None
    assert "larger than 12" in result.summary()


def test_the_cabinet_is_usable_again_after_a_stall():
    with fake(2) as c:
        probe.run(c.host, c.es_port, ks=[0, 2], read_timeout=READ_TIMEOUT, settle=0.05)
        seconds, status = probe.timed_request(c.host, c.es_port, timeout=2)
    assert seconds < READ_TIMEOUT / 2 and "201" in status


def test_a_refused_connection_is_reported(capsys):
    with fake(2) as c:
        port = c.es_port
    assert probe.main(["127.0.0.1", "--port", str(port), "--max", "2"]) == 1
    assert "probe failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("192.168.1.20", ("192.168.1.20", 1234)),
        ("http://192.168.1.20:1234", ("192.168.1.20", 1234)),
        ("cabinet.lan:8080", ("cabinet.lan", 8080)),
        ("http://cabinet.lan/", ("cabinet.lan", 1234)),
    ],
)
def test_parse_host(text, expected):
    assert probe.parse_host(text, 1234) == expected


def test_main_prints_the_rounds_and_the_answer(capsys, monkeypatch):
    with fake(3) as c:
        monkeypatch.setenv("BATOCERA_URL", c.url)
        assert probe.main(["--read-timeout", str(READ_TIMEOUT)]) == 0
    out = capsys.readouterr().out
    assert "  3 held:" in out and out.rstrip().endswith("connections were held")
