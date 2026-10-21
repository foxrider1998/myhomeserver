#!/usr/bin/env python3
"""
SMS OTP Service
================
Membaca SMS dari HP Android via ADB over WiFi,
parsing kode OTP, dan menyajikannya lewat REST API.

Endpoint:
  GET /otp/<nomor_hp>  → OTP terbaru untuk nomor HP tersebut
  GET /otp/status      → status koneksi ADB & statistik
  POST /otp/pull       → trigger pull manual
"""

import re
import json
import time
import sqlite3
import logging
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ─── CONFIG ──────────────────────────────────────────────────────────────────
ADB_HOST = "172.16.5.105"       # IP HP Samsung Galaxy Tab S6 (SM-T865)
ADB_PORT = 5555
POLL_INTERVAL_SEC = 30          # polling setiap 30 detik
OTP_EXPIRE_MINUTES = 10         # OTP dianggap expired setelah 10 menit
MAX_SMS_PER_PULL = 50           # ambil 50 SMS terbaru per pull
DB_PATH = "/home/ares/Documents/myserver/sms_otp.db"
SERVER_PORT = 3010              # port API server
LOG_LEVEL = logging.INFO

# Pattern OTP yang dikenali (ordered by priority)
OTP_PATTERNS = [
    # YAP / BNI style: "Kode OTP anda 7238"
    (r'(?:Kode\s+)?OTP\s+(?:anda\s+)?[:\-]?\s*(\d{4,8})', 'kode_otp'),
    # Generic: "kode verifikasi: 123456"
    (r'(?:kode|code)\s+(?:verifikasi|verification|konfirmasi|confirmation)\s*[:\-]?\s*(\d{4,8})', 'verifikasi'),
    # "PIN: 123456"
    (r'\bPIN\s*[:\-]\s*(\d{4,8})\b', 'pin'),
    # Angka 4–8 digit standalone (fallback)
    (r'\b(\d{4,8})\b', 'digit_fallback'),
]

# SMS yang diabaikan (tidak mengandung OTP)
SKIP_PATTERNS = [
    r'Call\s+\d{7,}',  # "Info BNI Call 1500046"
    r'www\.',
    r'http',
]

# ─── LOGGING ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=LOG_LEVEL,
    format='[%(asctime)s] [%(levelname)s] [SMS-OTP] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('sms_otp')

