#!/usr/bin/env bash
# Matikan USB autosuspend supaya koneksi ADB ke HP fisik tidak sering
# terputus karena sistem menganggap port USB idle.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Jalankan sebagai root/sudo" >&2
  exit 1
fi

cat > /etc/udev/rules.d/50-usb-autosuspend-disable.rules <<'EOF'
ACTION=="add", SUBSYSTEM=="usb", TEST=="power/control", ATTR{power/control}="on"
EOF

udevadm control --reload-rules
udevadm trigger

echo "udev rule terpasang (efektif untuk device yang baru dicolok setelah ini)."
echo "Untuk nonaktifkan autosuspend permanen di level kernel (butuh reboot),"
echo "tambahkan 'usbcore.autosuspend=-1' ke GRUB_CMDLINE_LINUX di /etc/default/grub"
echo "lalu jalankan: update-grub"
