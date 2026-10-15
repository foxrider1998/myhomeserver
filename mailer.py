#!/usr/bin/env python3
"""mailer.py - Layanan email KELUAR (SMTP) untuk Westpro Home Server.

Catatan Cloudflare Tunnel:
  Tunnel hanya meneruskan trafik HTTP(S) ke web. Email keluar tidak lewat tunnel: server menghubungi relay SMTP
  (Brevo, Resend, Amazon SES, Gmail App Password, dll.) langsung dari koneksi internet server (port 587 atau 465).
  Email MASUK ke domain tidak bisa dilayani lewat tunnel; gunakan Cloudflare Email Routing (diteruskan ke Gmail).

Konfigurasi (environment / .env):
  SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD,
  SMTP_SECURITY = starttls | ssl | none   (587 -> starttls, 465 -> ssl)
  SMTP_FROM  mis. "Westpro <no-reply@westpro.my.id>"  (domain harus lolos SPF/DKIM penyedia)
  MAIL_REPLY_TO (opsional), SMTP_TIMEOUT (20)
  PUBLIC_BASE_URL = https://westpro.my.id   (dipakai untuk tautan di email; JANGAN diambil dari header Host)
"""

from __future__ import annotations

import html as html_lib
import logging
import os
import queue
import re
import smtplib
import ssl
import threading
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

logger = logging.getLogger("mailer")

_EMAIL_RE = re.compile(r"^[^@\s<>\",;]+@[^@\s<>\",;]+\.[^@\s<>\",;]+$")


def cfg() -> dict:
    user = os.environ.get("SMTP_USER", "").strip()
    port_raw = os.environ.get("SMTP_PORT", "587").strip() or "587"
    security = os.environ.get("SMTP_SECURITY", "").strip().lower()
    port = int(port_raw) if port_raw.isdigit() else 587
    if security not in ("starttls", "ssl", "none"):
        security = "ssl" if port == 465 else "starttls"
    return {
        "host": os.environ.get("SMTP_HOST", "").strip(),
        "port": port,
        "user": user,
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "security": security,
        "from": os.environ.get("SMTP_FROM", "").strip() or user,
        "reply_to": os.environ.get("MAIL_REPLY_TO", "").strip(),
        "timeout": float(os.environ.get("SMTP_TIMEOUT", "20") or 20),
        "base_url": (os.environ.get("PUBLIC_BASE_URL", "").strip() or "https://westpro.my.id").rstrip("/"),
    }


def is_configured() -> bool:
    c = cfg()
    return bool(c["host"] and c["from"])


def valid_address(addr: str) -> bool:
    return bool(addr) and "\n" not in addr and "\r" not in addr and bool(_EMAIL_RE.match(addr.strip()))


def status() -> dict:
    c = cfg()
    return {
        "configured": is_configured(),
        "host": c["host"], "port": c["port"], "security": c["security"],
        "user": c["user"], "from": c["from"], "base_url": c["base_url"],
        "has_password": bool(c["password"]),
    }


def build_message(to: str, subject: str, text: str, html: str | None = None, reply_to: str | None = None) -> EmailMessage:
    c = cfg()
    if not valid_address(to):
        raise ValueError("Alamat email tujuan tidak valid")
    msg = EmailMessage()
    msg["From"] = c["from"]
    msg["To"] = to.strip()
    msg["Subject"] = re.sub(r"[\r\n]+", " ", subject)[:200]
    msg["Date"] = formatdate(localtime=True)
    domain = (parseaddr(c["from"])[1].split("@")[-1] or "westpro.local")
    msg["Message-ID"] = make_msgid(domain=domain)
    rt = reply_to or c["reply_to"]
    if rt and valid_address(parseaddr(rt)[1]):
        msg["Reply-To"] = rt
    msg["X-Auto-Response-Suppress"] = "OOF, AutoReply"
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def send_now(to: str, subject: str, text: str, html: str | None = None, reply_to: str | None = None) -> None:
    """Kirim sinkron. Melempar exception dengan pesan yang jelas bila gagal (dipakai tombol Email Uji)."""
    c = cfg()
    if not is_configured():
        raise RuntimeError("SMTP belum dikonfigurasi (isi SMTP_HOST dan SMTP_FROM di .env).")
    msg = build_message(to, subject, text, html, reply_to)
    ctx = ssl.create_default_context()
    if c["security"] == "ssl":
        server = smtplib.SMTP_SSL(c["host"], c["port"], timeout=c["timeout"], context=ctx)
    else:
        server = smtplib.SMTP(c["host"], c["port"], timeout=c["timeout"])
    try:
        server.ehlo()
        if c["security"] == "starttls":
            server.starttls(context=ctx)
            server.ehlo()
        if c["user"]:
            server.login(c["user"], c["password"])
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


