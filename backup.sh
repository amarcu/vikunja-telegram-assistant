#!/usr/bin/env sh
# Nightly backup of the Vikunja SQLite DB.
#
# Writes a CONSISTENT single-file copy (sqlite3 VACUUM INTO, safe to run while
# Vikunja is live), keeps the last N locally, and optionally pushes one copy
# off-box. The off-box push is best-effort: it skips quietly when the target is
# unreachable (e.g. a desktop that's asleep), so cron never errors over it.
#
# Tunables (env, or set them in the repo's .env which this sources if present):
#   VIKUNJA_BACKUP_DIR     where to write backups   (default ~/vikunja-backups)
#   VIKUNJA_BACKUP_KEEP    how many copies to keep  (default 7)
#   VIKUNJA_BACKUP_REMOTE  rsync target, or empty   (default empty = local only)
#                          e.g. alex@alex-ubuntu-pc:vikunja-backups/
#   COMPOSE_PROJECT_NAME   compose project name     (default = repo dir name)
set -eu

# cron runs with a minimal PATH — make sure docker/rsync/ssh are found.
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck disable=SC1091
[ -f "$SCRIPT_DIR/.env" ] && . "$SCRIPT_DIR/.env" 2>/dev/null || true

PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$SCRIPT_DIR")}"
VOLUME="${PROJECT}_vikunja_db"
OUT_DIR="${VIKUNJA_BACKUP_DIR:-$HOME/vikunja-backups}"
KEEP="${VIKUNJA_BACKUP_KEEP:-7}"
REMOTE="${VIKUNJA_BACKUP_REMOTE:-}"

mkdir -p "$OUT_DIR"
STAMP=$(date +%Y%m%d-%H%M%S)
NAME="vikunja-$STAMP.db"

# Consistent online backup via a throwaway alpine + sqlite. VACUUM INTO produces
# a clean, fully-checkpointed single file (no WAL) in one transaction. The
# container is root (apk needs it), so hand the file back to the host user.
HOST_UID=$(id -u)
HOST_GID=$(id -g)
docker run --rm -v "$VOLUME":/db -v "$OUT_DIR":/out alpine:3 sh -c \
  "apk add --no-cache -q sqlite && sqlite3 /db/vikunja.db \"VACUUM INTO '/out/$NAME'\" && chown $HOST_UID:$HOST_GID '/out/$NAME'"

# Rotate: keep the newest $KEEP, delete the rest.
ls -1t "$OUT_DIR"/vikunja-*.db 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f

# Best-effort off-box copy.
if [ -n "$REMOTE" ]; then
  if timeout 25 rsync -az \
       -e 'ssh -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 -o StrictHostKeyChecking=accept-new' \
       "$OUT_DIR/$NAME" "$REMOTE" 2>/dev/null; then
    echo "$(date -Is) backup ok: $NAME (pushed to $REMOTE)"
  else
    echo "$(date -Is) backup ok: $NAME (off-box push skipped — target unreachable)"
  fi
else
  echo "$(date -Is) backup ok: $NAME (local only)"
fi
