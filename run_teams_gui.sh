#!/bin/bash
# ==============================================================================
# Script untuk menjalankan Chromium Teams secara VISIBLE / HEADED di Kali Linux
# Membuka Teams langsung di layar desktop server agar bisa login normal
# (mendukung FIDO, PIN, Authenticator, dsb).
# Port CDP 9222 tetap aktif sehingga AI Westpro langsung terhubung.
# ==============================================================================

echo "🛑 Menghentikan instance Chromium Teams latar belakang..."
pkill -f "remote-debugging-port=9222" 2>/dev/null
sleep 1

export DISPLAY="${DISPLAY:-:0}"
PROFILE_DIR="$HOME/.teams_profile"
mkdir -p "$PROFILE_DIR"

echo "🚀 Menjalankan Chromium Teams di Layar Server (DISPLAY: $DISPLAY)..."
echo "📁 Profil tersimpan di: $PROFILE_DIR"
echo "Silakan login di jendela browser yang muncul di layar desktop Anda."

/usr/bin/chromium \
  --remote-debugging-port=9222 \
  --remote-allow-origins="*" \
  --user-data-dir="$PROFILE_DIR" \
  --no-sandbox \
  --disable-dev-shm-usage \
  --window-size=1366,768 \
  "https://teams.microsoft.com" &

echo "✅ Chromium berjalan di background! Port 9222 aktif untuk AI Westpro."
