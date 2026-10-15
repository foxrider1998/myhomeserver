#!/usr/bin/env bash
# Pasang 9Router sebagai systemd USER service (sama seperti appium.service / hermes-agent.service).
# Jalankan sebagai user server (ares), BUKAN root:  bash 9router/setup.sh
# 9Router dipakai hanya untuk speech-to-text; LLM teks tetap lewat Sumopod.
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
  echo "Jalankan sebagai user biasa (ares), bukan root." >&2
  exit 1
fi

command -v node >/dev/null || { echo "Node.js belum terpasang (butuh >= 18)." >&2; exit 1; }
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
[ "$NODE_MAJOR" -ge 18 ] || { echo "Node.js terlalu lama (v$NODE_MAJOR); butuh >= 18." >&2; exit 1; }

npm install -g 9router
BIN="$(command -v 9router)"
echo "9router terpasang di: $BIN"

DATA_DIR="$HOME/.9router"
mkdir -p "$DATA_DIR"
ENV_FILE="$DATA_DIR/env"
if [ ! -f "$ENV_FILE" ]; then
  umask 077
  PASS="$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-16)"
  cat > "$ENV_FILE" <<ENVEOF
INITIAL_PASSWORD=$PASS
JWT_SECRET=$(openssl rand -hex 32)
API_KEY_SECRET=$(openssl rand -hex 32)
REQUIRE_API_KEY=true
ENVEOF
  echo "Kata sandi dashboard 9Router (login pertama): $PASS   (tersimpan di $ENV_FILE)"
else
  echo "$ENV_FILE sudah ada, tidak diubah."
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$HOME/.config/systemd/user"
# Sesuaikan lokasi binary bila bukan /home/ares/nodejs/bin/9router
sed "s|/home/ares/nodejs/bin/9router|$BIN|" "$SCRIPT_DIR/9router.service" > "$HOME/.config/systemd/user/9router.service"

systemctl --user daemon-reload
systemctl --user enable --now 9router.service
loginctl enable-linger "$USER" 2>/dev/null || echo "Catatan: jalankan 'sudo loginctl enable-linger $USER' agar layanan hidup tanpa login."

sleep 4
systemctl --user --no-pager status 9router.service | head -8 || true
echo
echo "Dashboard (hanya localhost). Dari laptop buat tunnel SSH lalu buka http://localhost:20128 :"
echo "  ssh -L 20128:127.0.0.1:20128 $USER@<ip-server>"
echo "Langkah berikutnya: sambungkan provider & buat API key di dashboard, lalu isi STT_* di .env Westpro."
