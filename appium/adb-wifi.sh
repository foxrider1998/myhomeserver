#!/usr/bin/env bash
# Hubungkan HP Android via ADB over Wi-Fi, setelah pairing awal lewat kabel USB.
# Berguna kalau HP nanti ditaruh di ruangan lain, masih 1 jaringan LAN/Wi-Fi
# dengan ThinkCentre.
set -euo pipefail

DEVICE_IP="${1:?Usage: adb-wifi.sh <device-ip> [port]}"
PORT="${2:-5555}"

adb tcpip "$PORT"
sleep 2
adb connect "${DEVICE_IP}:${PORT}"
adb devices
