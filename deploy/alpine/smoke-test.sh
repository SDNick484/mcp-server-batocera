#!/bin/sh
# CI: install in a stock python:3.12-alpine container and drive the command on PATH
# and the OpenRC service against the simulator. Proves the dependencies install on
# musl, re-running upgrades the code, the layout and file modes, that the command
# drops to user mcp-batocera, that it picks up /etc/mcp-server-batocera/env, that the
# caller's environment beats that file, that OpenRC runs the service as the user, that
# an MCP call over HTTP reaches the cabinet, and that Access settings in the env file
# turn the Access check on.
#   docker run --rm -v "$PWD:/src" -w /src python:3.12-alpine sh deploy/alpine/smoke-test.sh
set -eux

# On failure, say where as a GitHub annotation (the job log isn't always at hand; annotations are).
report() {
	status=$?
	if [ "$status" != 0 ]; then
		tail_log=$(tail -n 30 "${STEP_LOG:-/dev/null}" 2>/dev/null | sed ':a;N;$!ba;s/%/%25/g;s/\n/%0A/g')
		echo "::error title=Alpine smoke test failed (exit $status)::last step: ${STEP:-?}%0A$tail_log"
	fi
}
trap report EXIT

STEP='install'
apk add --no-cache curl >/dev/null
sh deploy/alpine/install.sh /src

STEP='upgrade'
# Re-running install.sh must replace the code even though the version number hasn't changed
# (pip would otherwise keep the installed copy). Install a copy with a marker, then look for it.
cp -r /src /tmp/src2
echo '# smoke-test upgrade marker' >>/tmp/src2/src/batocera_mcp/__init__.py
sh /tmp/src2/deploy/alpine/install.sh /tmp/src2
grep -q 'smoke-test upgrade marker' /opt/mcp-server-batocera/venv/lib/python3*/site-packages/batocera_mcp/__init__.py

STEP='pytest on musl'
/opt/mcp-server-batocera/venv/bin/pip install --quiet "/src[dev]"
STEP_LOG=/tmp/pytest.log
cd /src
/opt/mcp-server-batocera/venv/bin/pytest -q -p no:cacheprovider -rf >"$STEP_LOG" 2>&1 || { cat "$STEP_LOG"; false; }
tail -n 3 "$STEP_LOG"
cd /
STEP_LOG=''

ETC=/etc/mcp-server-batocera
STATE=/var/lib/mcp-server-batocera
STEP='file modes'
[ "$(stat -c '%a %U:%G' $ETC)" = "750 root:mcp-batocera" ]
[ "$(stat -c '%a %U:%G' $ETC/env)" = "640 root:mcp-batocera" ]
[ "$(stat -c '%a %U' $STATE)" = "700 mcp-batocera" ]
[ "$(command -v mcp-server-batocera)" = /usr/local/bin/mcp-server-batocera ]
[ "$(stat -c '%a %U' /var/log/mcp-server-batocera)" = "750 mcp-batocera" ]
[ -x /etc/init.d/mcp-server-batocera ]

STEP='simulator, as the user'
# Started through the command on PATH: the config it writes must belong to mcp-batocera.
STEP_LOG=/tmp/sim.log
mcp-server-batocera simulate --port 1234 --retroarch-port 55355 --write-config "$STATE/sim" >"$STEP_LOG" 2>&1 &
for _ in $(seq 100); do [ -e "$STATE/sim/config.json" ] && break; sleep 0.1; done
[ "$(stat -c %U "$STATE/sim/config.json")" = mcp-batocera ]

STEP='doctor, settings from env'
sed -i 's|^BATOCERA_URL=.*|BATOCERA_URL=http://127.0.0.1:1234|' $ETC/env
mcp-server-batocera doctor --timeout 5

STEP='call'
mcp-server-batocera call find_games query=metroid limit=1 | grep -q '"Super Metroid"'
out=$(mcp-server-batocera call show_message text=HI)
echo "$out" | grep -q '"outcome": "done"'

STEP='caller beats env'
# Nothing listens on port 9, so doctor must fail if the caller's URL won.
if BATOCERA_URL=http://127.0.0.1:9 mcp-server-batocera doctor --timeout 2; then false; fi
out=$(BATOCERA_DRY_RUN=1 mcp-server-batocera call show_message text=HI)
echo "$out" | grep -q '"outcome": "dry_run"'

STEP='run as the user directly'
su-exec mcp-batocera mcp-server-batocera call get_status | grep -q '"reachable": true'

STEP='service start'
# OpenRC in a container: it needs to believe it has booted, and that the
# network (which the service `need`s) is provided by the host
printf 'rc_sys="docker"\nrc_provide="loopback net"\n' >>/etc/rc.conf
mkdir -p /run/openrc && touch /run/openrc/softlevel
STEP_LOG=/var/log/mcp-server-batocera/server.log
rc-service mcp-server-batocera start
for _ in $(seq 50); do curl -fsS http://127.0.0.1:8717/healthz && break; sleep 0.2; done
rc-service mcp-server-batocera status

STEP='service user'
# The process runs as the service user, not root. (busybox ps truncates user
# names to 8 characters, so ask /proc for the owner of the server's process.)
# shellcheck disable=SC2009
pid=$(ps -o pid,args | grep '[m]cp-server-batocera serve --http' | awk 'NR==1 {print $1}')
[ -n "$pid" ] && [ "$(stat -c %U "/proc/$pid")" = mcp-batocera ]

