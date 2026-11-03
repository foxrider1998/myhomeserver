#!/usr/bin/env python3
"""mail_apikey.py - Kelola API key Westpro Mail untuk satu akun langsung di server (tanpa login web).

Dipakai bila kunci perlu dibuat untuk akun pemilik (mis. areskretindo@gmail.com) tanpa harus login di halaman Mail.
Harus dijalankan di server (butuh akses file data/history.db), jadi hanya orang dengan akses shell yang bisa membuat kunci.

  python3 mail_apikey.py areskretindo@gmail.com            # buat kunci baru (menggantikan kunci lama akun itu)
  python3 mail_apikey.py areskretindo@gmail.com --status   # cek apakah akun punya kunci aktif
  python3 mail_apikey.py areskretindo@gmail.com --revoke   # cabut kunci

Kunci hanya ditampilkan sekali di terminal; di database yang tersimpan hanya hash SHA-256.
Satu akun = satu kunci aktif; membuat kunci baru langsung mematikan kunci lama.
Kunci hanya berlaku untuk /api/webmail/* (header X-API-Key).
"""

from __future__ import annotations

import argparse
import sys

import webmail as wm
from history_db import DB_PATH, HistoryDatabase


def main() -> int:
    ap = argparse.ArgumentParser(description="Kelola API key Westpro Mail untuk satu akun.")
    ap.add_argument("email", help="email akun Westpro (mis. areskretindo@gmail.com)")
    ap.add_argument("--status", action="store_true", help="hanya tampilkan status kunci")
    ap.add_argument("--revoke", action="store_true", help="cabut kunci aktif")
    ap.add_argument("--yes", "-y", action="store_true", help="ganti kunci yang sudah ada tanpa bertanya")
    ap.add_argument("--db", default=DB_PATH, help="path history.db (default: data/history.db)")
    a = ap.parse_args()

    db = HistoryDatabase(a.db)
    with db.lock, db._get_conn() as conn:
        user = conn.execute("SELECT id, email, name, role FROM users WHERE lower(email) = ?", (a.email.strip().lower(),)).fetchone()
        total = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    if not user:
        print(f"Akun '{a.email}' tidak ditemukan ({total} akun terdaftar). Periksa ejaan email.", file=sys.stderr)
        return 1

    has_key = wm.has_api_key(db, user["id"])
    who = f"{user['email']} (id {user['id']}, role {user['role']})"

    if a.status:
        print(f"{who}: {'ADA kunci API aktif' if has_key else 'belum punya kunci API'}")
        return 0
    if a.revoke:
        wm.revoke_api_key(db, user["id"])
        print(f"{who}: kunci API dicabut." if has_key else f"{who}: tidak ada kunci untuk dicabut.")
        return 0

    if has_key and not a.yes:
        if not sys.stdin.isatty():
            print(f"{who} sudah punya kunci aktif. Jalankan ulang dengan --yes untuk menggantinya.", file=sys.stderr)
            return 2
        if input(f"{who} sudah punya kunci aktif; kunci lama akan berhenti berfungsi. Lanjut? [y/N] ").strip().lower() != "y":
            print("Dibatalkan.")
            return 0

    key = wm.create_api_key(db, user["id"])
    print(f"Kunci API untuk {who}:")
    print(key)
    print("Simpan sekarang; kunci tidak ditampilkan lagi. Pakai di header:  X-API-Key: " + key[:8] + "...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
