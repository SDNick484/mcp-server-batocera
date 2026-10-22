"""The command line: doctor, call and simulate, against the fake cabinet."""

from __future__ import annotations

import asyncio
import json

import pytest

from batocera_mcp import cli
from batocera_mcp.assumptions import unverified
from batocera_mcp.doctor import STEP, run_doctor


def run(argv: list[str], capsys) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    out, err = capsys.readouterr()
    return int(exc.value.code or 0), out, err


@pytest.fixture
def env(cabinet, monkeypatch):
    monkeypatch.setenv("BATOCERA_URL", cabinet.url)
    monkeypatch.setenv("BATOCERA_RETROARCH_PORT", str(cabinet.ra_port))
    return cabinet


def test_doctor_ok(env, capsys):
    code, out, _ = run(["doctor"], capsys)
    assert code == 0 and out.rstrip().endswith("OK")
    assert "ok library    3 systems, 6 games" in out
    # Any still-unverified assumption, so recording hardware results doesn't break this test.
    pending = unverified()
    assert not pending or ("Not yet confirmed on hardware" in out and pending[0].id in out)


def test_doctor_points_at_the_validation_step(env, capsys):
    env.forbidden = True  # ASSUMPTION B-ES-PUBLIC-ACCESS
    code, out, _ = run(["doctor"], capsys)
    assert code == 1 and "PROBLEMS FOUND" in out
    assert "Enable public web access" in out and f"step {STEP['api']}" in out


def test_doctor_while_a_standalone_emulator_runs(env, capsys):
    env.play("gamecube")
    code, out, _ = run(["check"], capsys)  # `check` is an alias
    assert code == 1 and "network_cmd_enable" in out and f"step {STEP['retroarch']}" in out


def test_doctor_json(env, capsys):
    env.play("snes")
    code, out, _ = run(["doctor", "--json"], capsys)
    report = json.loads(out)
    assert code == 0 and report["ok"] is True
    assert {c["layer"] for c in report["checks"]} == set(STEP)


@pytest.mark.anyio
async def test_doctor_with_nothing_listening():
    from batocera_mcp.config import Settings

    report = await run_doctor(Settings(url="http://127.0.0.1:9"), timeout=1)
    assert not report.ok and report.checks[-1].layer == "tcp"


def test_call(env, capsys):
    code, out, _ = run(["call", "find_games", "query=metroid", "limit=1"], capsys)
    assert code == 0 and json.loads(out)["games"][0]["name"] == "Super Metroid"


def test_call_dry_run_sends_nothing(env, capsys):
    gid = env.game("snes")["id"]
    code, out, _ = run(["call", "--dry-run", "launch_game", "system=snes", f"game_id={gid}"], capsys)
    assert code == 0 and json.loads(out)["outcome"] == "dry_run"
    assert env.calls("/launch") == []


def test_call_reports_errors(env, capsys):
    code, _, err = run(["call", "launch_game", "system=snes", "game_id=" + "0" * 32], capsys)
    assert code == 1 and "No game with id" in err


def test_call_lists_tools(env, capsys):
    code, out, _ = run(["call", "tools"], capsys)
    assert code == 0 and "launch_game" in out


@pytest.mark.anyio
async def test_simulate_writes_a_config(tmp_path, capsys):
    args = cli.build_parser().parse_args(["simulate", "--write-config", str(tmp_path / "sim")])
    task = asyncio.create_task(cli._cmd_simulate(args))
    config = tmp_path / "sim" / "config.json"
    for _ in range(100):
        if config.exists():
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    data = json.loads(config.read_text())
    assert data["url"].startswith("http://127.0.0.1:") and data["retroarch_port"] > 0
    assert "export BATOCERA_CONFIG_DIR=" in capsys.readouterr().out
