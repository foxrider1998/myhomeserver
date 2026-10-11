#!/usr/bin/env bash
# ==============================================================================
# Westpro Home Server - Automated Deployment Script
# Automatically pulls and deploys latest changes from origin/main
# ==============================================================================
set -euo pipefail

REPO_DIR="/home/ares/Documents/myserver"
LOG_FILE="$REPO_DIR/data/deploy.log"
STATUS_FILE="$REPO_DIR/data/deploy.json"
REQ_SUM_FILE="$REPO_DIR/data/requirements.sha256"
BACKUP_BASE="$REPO_DIR/.deploy_backups"

mkdir -p "$REPO_DIR/data" "$BACKUP_BASE"

FORCE=false
SOURCE="manual"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)
      FORCE=true
      shift
      ;;
    --source)
      SOURCE="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done

log() {
  local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
  echo "$msg"
  echo "$msg" >> "$LOG_FILE"
}

write_status() {
  local status="$1"
  local hash="$2"
  local msg="$3"
  local author="$4"
  local error_msg="${5:-}"

  cat <<EOF > "$STATUS_FILE"
{
  "status": "$status",
  "timestamp": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')",
  "commit_hash": "$hash",
  "commit_short": "${hash:0:7}",
  "commit_message": $(python3 -c "import json, sys; print(json.dumps(sys.argv[1]))" "$msg"),
  "commit_author": $(python3 -c "import json, sys; print(json.dumps(sys.argv[1]))" "$author"),
  "trigger_source": "$SOURCE",
  "error": $(python3 -c "import json, sys; print(json.dumps(sys.argv[1]))" "$error_msg")
}
EOF
}

cd "$REPO_DIR"

log "================================================================="
log "Starting deployment (source: $SOURCE, force: $FORCE)..."

# 1. Fetch origin main
log "Fetching latest changes from origin/main..."
if ! git fetch origin main >> "$LOG_FILE" 2>&1; then
  log "ERROR: Gagal git fetch origin main!"
  write_status "failed" "$(git rev-parse HEAD 2>/dev/null || echo 'unknown')" "Fetch failed" "Unknown" "Git fetch origin main failed"
  exit 1
fi

LOCAL_HASH=$(git rev-parse HEAD)
REMOTE_HASH=$(git rev-parse origin/main)

log "Local commit : $LOCAL_HASH"
log "Remote commit: $REMOTE_HASH"

if [ "$LOCAL_HASH" = "$REMOTE_HASH" ] && [ "$FORCE" = false ]; then
  log "Server sudah up-to-date dengan origin/main ($LOCAL_HASH). Tidak perlu deploy."
  COMMIT_MSG=$(git log -1 --pretty=%B "$LOCAL_HASH" | head -n 1)
  AUTHOR=$(git log -1 --pretty=%an "$LOCAL_HASH")
  write_status "up_to_date" "$LOCAL_HASH" "$COMMIT_MSG" "$AUTHOR"
  exit 0
fi

# 2. Backup any uncommitted tracked files before pull
BACKUP_DIR="$BACKUP_BASE/backup_$(date '+%Y%m%d_%H%M%S')"
mkdir -p "$BACKUP_DIR"
log "Membuat backup perubahan lokal ke $BACKUP_DIR..."
git diff --name-only | while read -r file; do
  if [ -n "$file" ] && [ -f "$file" ]; then
    mkdir -p "$BACKUP_DIR/$(dirname "$file")"
    cp "$file" "$BACKUP_DIR/$file" 2>/dev/null || true
  fi
done

# 3. Handle local uncommitted tracked changes safely (tanpa -u agar untracked tetap aman)
HAS_STASH=false
if ! git diff --quiet || ! git diff --cached --quiet; then
  log "Menyimpan perubahan lokal ke git stash..."
  git stash push -m "auto-deploy-stash-$(date +%s)" >> "$LOG_FILE" 2>&1 || true
  HAS_STASH=true
fi

# 4. Pull origin/main
log "Mengambil perubahan baru dari origin/main..."
if ! git pull --rebase origin main >> "$LOG_FILE" 2>&1; then
  log "Rebase gagal, mencoba merge standard..."
  git rebase --abort 2>/dev/null || true
  if ! git pull origin main >> "$LOG_FILE" 2>&1; then
    log "Pull origin main gagal. Mengembalikan stash jika ada..."
    if [ "$HAS_STASH" = true ]; then
      git stash pop >> "$LOG_FILE" 2>&1 || true
    fi
    write_status "failed" "$LOCAL_HASH" "Merge conflict" "Unknown" "Git pull failed due to conflicts"
    exit 1
  fi
fi

if [ "$HAS_STASH" = true ]; then
  log "Mengembalikan perubahan lokal dari stash..."
  git stash pop >> "$LOG_FILE" 2>&1 || true
fi

NEW_HASH=$(git rev-parse HEAD)
COMMIT_MSG=$(git log -1 --pretty=%B "$NEW_HASH" | head -n 1)
AUTHOR=$(git log -1 --pretty=%an "$NEW_HASH")
log "Berhasil sinkronisasi ke commit $NEW_HASH: $COMMIT_MSG (oleh $AUTHOR)"

# 5. Update service files if present
log "Sinkronisasi file service systemd..."
mkdir -p /home/ares/.config/systemd/user
for s in "$REPO_DIR"/*.service; do
  if [ -f "$s" ]; then
    cp "$s" /home/ares/.config/systemd/user/
  fi
done

# 6. Update Python dependencies only if requirements changed
CURRENT_REQ_SUM=$(sha256sum hermes-agent/requirements.txt mail-collector/requirements.txt 2>/dev/null || echo "")
LAST_REQ_SUM=$(cat "$REQ_SUM_FILE" 2>/dev/null || echo "")

if [ "$CURRENT_REQ_SUM" != "$LAST_REQ_SUM" ] && [ -n "$CURRENT_REQ_SUM" ]; then
  VENV_PIP="$REPO_DIR/.venv/bin/pip"
  if [ -x "$VENV_PIP" ]; then
    log "Perubahan dependensi terdeteksi. Menjalankan pip install..."
    "$VENV_PIP" install -q -r "$REPO_DIR/hermes-agent/requirements.txt" >> "$LOG_FILE" 2>&1 || true
    "$VENV_PIP" install -q -r "$REPO_DIR/mail-collector/requirements.txt" >> "$LOG_FILE" 2>&1 || true
    echo "$CURRENT_REQ_SUM" > "$REQ_SUM_FILE"
  fi
else
  log "Dependensi Python tidak berubah, melewati pip install."
fi

# 7. Reload systemd & restart services
log "Reloading systemd user daemon & restarting services..."
systemctl --user daemon-reload >> "$LOG_FILE" 2>&1 || true

systemctl --user restart powmr.service hermes-agent.service anti-sleep.service >> "$LOG_FILE" 2>&1 || true

# Only restart mail-collector if config.json exists
if [ -f "$REPO_DIR/mail-collector/config.json" ]; then
  systemctl --user restart mail-collector.service >> "$LOG_FILE" 2>&1 || true
fi

# 8. Record success
write_status "success" "$NEW_HASH" "$COMMIT_MSG" "$AUTHOR"
log "=== Deploy BERHASIL pada $(date '+%Y-%m-%d %H:%M:%S') ==="
log "================================================================="
exit 0
