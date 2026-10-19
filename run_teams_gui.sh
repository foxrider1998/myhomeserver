#!/bin/bash
# ==============================================================================
# Script untuk menjalankan Chromium Teams secara VISIBLE / HEADED di Kali Linux
# Mengatasi masalah looping reload Microsoft Teams dengan mengizinkan cookie pihak ketiga.
# ==============================================================================

echo "🛑 1. Menghentikan instance Chromium Teams yang sedang berjalan..."
pkill -9 -f "chromium" 2>/dev/null || true
sleep 1

export DISPLAY="${DISPLAY:-:0}"
PROFILE_DIR="$HOME/.teams_profile"
mkdir -p "$PROFILE_DIR/Default"

echo "🧹 2. Menghapus cookies & cache lama yang corrupt..."
rm -rf "$PROFILE_DIR/Default/Cookies"* \
       "$PROFILE_DIR/Default/Network" \
       "$PROFILE_DIR/Default/Local Storage" \
       "$PROFILE_DIR/Default/IndexedDB" \
       "$PROFILE_DIR/Default/Session Storage" \
       "$PROFILE_DIR/Default/Service Worker" \
       "$PROFILE_DIR/Default/Cache" 2>/dev/null || true

echo "🍪 3. Menyetel izin cookie Microsoft di konfigurasi Chromium..."
cat << 'EOF' > "$PROFILE_DIR/Default/Preferences"
{
  "profile": {
    "cookie_controls_mode": 0,
    "default_content_setting_values": {
      "cookies": 1
    },
    "content_settings": {
      "exceptions": {
        "cookies": {
          "https://teams.microsoft.com,*": { "setting": 1 },
          "https://teams.cloud.microsoft,*": { "setting": 1 },
          "https://login.microsoftonline.com,*": { "setting": 1 },
          "[*.]microsoft.com,*": { "setting": 1 },
          "[*.]microsoftonline.com,*": { "setting": 1 },
          "[*.]teams.microsoft.com,*": { "setting": 1 },
          "[*.]cloud.microsoft,*": { "setting": 1 }
        }
      }
    }
  }
}
EOF

echo "🚀 4. Membuka Chromium di layar desktop (DISPLAY: $DISPLAY)..."
echo "Silakan login di jendela browser yang muncul di monitor desktop Anda."

/usr/bin/chromium \
  --remote-debugging-port=9222 \
  --remote-allow-origins="*" \
  --user-data-dir="$PROFILE_DIR" \
  --disable-features=BlockThirdPartyCookies,ThirdPartyCookieDeprecation,Translate,OptimizationHints \
  --no-sandbox \
  --disable-dev-shm-usage \
  --password-store=basic \
  --window-size=1366,768 \
  "https://teams.cloud.microsoft" &

echo "✅ Chromium sudah dijalankan! Port 9222 aktif untuk AI Westpro."
