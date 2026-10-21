#!/bin/bash
# setup_sms_otp.sh
# Setup SMS OTP Service di server
# Jalankan: bash setup_sms_otp.sh <IP_HP_ANDROID>
#
# Contoh: bash setup_sms_otp.sh 192.168.1.50

set -e

PHONE_IP="${1:-192.168.1.x}"
SERVER_DIR="/home/ares/Documents/myserver"
SERVICE_FILE="/etc/systemd/system/sms_otp.service"

echo "=== SMS OTP Service Setup ==="
echo "Target HP IP: $PHONE_IP"
echo ""

# 1. Cek adb tersedia
if ! command -v adb &>/dev/null; then
    echo "→ Installing adb..."
    sudo apt-get install -y android-tools-adb
else
    echo "✓ adb sudah terinstall: $(adb version | head -1)"
fi

# 2. Copy script ke server
echo "→ Script sudah ada di $SERVER_DIR/sms_otp_service.py"

# 3. Update IP HP di service file
sed "s/192.168.1.x/$PHONE_IP/g" "$SERVER_DIR/sms_otp.service" | sudo tee "$SERVICE_FILE" > /dev/null

# 4. Enable & start service
sudo systemctl daemon-reload
sudo systemctl enable sms_otp
sudo systemctl restart sms_otp

echo ""
echo "=== Setup Selesai ==="
echo "✓ Service sms_otp berjalan"
echo ""
echo "Test koneksi ADB ke HP:"
echo "  adb connect $PHONE_IP:5555"
echo "  adb -s $PHONE_IP:5555 devices"
echo ""
echo "API tersedia di:"
echo "  GET  http://localhost:3010/otp/085743019186"
echo "  GET  http://localhost:3010/otp/status"
echo "  POST http://localhost:3010/otp/pull"
echo ""
echo "Cek log:"
echo "  journalctl -u sms_otp -f"

# 5. Coba koneksi ADB
echo ""
echo "→ Mencoba koneksi ADB ke $PHONE_IP:5555..."
if adb connect "$PHONE_IP:5555" 2>&1 | grep -q "connected"; then
    echo "✓ ADB berhasil terhubung!"
    echo ""
    echo "→ Test pull SMS:"
    python3 "$SERVER_DIR/sms_otp_service.py" --adb-host "$PHONE_IP" --pull
else
    echo "⚠ ADB belum terhubung."
    echo ""
    echo "Pastikan di HP Samsung:"
    echo "  1. Settings → About phone → ketuk 'Build number' 7x"
    echo "  2. Settings → Developer options → aktifkan 'USB Debugging'"
    echo "  3. Settings → Developer options → aktifkan 'Wireless debugging'"
    echo "  4. Catat IP HP dari Settings → About phone → Status → IP address"
fi
