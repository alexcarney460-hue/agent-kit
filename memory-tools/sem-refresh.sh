#!/bin/bash
# sem-refresh — keep the memory semantic layer current.
#
# Run hourly (launchd/cron). Three steps, in this order:
#   1. prune      drop mentions whose target no longer exists
#   2. backfill   link new hivemind learnings / observations / file memories
#   3. artifacts  re-index files on disk
#
# Deliberately uses prune+upsert rather than --reset: a blanket delete leaves a
# window where the layer is empty, and a session querying during that window
# silently gets no entity boosts.
#
# Never fails loudly. A broken refresh must degrade recall to lexical, not break
# a session. Niced because this box has a jetsam history — memory tooling must
# never compete with a build.

set -uo pipefail

TOOLS="$HOME/.claude/memory-tools"
LOG="$TOOLS/sem-refresh.log"
LOCK="$TOOLS/.sem-refresh.lock"
PY=/opt/homebrew/bin/python3   # EDIT: a python3 that has numpy/scipy installed

log() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*" >> "$LOG"; }

# Keep the log from growing without bound.
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 262144 ]; then
  tail -n 300 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

# Single instance. A stale lock from a killed run is cleared after 30 minutes.
if [ -d "$LOCK" ]; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then
    log "WARN clearing stale lock"
    rmdir "$LOCK" 2>/dev/null || true
  else
    log "SKIP another refresh is running"
    exit 0
  fi
fi
mkdir "$LOCK" 2>/dev/null || { log "SKIP could not take lock"; exit 0; }
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

cd "$TOOLS" || { log "FATAL $TOOLS missing"; exit 0; }

started=$(date +%s)
status=ok

run() {
  local name="$1"; shift
  local out
  if ! out=$(nice -n 10 "$PY" "$@" 2>&1); then
    log "ERROR $name: $(printf '%s' "$out" | tail -n 3 | tr '\n' ' ')"
    status=degraded
    return 1
  fi
  # printf with a trailing newline: without it `read` silently drops the final
  # line, which is the whole output when a step reports a single summary line.
  printf '%s\n' "$out" | tail -n 2 | while IFS= read -r line; do
    [ -n "$line" ] && log "  $name: $line"
  done
  touch "$LOCK" 2>/dev/null || true   # keep the stale-lock clock honest during long steps
  return 0
}

# Order matters: discover creates entities, so it must run BEFORE backfill and
# artifacts, or a newly-found entity waits an hour for its first mentions.
run prune     sem.py prune
run discover  sem_discover.py
run backfill  sem_backfill.py
run artifacts sem_artifacts.py
# embeddings last: incremental (text_hash), Ollama-down = clean no-op
run embed     embed_index.py --update
# alias proposals: MinHash candidates -> sem-alias-candidates.md review queue
run aliases   alias_propose.py

counts=$(nice -n 10 "$PY" - <<'EOF' 2>/dev/null
import os, sqlite3
db = os.path.expanduser("~/.claude/memory-tools/semantic.db")
c = sqlite3.connect(db)
e = c.execute("SELECT count(*) FROM entity").fetchone()[0]
m = c.execute("SELECT count(*) FROM mention").fetchone()[0]
print(f"{e} entities / {m} mentions")
EOF
)

log "DONE ${status} in $(( $(date +%s) - started ))s — ${counts:-unknown}"
exit 0
