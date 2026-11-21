# Changelog

## [Unreleased]

### Added

- Every tool now has an authorization tier (`read`, `power-off`, `everyday` or `costly`), from the shared `remote.py`. The costly tools, `batocera_quit_game`, `batocera_load_state` and `batocera_reset_game`, ask the person to confirm before they run, and refuse where the app can't show the prompt or the answer isn't an explicit yes. `mcp-server-batocera call` asks on the terminal.
- `MCP_ALLOWED_SERVICE_TOKENS` and `MCP_AUTOMATION_TIERS` settings (README, `deploy/alpine/env.example`).

### Changed

- A Cloudflare Access service token now gets 403 unless its client ID is in `MCP_ALLOWED_SERVICE_TOKENS`, and an automated caller may use only the tiers in `MCP_AUTOMATION_TIERS` (default `read,power-off`; never `costly`). Deployments that use a service token must set the variable.

## [0.1.0]

First release on PyPI.

### Added

- MCP server for a Batocera cabinet through EmulationStation's web API and RetroArch network commands: status, systems, game search and launch, quit, pause, save and load state, screenshots, on-screen messages, volume and save slots.
- CLI: `serve` (stdio, or `--http` behind Cloudflare Access), `doctor`, `call` and `simulate` (a wire-level fake cabinet, no hardware needed).
- `deploy/alpine/`: OpenRC service, install script and `setup-arcade.sh`.
- `playtracker/` (in the repository, not the package): a hook script for the cabinet that publishes play sessions to Home Assistant over MQTT.
