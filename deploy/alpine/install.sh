#!/bin/sh
# Install mcp-server-batocera on Alpine (e.g. a Proxmox LXC), to run from the command line.
#
#   sh deploy/alpine/install.sh [SOURCE]      # SOURCE: a checkout dir or a pip spec
#
# This is the siblings' deploy/alpine layout without the service: there's no OpenRC
# script and nothing listens on a port. You run `mcp-server-batocera doctor` or
# `... call <tool>` over SSH, and a command on PATH runs it as the unprivileged
# user with the settings in /etc. The layout leaves room to add the service later.
#
#   /opt/mcp-server-batocera/venv      the code                         root:root            0755
#   /opt/mcp-server-batocera/run.sh    loads the settings, runs the CLI root:root            0755
#   /usr/local/bin/mcp-server-batocera runs run.sh as mcp-batocera     root:root            0755
#   /etc/mcp-server-batocera/env       every setting (BATOCERA_*)      root:mcp-batocera   0640
#   /var/lib/mcp-server-batocera/      the user's home and working dir  mcp-batocera        0700
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
# would need them in one string). Every compiled dependency ships a musl wheel, so
# no compiler is needed (the smoke test proves it in a stock Alpine container).
apk add --no-cache python3 py3-pip git su-exec >/dev/null

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

echo "Installed $("$PREFIX/venv/bin/mcp-server-batocera" --version)."
echo "Next: set BATOCERA_URL in $ETC/env (the arcade's address), then:"
echo "  mcp-server-batocera doctor"
echo "  mcp-server-batocera call get_status"
