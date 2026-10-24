#!/bin/sh
# setup-arcade.sh: install the arcade's MCP servers, mcp-server-batocera and mcp-server-pixelcade,
# for command-line use on an Alpine machine on the cabinet's LAN (e.g. a Proxmox LXC), in one go.
# Run as root on that machine; from elsewhere, through the Proxmox host:
#
#   ssh root@<proxmox host> "pct exec <container id> -- env ARCADE_HOST=<cabinet IP> sh -s" < setup-arcade.sh
#
# Settings (environment variables, all optional):
#   ARCADE_HOST     the cabinet's IP address or name: sets BATOCERA_URL and PIXELCADE_URL in each
#                   server's /etc/mcp-server-<name>/env (omit to keep what's there)
#   ARCADE_REF      the branch, tag or commit to install (default main)
#   ARCADE_REF_BATOCERA, ARCADE_REF_PIXELCADE
#                   that server's ref instead of ARCADE_REF, e.g. a commit of one repo only
#                   (CI pins each repo's commit, so a branch deleted mid-run can't break it)
#   ARCADE_SERVERS  which servers (default "batocera pixelcade")
#   ARCADE_GIT      where the repos live (default https://github.com/SDNick484)
#
# Each repo is checked out in /srv/src/mcp-server-<name> (the layout the sibling servers deploy
# from) and installed by its own deploy/alpine/install.sh. Safe to re-run: it updates the code and
# keeps the settings. It ends with each server's doctor, which reads but changes nothing.
set -eu

HOST="${ARCADE_HOST:-}"
REF="${ARCADE_REF:-main}"
SERVERS="${ARCADE_SERVERS:-batocera pixelcade}"
GIT="${ARCADE_GIT:-https://github.com/SDNick484}"

say() { printf '\n==> %s\n' "$*"; }

[ "$(id -u)" = 0 ] || { echo "Run as root." >&2; exit 1; }
[ -f /etc/alpine-release ] || { echo "This installer is for Alpine Linux." >&2; exit 1; }
case "$HOST" in
*[!A-Za-z0-9.:-]*) echo "ARCADE_HOST should be an IP address or host name, got: $HOST" >&2; exit 1 ;;
esac
for s in $SERVERS; do
	case "$s" in
	batocera | pixelcade) ;;
	*) echo "Unknown server in ARCADE_SERVERS: $s (expected batocera and/or pixelcade)" >&2; exit 1 ;;
	esac
done

apk add --no-cache git >/dev/null
mkdir -p /srv/src

for s in $SERVERS; do
	dir="/srv/src/mcp-server-$s"
	case "$s" in
	batocera) ref="${ARCADE_REF_BATOCERA:-$REF}" refvar=ARCADE_REF_BATOCERA ;;
	pixelcade) ref="${ARCADE_REF_PIXELCADE:-$REF}" refvar=ARCADE_REF_PIXELCADE ;;
	esac
	[ "$ref" != "$REF" ] || refvar=ARCADE_REF
	if [ -d "$dir/.git" ]; then
		git -C "$dir" fetch -q --tags origin
	else
		git clone -q "$GIT/mcp-server-$s.git" "$dir"
	fi
	# A branch name means the remote's copy of it; anything else (a tag, a commit) is used as given.
	if git -C "$dir" rev-parse -q --verify "origin/$ref^{commit}" >/dev/null; then
		git -C "$dir" checkout -q --detach "origin/$ref"
	elif git -C "$dir" rev-parse -q --verify "$ref^{commit}" >/dev/null; then
		git -C "$dir" checkout -q --detach "$ref"
	else
		echo "mcp-server-$s has no branch, tag or commit '$ref' ($refvar)." >&2
		exit 1
	fi
	say "mcp-server-$s at $(git -C "$dir" log -1 --format='%h %s')"
	sh "$dir/deploy/alpine/install.sh" "$dir"
	if [ -n "$HOST" ]; then
		var="$(echo "$s" | tr '[:lower:]' '[:upper:]')_URL"
		sed -i "s|^#*$var=.*|$var=$HOST|" "/etc/mcp-server-$s/env"
		grep -q "^$var=" "/etc/mcp-server-$s/env" || echo "$var=$HOST" >>"/etc/mcp-server-$s/env"
		echo "$var=$HOST in /etc/mcp-server-$s/env"
	fi
done

problems=""
for s in $SERVERS; do
	say "mcp-server-$s doctor"
	# By full path: the shell running this may not have /usr/local/bin on PATH (seen once on an LXC).
	"/usr/local/bin/mcp-server-$s" doctor || problems="$problems $s"
done
if [ -n "$problems" ]; then
	say "doctor found problems for:$problems"
	echo "Each failing line names the HARDWARE_VALIDATION.md step that covers it. Before Batocera's"
	echo "'Enable public web access' is on, mcp-server-batocera's doctor is expected to fail at tcp."
else
	say "Every server reaches the cabinet."
fi
echo "Run them from any shell here: mcp-server-batocera call get_status, mcp-server-pixelcade call get_status."
case ":$PATH:" in
*:/usr/local/bin:*) ;;
*) echo "(This shell's PATH has no /usr/local/bin, where they're installed: use the full path, or add it to PATH.)" ;;
esac
