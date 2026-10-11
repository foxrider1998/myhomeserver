#!/usr/bin/env bash
# ==============================================================================
# Westpro Home Server - Auto-Deploy Watcher
# Periodically checks for new commits on origin/main and triggers deploy.service
# ==============================================================================
set -euo pipefail

REPO_DIR="/home/ares/Documents/myserver"
cd "$REPO_DIR"

mkdir -p "$REPO_DIR/data"

# Ensure we are inside git repository
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  exit 0
fi

# Fetch origin main quietly
if ! git fetch -q origin main 2>/dev/null; then
  exit 0
fi

LOCAL=$(git rev-parse HEAD 2>/dev/null || echo "")
REMOTE=$(git rev-parse origin/main 2>/dev/null || echo "")

if [ -n "$REMOTE" ] && [ -n "$LOCAL" ]; then
  # Cek apakah origin/main memiliki commit baru yang belum ada di branch lokal
  if ! git merge-base --is-ancestor "$REMOTE" "$LOCAL" 2>/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Watcher mendeteksi perubahan pada origin/main (Local: ${LOCAL:0:7}, Remote: ${REMOTE:0:7}). Memulai deploy..." >> "$REPO_DIR/data/deploy.log"
    systemctl --user start deploy.service
  fi
fi
