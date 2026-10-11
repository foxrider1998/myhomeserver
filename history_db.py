"""SQLite Historical Data Storage for PowMr Solar Telemetry."""

from __future__ import annotations

from datetime import datetime, timedelta
import os
import sqlite3
import threading

DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "history.db")


class HistoryDatabase:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.lock = threading.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self.lock, self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS telemetry_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at DATETIME DEFAULT (datetime('now', 'localtime')),
                    mode TEXT,
                    pv_w INTEGER,
                    pv_v REAL,
                    pv_a REAL,
                    batt_v REAL,
                    batt_soc INTEGER,
                    batt_charge_a INTEGER,
                    batt_discharge_a INTEGER,
                    batt_power_w INTEGER,
                    load_w INTEGER,
                    load_va INTEGER,
                    load_pct INTEGER,
                    ac_out_v REAL,
                    ac_out_hz REAL,
                    grid_v REAL,
                    grid_hz REAL,
                    bus_v INTEGER,
                    temp_c INTEGER
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_created_at ON telemetry_history(created_at)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS meetings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at DATETIME DEFAULT (datetime('now', 'localtime')),
                    title TEXT,
                    duration_seconds INTEGER DEFAULT 0,
                    transcript TEXT,
                    summary TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE NOT NULL,
                    name TEXT DEFAULT '',
                    role TEXT DEFAULT 'user',
                    created_at DATETIME DEFAULT (datetime('now', 'localtime')),
                    last_login DATETIME
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER,
                    email TEXT,
                    created_at DATETIME DEFAULT (datetime('now', 'localtime')),
                    expires_at DATETIME
                )
            """)
            conn.execute("""
                INSERT OR IGNORE INTO users (email, name, role)
                VALUES ('areskretindo@gmail.com', 'Ares', 'owner')
            """)
            conn.commit()

    def insert(self, data: dict):
        """Menyimpan satu snapshot data telemetry."""
        with self.lock, self._get_conn() as conn:
            conn.execute("""
                INSERT INTO telemetry_history (
                    mode, pv_w, pv_v, pv_a, batt_v, batt_soc,
                    batt_charge_a, batt_discharge_a, batt_power_w,
                    load_w, load_va, load_pct, ac_out_v, ac_out_hz,
                    grid_v, grid_hz, bus_v, temp_c
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data.get("mode", ""),
                data.get("pv_power_watt", 0),
                data.get("pv_voltage", 0.0),
                data.get("pv_current", 0.0),
                data.get("battery_voltage", 0.0),
                data.get("soc", 0),
                data.get("battery_charging_current", 0),
                data.get("battery_discharge_current", 0),
                data.get("battery_power_watt", 0),
                data.get("ac_output_watt", 0),
                data.get("ac_output_va", 0),
                data.get("load_percent", 0),
                data.get("ac_output_voltage", 0.0),
                data.get("ac_output_frequency", 0.0),
                data.get("grid_voltage", 0.0),
                data.get("grid_frequency", 0.0),
                data.get("bus_voltage", 0),
                data.get("temperature", 0),
            ))
            conn.commit()

    def get_history(self, range_str: str = "1h", limit: int = 300) -> list[dict]:
        """Mengambil riwayat data telemetry dalam rentang waktu tertentu.
        
        range_str: '15m', '1h', '6h', '24h', '7d'
        """
        modifier_map = {
            "15m": "-15 minutes",
            "1h": "-1 hour",
            "6h": "-6 hours",
            "24h": "-24 hours",
            "7d": "-7 days",
        }
        modifier = modifier_map.get(range_str, "-1 hour")

        with self.lock, self._get_conn() as conn:
            cursor = conn.cursor()
            # Ambil data dalam rentang waktu
            query = f"""
                SELECT created_at, mode, pv_w, pv_v, pv_a, batt_v, batt_soc,
                       batt_charge_a, batt_discharge_a, batt_power_w,
                       load_w, load_va, load_pct, ac_out_v, grid_v, temp_c
                FROM telemetry_history
                WHERE created_at >= datetime('now', 'localtime', '{modifier}')
                ORDER BY created_at ASC
            """
            cursor.execute(query)
            rows = cursor.fetchall()

            # Downsample jika titik data terlalu padat untuk browser
            total = len(rows)
            if total > limit:
                step = total // limit
                rows = [rows[i] for i in range(0, total, step)]

            results = []
            for r in rows:
                results.append({
                    "time": r["created_at"],
                    "mode": r["mode"],
                    "pv_w": r["pv_w"],
                    "pv_v": r["pv_v"],
                    "pv_a": r["pv_a"],
                    "batt_v": r["batt_v"],
                    "batt_soc": r["batt_soc"],
                    "batt_charge_a": r["batt_charge_a"],
                    "batt_discharge_a": r["batt_discharge_a"],
                    "batt_power_w": r["batt_power_w"],
                    "load_w": r["load_w"],
                    "load_va": r["load_va"],
                    "load_pct": r["load_pct"],
                    "ac_out_v": r["ac_out_v"],
                    "grid_v": r["grid_v"],
                    "temp_c": r["temp_c"],
                })
            return results

    def prune_old(self, days: int = 30):
        """Menghapus data yang lebih lama dari N hari."""
        with self.lock, self._get_conn() as conn:
            conn.execute("DELETE FROM telemetry_history WHERE created_at < datetime('now', 'localtime', ?)", (f"-{days} days",))
            conn.commit()

    def save_meeting(self, title: str, duration_sec: int, transcript: str, summary: str) -> int:
        with self.lock, self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO meetings (title, duration_seconds, transcript, summary)
                VALUES (?, ?, ?, ?)
            """, (title, duration_sec, transcript, summary))
            conn.commit()
            return cursor.lastrowid

    def get_meetings(self, limit: int = 30) -> list[dict]:
        with self.lock, self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, created_at, title, duration_seconds, transcript, summary
                FROM meetings
                ORDER BY id DESC
                LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def delete_meeting(self, meeting_id: int):
        with self.lock, self._get_conn() as conn:
            conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
            conn.commit()

    def get_or_create_user(self, email: str, name: str = "") -> dict:
        email_clean = email.strip().lower()
        role = "owner" if email_clean == "areskretindo@gmail.com" else "user"
        with self.lock, self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE lower(email) = ?", (email_clean,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            cursor.execute("""
                INSERT INTO users (email, name, role, last_login)
                VALUES (?, ?, ?, datetime('now', 'localtime'))
            """, (email_clean, name or email_clean.split("@")[0], role))
            conn.commit()
            cursor.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,))
            return dict(cursor.fetchone())

    def create_session(self, email: str) -> str:
        import secrets
        email_clean = email.strip().lower()
        user = self.get_or_create_user(email_clean)
        token = secrets.token_hex(24)
        with self.lock, self._get_conn() as conn:
            conn.execute("""
                INSERT INTO sessions (token, user_id, email, expires_at)
                VALUES (?, ?, ?, datetime('now', 'localtime', '+30 days'))
            """, (token, user["id"], email_clean))
            conn.execute("""
                UPDATE users SET last_login = datetime('now', 'localtime') WHERE id = ?
            """, (user["id"],))
            conn.commit()
        return token

    def validate_session(self, token: str) -> dict | None:
        if not token:
            return None
        with self.lock, self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT u.id, u.email, u.name, u.role, u.created_at, u.last_login
                FROM sessions s
                JOIN users u ON s.user_id = u.id
                WHERE s.token = ? AND (s.expires_at IS NULL OR s.expires_at >= datetime('now', 'localtime'))
            """, (token,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def delete_session(self, token: str):
        if not token:
            return
        with self.lock, self._get_conn() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()

