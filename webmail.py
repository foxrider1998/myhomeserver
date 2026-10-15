"""webmail.py - Westpro Mail: kotak masuk untuk domain sendiri (banyak alamat uji) + Sent + API untuk otomasi tes.

Alur email MASUK dengan Cloudflare Tunnel:
  Internet -> MX Cloudflare (Email Routing, catch-all *@domain) -> Email Worker (cloudflare/email-worker)
  -> HTTPS POST (raw RFC822 + tanda tangan HMAC) -> https://<domain>/api/mail/inbound (lewat tunnel) -> database ini.
Tidak ada server SMTP masuk di ThinkCentre dan tidak perlu port 25 terbuka.

Email KELUAR memakai mailer.py (relay SMTP); Balasan diarahkan ke alamat Westpro lewat Reply-To.
"""

from __future__ import annotations

import email
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime
from email import policy
from urllib.parse import urlparse

MAX_RAW_BYTES = 10 * 1024 * 1024
MAX_TEXT = 200_000
MAX_HTML = 400_000
RETENTION_DAYS = 30
MAX_MESSAGES = 5000
MAX_ALIASES_PER_USER = 50
SIGNATURE_WINDOW_SEC = 300
LOCAL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,38}[a-z0-9]$")

def _load_env():
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.isfile(env_file):
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k and k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass

_load_env()


def mail_domain() -> str:
    d = os.environ.get("MAIL_DOMAIN", "").strip().lower()
    if d:
        return d
    base = os.environ.get("PUBLIC_BASE_URL", "").strip() or "https://westpro.my.id"
    return (urlparse(base).hostname or "westpro.my.id").lower()


def inbound_secret() -> str:
    return os.environ.get("MAIL_INBOUND_SECRET", "")


