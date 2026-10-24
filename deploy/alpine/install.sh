#!/bin/sh
# Install mcp-server-batocera on Alpine (e.g. a Proxmox LXC): an OpenRC service over
# Streamable HTTP, plus a command on PATH.
#
#   sh deploy/alpine/install.sh [SOURCE]      # SOURCE: a checkout dir or a pip spec
#
# The siblings' deploy/alpine layout (mcp-server-sofabaton's, -webos's...), so they can
# share an LXC, plus a command on PATH: `mcp-server-batocera doctor` or `... call <tool>`
# runs as the unprivileged user with the settings in /etc, like the service does.
# It installs the service but doesn't enable or start it: setup-arcade.sh does that.
#
#   /opt/mcp-server-batocera/venv      the code                         root:root            0755
#   /opt/mcp-server-batocera/run.sh    loads the settings, runs the CLI root:root            0755
#   /usr/local/bin/mcp-server-batocera runs run.sh as mcp-batocera     root:root            0755
#   /etc/mcp-server-batocera/env       every setting (BATOCERA_*)      root:mcp-batocera   0640
#   /var/lib/mcp-server-batocera/      the user's home and working dir  mcp-batocera        0700
#   /var/log/mcp-server-batocera/      the service's server.log         mcp-batocera        0750
#   /etc/init.d/mcp-server-batocera    the OpenRC script                root:root            0755
# Re-running upgrades the code and keeps the settings.
set -eu

SRC="${1:-git+https://github.com/SDNick484/mcp-server-batocera}"
USER_NAME=mcp-batocera
PREFIX=/opt/mcp-server-batocera
ETC=/etc/mcp-server-batocera
STATE=/var/lib/mcp-server-batocera
HERE=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

# python3 and its venv module; git for the default git+https source; su-exec so the
# command on PATH can drop to the user without re-quoting its arguments (busybox su
# would need them in one string); openrc for the service. Every compiled dependency
# (pydantic-core, cryptography for the Access checks) ships a musl wheel, so no
# compiler is needed (the smoke test proves it in a stock Alpine container).
apk add --no-cache python3 py3-pip git su-exec openrc >/dev/null

if ! id "$USER_NAME" >/dev/null 2>&1; then
	addgroup -S "$USER_NAME"
	adduser -S -D -H -h "$STATE" -s /sbin/nologin -G "$USER_NAME" "$USER_NAME"
fi

python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet --upgrade "$SRC"
# Every commit carries the same version number, so pip calls an installed copy "already satisfied"
# and keeps it: re-running this script would upgrade nothing. Reinstall the package itself (not its
# dependencies) so it always gets the code from SOURCE.
"$PREFIX/venv/bin/pip" install --quiet --force-reinstall --no-deps "$SRC"
install -m 0755 "$HERE/run.sh" "$PREFIX/run.sh"
install -m 0755 "$HERE/mcp-server-batocera" /usr/local/bin/mcp-server-batocera

install -d -m 0750 -o root -g "$USER_NAME" "$ETC"
[ -e "$ETC/env" ] || install -m 0640 -o root -g "$USER_NAME" "$HERE/env.example" "$ETC/env"
install -d -m 0700 -o "$USER_NAME" -g "$USER_NAME" "$STATE"
install -d -m 0750 -o "$USER_NAME" -g "$USER_NAME" /var/log/mcp-server-batocera
install -m 0755 "$HERE/mcp-server-batocera.initd" /etc/init.d/mcp-server-batocera

echo "Installed $("$PREFIX/venv/bin/mcp-server-batocera" --version)."
echo "Next: set BATOCERA_URL in $ETC/env (the arcade's address), then:"
echo "  mcp-server-batocera doctor"
echo "  mcp-server-batocera call get_status"
echo "  rc-update add mcp-server-batocera default && rc-service mcp-server-batocera restart"
echo "It listens on 127.0.0.1:8717/batocera/mcp. For a Claude connector, see the README's"
echo "'Service and Cloudflare' section (CF_ACCESS_* and MCP_PUBLIC_HOSTS in $ETC/env)."
