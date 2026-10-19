#!/bin/bash
# ==============================================================================
# Script untuk menjalankan Chromium Teams secara VISIBLE / HEADED di Kali Linux
# Membuka Teams langsung di layar desktop server agar bisa login normal.
# Port CDP 9222 tetap aktif sehingga AI Westpro langsung terhubung.
#
# Penggunaan:
#   bash run_teams_gui.sh          -> Buka browser biasa di layar desktop
#   bash run_teams_gui.sh --clean  -> Bersihkan cookies/cache corrupt (jika looping reload)
#   bash run_teams_gui.sh --reset  -> Reset total profil Teams dari nol
# ==============================================================================

echo "🛑 Menghentikan instance Chromium Teams latar belakang..."
pkill -f "remote-debugging-port=9222" 2>/dev/null
sleep 1

export DISPLAY="${DISPLAY:-:0}"
PROFILE_DIR="$HOME/.teams_profile"
mkdir -p "$PROFILE_DIR"

if [ "$1" == "--reset" ]; then
    echo "🧹 Mereset total profil Teams di $PROFILE_DIR..."
    rm -rf "$PROFILE_DIR"
    mkdir -p "$PROFILE_DIR"
elif [ "$1" == "--clean" ] || [ ! -f "$PROFILE_DIR/clean_init.done" ]; then
    echo "🧹 Membersihkan cookies & cache lama yang corrupt penyebab loop refresh..."
    rm -rf "$PROFILE_DIR/Default/Cookies"* \
           "$PROFILE_DIR/Default/Network" \
           "$PROFILE_DIR/Default/Local Storage" \
           "$PROFILE_DIR/Default/IndexedDB" \
           "$PROFILE_DIR/Default/Session Storage" \
           "$PROFILE_DIR/Default/Service Worker" \
           "$PROFILE_DIR/Default/Cache"
    touch "$PROFILE_DIR/clean_init.done"
fi

echo "🚀 Menjalankan Chromium Teams di Layar Server (DISPLAY: $DISPLAY)..."
echo "📁 Profil tersimpan di: $PROFILE_DIR"
echo "Silakan masukkan email & password Anda di jendela browser yang muncul di desktop."

/usr/bin/chromium \
  --remote-debugging-port=9222 \
  --remote-allow-origins="*" \
  --user-data-dir="$PROFILE_DIR" \
  --no-sandbox \
  --disable-dev-shm-usage \
  --password-store=basic \
  --window-size=1366,768 \
  "https://teams.microsoft.com" &

echo "✅ Chromium berjalan! Port 9222 aktif untuk AI Westpro."
