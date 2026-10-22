#!/bin/sh
# CI: install in a stock python:3.12-alpine container and drive the command on PATH
# against the simulator. Proves the dependencies install on musl, re-running
# upgrades the code, the layout and file modes, that the command drops to user
# mcp-batocera, that it picks up /etc/mcp-server-batocera/env, and that the
# caller's environment beats that file.
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

STEP='setup-arcade.sh'
# Both servers from GitHub at ARCADE_REF, as on a real container (CI passes the branch; both repos
# use the same branch name for changes to the kit). The simulated cabinet answers on the standard
# port at 127.0.0.1; nothing plays pixelweb, so Pixelcade's doctor is expected to fail and the
# script to say so without stopping.
if [ -n "${ARCADE_REF:-}" ]; then
	STEP_LOG=/tmp/setup-arcade.log
	ARCADE_HOST=127.0.0.1 sh /src/deploy/alpine/setup-arcade.sh >"$STEP_LOG" 2>&1 || { cat "$STEP_LOG"; false; }
	cat "$STEP_LOG"
	grep -q '^BATOCERA_URL=127.0.0.1$' $ETC/env
	grep -q '^PIXELCADE_URL=127.0.0.1$' /etc/mcp-server-pixelcade/env
	grep -q 'doctor found problems for: pixelcade$' "$STEP_LOG"
	mcp-server-pixelcade --version
	ARCADE_HOST='127.0.0.1;reboot' sh /src/deploy/alpine/setup-arcade.sh 2>&1 | grep -q 'should be an IP address'
fi

echo "Alpine smoke test passed"
