#!/usr/bin/env bash
# Install & jalankan Appium sebagai systemd service di host (Ubuntu Server),
# khusus untuk remote control HP Android fisik lewat USB/ADB - bukan emulator.
# Dijalankan langsung di host (bukan Docker) karena passthrough USB/ADB ke
# container itu rewel (butuh --privileged atau device mapping presisi); di host
# tinggal colok kabel.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Jalankan sebagai root/sudo" >&2
  exit 1
fi

# Node.js 20 LTS
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs android-tools-adb android-tools-fastboot

npm install -g appium
appium driver install uiautomator2

id -u appium &>/dev/null || useradd -r -m -G plugdev,dialout appium

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "$SCRIPT_DIR/appium.service" /etc/systemd/system/appium.service
systemctl daemon-reload
systemctl enable --now appium.service

bash "$SCRIPT_DIR/disable-usb-autosuspend.sh"

echo
echo "Appium terpasang. Cek status: systemctl status appium"
echo "Colok HP Android via USB, aktifkan USB debugging di HP, lalu: adb devices"
echo "Kalau device belum muncul/unauthorized, cek popup 'Allow USB debugging' di HP."
