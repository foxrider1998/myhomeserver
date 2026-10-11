#!/usr/bin/env python3
"""Entrypoint PowMr Inverter Monitor & Server.

Pemakaian:
    python3 run.py              # Tampilkan live monitor di terminal (otomatis connect ke service lokal atau serial langsung)
    python3 run.py --direct     # Paksa baca serial port langsung
    python3 run.py --no-cli     # Jalankan hanya web server di background
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

from powmr_service import main as service_main, print_terminal_dashboard


def stream_from_api(url: str = "http://localhost:3005/api/state", interval: float = 2.0):
    """Membaca telemetry dari service background yang sudah jalan, tanpa rebutan serial port."""
    print(f"[INFO] Terhubung ke service background ({url}). Menampilkan live monitor...")
    time.sleep(0.5)
    try:
        while True:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "PowMrCLI/1.0"})
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        if data and "soc" in data:
                            print_terminal_dashboard(data)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                print(f"[WARN] Gagal mengambil data dari {url}: {e}. Mencoba lagi...", file=sys.stderr)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[INFO] Monitor dihentikan.")
        return 0


def main():
    parser = argparse.ArgumentParser(description="PowMr Hybrid Inverter Live Terminal & Gateway")
    parser.add_argument("--port", "-p", default="/dev/ttyS0", help="Serial port (/dev/ttyS0)")
    parser.add_argument("--web-port", "-w", type=int, default=3005, help="Port Web Server (default: 3005)")
    parser.add_argument("--interval", "-i", type=float, default=2.0, help="Interval refresh (detik)")
    parser.add_argument("--direct", action="store_true", help="Paksa buka serial port langsung tanpa cek service lokal")
    parser.add_argument("--no-cli", action="store_true", help="Hanya jalankan web server tanpa live monitor")
    args, unknown = parser.parse_known_args()

    # Jika user hanya menjalankan 'python3 run.py' tanpa flag khusus,
    # periksa apakah background service sudah aktif di port 3005:
    if not args.direct and not args.no_cli:
        api_url = f"http://localhost:{args.web_port}/api/state"
        try:
            with urllib.request.urlopen(api_url, timeout=1.0) as resp:
                if resp.status == 200:
                    return stream_from_api(api_url, interval=args.interval)
        except Exception:
            # Service belum jalan, lanjut ke service_main langsung
            pass

    return service_main()


if __name__ == "__main__":
    sys.exit(main())