STEP='mcp over http'
# A real MCP session against the service: initialize, then get_status, which must
# reach the simulated cabinet (structuredContent is compact JSON: no spaces).
URL=http://127.0.0.1:8717/batocera/mcp
H='-H Content-Type:application/json -H Accept:application/json,text/event-stream'
# shellcheck disable=SC2086
SID=$(curl -fsS -D - -o /dev/null $H "$URL" -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' \
	| tr -d '\r' | awk -F': ' 'tolower($1)=="mcp-session-id" {print $2}')
[ -n "$SID" ]
# shellcheck disable=SC2086
curl -fsS $H -H "mcp-session-id: $SID" "$URL" -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null
# shellcheck disable=SC2086
out=$(curl -fsS $H -H "mcp-session-id: $SID" "$URL" -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_status","arguments":{}}}')
echo "$out" | tail -c 600
echo "$out" | grep -q '"reachable":true'

STEP='access from the env file'
# With CF_ACCESS_* in the env file, a request without an Access assertion is refused
# (nothing here can sign one: tests/test_http_tools.py covers valid ones), and the
# health check still answers, before auth.
cat >>$ETC/env <<'ENV'
MCP_PUBLIC_HOSTS=mcp.example.com
CF_ACCESS_TEAM_DOMAIN=example.cloudflareaccess.com
CF_ACCESS_AUD=smoke-test-aud
ENV
rc-service mcp-server-batocera restart
for _ in $(seq 50); do curl -fsS http://127.0.0.1:8717/healthz && break; sleep 0.2; done
# shellcheck disable=SC2086
code=$(curl -sS -o /dev/null -w '%{http_code}' $H "$URL" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
[ "$code" = 403 ]
STEP_LOG=''

STEP='setup-arcade.sh'
# Both servers from GitHub, as on a real container. CI pins each to a commit (ARCADE_REF_BATOCERA:
# the commit under test; ARCADE_REF_PIXELCADE: its same-named branch's tip, else main's). The
# simulated cabinet answers on the standard port at 127.0.0.1; nothing plays pixelweb, so
# Pixelcade's doctor is expected to fail and the script to say so without stopping.
checked_out() { # server, ref: that server's checkout is at that ref (a branch means the remote's)
	d="/srv/src/mcp-server-$1"
	want=$(git -C "$d" rev-parse -q --verify "origin/$2^{commit}" || git -C "$d" rev-parse -q --verify "$2^{commit}")
	[ "$(git -C "$d" rev-parse HEAD)" = "$want" ]
}
if [ -n "${ARCADE_REF:-}" ]; then
	STEP_LOG=/tmp/setup-arcade.log
	ARCADE_HOST=127.0.0.1 sh /src/deploy/alpine/setup-arcade.sh >"$STEP_LOG" 2>&1 || { cat "$STEP_LOG"; false; }
	cat "$STEP_LOG"
	grep -q '^BATOCERA_URL=127.0.0.1$' $ETC/env
	grep -q '^PIXELCADE_URL=127.0.0.1$' /etc/mcp-server-pixelcade/env
	grep -q 'doctor found problems for: pixelcade$' "$STEP_LOG"
	mcp-server-pixelcade --version
	# Both services enabled at boot and answering (Batocera's still with the Access settings above)
	grep -q 'mcp-server-batocera is running and answers on 127.0.0.1:8717' "$STEP_LOG"
	grep -q 'mcp-server-pixelcade is running and answers on 127.0.0.1:8718' "$STEP_LOG"
	rc-update show default | grep -q 'mcp-server-batocera'
	rc-update show default | grep -q 'mcp-server-pixelcade'
	curl -fsS http://127.0.0.1:8718/healthz
	checked_out batocera "${ARCADE_REF_BATOCERA:-$ARCADE_REF}"
	checked_out pixelcade "${ARCADE_REF_PIXELCADE:-$ARCADE_REF}"
	ARCADE_HOST='127.0.0.1;reboot' sh /src/deploy/alpine/setup-arcade.sh 2>&1 | grep -q 'should be an IP address'
	# A ref that isn't there stops it, naming the setting it came from. The pins are unset so
	# ARCADE_REF applies; ARCADE_SERVERS=pixelcade skips a slow reinstall of this server.
	(
		unset ARCADE_REF_BATOCERA ARCADE_REF_PIXELCADE
		ARCADE_REF=no-such-ref-here sh /src/deploy/alpine/setup-arcade.sh 2>&1
	) | grep -q "has no branch, tag or commit 'no-such-ref-here' (ARCADE_REF)"
	ARCADE_SERVERS=pixelcade ARCADE_REF_PIXELCADE=no-such-ref-here sh /src/deploy/alpine/setup-arcade.sh 2>&1 |
		grep -q "mcp-server-pixelcade has no branch, tag or commit 'no-such-ref-here' (ARCADE_REF_PIXELCADE)"
fi

rc-service mcp-server-batocera stop
if [ -e /etc/init.d/mcp-server-pixelcade ]; then rc-service mcp-server-pixelcade stop; fi  # setup-arcade.sh started it
echo "Alpine smoke test passed"