def ensure_tables(db) -> None:
    with db.lock, db._get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mail_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder TEXT NOT NULL DEFAULT 'inbox',
                received_at TEXT NOT NULL,
                to_addr TEXT NOT NULL,
                base_to TEXT NOT NULL,
                from_addr TEXT,
                subject TEXT,
                body_text TEXT,
                body_html TEXT,
                otp_json TEXT,
                links_json TEXT,
                attach_json TEXT,
                message_id TEXT,
                size INTEGER DEFAULT 0,
                is_read INTEGER DEFAULT 0,
                owner_user_id INTEGER
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mail_base_to ON mail_messages (folder, base_to, id)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mail_aliases (
                address TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                label TEXT,
                created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mail_api_keys (
                key_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT,
                last_used TEXT
            )
        """)
        conn.commit()


# ------------------------------------------------------------------ keamanan webhook
def signature_for(secret: str, ts: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), ts.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, ts: str, sig: str, body: bytes) -> bool:
    if not secret or not ts or not sig:
        return False
    try:
        if abs(time.time() - int(ts)) > SIGNATURE_WINDOW_SEC:
            return False
    except ValueError:
        return False
    return hmac.compare_digest(signature_for(secret, ts, body), sig.strip().lower())


# ------------------------------------------------------------------ penguraian email
_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_HREF_RE = re.compile(r'href\s*=\s*["\'](https?://[^"\']+)["\']', re.I)
_OTP_KW = re.compile(r"(?:kode|code|otp|token|pin|verifikasi|verification|passcode|security code|confirm|sandi sekali pakai)[^\d\n]{0,60}(?<!\d)(?<!\d\.)(\d{4,8})(?!\d)(?!\.\d)", re.I)
_OTP_6 = re.compile(r"(?<!\d)(?<!\d[.\-])(\d{6})(?!\d)(?![.\-]\d)")
_VERIFY_HINT = re.compile(r"verif|confirm|activat|aktivasi|reset|magic|token|signin|sign-in|login|otp", re.I)


def strip_tags(html: str) -> str:
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", t)
    t = _TAG_RE.sub(" ", t)
    import html as _h
    t = _h.unescape(t)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", t)).strip()


def extract_otps(text: str) -> list[str]:
    codes: list[str] = []
    for m in _OTP_KW.finditer(text or ""):
        if m.group(1) not in codes:
            codes.append(m.group(1))
    if not codes:
        for m in _OTP_6.finditer(text or ""):
            if m.group(1) not in codes:
                codes.append(m.group(1))
    return codes[:5]


def extract_links(text: str, html: str) -> list[dict]:
    urls: list[str] = []
    for u in _HREF_RE.findall(html or "") + _URL_RE.findall(text or ""):
        u = u.rstrip(".,;")
        if u not in urls:
            urls.append(u)
    out = [{"url": u[:600], "verify": bool(_VERIFY_HINT.search(u))} for u in urls[:20]]
    out.sort(key=lambda x: not x["verify"])
    return out[:12]


def _body(msg, kind: str) -> str:
    part = msg.get_body(preferencelist=(kind,))
    if part is None:
        return ""
    try:
        return part.get_content()
    except Exception:
        try:
            return (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        except Exception:
            return ""


def parse_raw(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw, policy=policy.default)
    text = _body(msg, "plain")
    html = _body(msg, "html")
    if not text and html:
        text = strip_tags(html)
    attachments = []
    try:
        for part in msg.iter_attachments():
            payload = part.get_payload(decode=True) or b""
            attachments.append({"filename": part.get_filename() or "", "type": part.get_content_type(), "size": len(payload)})
    except Exception:
        pass
    return {
        "from": str(msg.get("from") or "")[:300],
        "to_header": str(msg.get("to") or "")[:300],
        "subject": str(msg.get("subject") or "")[:500],
        "message_id": str(msg.get("message-id") or "")[:300],
        "text": text[:MAX_TEXT],
        "html": html[:MAX_HTML],
        "attachments": attachments[:20],
    }


def base_address(addr: str) -> str:
    """qa+tag@domain -> qa@domain (plus-addressing tetap masuk ke alamat dasar)."""
    a = (addr or "").strip().lower()
    if "@" not in a:
        return a
    local, domain = a.rsplit("@", 1)
    return local.split("+", 1)[0] + "@" + domain


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ingest(db, raw: bytes, envelope_to: str) -> dict:
    """Simpan email masuk. Mengembalikan {id, duplicate, to}."""
    ensure_tables(db)
    p = parse_raw(raw)
    to_addr = (envelope_to or "").strip().lower()
    if "@" not in to_addr:
        m = re.search(r"[\w.+-]+@[\w.-]+", p["to_header"])
        to_addr = m.group(0).lower() if m else "unknown@" + mail_domain()
    to_addr = to_addr[:200]
    base = base_address(to_addr)
    with db.lock, db._get_conn() as conn:
        if p["message_id"]:
            dup = conn.execute("SELECT id FROM mail_messages WHERE folder='inbox' AND message_id = ? AND to_addr = ?", (p["message_id"], to_addr)).fetchone()
            if dup:
                return {"id": dup["id"], "duplicate": True, "to": to_addr}
        cur = conn.execute("""
            INSERT INTO mail_messages (folder, received_at, to_addr, base_to, from_addr, subject, body_text, body_html,
                                       otp_json, links_json, attach_json, message_id, size)
            VALUES ('inbox', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (_now(), to_addr, base, p["from"], p["subject"], p["text"], p["html"],
              json.dumps(extract_otps(p["subject"] + "\n" + p["text"])), json.dumps(extract_links(p["text"], p["html"])),
              json.dumps(p["attachments"]), p["message_id"], len(raw)))
        new_id = cur.lastrowid
        # retensi: hapus yang lama & batasi jumlah
        conn.execute("DELETE FROM mail_messages WHERE received_at < datetime('now','localtime', ?)", (f"-{RETENTION_DAYS} days",))
        conn.execute("DELETE FROM mail_messages WHERE id NOT IN (SELECT id FROM mail_messages ORDER BY id DESC LIMIT ?)", (MAX_MESSAGES,))
        conn.commit()
    return {"id": new_id, "duplicate": False, "to": to_addr}


def store_sent(db, user_id: int, from_alias: str, to: str, subject: str, text: str) -> int:
    ensure_tables(db)
    with db.lock, db._get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO mail_messages (folder, received_at, to_addr, base_to, from_addr, subject, body_text, body_html,
                                       otp_json, links_json, attach_json, message_id, size, is_read, owner_user_id)
            VALUES ('sent', ?, ?, ?, ?, ?, ?, '', '[]', '[]', '[]', '', ?, 1, ?)
        """, (_now(), to[:200], base_address(to)[:200], from_alias[:200], subject[:500], text[:MAX_TEXT], len(text), user_id))
        conn.commit()
        return cur.lastrowid


# ------------------------------------------------------------------ alias (alamat uji)
def create_alias(db, user_id: int, local: str | None, label: str = "", is_owner: bool = False) -> tuple[str | None, str]:
    ensure_tables(db)
    domain = mail_domain()
    local = (local or "").strip().lower()
    if not local:
        local = "qa-" + secrets.token_hex(3)
    if not LOCAL_RE.match(local) or ".." in local or "+" in local:
        return None, "Nama alamat 3-40 karakter: huruf kecil, angka, titik, minus, garis bawah."
    if local in ("admin", "root", "postmaster", "abuse", "webmaster", "hostmaster", "noreply", "no-reply", "mailer-daemon", "security"):
        return None, "Nama alamat ini dicadangkan."
    addr = f"{local}@{domain}"
    with db.lock, db._get_conn() as conn:
        row = conn.execute("SELECT user_id FROM mail_aliases WHERE address = ?", (addr,)).fetchone()
        if row:
            return None, "Alamat sudah dipakai." if row["user_id"] != user_id else "Alamat sudah Anda miliki."
        n = conn.execute("SELECT COUNT(*) AS n FROM mail_aliases WHERE user_id = ?", (user_id,)).fetchone()["n"]
        if not is_owner and n >= MAX_ALIASES_PER_USER:
            return None, f"Batas {MAX_ALIASES_PER_USER} alamat per user tercapai. Hapus yang tidak dipakai."
        conn.execute("INSERT INTO mail_aliases (address, user_id, label, created_at) VALUES (?, ?, ?, ?)", (addr, user_id, label[:60], _now()))
        conn.commit()
    return addr, "ok"


def delete_alias(db, user_id: int, address: str, is_owner: bool = False) -> bool:
    ensure_tables(db)
    with db.lock, db._get_conn() as conn:
        if is_owner:
            cur = conn.execute("DELETE FROM mail_aliases WHERE address = ?", (address.lower(),))
        else:
            cur = conn.execute("DELETE FROM mail_aliases WHERE address = ? AND user_id = ?", (address.lower(), user_id))
        conn.commit()
        return cur.rowcount > 0


def list_aliases(db, user_id: int, is_owner: bool) -> list[dict]:
    ensure_tables(db)
    with db.lock, db._get_conn() as conn:
        if is_owner:
            rows = conn.execute("""SELECT a.address, a.label, a.created_at, a.user_id, u.name AS owner_name,
                (SELECT COUNT(*) FROM mail_messages m WHERE m.folder='inbox' AND m.base_to = a.address) AS total,
                (SELECT COUNT(*) FROM mail_messages m WHERE m.folder='inbox' AND m.base_to = a.address AND m.is_read = 0) AS unread
                FROM mail_aliases a LEFT JOIN users u ON u.id = a.user_id ORDER BY a.created_at DESC""").fetchall()
        else:
            rows = conn.execute("""SELECT a.address, a.label, a.created_at, a.user_id, '' AS owner_name,
                (SELECT COUNT(*) FROM mail_messages m WHERE m.folder='inbox' AND m.base_to = a.address) AS total,
                (SELECT COUNT(*) FROM mail_messages m WHERE m.folder='inbox' AND m.base_to = a.address AND m.is_read = 0) AS unread
                FROM mail_aliases a WHERE a.user_id = ? ORDER BY a.created_at DESC""", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def my_addresses(db, user_id: int) -> list[str]:
    ensure_tables(db)
    with db.lock, db._get_conn() as conn:
        return [r["address"] for r in conn.execute("SELECT address FROM mail_aliases WHERE user_id = ?", (user_id,)).fetchall()]


# ------------------------------------------------------------------ baca / kelola pesan
def _visibility(user_id: int, is_owner: bool, folder: str, db) -> tuple[str, list]:
    """Klausa WHERE + parameter untuk pesan yang boleh dilihat user."""
    if folder == "sent":
        return ("folder='sent'" + ("" if is_owner else " AND owner_user_id = ?"), [] if is_owner else [user_id])
    if is_owner:
        return ("folder='inbox'", [])
    mine = my_addresses(db, user_id)
    if not mine:
        return ("0", [])
    return ("folder='inbox' AND base_to IN (%s)" % ",".join("?" * len(mine)), mine)


def _row(r, full: bool = False) -> dict:
    d = {"id": r["id"], "folder": r["folder"], "received_at": r["received_at"], "to": r["to_addr"], "from": r["from_addr"],
         "subject": r["subject"] or "(tanpa subjek)", "is_read": bool(r["is_read"]), "otp": json.loads(r["otp_json"] or "[]"), "size": r["size"]}
    d["preview"] = re.sub(r"\s+", " ", (r["body_text"] or ""))[:140]
    if full:
        d["text"] = r["body_text"] or ""
        d["html"] = r["body_html"] or ""
        d["links"] = json.loads(r["links_json"] or "[]")
        d["attachments"] = json.loads(r["attach_json"] or "[]")
    return d


def list_messages(db, user_id: int, is_owner: bool, folder: str = "inbox", address: str = "", q: str = "",
                  unread: bool = False, limit: int = 50, offset: int = 0) -> dict:
    ensure_tables(db)
    where, params = _visibility(user_id, is_owner, folder, db)
    params = list(params)
    if address:
        if folder == "inbox":
            where += " AND base_to = ?"
            params.append(base_address(address))
        else:
            where += " AND lower(from_addr) = ?"
            params.append(address.lower())
    if q:
        like = f"%{q.lower()}%"
        where += " AND (lower(subject) LIKE ? OR lower(from_addr) LIKE ? OR lower(body_text) LIKE ? OR lower(to_addr) LIKE ?)"
        params += [like, like, like, like]
    if unread:
        where += " AND is_read = 0"
    with db.lock, db._get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n FROM mail_messages WHERE {where}", params).fetchone()["n"]
        rows = conn.execute(f"SELECT * FROM mail_messages WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                            params + [max(1, min(limit, 200)), max(0, offset)]).fetchall()
    return {"total": total, "messages": [_row(r) for r in rows]}


def get_message(db, user_id: int, is_owner: bool, msg_id: int, mark_read: bool = True) -> dict | None:
    ensure_tables(db)
    with db.lock, db._get_conn() as conn:
        r = conn.execute("SELECT * FROM mail_messages WHERE id = ?", (msg_id,)).fetchone()
    if not r:
        return None
    where, params = _visibility(user_id, is_owner, r["folder"], db)
    with db.lock, db._get_conn() as conn:
        ok = conn.execute(f"SELECT 1 FROM mail_messages WHERE id = ? AND {where}", [msg_id] + list(params)).fetchone()
        if not ok:
            return None
        if mark_read and not r["is_read"]:
            conn.execute("UPDATE mail_messages SET is_read = 1 WHERE id = ?", (msg_id,))
            conn.commit()
    return _row(r, full=True)


def delete_messages(db, user_id: int, is_owner: bool, ids: list[int]) -> int:
    ensure_tables(db)
    n = 0
    for mid in ids[:200]:
        if get_message(db, user_id, is_owner, int(mid), mark_read=False):
            with db.lock, db._get_conn() as conn:
                n += conn.execute("DELETE FROM mail_messages WHERE id = ?", (int(mid),)).rowcount
                conn.commit()
    return n


def set_read(db, user_id: int, is_owner: bool, ids: list[int], read: bool) -> int:
    n = 0
    for mid in ids[:200]:
        if get_message(db, user_id, is_owner, int(mid), mark_read=False):
            with db.lock, db._get_conn() as conn:
                n += conn.execute("UPDATE mail_messages SET is_read = ? WHERE id = ?", (1 if read else 0, int(mid))).rowcount
                conn.commit()
    return n


def wait_for(db, user_id: int, is_owner: bool, address: str, since_id: int = 0, timeout: float = 30,
             subject_contains: str = "", poll: float = 0.7) -> dict | None:
    """Long-poll untuk otomasi tes: tunggu email baru ke alamat (id > since_id)."""
    deadline = time.time() + max(1.0, min(timeout, 60.0))
    needle = subject_contains.lower()
    while True:
        res = list_messages(db, user_id, is_owner, "inbox", address, limit=20)
        for m in res["messages"]:
            if m["id"] > since_id and (not needle or needle in m["subject"].lower()):
                return get_message(db, user_id, is_owner, m["id"], mark_read=True)
        if time.time() >= deadline:
            return None
        time.sleep(poll)


def stats(db) -> dict:
    ensure_tables(db)
    with db.lock, db._get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM mail_messages WHERE folder='inbox'").fetchone()["n"]
        last = conn.execute("SELECT received_at FROM mail_messages WHERE folder='inbox' ORDER BY id DESC LIMIT 1").fetchone()
        a = conn.execute("SELECT COUNT(*) AS n FROM mail_aliases").fetchone()["n"]
    return {"inbox_total": n, "last_received": last["received_at"] if last else None, "aliases": a}


# ------------------------------------------------------------------ API key (otomasi)
def create_api_key(db, user_id: int) -> str:
    ensure_tables(db)
    key = "wpm_" + secrets.token_urlsafe(24)
    with db.lock, db._get_conn() as conn:
        conn.execute("DELETE FROM mail_api_keys WHERE user_id = ?", (user_id,))  # satu kunci aktif per user
        conn.execute("INSERT INTO mail_api_keys (key_hash, user_id, created_at) VALUES (?, ?, ?)",
                     (hashlib.sha256(key.encode()).hexdigest(), user_id, _now()))
        conn.commit()
    return key


def has_api_key(db, user_id: int) -> bool:
    ensure_tables(db)
    with db.lock, db._get_conn() as conn:
        return bool(conn.execute("SELECT 1 FROM mail_api_keys WHERE user_id = ?", (user_id,)).fetchone())


def revoke_api_key(db, user_id: int) -> None:
    ensure_tables(db)
    with db.lock, db._get_conn() as conn:
        conn.execute("DELETE FROM mail_api_keys WHERE user_id = ?", (user_id,))
        conn.commit()


def user_by_key(db, key: str) -> dict | None:
    if not key or not key.startswith("wpm_") or len(key) > 100:
        return None
    ensure_tables(db)
    h = hashlib.sha256(key.encode()).hexdigest()
    with db.lock, db._get_conn() as conn:
        row = conn.execute("""SELECT u.id, u.email, u.name, u.role, u.created_at, u.last_login
                              FROM mail_api_keys k JOIN users u ON u.id = k.user_id WHERE k.key_hash = ?""", (h,)).fetchone()
        if row:
            conn.execute("UPDATE mail_api_keys SET last_used = ? WHERE key_hash = ?", (_now(), h))
            conn.commit()
    return dict(row) if row else None
