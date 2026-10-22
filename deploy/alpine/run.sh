#!/bin/sh
# Runs mcp-server-batocera with the settings in /etc/mcp-server-batocera/env.
# /usr/local/bin/mcp-server-batocera calls this as user mcp-batocera, so
#   mcp-server-batocera doctor
#   mcp-server-batocera call find_games query=mario
# work from any shell on this machine. With no arguments the CLI's default is
# `serve` over stdio, which is how a client could reach it over SSH later.
#
# A variable already in the caller's environment beats the file, so one-off
# overrides work as they do elsewhere:
#   BATOCERA_URL=http://127.0.0.1:1235 mcp-server-batocera doctor   (a simulator)
set -eu
ENV_FILE="${MCP_BATOCERA_ENV:-/etc/mcp-server-batocera/env}"
if [ -r "$ENV_FILE" ]; then
	caller=$(export -p)  # the caller's exports, quoted so the shell can re-read them
	set -a               # export every variable the file sets
	# shellcheck disable=SC1090
	. "$ENV_FILE"
	set +a
	eval "$caller"       # ...then put the caller's back on top
fi
exec /opt/mcp-server-batocera/venv/bin/mcp-server-batocera "$@"
