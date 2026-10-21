#!/bin/bash
# deploy_sms_otp.sh
# Dijalankan di server: bash deploy_sms_otp.sh <IP_HP>
# Contoh: bash deploy_sms_otp.sh 192.168.1.50

set -e
PHONE_IP="${1:?Usage: bash deploy_sms_otp.sh <IP_HP_SAMSUNG>}"
SERVER_DIR="/home/ares/Documents/myserver"
SVC="sms_otp"
PORT=3010

echo "=== Deploy SMS OTP Service ==="

# 1. Install adb jika belum ada
if ! command -v adb &>/dev/null; then
    echo "→ Install adb..."
    sudo apt-get install -y android-tools-adb
fi
echo "✓ adb: $(adb version | head -1)"

# 2. Update IP di script
sed -i "s|ADB_HOST = \"192.168.1.x\"|ADB_HOST = \"$PHONE_IP\"|g" \
    "$SERVER_DIR/sms_otp_service.py"
echo "✓ IP HP diset ke $PHONE_IP"

# 3. Buat systemd service
cat > /tmp/${SVC}.service << EOF
[Unit]
Description=SMS OTP Service via ADB over WiFi
After=network.target

[Service]
Type=simple
User=ares
WorkingDirectory=$SERVER_DIR
ExecStart=/usr/bin/python3 $SERVER_DIR/sms_otp_service.py --port $PORT
Restart=always
RestartSec=15
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
sudo mv /tmp/${SVC}.service /etc/systemd/system/${SVC}.service
echo "✓ systemd service dibuat"

# 4. Reload & enable service
sudo systemctl daemon-reload
sudo systemctl enable $SVC
sudo systemctl restart $SVC
sleep 2
sudo systemctl status $SVC --no-pager | head -15
echo "✓ Service berjalan di port $PORT"

# 5. Coba koneksi ADB
echo ""
echo "→ Koneksi ADB ke $PHONE_IP:5555..."
adb connect "$PHONE_IP:5555" || true

# 6. Test API
sleep 2
echo ""
echo "→ Test API..."
curl -s "http://localhost:$PORT/otp/status" | python3 -m json.tool || true

echo ""
echo "=== SELESAI ==="
echo "API aktif di: http://localhost:$PORT"
echo "  GET /otp/085743019186"
echo "  GET /otp/status"
echo "  POST /otp/pull"
echo ""
echo "Untuk expose ke westpro.my.id, tambahkan ke nginx:"
echo "  location /otp/ { proxy_pass http://localhost:$PORT; }"
