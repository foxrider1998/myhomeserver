#!/usr/bin/env bash
set -e

CLOUDFLARED="/home/ares/bin/cloudflared"

if [ ! -f "$HOME/.cloudflared/cert.pem" ]; then
    echo "[!] File cert.pem belum ada. Selesaikan login browser terlebih dahulu."
    exit 1
fi

echo "[+] Membuat tunnel 'westpro-home'..."
$CLOUDFLARED tunnel create westpro-home || true

TUNNEL_ID=$($CLOUDFLARED tunnel list | grep "westpro-home" | awk '{print $1}')
echo "[+] Tunnel ID: $TUNNEL_ID"

echo "[+] Menghubungkan DNS westpro.my.id ke tunnel..."
$CLOUDFLARED tunnel route dns westpro-home westpro.my.id || true

cat <<EOF > ~/.cloudflared/config.yml
tunnel: $TUNNEL_ID
credentials-file: /home/ares/.cloudflared/${TUNNEL_ID}.json

ingress:
  - hostname: westpro.my.id
    service: http://localhost:3005
  - service: http_status:404
EOF

echo "[+] Mengonfigurasi systemd service untuk cloudflared..."
cat <<EOF > /home/ares/.config/systemd/user/cloudflared.service
[Unit]
Description=Cloudflare Tunnel for westpro.my.id
After=network.target

[Service]
Type=simple
ExecStart=/home/ares/bin/cloudflared tunnel --config /home/ares/.cloudflared/config.yml run
Restart=always
RestartSec=5s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now cloudflared.service

echo "[OK] Tunnel berhasil dikonfigurasi dan aktif di cloudflared.service!"
echo "Buka: https://westpro.my.id"
