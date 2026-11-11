# Changelog

## [Unreleased]

## [0.1.0]

First release on PyPI.

### Added

- MCP server for a Batocera cabinet through EmulationStation's web API and RetroArch network commands: status, systems, game search and launch, quit, pause, save and load state, screenshots, on-screen messages, volume and save slots.
- CLI: `serve` (stdio, or `--http` behind Cloudflare Access), `doctor`, `call` and `simulate` (a wire-level fake cabinet, no hardware needed).
- `deploy/alpine/`: OpenRC service, install script and `setup-arcade.sh`.
- `playtracker/` (in the repository, not the package): a hook script for the cabinet that publishes play sessions to Home Assistant over MQTT.