# ─── DATABASE ─────────────────────────────────────────────────────────────────
def init_db(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS sms_otp (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            phone       TEXT NOT NULL,          -- nomor HP tujuan (nomor kita)
            sender      TEXT NOT NULL,          -- pengirim SMS (misal: YAP, BNI)
            body        TEXT NOT NULL,          -- isi SMS mentah
            otp_code    TEXT,                   -- OTP yang berhasil di-parse
            otp_pattern TEXT,                   -- nama pattern yang cocok
            expires_at  TEXT,                   -- waktu expire dari SMS (jika ada)
            received_at TEXT NOT NULL,          -- waktu terima di HP (dari ADB)
            pulled_at   TEXT NOT NULL,          -- waktu di-pull oleh server
            adb_id      TEXT UNIQUE             -- ID unik dari ADB (_id kolom)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_phone ON sms_otp(phone)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_pulled ON sms_otp(pulled_at)")
    con.commit()
    return con


# ─── ADB HELPERS ─────────────────────────────────────────────────────────────
class ADBClient:
    def __init__(self, host: str, port: int = 5555):
        self.host = host
        self.port = port
        self.device = f"{host}:{port}"
        self._connected = False

    def _run(self, cmd: list[str], timeout: int = 15) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout, encoding='utf-8', errors='replace'
            )
            return result.returncode == 0, (result.stdout + result.stderr).strip()
        except subprocess.TimeoutExpired:
            return False, "timeout"
        except FileNotFoundError:
            return False, "adb not found in PATH"

    def connect(self) -> bool:
        ok, out = self._run(['adb', 'connect', self.device])
        self._connected = ok and 'connected' in out.lower()
        if self._connected:
            log.info(f"ADB connected to {self.device}")
        else:
            log.warning(f"ADB connect failed: {out}")
        return self._connected

    def is_connected(self) -> bool:
        ok, out = self._run(['adb', '-s', self.device, 'get-state'])
        self._connected = ok and 'device' in out
        return self._connected

    def pull_sms_inbox(self, limit: int = 50) -> list[dict]:
        """
        Ambil SMS dari inbox HP via ADB content provider.
        Kolom: _id, address, date, body, read, type (1=inbox, 2=sent)
        """
        if not self.is_connected():
            if not self.connect():
                return []

        # Query SMS inbox (Android default sudah date DESC)
        ok, out = self._run([
            'adb', '-s', self.device, 'shell',
            'content', 'query', '--uri', 'content://sms/inbox',
            '--projection', '_id:address:date:body:read',
        ], timeout=20)

        if not ok or not out:
            log.warning(f"ADB SMS query failed: {out[:200]}")
            return []

        return self._parse_adb_content_output(out, limit)

    def _parse_adb_content_output(self, raw: str, limit: int) -> list[dict]:
        """Parse output dari `adb shell content query`"""
        rows = []
        current = {}

        # Pattern: Row: 0 _id=503, address=Bonus IM3, date=1792563633258, body=..., read=0
        row_re = re.compile(
            r'^Row:\s*\d+\s+_id=([^,]+),\s*address=([^,]*),\s*date=(\d+),\s*body=(.*?)(?:,\s*read=(\d+))?$'
        )

        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            m = row_re.match(line)
            if m:
                if current:
                    rows.append(current)
                    if len(rows) >= limit:
                        return rows
                current = {
                    '_id': m.group(1).strip(),
                    'address': m.group(2).strip(),
                    'date': m.group(3).strip(),
                    'body': m.group(4).strip(),
                    'read': m.group(5) or '0',
                }
            elif current and 'body' in current:
                # Continuation line jika ada baris baru di SMS
                current['body'] += "\n" + line

        if current and len(rows) < limit:
            rows.append(current)

        return rows


# ─── OTP PARSER ──────────────────────────────────────────────────────────────
def parse_otp(body: str) -> tuple[str | None, str | None]:
    """
    Parse kode OTP dari body SMS.
    Return (otp_code, pattern_name) atau (None, None)
    """
    if not body:
        return None, None

    # Cek apakah ini SMS OTP (harus ada keyword)
    body_lower = body.lower()
    is_otp_sms = any(kw in body_lower for kw in [
        'otp', 'kode', 'pin', 'verifikasi', 'verification',
        'password', 'token', 'berlaku sampai', 'expires'
    ])
    if not is_otp_sms:
        return None, None

    for pattern, name in OTP_PATTERNS:
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            code = match.group(1)
            # Validasi: jangan return nomor telepon panjang
            if len(code) > 8:
                continue
            # Skip nomor yang ada di SKIP_PATTERNS context
            skip = False
            for sp in SKIP_PATTERNS:
                if re.search(sp, body[max(0, match.start()-20):match.end()+20], re.IGNORECASE):
                    skip = True
                    break
            if not skip:
                return code, name

    return None, None


def parse_expires_at(body: str) -> str | None:
    """Parse waktu expire dari body SMS. Contoh: 'Berlaku sampai 2026-10-20 15:08:09'"""
    patterns = [
        r'[Bb]erlaku\s+sampai\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})',
        r'[Ee]xpires?\s+(?:at\s+)?(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})',
        r'[Vv]alid\s+(?:until|till)\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})',
        r'[Bb]erlaku\s+sampai\s+(\d{2}:\d{2}:\d{2})',  # hanya jam
    ]
    for p in patterns:
        m = re.search(p, body)
        if m:
            return m.group(1)
    return None


# ─── SMS POLLER ──────────────────────────────────────────────────────────────
class SMSPoller:
    def __init__(self, adb: ADBClient, db: sqlite3.Connection, phone: str):
        self.adb = adb
        self.db = db
        self.phone = phone  # nomor HP kita (085743019186)
        self._lock = threading.Lock()
        self._last_pull = None
        self._pull_count = 0
        self._new_count = 0

    def pull(self) -> dict:
        """Tarik SMS dari HP, parse OTP, simpan ke DB. Return statistik."""
        log.info("Pulling SMS from Android via ADB...")
        sms_list = self.adb.pull_sms_inbox(limit=MAX_SMS_PER_PULL)
        pulled_at = datetime.now(timezone.utc).isoformat()
        new_count = 0

        with self._lock:
            for sms in sms_list:
                adb_id = sms.get('_id', '')
                if not adb_id:
                    continue

                # Cek apakah sudah tersimpan
                existing = self.db.execute(
                    "SELECT id FROM sms_otp WHERE adb_id = ?", (adb_id,)
                ).fetchone()
                if existing:
                    continue

                body = sms.get('body', '')
                sender = sms.get('address', 'UNKNOWN')
                date_ms = sms.get('date', '0')

                # Convert timestamp ms → ISO
                try:
                    ts = int(date_ms) / 1000
                    received_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                except (ValueError, OSError):
                    received_at = pulled_at

                # Parse OTP
                otp_code, otp_pattern = parse_otp(body)
                expires_at = parse_expires_at(body)

                self.db.execute("""
                    INSERT OR IGNORE INTO sms_otp
                    (phone, sender, body, otp_code, otp_pattern, expires_at, received_at, pulled_at, adb_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    self.phone, sender, body,
                    otp_code, otp_pattern, expires_at,
                    received_at, pulled_at, adb_id
                ))
                new_count += 1

                if otp_code:
                    log.info(f"✓ OTP dari {sender}: [{otp_code}] (pattern: {otp_pattern})")

            self.db.commit()

        self._last_pull = pulled_at
        self._pull_count += 1
        self._new_count += new_count
        log.info(f"Pull selesai: {new_count} SMS baru dari {len(sms_list)} total")
        return {'new': new_count, 'total_found': len(sms_list), 'pulled_at': pulled_at}

    def get_latest_otp(self, phone: str | None = None, max_age_minutes: int = OTP_EXPIRE_MINUTES) -> dict | None:
        """Ambil OTP terbaru untuk nomor HP tertentu."""
        p = phone or self.phone
        p_norm = re.sub(r'^\+?62', '0', p)

        if max_age_minutes and max_age_minutes > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat()
            row = self.db.execute("""
                SELECT * FROM sms_otp
                WHERE phone = ?
                  AND otp_code IS NOT NULL
                  AND received_at >= ?
                ORDER BY received_at DESC
                LIMIT 1
            """, (p_norm, cutoff)).fetchone()
        else:
            row = self.db.execute("""
                SELECT * FROM sms_otp
                WHERE phone = ?
                  AND otp_code IS NOT NULL
                ORDER BY received_at DESC
                LIMIT 1
            """, (p_norm,)).fetchone()

        if row:
            return dict(row)
        return None

    def status(self) -> dict:
        total_sms = self.db.execute("SELECT COUNT(*) FROM sms_otp").fetchone()[0]
        total_otp = self.db.execute("SELECT COUNT(*) FROM sms_otp WHERE otp_code IS NOT NULL").fetchone()[0]
        return {
            'adb_connected': self.adb.is_connected(),
            'adb_device': self.adb.device,
            'phone': self.phone,
            'last_pull': self._last_pull,
            'pull_count': self._pull_count,
            'total_sms_stored': total_sms,
            'total_otp_found': total_otp,
        }


# ─── HTTP API ─────────────────────────────────────────────────────────────────
_poller: SMSPoller = None

class OTPHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info(f"HTTP {self.address_string()} - {fmt % args}")

    def send_json(self, data: dict, code: int = 200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/')

        # GET /otp/<nomor_hp>
        m = re.match(r'^/otp/(\d+)$', path)
        if m:
            phone = m.group(1)
            # Normalize nomor
            phone_norm = re.sub(r'^\+?62', '0', phone)
            params = parse_qs(parsed.query)
            max_age_param = params.get('max_age', [None])[0]
            max_age = int(max_age_param) if max_age_param is not None else OTP_EXPIRE_MINUTES

            otp = _poller.get_latest_otp(phone_norm, max_age)
            if otp:
                self.send_json({
                    'success': True,
                    'phone': phone,
                    'otp': otp['otp_code'],
                    'sender': otp['sender'],
                    'body': otp['body'],
                    'expires_at': otp['expires_at'],
                    'received_at': otp['received_at'],
                    'pulled_at': otp['pulled_at'],
                    'is_recent': True,
                })
            else:
                # Cek OTP historis tersimpan (all-time)
                last_hist = _poller.get_latest_otp(phone_norm, max_age_minutes=0)
                if last_hist:
                    self.send_json({
                        'success': True,
                        'phone': phone,
                        'otp': last_hist['otp_code'],
                        'sender': last_hist['sender'],
                        'body': last_hist['body'],
                        'expires_at': last_hist['expires_at'],
                        'received_at': last_hist['received_at'],
                        'pulled_at': last_hist['pulled_at'],
                        'is_recent': False,
                        'message': f'OTP terbaru yang tersimpan (lebih dari {max_age} menit yang lalu)',
                    })
                else:
                    self.send_json({
                        'success': False,
                        'phone': phone,
                        'otp': None,
                        'message': f'Tidak ada OTP ditemukan untuk nomor {phone}',
                    }, code=404)
            return

        # GET /otp/status
        if path == '/otp/status':
            self.send_json({'success': True, **_poller.status()})
            return

        self.send_json({'success': False, 'message': 'Not found'}, code=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/')

        # POST /otp/pull → trigger manual pull
        if path == '/otp/pull':
            result = _poller.pull()
            self.send_json({'success': True, **result})
            return

        self.send_json({'success': False, 'message': 'Not found'}, code=404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.end_headers()


# ─── SCHEDULER ────────────────────────────────────────────────────────────────
def _scheduler_loop(poller: SMSPoller):
    """Background thread: pull SMS setiap POLL_INTERVAL_SEC"""
    while True:
        try:
            poller.pull()
        except Exception as e:
            log.error(f"Pull error: {e}")
        time.sleep(POLL_INTERVAL_SEC)


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(description='SMS OTP Service via ADB')
    parser.add_argument('--adb-host', default=ADB_HOST, help='IP HP Android')
    parser.add_argument('--adb-port', type=int, default=ADB_PORT)
    parser.add_argument('--phone', default='085743019186', help='Nomor HP kita')
    parser.add_argument('--db', default=DB_PATH)
    parser.add_argument('--port', type=int, default=SERVER_PORT)
    parser.add_argument('--pull', action='store_true', help='Pull sekali lalu keluar')
    parser.add_argument('--no-server', action='store_true', help='Jalankan scheduler tanpa HTTP server')
    args = parser.parse_args()

    global _poller

    log.info(f"SMS OTP Service starting...")
    log.info(f"ADB target: {args.adb_host}:{args.adb_port}")
    log.info(f"Nomor HP: {args.phone}")
    log.info(f"DB: {args.db}")

    adb = ADBClient(args.adb_host, args.adb_port)
    db = init_db(args.db)
    _poller = SMSPoller(adb, db, args.phone)

    # Koneksi awal
    adb.connect()

    if args.pull:
        # One-shot pull
        result = _poller.pull()
        print(json.dumps(result, indent=2))
        # Print OTP terbaru
        otp = _poller.get_latest_otp()
        if otp:
            print(f"\n✓ OTP terbaru: {otp['otp_code']} (dari {otp['sender']})")
        else:
            print("\n⚠ Tidak ada OTP terbaru ditemukan")
        return

    # Start scheduler thread
    t = threading.Thread(target=_scheduler_loop, args=(_poller,), daemon=True)
    t.start()
    log.info(f"Scheduler berjalan, interval: {POLL_INTERVAL_SEC}s")

    if not args.no_server:
        server = HTTPServer(('0.0.0.0', args.port), OTPHandler)
        log.info(f"HTTP API berjalan di port {args.port}")
        log.info(f"  GET /otp/{args.phone}  → OTP terbaru")
        log.info(f"  GET /otp/status        → status koneksi")
        log.info(f"  POST /otp/pull         → trigger pull manual")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            log.info("Stopped.")


if __name__ == '__main__':
    main()
