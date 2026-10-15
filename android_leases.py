"""android_leases.py - Pembagian perangkat Android per user ("sewa"/lease).

Aturan:
  * Satu user memegang satu perangkat pada satu waktu (pemilik server dikecualikan dari batas ini).
  * "Ambil perangkat kosong" memilih otomatis perangkat online yang belum dipakai user lain.
  * Sewa otomatis kedaluwarsa bila tidak ada aktivitas (default 30 menit) sehingga perangkat kembali kosong.
  * Semua aksi (layar, tap, adb shell, dsb.) divalidasi: hanya pemegang sewa (atau pemilik server) yang boleh.
  * Tiap perangkat mendapat "slot" tetap -> port Appium unik (systemPort/chromedriver/mjpeg) agar beberapa
    sesi Appium pada SATU server Appium bisa berjalan paralel tanpa tabrakan.
"""

from __future__ import annotations

import time

IDLE_TTL_SEC = 30 * 60
MAX_PER_USER = 1
SYSTEM_PORT_BASE = 8200
CHROMEDRIVER_PORT_BASE = 9515
MJPEG_PORT_BASE = 7810
MAX_SLOTS = 90


def ensure_tables(db) -> None:
    with db.lock, db._get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS device_leases (
                serial TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                leased_at REAL NOT NULL,
                last_activity REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS device_slots (
                slot INTEGER PRIMARY KEY AUTOINCREMENT,
                serial TEXT UNIQUE NOT NULL
            )
        """)
        conn.commit()


def expire_stale(db, ttl: int = IDLE_TTL_SEC) -> None:
    ensure_tables(db)
    cutoff = time.time() - ttl
    with db.lock, db._get_conn() as conn:
        conn.execute("DELETE FROM device_leases WHERE last_activity < ?", (cutoff,))
        conn.commit()


def all_leases(db) -> dict:
    """serial -> {user_id, name, last_activity, idle_sec}"""
    expire_stale(db)
    with db.lock, db._get_conn() as conn:
        rows = conn.execute("""
            SELECT l.serial, l.user_id, l.leased_at, l.last_activity, u.name, u.email
            FROM device_leases l LEFT JOIN users u ON u.id = l.user_id
        """).fetchall()
    now = time.time()
    return {r["serial"]: {"user_id": r["user_id"], "name": r["name"] or (r["email"] or "").split("@")[0],
                          "leased_at": r["leased_at"], "idle_sec": int(now - r["last_activity"])} for r in rows}


def lease_of(db, user_id: int) -> str | None:
    leases = all_leases(db)
    for serial, l in leases.items():
        if l["user_id"] == user_id:
            return serial
    return None


def claim(db, user_id: int, devices: list, serial: str | None = None, is_owner: bool = False) -> tuple[dict | None, str]:
    """Ambil perangkat. devices = daftar dari adb (dengan field serial, state). Mengembalikan (lease, pesan)."""
    leases = all_leases(db)
    online = [d for d in devices if d.get("state") == "device"]
    mine = [s for s, l in leases.items() if l["user_id"] == user_id]

    if serial:
        dev = next((d for d in online if d["serial"] == serial), None)
        if not dev:
            return None, "Perangkat tidak ditemukan atau belum online/terotorisasi."
        holder = leases.get(serial)
        if holder and holder["user_id"] != user_id:
            return None, f"Perangkat sedang dipakai {holder['name'] or 'user lain'}."
        if mine and serial not in mine and not is_owner and len(mine) >= MAX_PER_USER:
            return None, "Anda sudah memegang perangkat lain. Lepas dulu sebelum mengambil yang baru."
        target = serial
    else:
        # idempotent: bila sudah punya sewa yang perangkatnya masih online, pakai itu
        for s in mine:
            if any(d["serial"] == s for d in online):
                touch(db, user_id, s)
                return {"serial": s, "reused": True}, "Anda sudah memegang perangkat ini."
        free = [d for d in online if d["serial"] not in leases]
        if not free:
            return None, "Semua perangkat sedang dipakai atau belum ada perangkat online. Coba lagi nanti."
        # pilih yang baterainya paling penuh; USB lebih disukai daripada Wi-Fi (lebih stabil)
        free.sort(key=lambda d: ((":" in d["serial"]), -(d.get("battery") or 0), d["serial"]))
        target = free[0]["serial"]

    now = time.time()
    with db.lock, db._get_conn() as conn:
        if not is_owner:
            conn.execute("DELETE FROM device_leases WHERE user_id = ? AND serial != ?", (user_id, target))
        conn.execute("""
            INSERT INTO device_leases (serial, user_id, leased_at, last_activity) VALUES (?, ?, ?, ?)
            ON CONFLICT(serial) DO UPDATE SET last_activity = excluded.last_activity
        """, (target, user_id, now, now))
        conn.commit()
    return {"serial": target, "reused": False}, "Perangkat ditetapkan untuk Anda."


def release(db, user_id: int, serial: str | None = None, force: bool = False) -> int:
    ensure_tables(db)
    with db.lock, db._get_conn() as conn:
        if serial and force:
            cur = conn.execute("DELETE FROM device_leases WHERE serial = ?", (serial,))
        elif serial:
            cur = conn.execute("DELETE FROM device_leases WHERE serial = ? AND user_id = ?", (serial, user_id))
        else:
            cur = conn.execute("DELETE FROM device_leases WHERE user_id = ?", (user_id,))
        conn.commit()
        return cur.rowcount


def touch(db, user_id: int, serial: str) -> None:
    with db.lock, db._get_conn() as conn:
        conn.execute("UPDATE device_leases SET last_activity = ? WHERE serial = ? AND user_id = ?",
                     (time.time(), serial, user_id))
        conn.commit()


def holds(db, user_id: int, serial: str) -> bool:
    l = all_leases(db).get(serial)
    return bool(l and l["user_id"] == user_id)


def slot_for(db, serial: str) -> int:
    ensure_tables(db)
    with db.lock, db._get_conn() as conn:
        row = conn.execute("SELECT slot FROM device_slots WHERE serial = ?", (serial,)).fetchone()
        if not row:
            conn.execute("INSERT OR IGNORE INTO device_slots (serial) VALUES (?)", (serial,))
            conn.commit()
            row = conn.execute("SELECT slot FROM device_slots WHERE serial = ?", (serial,)).fetchone()
    return (int(row["slot"]) - 1) % MAX_SLOTS


def appium_caps(db, serial: str) -> dict:
    """Template capabilities Appium untuk perangkat ini dengan port unik per perangkat."""
    n = slot_for(db, serial)
    return {
        "platformName": "Android",
        "appium:automationName": "UiAutomator2",
        "appium:udid": serial,
        "appium:systemPort": SYSTEM_PORT_BASE + n,
        "appium:chromedriverPort": CHROMEDRIVER_PORT_BASE + n,
        "appium:mjpegServerPort": MJPEG_PORT_BASE + n,
        "appium:newCommandTimeout": 300,
        "appium:noReset": True,
    }
