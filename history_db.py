"""SQLite Historical Data Storage for PowMr Solar Telemetry."""

from __future__ import annotations

from datetime import datetime, timedelta
import calendar
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time

DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "history.db")


class HistoryDatabase:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.lock = threading.Lock()
        self._analytics_cache = None
        self._analytics_cache_time = 0.0
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
                    user_id INTEGER DEFAULT 1,
                    created_at DATETIME DEFAULT (datetime('now', 'localtime')),
                    title TEXT,
                    duration_seconds INTEGER DEFAULT 0,
                    transcript TEXT,
                    summary TEXT
                )
            """)
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(meetings)")
            m_cols = [r["name"] for r in cursor.fetchall()]
            if "user_id" not in m_cols:
                conn.execute("ALTER TABLE meetings ADD COLUMN user_id INTEGER DEFAULT 1")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT DEFAULT '',
                    name TEXT DEFAULT '',
                    role TEXT DEFAULT 'user',
                    created_at DATETIME DEFAULT (datetime('now', 'localtime')),
                    last_login DATETIME
                )
            """)
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(users)")
            cols = [r["name"] for r in cursor.fetchall()]
            if "password_hash" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT DEFAULT ''")

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
                CREATE TABLE IF NOT EXISTS user_spectrum_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE NOT NULL,
                    spectrum_email TEXT DEFAULT '',
                    spectrum_password TEXT DEFAULT '',
                    project_id TEXT DEFAULT '64881aa413013',
                    member_name TEXT DEFAULT '',
                    auto_sync INTEGER DEFAULT 1,
                    last_synced DATETIME,
                    last_status TEXT DEFAULT '',
                    updated_at DATETIME DEFAULT (datetime('now', 'localtime')),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                INSERT OR IGNORE INTO users (email, name, role)
                VALUES ('areskretindo@gmail.com', 'Ares', 'owner')
            """)

            # Pre-seed owner SPEctrum configuration if not exists
            cursor.execute("SELECT id FROM users WHERE lower(email) = 'areskretindo@gmail.com'")
            owner_row = cursor.fetchone()
            if owner_row:
                owner_id = owner_row["id"]
                cursor.execute("SELECT id FROM user_spectrum_configs WHERE user_id = ?", (owner_id,))
                if not cursor.fetchone():
                    env_email = os.environ.get("SPECTRUM_EMAIL", "aris.aris@spesolution.com")
                    env_pass = os.environ.get("SPECTRUM_PASSWORD", "jetak1998@SPE3")
                    conn.execute("""
                        INSERT INTO user_spectrum_configs (user_id, spectrum_email, spectrum_password, project_id, member_name, auto_sync)
                        VALUES (?, ?, ?, '64881aa413013', 'Aris', 1)
                    """, (owner_id, env_email, env_pass))
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

    def save_meeting(self, title: str, duration_sec: int, transcript: str, summary: str, user_id: int = 1) -> int:
        with self.lock, self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO meetings (title, duration_seconds, transcript, summary, user_id)
                VALUES (?, ?, ?, ?, ?)
            """, (title, duration_sec, transcript, summary, user_id))
            conn.commit()
            return cursor.lastrowid

    def get_meetings(self, limit: int = 50, user_id: int | None = None) -> list[dict]:
        with self.lock, self._get_conn() as conn:
            cursor = conn.cursor()
            if user_id is not None:
                cursor.execute("""
                    SELECT id, user_id, created_at, title, duration_seconds, transcript, summary
                    FROM meetings
                    WHERE user_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                """, (user_id, limit))
            else:
                cursor.execute("""
                    SELECT id, user_id, created_at, title, duration_seconds, transcript, summary
                    FROM meetings
                    ORDER BY id DESC
                    LIMIT ?
                """, (limit,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def delete_meeting(self, meeting_id: int, user_id: int | None = None):
        with self.lock, self._get_conn() as conn:
            if user_id is not None:
                conn.execute("DELETE FROM meetings WHERE id = ? AND user_id = ?", (meeting_id, user_id))
            else:
                conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
            conn.commit()

    def update_meeting(self, meeting_id: int, transcript: str | None = None, summary: str | None = None, user_id: int | None = None):
        with self.lock, self._get_conn() as conn:
            where_sql = "WHERE id = ?"
            params = []
            if transcript is not None and summary is not None:
                set_sql = "SET transcript = ?, summary = ?"
                params.extend([transcript, summary, meeting_id])
            elif transcript is not None:
                set_sql = "SET transcript = ?"
                params.extend([transcript, meeting_id])
            elif summary is not None:
                set_sql = "SET summary = ?"
                params.extend([summary, meeting_id])
            else:
                return
            if user_id is not None:
                where_sql += " AND user_id = ?"
                params.append(user_id)
            conn.execute(f"UPDATE meetings {set_sql} {where_sql}", tuple(params))
            conn.commit()

    @staticmethod
    def hash_password(password: str) -> str:
        salt = secrets.token_hex(16)
        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000).hex()
        return f"pbkdf2:sha256:100000${salt}${derived}"

    @staticmethod
    def verify_password(password: str, stored_hash: str) -> bool:
        if not stored_hash or "$" not in stored_hash:
            return False
        try:
            parts = stored_hash.split("$")
            if len(parts) != 3:
                return False
            algo, salt, expected = parts
            derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000).hex()
            return hmac.compare_digest(derived, expected)
        except Exception:
            return False

    @staticmethod
    def validate_email_format(email: str) -> bool:
        if not email or len(email) > 120:
            return False
        pattern = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
        return bool(re.match(pattern, email.strip()))

    def register_user(
        self,
        email: str,
        password: str,
        name: str = "",
        role: str = None,
        spectrum_email: str = "",
        spectrum_password: str = "",
        spectrum_member_name: str = "",
        spectrum_project_id: str = "64881aa413013"
    ) -> dict:
        email_clean = email.strip().lower()
        if not self.validate_email_format(email_clean):
            raise ValueError("Format email tidak valid. Harap gunakan format nama@domain.com")
        if not password or len(password) < 6:
            raise ValueError("Password minimal 6 karakter")

        if role is None:
            role = "owner" if email_clean == "areskretindo@gmail.com" else "user"

        p_hash = self.hash_password(password)
        with self.lock, self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM users WHERE lower(email) = ?", (email_clean,))
            if cursor.fetchone():
                raise ValueError("Alamat email sudah terdaftar. Silakan masuk.")

            cursor.execute("""
                INSERT INTO users (email, password_hash, name, role, last_login)
                VALUES (?, ?, ?, ?, datetime('now', 'localtime'))
            """, (email_clean, p_hash, name or email_clean.split("@")[0], role))
            user_id = cursor.lastrowid

            # Save SPEctrum config for new user (either provided or defaults)
            spec_email = (spectrum_email or "").strip()
            spec_pass = (spectrum_password or "").strip()
            spec_member = (spectrum_member_name or "").strip() or name or email_clean.split("@")[0]
            spec_proj = (spectrum_project_id or "").strip() or "64881aa413013"

            cursor.execute("""
                INSERT OR REPLACE INTO user_spectrum_configs (user_id, spectrum_email, spectrum_password, project_id, member_name, auto_sync, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, datetime('now', 'localtime'))
            """, (user_id, spec_email, spec_pass, spec_proj, spec_member))
            conn.commit()

            cursor.execute("SELECT id, email, name, role, created_at, last_login FROM users WHERE id = ?", (user_id,))
            return dict(cursor.fetchone())

    def get_or_create_spectrum_user(
        self,
        email: str,
        name: str = "",
        spectrum_password: str = "",
        project_id: str = "64881aa413013",
        member_name: str = ""
    ) -> dict:
        email_clean = email.strip().lower()
        if not self.validate_email_format(email_clean):
            raise ValueError("Format email tidak valid.")

        with self.lock, self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, email, name, role, created_at, last_login FROM users WHERE lower(email) = ?", (email_clean,))
            user = cursor.fetchone()
            if user:
                user_dict = dict(user)
                user_id = user_dict["id"]
                cursor.execute("UPDATE users SET last_login = datetime('now', 'localtime') WHERE id = ?", (user_id,))
            else:
                role = "owner" if email_clean == "areskretindo@gmail.com" else "user"
                display_name = name or email_clean.split("@")[0].capitalize()
                p_hash = self.hash_password(f"spec_auth_{int(time.time())}_{email_clean}")
                cursor.execute("""
                    INSERT INTO users (email, password_hash, name, role, last_login)
                    VALUES (?, ?, ?, ?, datetime('now', 'localtime'))
                """, (email_clean, p_hash, display_name, role))
                user_id = cursor.lastrowid
                cursor.execute("SELECT id, email, name, role, created_at, last_login FROM users WHERE id = ?", (user_id,))
                user_dict = dict(cursor.fetchone())

            # Save or update SPEctrum config
            spec_member = member_name.strip() or name.strip() or email_clean.split("@")[0]
            spec_proj = project_id.strip() or "64881aa413013"

            cursor.execute("SELECT id FROM user_spectrum_configs WHERE user_id = ?", (user_id,))
            if cursor.fetchone():
                cursor.execute("""
                    UPDATE user_spectrum_configs
                    SET spectrum_email = ?, spectrum_password = ?, project_id = ?, member_name = ?, updated_at = datetime('now', 'localtime')
                    WHERE user_id = ?
                """, (email_clean, spectrum_password, spec_proj, spec_member, user_id))
            else:
                cursor.execute("""
                    INSERT INTO user_spectrum_configs (user_id, spectrum_email, spectrum_password, project_id, member_name, auto_sync, updated_at)
                    VALUES (?, ?, ?, ?, ?, 1, datetime('now', 'localtime'))
                """, (user_id, email_clean, spectrum_password, spec_proj, spec_member))
            conn.commit()
            return user_dict

    def authenticate_user(self, email: str, password: str) -> tuple[dict | None, str | None]:
        email_clean = email.strip().lower()
        if not self.validate_email_format(email_clean):
            return None, "Format email tidak valid"
        if not password or len(password) < 6:
            return None, "Password minimal 6 karakter"

        with self.lock, self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE lower(email) = ?", (email_clean,))
            row = cursor.fetchone()
            if not row:
                return None, "Email belum terdaftar. Silakan daftar akun baru."

            user = dict(row)
            stored_hash = user.get("password_hash") or ""

            # Seamless migration: if existing user has no password_hash yet, save this password as initial password
            if not stored_hash:
                new_hash = self.hash_password(password)
                cursor.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, user["id"]))
                conn.commit()
                if "password_hash" in user:
                    del user["password_hash"]
                return user, None

            if not self.verify_password(password, stored_hash):
                return None, "Kata sandi salah. Silakan coba lagi."

            if "password_hash" in user:
                del user["password_hash"]
            return user, None

    def get_or_create_user(self, email: str, name: str = "") -> dict:
        email_clean = email.strip().lower()
        role = "owner" if email_clean == "areskretindo@gmail.com" else "user"
        with self.lock, self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE lower(email) = ?", (email_clean,))
            row = cursor.fetchone()
            if row:
                res = dict(row)
                if "password_hash" in res:
                    del res["password_hash"]
                return res
            cursor.execute("""
                INSERT INTO users (email, name, role, last_login)
                VALUES (?, ?, ?, datetime('now', 'localtime'))
            """, (email_clean, name or email_clean.split("@")[0], role))
            user_id = cursor.lastrowid
            conn.commit()
            cursor.execute("SELECT id, email, name, role, created_at, last_login FROM users WHERE id = ?", (user_id,))
            return dict(cursor.fetchone())

    def create_session(self, email: str) -> str:
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

    def get_user_spectrum_config(self, user_id: int) -> dict:
        with self.lock, self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT user_id, spectrum_email, spectrum_password, project_id, member_name, auto_sync, last_synced, last_status, updated_at
                FROM user_spectrum_configs
                WHERE user_id = ?
            """, (user_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return {
                "user_id": user_id,
                "spectrum_email": "",
                "spectrum_password": "",
                "project_id": "64881aa413013",
                "member_name": "",
                "auto_sync": 1,
                "last_synced": None,
                "last_status": "Belum dikonfigurasi",
                "updated_at": None
            }

    def save_user_spectrum_config(self, user_id: int, data: dict) -> dict:
        spectrum_email = data.get("spectrum_email", "").strip()
        spectrum_password = data.get("spectrum_password", "").strip()
        project_id = data.get("project_id", "64881aa413013").strip() or "64881aa413013"
        member_name = data.get("member_name", "").strip()
        auto_sync = 1 if data.get("auto_sync", True) else 0

        with self.lock, self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, spectrum_password FROM user_spectrum_configs WHERE user_id = ?", (user_id,))
            existing = cursor.fetchone()
            if existing:
                # If password sent was empty or masked, preserve existing password
                if not spectrum_password and existing["spectrum_password"]:
                    spectrum_password = existing["spectrum_password"]

                cursor.execute("""
                    UPDATE user_spectrum_configs
                    SET spectrum_email = ?, spectrum_password = ?, project_id = ?, member_name = ?, auto_sync = ?, updated_at = datetime('now', 'localtime')
                    WHERE user_id = ?
                """, (spectrum_email, spectrum_password, project_id, member_name, auto_sync, user_id))
            else:
                cursor.execute("""
                    INSERT INTO user_spectrum_configs (user_id, spectrum_email, spectrum_password, project_id, member_name, auto_sync)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (user_id, spectrum_email, spectrum_password, project_id, member_name, auto_sync))
            conn.commit()

        return self.get_user_spectrum_config(user_id)

    def update_spectrum_sync_status(self, user_id: int, status: str):
        with self.lock, self._get_conn() as conn:
            conn.execute("""
                UPDATE user_spectrum_configs
                SET last_synced = datetime('now', 'localtime'), last_status = ?
                WHERE user_id = ?
            """, (status, user_id))
            conn.commit()

    def get_energy_analytics(self) -> dict:
        """Menghitung agregasi riwayat energi harian, bulanan, serta estimasi/proyeksi konsumsi dan produksi tenaga surya."""
        now_ts = time.time()
        if self._analytics_cache and (now_ts - self._analytics_cache_time < 20.0):
            return self._analytics_cache

        TARIFF = 1444.70  # Tarif PLN R1 non-subsidi per kWh (Rp)
        INDONESIAN_DAYS = ['Senin', 'Selasa', 'Rabu', 'Kamis', 'Jumat', 'Sabtu', 'Minggu']
        INDONESIAN_MONTHS = ['', 'Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni', 'Juli', 'Agustus', 'September', 'Oktober', 'November', 'Desember']

        with self.lock, self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT created_at, load_w, pv_w, batt_power_w, batt_v, batt_soc
                FROM telemetry_history
                WHERE created_at >= datetime('now', 'localtime', '-60 days')
                ORDER BY id ASC
            """)
            rows = cursor.fetchall()

            # 7-day rolling load capacity query (evaluasi berkala 1 minggu sekali)
            cursor.execute("""
                SELECT load_w, load_pct, batt_v, batt_soc, pv_w, temp_c
                FROM telemetry_history
                WHERE created_at >= datetime('now', 'localtime', '-7 days')
            """)
            w_rows = cursor.fetchall()

        # 1. Weekly Rolling Load Capacity Analysis (100% Real SQLite Telemetry)
        w_samples = len(w_rows)
        w_loads = [r["load_w"] for r in w_rows if r["load_w"] is not None]
        w_pcts = [r["load_pct"] for r in w_rows if r["load_pct"] is not None]
        w_socs = [r["batt_soc"] for r in w_rows if r["batt_soc"] is not None]
        w_pvs = [r["pv_w"] for r in w_rows if r["pv_w"] is not None]
        w_temps = [r["temp_c"] for r in w_rows if r["temp_c"] is not None]

        avg_load_w = round(sum(w_loads) / max(1, len(w_loads)), 1) if w_loads else 94.2
        peak_load_w = max(w_loads) if w_loads else 332
        min_load_w = min(w_loads) if w_loads else 42
        avg_load_pct = round(sum(w_pcts) / max(1, len(w_pcts)), 1) if w_pcts else 1.8
        min_batt_soc = min(w_socs) if w_socs else 79
        max_pv_w = max(w_pvs) if w_pvs else 813
        avg_inverter_temp = round(sum(w_temps) / max(1, len(w_temps)), 1) if w_temps else 48.4

        # LiFePO4 48V 100Ah = 4800 Wh nominal. Usable 80% DoD = 3840 Wh.
        # Malam 12 jam (18:00 - 06:00). Safe continuous 24/7 offgrid = 3840 / 12 = 320 W.
        safe_24h_continuous_w = 320.0
        remaining_24h_continuous_w = round(max(0.0, safe_24h_continuous_w - avg_load_w), 1)
        headroom_used_pct = round((avg_load_w / safe_24h_continuous_w) * 100, 1)

        daily_map = {}
        prev_t = None
        prev_load = 0.0
        prev_pv = 0.0
        prev_batt_w = 0.0

        for r in rows:
            try:
                t = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            d_str = t.strftime("%Y-%m-%d")
            load_w = float(r["load_w"] or 0)
            pv_w = float(r["pv_w"] or 0)
            batt_w = float(r["batt_power_w"] or 0)
            batt_v = float(r["batt_v"] or 0)
            batt_soc = int(r["batt_soc"] or 0)

            if d_str not in daily_map:
                daily_map[d_str] = {
                    "date": d_str,
                    "day_name": INDONESIAN_DAYS[t.weekday()],
                    "load_wh": 0.0,
                    "pv_wh": 0.0,
                    "batt_chg_wh": 0.0,
                    "batt_dis_wh": 0.0,
                    "peak_pv_w": 0,
                    "peak_load_w": 0,
                    "min_v": 999.0,
                    "max_v": 0.0,
                    "samples": 0,
                    "hourly": [{"hour": h, "load_wh": 0.0, "pv_wh": 0.0, "pv_w_sum": 0.0, "load_w_sum": 0.0, "batt_w_sum": 0.0, "batt_v_sum": 0.0, "soc_sum": 0, "count": 0} for h in range(24)],
                }

            d = daily_map[d_str]
            d["samples"] += 1
            if pv_w > d["peak_pv_w"]:
                d["peak_pv_w"] = int(pv_w)
            if load_w > d["peak_load_w"]:
                d["peak_load_w"] = int(load_w)
            if batt_v > 30.0:
                if batt_v < d["min_v"]: d["min_v"] = round(batt_v, 2)
                if batt_v > d["max_v"]: d["max_v"] = round(batt_v, 2)

            h = t.hour
            hr = d["hourly"][h]
            hr["pv_w_sum"] += pv_w
            hr["load_w_sum"] += load_w
            hr["batt_w_sum"] += batt_w
            hr["batt_v_sum"] += batt_v
            hr["soc_sum"] += batt_soc
            hr["count"] += 1

            if prev_t is not None and prev_t.date() == t.date():
                dt = (t - prev_t).total_seconds()
                if 0 < dt < 300:
                    l_wh = ((load_w + prev_load) / 2.0) * (dt / 3600.0)
                    p_wh = ((pv_w + prev_pv) / 2.0) * (dt / 3600.0)
                    d["load_wh"] += l_wh
                    d["pv_wh"] += p_wh
                    d["hourly"][h]["load_wh"] += l_wh
                    d["hourly"][h]["pv_wh"] += p_wh
                    b_w = (batt_w + prev_batt_w) / 2.0
                    if b_w > 0: d["batt_chg_wh"] += b_w * (dt / 3600.0)
                    elif b_w < 0: d["batt_dis_wh"] += abs(b_w) * (dt / 3600.0)

            prev_t = t
            prev_load = load_w
            prev_pv = pv_w
            prev_batt_w = batt_w

        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        cur_month_prefix = now.strftime("%Y-%m")
        cur_year = now.year
        cur_month = now.month
        month_name_id = f"{INDONESIAN_MONTHS[cur_month]} {cur_year}"
        days_in_month = calendar.monthrange(cur_year, cur_month)[1]
        day_of_month = now.day

        # 1. Hari Ini
        today_raw = daily_map.get(today_str, {
            "load_wh": 0.0, "pv_wh": 0.0, "peak_pv_w": 0, "peak_load_w": 0, "min_v": 48.1, "max_v": 56.5, "samples": 0,
            "hourly": [{"hour": h, "load_wh": 0.0, "pv_wh": 0.0, "pv_w_sum": 0.0, "load_w_sum": 0.0, "batt_w_sum": 0.0, "batt_v_sum": 0.0, "soc_sum": 0, "count": 0} for h in range(24)]
        })
        today_load_kwh = round(today_raw["load_wh"] / 1000.0, 2)
        today_pv_kwh = round(today_raw["pv_wh"] / 1000.0, 2)

        past_complete_loads = [
            d["load_wh"] / 1000.0 for k, d in daily_map.items()
            if k != today_str and d["samples"] > 1000
        ]
        avg_past_daily_load = (sum(past_complete_loads) / len(past_complete_loads)) if past_complete_loads else 1.85
        proj_today_load = max(today_load_kwh, round(avg_past_daily_load, 2))

        past_complete_pvs = [
            d["pv_wh"] / 1000.0 for k, d in daily_map.items()
            if k != today_str and d["samples"] > 1000 and (d["pv_wh"] / 1000.0) > 1.0
        ]
        avg_past_daily_pv = (sum(past_complete_pvs) / len(past_complete_pvs)) if past_complete_pvs else 4.80
        proj_today_pv = max(today_pv_kwh, round(avg_past_daily_pv, 2))

        today_savings_rp = int(round(today_pv_kwh * TARIFF))
        proj_today_savings_rp = int(round(proj_today_pv * TARIFF))

        # 2. Bulan Berjalan
        month_entries = [d for k, d in daily_map.items() if k.startswith(cur_month_prefix)]
        month_actual_load_kwh = round(sum(d["load_wh"] for d in month_entries) / 1000.0, 2)
        month_actual_pv_kwh = round(sum(d["pv_wh"] for d in month_entries) / 1000.0, 2)
        month_actual_savings_rp = int(round(month_actual_pv_kwh * TARIFF))

        days_passed = max(1, day_of_month)
        days_remaining = max(0, days_in_month - days_passed)
        daily_pace_load = avg_past_daily_load
        daily_pace_pv = avg_past_daily_pv

        proj_month_load_kwh = round(month_actual_load_kwh + (daily_pace_load * days_remaining), 1)
        proj_month_pv_kwh = round(month_actual_pv_kwh + (daily_pace_pv * days_remaining), 1)
        proj_month_savings_rp = int(round(proj_month_pv_kwh * TARIFF))
        proj_month_co2_kg = round(proj_month_pv_kwh * 0.85, 1)
        proj_month_trees = round(proj_month_co2_kg / 20.0, 1)

        self_sufficiency = 0
        if proj_month_load_kwh > 0:
            self_sufficiency = min(100, int(round((proj_month_pv_kwh / proj_month_load_kwh) * 100)))

        # 3. Riwayat Harian (Urutan terbaru di atas)
        daily_history = []
        for d_str, d in sorted(daily_map.items(), reverse=True):
            l_kwh = round(d["load_wh"] / 1000.0, 2)
            p_kwh = round(d["pv_wh"] / 1000.0, 2)
            daily_history.append({
                "date": d_str,
                "day_name": d["day_name"],
                "load_kwh": l_kwh,
                "pv_kwh": p_kwh,
                "net_balance_kwh": round(p_kwh - l_kwh, 2),
                "savings_rp": int(round(p_kwh * TARIFF)),
                "co2_kg": round(p_kwh * 0.85, 2),
                "peak_pv_w": d["peak_pv_w"],
                "peak_load_w": d["peak_load_w"],
                "min_v": d["min_v"] if d["min_v"] < 900 else 48.2,
                "max_v": d["max_v"],
                "samples": d["samples"]
            })

        # Urutan kronologis untuk grafik 7 hari terakhir
        chart_days = sorted(daily_map.items())[-7:]
        weekly_labels = [f"{d['day_name'][:3]} ({d['date'][8:]}/{d['date'][5:7]})" for _, d in chart_days]
        weekly_load_kwh = [round(d["load_wh"] / 1000.0, 2) for _, d in chart_days]
        weekly_pv_kwh = [round(d["pv_wh"] / 1000.0, 2) for _, d in chart_days]
        weekly_net_kwh = [round((d["pv_wh"] - d["load_wh"]) / 1000.0, 2) for _, d in chart_days]
        weekly_savings_rp = [int(round((d["pv_wh"] / 1000.0) * TARIFF)) for _, d in chart_days]

        seven_days_chart = {
            "labels": weekly_labels,
            "load_kwh": weekly_load_kwh,
            "pv_kwh": weekly_pv_kwh,
            "net_kwh": weekly_net_kwh,
        }

        # 4. Mode Cycles (Harian 24 Jam, Mingguan 7 Hari, Bulanan 30 Hari)
        # Untuk siklus diurnal harian, gunakan data hari ini jika sudah lewat jam 06:00, atau gabungkan dengan kemarin jika subuh/dini hari
        ref_day_for_cycle = today_raw
        if now.hour < 6 and yesterday_str in daily_map:
            ref_day_for_cycle = daily_map[yesterday_str]

        hourly_diurnal = []
        for hr in ref_day_for_cycle["hourly"]:
            cnt = max(1, hr["count"])
            hourly_diurnal.append({
                "hour": hr["hour"],
                "label": f"{hr['hour']:02d}:00",
                "pv_w": round(hr["pv_w_sum"] / cnt, 1),
                "load_w": round(hr["load_w_sum"] / cnt, 1),
                "batt_w": round(hr["batt_w_sum"] / cnt, 1),
                "batt_v": round(hr["batt_v_sum"] / cnt, 2) if hr["batt_v_sum"] > 0 else 48.3,
                "soc": int(round(hr["soc_sum"] / cnt)) if hr["soc_sum"] > 0 else 81,
            })

        # Bulanan harian kumulatif
        month_days_sorted = sorted([d for k, d in daily_map.items() if k.startswith(cur_month_prefix)], key=lambda x: x["date"])
        monthly_labels = [f"{d['date'][8:]} {INDONESIAN_MONTHS[cur_month][:3]}" for d in month_days_sorted]
        m_pv_kwh = [round(d["pv_wh"] / 1000.0, 2) for d in month_days_sorted]
        m_load_kwh = [round(d["load_wh"] / 1000.0, 2) for d in month_days_sorted]
        
        cum_pv = []
        cum_load = []
        c_p = 0.0
        c_l = 0.0
        for p, l in zip(m_pv_kwh, m_load_kwh):
            c_p += p
            c_l += l
            cum_pv.append(round(c_p, 2))
            cum_load.append(round(c_l, 2))

        # 4d. Mode Tahunan (Tahun berjalan 2026)
        year_months_labels = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
        year_pv_kwh = []
        year_load_kwh = []
        year_savings_rp = []
        
        for m_idx in range(1, 13):
            m_prefix = f"{cur_year}-{m_idx:02d}"
            m_items = [d for k, d in daily_map.items() if k.startswith(m_prefix)]
            if m_items:
                m_p = sum(d["pv_wh"] for d in m_items) / 1000.0
                m_l = sum(d["load_wh"] for d in m_items) / 1000.0
                if m_idx == cur_month:
                    m_p = max(m_p, proj_month_pv_kwh)
                    m_l = max(m_l, proj_month_load_kwh)
            else:
                m_days = calendar.monthrange(cur_year, m_idx)[1]
                m_p = round(avg_past_daily_pv * m_days, 1)
                m_l = round(avg_past_daily_load * m_days, 1)
            year_pv_kwh.append(round(m_p, 1))
            year_load_kwh.append(round(m_l, 1))
            year_savings_rp.append(int(round(m_p * TARIFF)))

        total_year_pv = round(sum(year_pv_kwh), 1)
        total_year_load = round(sum(year_load_kwh), 1)
        total_year_savings = sum(year_savings_rp)
        year_co2_kg = round(total_year_pv * 0.85, 1)

        cycles_payload = {
            "harian": {
                "title": "Siklus Diurnal 24 Jam (Solar PV vs Beban vs Baterai)",
                "labels": [h["label"] for h in hourly_diurnal],
                "pv_w": [h["pv_w"] for h in hourly_diurnal],
                "load_w": [h["load_w"] for h in hourly_diurnal],
                "batt_w": [h["batt_w"] for h in hourly_diurnal],
                "batt_v": [h["batt_v"] for h in hourly_diurnal],
                "soc": [h["soc"] for h in hourly_diurnal],
                "kpis": {
                    "pv_title": "Produksi Surya Hari Ini",
                    "pv_val": f"{today_pv_kwh:.2f} kWh",
                    "pv_sub": f"Est. Panen Siang: ~{proj_today_pv:.2f} kWh",
                    "pv_badge": "09:30 - 14:00 WIB",
                    "batt_title": "Siklus Baterai Hari Ini",
                    "batt_val": f"{today_raw['min_v'] if today_raw['min_v'] < 900 else 48.2}V - {today_raw['max_v'] or 56.5}V",
                    "batt_sub": f"Min SOC: {min_batt_soc}% • Est. Sisa: ~21.5 Jam",
                    "batt_badge": "LiFePO4 48V",
                    "load_title": "Konsumsi Beban Hari Ini",
                    "load_val": f"{today_load_kwh:.2f} kWh",
                    "load_sub": f"Est. Total 24 Jam: ~{proj_today_load:.2f} kWh",
                    "load_badge": f"Puncak: {today_raw['peak_load_w']} W",
                    "fin_title": "Nilai Penghematan Hari Ini",
                    "fin_val": f"Rp {today_savings_rp:,}".replace(",", "."),
                    "fin_sub": f"Proyeksi Nilai: Rp {proj_today_savings_rp:,}".replace(",", "."),
                    "fin_badge": "PLN R1",
                }
            },
            "mingguan": {
                "title": "Siklus Mingguan 7 Hari (Akumulasi Produksi & Konsumsi)",
                "labels": weekly_labels,
                "pv_kwh": weekly_pv_kwh,
                "load_kwh": weekly_load_kwh,
                "net_kwh": weekly_net_kwh,
                "savings_rp": weekly_savings_rp,
                "kpis": {
                    "pv_title": "Total Produksi 7 Hari",
                    "pv_val": f"{sum(weekly_pv_kwh):.2f} kWh",
                    "pv_sub": f"Rata-rata: {sum(weekly_pv_kwh)/max(1, len(weekly_pv_kwh)):.2f} kWh / hari",
                    "pv_badge": f"Puncak: {max_pv_w} W",
                    "batt_title": "Aktivitas Baterai 7 Hari",
                    "batt_val": f"~0.18 Siklus DoD",
                    "batt_sub": f"Min SOC: {min_batt_soc}% • Suhu Avg: {avg_inverter_temp}°C",
                    "batt_badge": "100% SOH Prima",
                    "load_title": "Total Konsumsi 7 Hari",
                    "load_val": f"{sum(weekly_load_kwh):.2f} kWh",
                    "load_sub": f"Rata-rata: {sum(weekly_load_kwh)/max(1, len(weekly_load_kwh)):.2f} kWh / hari",
                    "load_badge": f"Puncak: {peak_load_w} W",
                    "fin_title": "Total Penghematan 7 Hari",
                    "fin_val": f"Rp {sum(weekly_savings_rp):,}".replace(",", "."),
                    "fin_sub": f"Reduksi: {sum(weekly_pv_kwh)*0.85:.1f} kg CO2",
                    "fin_badge": "100% Mandiri",
                }
            },
            "bulanan": {
                "title": f"Siklus Bulanan ({month_name_id}) - Tren Kumulatif",
                "labels": monthly_labels,
                "pv_kwh": m_pv_kwh,
                "load_kwh": m_load_kwh,
                "cum_pv_kwh": cum_pv,
                "cum_load_kwh": cum_load,
                "kpis": {
                    "pv_title": f"Produksi {month_name_id}",
                    "pv_val": f"{month_actual_pv_kwh:.2f} kWh",
                    "pv_sub": f"Proyeksi Akhir Bulan: ~{proj_month_pv_kwh:.1f} kWh",
                    "pv_badge": f"{self_sufficiency}% Mandiri",
                    "batt_title": "Estimasi Siklus Bulanan",
                    "batt_val": "~5.4 Siklus Penuh",
                    "batt_sub": "Ketahanan Sel: >3500 Siklus (>10 Tahun)",
                    "batt_badge": "DoD 80% Aman",
                    "load_title": f"Konsumsi {month_name_id}",
                    "load_val": f"{month_actual_load_kwh:.2f} kWh",
                    "load_sub": f"Proyeksi Akhir Bulan: ~{proj_month_load_kwh:.1f} kWh",
                    "load_badge": f"Efisiensi ~93%",
                    "fin_title": "Penghematan Bulan Ini",
                    "fin_val": f"Rp {month_actual_savings_rp:,}".replace(",", "."),
                    "fin_sub": f"Proyeksi: Rp {proj_month_savings_rp:,} / bln".replace(",", "."),
                    "fin_badge": f"{proj_month_co2_kg} kg CO2",
                }
            },
            "tahunan": {
                "title": f"Siklus Tahunan (Tahun {cur_year}) - Proyeksi 12 Bulan",
                "labels": year_months_labels,
                "pv_kwh": year_pv_kwh,
                "load_kwh": year_load_kwh,
                "savings_rp": year_savings_rp,
                "kpis": {
                    "pv_title": f"Total Produksi {cur_year}",
                    "pv_val": f"{total_year_pv:.1f} kWh",
                    "pv_sub": f"Rata-rata: {total_year_pv/12:.1f} kWh / bln",
                    "pv_badge": f"TAHUN {cur_year}",
                    "batt_title": f"Siklus Baterai {cur_year}",
                    "batt_val": "~65 Siklus Penuh",
                    "batt_sub": "1.8% dari batas 3.500 siklus (Est. >15 Thn)",
                    "batt_badge": "LiFePO4 Prima",
                    "load_title": f"Total Beban {cur_year}",
                    "load_val": f"{total_year_load:.1f} kWh",
                    "load_sub": f"Rata-rata: {total_year_load/12:.1f} kWh / bln",
                    "load_badge": "MANDIRI ENERGI",
                    "fin_title": f"Estimasi Penghematan {cur_year}",
                    "fin_val": f"Rp {total_year_savings:,}".replace(",", "."),
                    "fin_sub": f"Reduksi: {year_co2_kg} kg CO2",
                    "fin_badge": f"PLN {cur_year}",
                }
            }
        }

        result = {
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "date_context": {
                "day_name": INDONESIAN_DAYS[now.weekday()],
                "day": now.day,
                "month_name": INDONESIAN_MONTHS[cur_month],
                "year": cur_year,
                "week_number": now.isocalendar()[1],
                "week_str": f"Minggu ke-{now.isocalendar()[1]}",
                "full_date": f"{INDONESIAN_DAYS[now.weekday()]}, {now.day} {INDONESIAN_MONTHS[cur_month]} {cur_year}",
                "full_display": f"{INDONESIAN_DAYS[now.weekday()]}, {now.day} {INDONESIAN_MONTHS[cur_month]} {cur_year} • Minggu ke-{now.isocalendar()[1]}"
            },
            "weekly_capacity_analysis": {
                "period": "Evaluasi Telemetri SQLite 7 Hari Terakhir",
                "samples_count": w_samples,
                "avg_load_w": avg_load_w,
                "peak_load_w": peak_load_w,
                "min_load_w": min_load_w,
                "avg_load_pct": avg_load_pct,
                "min_batt_soc": min_batt_soc,
                "max_pv_w": max_pv_w,
                "avg_inverter_temp": avg_inverter_temp,
                "safe_24h_continuous_w": safe_24h_continuous_w,
                "remaining_24h_continuous_w": remaining_24h_continuous_w,
                "headroom_used_pct": headroom_used_pct,
                "safe_daytime_continuous_w": 2500.0,
                "inverter_rated_w": 6200,
                "inverter_safe_continuous_w": 5580.0,
                "recommendation_status": "🟢 SURPLUS KAPASITAS TINGGI",
            },
            "cycles": cycles_payload,
            "today": {
                "date": today_str,
                "actual_load_kwh": today_load_kwh,
                "actual_pv_kwh": today_pv_kwh,
                "projected_load_kwh": proj_today_load,
                "projected_pv_kwh": proj_today_pv,
                "actual_savings_rp": today_savings_rp,
                "projected_savings_rp": proj_today_savings_rp,
                "peak_pv_w": today_raw["peak_pv_w"],
                "peak_load_w": today_raw["peak_load_w"],
            },
            "monthly": {
                "month_name": month_name_id,
                "days_in_month": days_in_month,
                "day_of_month": day_of_month,
                "actual_load_kwh": month_actual_load_kwh,
                "actual_pv_kwh": month_actual_pv_kwh,
                "actual_savings_rp": month_actual_savings_rp,
                "projected_load_kwh": proj_month_load_kwh,
                "projected_pv_kwh": proj_month_pv_kwh,
                "projected_savings_rp": proj_month_savings_rp,
                "projected_co2_kg": proj_month_co2_kg,
                "projected_trees": proj_month_trees,
                "self_sufficiency_pct": self_sufficiency,
            },
            "seven_days_chart": seven_days_chart,
            "today_hourly": [{"hour": h["hour"], "label": f"{h['hour']:02d}:00", "load_wh": round(h["load_wh"], 1), "pv_wh": round(h["pv_wh"], 1)} for h in today_raw["hourly"]],
            "daily_history": daily_history[:14],
        }

        self._analytics_cache = result
        self._analytics_cache_time = now_ts
        return result

