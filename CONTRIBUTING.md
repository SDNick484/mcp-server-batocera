# Contributing

The most useful contribution right now is a **hardware report**: run
[HARDWARE_VALIDATION.md](HARDWARE_VALIDATION.md) (or just try the tools) on your Batocera cabinet and
[tell us what happened](https://github.com/SDNick484/mcp-server-batocera/issues/new).
Much of what the server assumes has only run against a simulator.

## Working on the code

```sh
git clone https://github.com/SDNick484/mcp-server-batocera.git && cd mcp-server-batocera
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest && ruff check . && ruff format --check . && mypy
mcp-server-batocera simulate --write-config /tmp/sim   # a fake cabinet, no hardware needed
```

The rules the code follows (the model never supplies a path or a raw command, system-changing endpoints stay unreachable, every unconfirmed Batocera behavior recorded as an assumption, ...) are in [CLAUDE.md](CLAUDE.md).
`remote.py`, `logsafe.py`, `tasks.py`, `ratelimit.py`, `registry.py`, `lookup.py`, `tools.py` and their tests are shared with sibling servers and come from
[mcp-server-shared](https://github.com/SDNick484/mcp-server-shared); change them there, then run
`python scripts/sync_shared.py --update` here. The package carries its own copies, so it doesn't
depend on mcp-server-shared at runtime.

## Releasing

1. Bump `__version__` in `src/batocera_mcp/__init__.py` (`pyproject.toml` reads it from there) and
   add a `## [x.y.z] - YYYY-MM-DD` section to [CHANGELOG.md](CHANGELOG.md). Merge that to `main`.
2. Tag the merge commit (the one whose `__version__` matches the tag) and push the tag:

   ```sh
   git checkout main && git pull
   git tag -a v0.1.0 -m v0.1.0 && git push origin v0.1.0
   ```

3. `.github/workflows/release.yml` checks the tag against the version and the changelog, builds,
   checks the wheel installs and runs on its own, publishes to PyPI and creates the GitHub release
   with the changelog section as its notes.

### One-time setup for PyPI (trusted publishing, no token)

1. Make a [PyPI](https://pypi.org/account/register/) account with two-factor authentication.
2. Account → Publishing → **Add a new pending publisher** (GitHub): project
   `mcp-server-batocera`, owner `SDNick484`, repository `mcp-server-batocera`, workflow
   `release.yml`, environment `pypi`. "Pending" means the project is created by its first upload.
3. In the GitHub repo: Settings → Environments → **New environment** `pypi`. Optionally add
   yourself as a required reviewer, so every publish waits for your click.

To rehearse first, do the same on [TestPyPI](https://test.pypi.org) and point a copy of the
publish step at it with `repository-url: https://test.pypi.org/legacy/`.