# ---------------------------------------------------------------- log + antrean
def ensure_log_table(db) -> None:
    with db.lock, db._get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mail_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT,
                to_addr TEXT,
                subject TEXT,
                kind TEXT,
                status TEXT,
                error TEXT
            )
        """)
        conn.commit()


def _log(db, to: str, subject: str, kind: str, status_: str, error: str = "") -> None:
    if db is None:
        return
    try:
        ensure_log_table(db)
        with db.lock, db._get_conn() as conn:
            conn.execute("INSERT INTO mail_log (created_at, to_addr, subject, kind, status, error) VALUES (?, ?, ?, ?, ?, ?)",
                         (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), to, subject[:120], kind, status_, error[:300]))
            conn.execute("DELETE FROM mail_log WHERE id NOT IN (SELECT id FROM mail_log ORDER BY id DESC LIMIT 200)")
            conn.commit()
    except Exception as e:
        logger.warning("Gagal menulis mail_log: %s", e)


def recent_logs(db, limit: int = 15) -> list[dict]:
    ensure_log_table(db)
    with db.lock, db._get_conn() as conn:
        rows = conn.execute("SELECT created_at, to_addr, subject, kind, status, error FROM mail_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        a = d["to_addr"] or ""
        d["to_addr"] = (a[:2] + "***" + a[a.find("@"):]) if "@" in a else a  # samarkan penerima di tampilan
        out.append(d)
    return out


_queue: "queue.Queue[tuple]" = queue.Queue(maxsize=500)
_worker_started = False
_worker_lock = threading.Lock()
RETRY_DELAYS = (0, 6, 25)


def _worker():
    while True:
        db, to, subject, text, html, kind, reply_to = _queue.get()
        last_err = ""
        for delay in RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                send_now(to, subject, text, html, reply_to)
                _log(db, to, subject, kind, "terkirim")
                last_err = ""
                break
            except Exception as e:  # coba lagi dengan jeda
                last_err = str(e)
                logger.warning("Kirim email (%s) gagal: %s", kind, e)
        if last_err:
            _log(db, to, subject, kind, "gagal", last_err)


def send_async(db, to: str, subject: str, text: str, html: str | None = None, kind: str = "umum", reply_to: str | None = None) -> bool:
    """Masukkan ke antrean (tidak memblokir request web). False bila SMTP belum diatur / antrean penuh."""
    global _worker_started
    if not is_configured() or not valid_address(to):
        return False
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_worker, name="mailer-worker", daemon=True).start()
            _worker_started = True
    try:
        _queue.put_nowait((db, to, subject, text, html, kind, reply_to))
        return True
    except queue.Full:
        return False


# ---------------------------------------------------------------- templat
def render(title: str, paragraphs: list[str], button: tuple[str, str] | None = None, footer: str = "") -> tuple[str, str]:
    """Kembalikan (teks, html) untuk email sederhana yang rapi di semua klien email."""
    e = html_lib.escape
    text_lines = [title, ""] + paragraphs
    if button:
        text_lines += ["", f"{button[0]}: {button[1]}"]
    if footer:
        text_lines += ["", footer]
    text_lines += ["", "-- Westpro Home Server"]
    body = "".join(f'<p style="margin:0 0 14px;line-height:1.55;color:#334155;font-size:14px;">{e(p)}</p>' for p in paragraphs)
    btn = ""
    if button:
        btn = (f'<p style="margin:22px 0;"><a href="{e(button[1], quote=True)}" style="background:#2563eb;color:#ffffff;text-decoration:none;'
               f'padding:11px 22px;border-radius:8px;font-weight:600;font-size:14px;display:inline-block;">{e(button[0])}</a></p>'
               f'<p style="margin:0 0 14px;color:#64748b;font-size:12px;word-break:break-all;">Atau salin tautan ini: {e(button[1])}</p>')
    foot = f'<p style="margin:18px 0 0;color:#94a3b8;font-size:12px;line-height:1.5;">{e(footer)}</p>' if footer else ""
    html = (
        '<!doctype html><html><body style="margin:0;padding:24px 12px;background:#f1f5f9;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">'
        '<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border-radius:12px;'
        'border:1px solid #e2e8f0;"><tr><td style="padding:26px 28px;">'
        '<div style="font-weight:800;color:#0f172a;font-size:15px;letter-spacing:.3px;margin-bottom:18px;">WESTPRO HOME SERVER</div>'
        f'<h1 style="margin:0 0 16px;font-size:20px;color:#0f172a;">{e(title)}</h1>{body}{btn}{foot}'
        '</td></tr></table></td></tr></table></body></html>'
    )
    return "\n".join(text_lines), html


def wrap_html(title: str, inner_html: str, button: tuple[str, str] | None = None, footer: str = "") -> str:
    """Bungkus HTML (yang SUDAH disanitasi pemanggil) dengan kerangka email yang sama seperti render()."""
    e = html_lib.escape
    btn = ""
    if button:
        btn = (f'<p style="margin:22px 0;"><a href="{e(button[1], quote=True)}" style="background:#2563eb;color:#ffffff;text-decoration:none;'
               f'padding:11px 22px;border-radius:8px;font-weight:600;font-size:14px;display:inline-block;">{e(button[0])}</a></p>')
    foot = f'<p style="margin:18px 0 0;color:#94a3b8;font-size:12px;line-height:1.5;">{e(footer)}</p>' if footer else ""
    return (
        '<!doctype html><html><body style="margin:0;padding:24px 12px;background:#f1f5f9;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:12px;border:1px solid #e2e8f0;">'
        '<tr><td style="padding:24px 26px;color:#334155;font-size:14px;line-height:1.55;">'
        '<div style="font-weight:800;color:#0f172a;font-size:15px;letter-spacing:.3px;margin-bottom:16px;">WESTPRO HOME SERVER</div>'
        f'<h1 style="margin:0 0 16px;font-size:20px;color:#0f172a;">{e(title)}</h1>{inner_html}{btn}{foot}'
        '</td></tr></table></td></tr></table></body></html>'
    )
