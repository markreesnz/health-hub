#!/bin/sh
# WSET Coach Connector add-on entrypoint.
set -eu

REPO=/share/wset_coach/repo

# Shared secret between this add-on and the wset_mcp integration (never leaves the Green).
if [ ! -s /data/proxy_secret ]; then
  python3 -c "import secrets;print(secrets.token_urlsafe(48))" > /data/proxy_secret
fi
chmod 600 /data/proxy_secret

# The coaching workspace: a git repository whose checked-out files are what the
# coach reads and writes. A push from the Mac updates those files in place
# (updateInstead), and is refused unless it fast-forwards.
mkdir -p "$REPO"
chmod 700 /share/wset_coach
if [ ! -d "$REPO/.git" ]; then
  git init -q -b main "$REPO"
  echo "[wset-coach] created an empty workspace repository at $REPO; push the first copy from the Mac"
fi
git -C "$REPO" config receive.denyCurrentBranch updateInstead
git -C "$REPO" config receive.denyNonFastForwards true
git -C "$REPO" config receive.denyDeletes true
git -C "$REPO" config http.receivepack true
git -C "$REPO" config user.name "WSET Study Coach"
git -C "$REPO" config user.email "wset-coach@homeassistant.local"
if git -C "$REPO" rev-parse -q --verify HEAD >/dev/null; then
  echo "[wset-coach] workspace at $(git -C "$REPO" log -1 --format='%h %ci %s')"
  if [ -n "$(git -C "$REPO" status --porcelain)" ]; then
    echo "[wset-coach] WARNING: the workspace has uncommitted changes; Mac pushes will be refused until they are committed or removed"
  fi
fi

# Install / update the companion Home Assistant integration.
DEST=/homeassistant/custom_components/wset_mcp
mkdir -p "$DEST"
cp -R /integration/wset_mcp/. "$DEST/"
python3 -c "import socket;print('http://%s:8099' % socket.gethostname())" > "$DEST/.upstream"
cp /data/proxy_secret "$DEST/.proxy_secret"
chmod 600 "$DEST/.proxy_secret"
echo "[wset-coach] integration files installed in $DEST (restart Home Assistant after first install or update)"

export WSET_ROOT="$REPO"
export WSET_CONNECTOR_RUNTIME=/data
export WSET_CONNECTOR_CONFIG=/data/no-connection.json
export WSET_PROXY_SECRET_FILE=/data/proxy_secret
echo "[wset-coach] MCP server listening on :8099 (private network only); one JSON log line per request"
exec python3 /app/server.py http
