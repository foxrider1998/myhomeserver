"""password_reset.py - Lupa / reset kata sandi lewat tautan email.

Keamanan:
  * Token acak 256-bit; yang disimpan di DB hanya hash SHA-256-nya (bocor DB != bocor token).
  * Kedaluwarsa 30 menit, sekali pakai; permintaan baru membatalkan token lama.
  * Respons /forgot selalu sama (tidak membocorkan email mana yang terdaftar) dan pengiriman via antrean
    sehingga waktu respons tidak membedakan.
  * Dibatasi: maks 3 permintaan per akun per jam dan 10 per IP per jam.
  * Setelah reset: semua sesi akun dihapus dan pemilik akun diberi tahu lewat email.
  * Tautan memakai PUBLIC_BASE_URL (bukan header Host) agar tidak bisa dibelokkan (host header injection).
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import defaultdict

TOKEN_TTL_SEC = 30 * 60
MAX_PER_ACCOUNT_PER_HOUR = 3
MAX_PER_IP_PER_HOUR = 10

_ip_hits: dict[str, list[float]] = defaultdict(list)
_ip_lock = threading.Lock()


def ensure_table(db) -> None:
    with db.lock, db._get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS password_resets (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ip_allowed(ip: str) -> bool:
    now = time.time()
    with _ip_lock:
        hits = [t for t in _ip_hits[ip] if now - t < 3600]
        _ip_hits[ip] = hits
        if len(hits) >= MAX_PER_IP_PER_HOUR:
            return False
        hits.append(now)
        return True


def create_token(db, user_id: int) -> str | None:
    """Buat token baru; None bila akun sudah melewati batas permintaan per jam."""
    ensure_table(db)
    now = time.time()
    with db.lock, db._get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM password_resets WHERE user_id = ? AND created_at > ?", (user_id, now - 3600)).fetchone()["n"]
        if n >= MAX_PER_ACCOUNT_PER_HOUR:
            return None
        conn.execute("UPDATE password_resets SET used = 1 WHERE user_id = ? AND used = 0", (user_id,))  # batalkan token lama
        token = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO password_resets (token_hash, user_id, created_at, expires_at, used) VALUES (?, ?, ?, ?, 0)",
                     (_hash(token), user_id, now, now + TOKEN_TTL_SEC))
        conn.execute("DELETE FROM password_resets WHERE expires_at < ?", (now - 86400,))
        conn.commit()
    return token


def peek_valid(db, token: str) -> bool:
    ensure_table(db)
    if not token or len(token) > 200:
        return False
    with db.lock, db._get_conn() as conn:
        row = conn.execute("SELECT used, expires_at FROM password_resets WHERE token_hash = ?", (_hash(token),)).fetchone()
    return bool(row and not row["used"] and row["expires_at"] > time.time())


def consume(db, token: str) -> int | None:
    """Tandai token terpakai (atomik) dan kembalikan user_id; None bila tidak valid/kedaluwarsa/sudah dipakai."""
    ensure_table(db)
    if not token or len(token) > 200:
        return None
    with db.lock, db._get_conn() as conn:
        row = conn.execute("SELECT user_id, used, expires_at FROM password_resets WHERE token_hash = ?", (_hash(token),)).fetchone()
        if not row or row["used"] or row["expires_at"] <= time.time():
            return None
        cur = conn.execute("UPDATE password_resets SET used = 1 WHERE token_hash = ? AND used = 0", (_hash(token),))
        conn.commit()
        if cur.rowcount != 1:
            return None
        return int(row["user_id"])


RESET_PAGE = """<!doctype html>
<html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Atur Ulang Kata Sandi - Westpro</title>
<meta name="robots" content="noindex"><meta name="referrer" content="no-referrer">
<style>
  *{box-sizing:border-box} body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0b0c0e;color:#e4e4e7;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;padding:1rem}
  .card{width:100%;max-width:400px;background:#131418;border:1px solid #27272e;border-radius:14px;padding:1.6rem}
  h1{font-size:1.1rem;margin:0 0 .3rem} p{color:#94a3b8;font-size:.82rem;margin:0 0 1rem;line-height:1.5}
  label{display:block;font-size:.78rem;font-weight:600;margin:.8rem 0 .3rem;color:#cbd5e1}
  input{width:100%;background:#0b0c0e;border:1px solid #334155;border-radius:8px;padding:.6rem .75rem;color:#fff;font-size:.9rem}
  input:focus{outline:none;border-color:#38bdf8}
  button{width:100%;margin-top:1.1rem;background:#2563eb;color:#fff;border:0;border-radius:8px;padding:.7rem;font-weight:700;font-size:.9rem;cursor:pointer}
  button:disabled{opacity:.6;cursor:default} .msg{margin-top:.9rem;font-size:.8rem;padding:.6rem .75rem;border-radius:8px;display:none}
  .err{background:rgba(239,68,68,.12);color:#fca5a5;border:1px solid rgba(239,68,68,.3)} .ok{background:rgba(16,185,129,.12);color:#6ee7b7;border:1px solid rgba(16,185,129,.3)}
  a{color:#38bdf8}
</style></head><body>
<div class="card">
  <h1>Atur ulang kata sandi</h1>
  <p id="intro">Masukkan kata sandi baru untuk akun Anda. Setelah disimpan, semua perangkat akan keluar dan Anda perlu masuk lagi.</p>
  <form id="f" novalidate>
    <label for="p1">Kata sandi baru (min. 6 karakter)</label><input id="p1" type="password" autocomplete="new-password" minlength="6" required>
    <label for="p2">Ulangi kata sandi baru</label><input id="p2" type="password" autocomplete="new-password" minlength="6" required>
    <button id="b" type="submit">Simpan kata sandi baru</button>
  </form>
  <div id="m" class="msg"></div>
  <p style="margin-top:1rem;text-align:center"><a href="/login">Kembali ke halaman masuk</a></p>
</div>
<script>
  const token = new URLSearchParams(location.search).get('token') || '';
  if (history.replaceState) history.replaceState(null, '', '/reset');  // sembunyikan token dari bilah alamat/riwayat
  const m = document.getElementById('m');
  function show(t, ok){ m.textContent = t; m.className = 'msg ' + (ok ? 'ok' : 'err'); m.style.display = 'block'; }
  if (!token) { document.getElementById('f').style.display = 'none'; show('Tautan tidak lengkap. Minta tautan baru dari halaman masuk (Lupa kata sandi).', false); }
  document.getElementById('f').addEventListener('submit', async function (e) {
    e.preventDefault();
    const p1 = document.getElementById('p1').value, p2 = document.getElementById('p2').value;
    if (p1.length < 6) return show('Kata sandi minimal 6 karakter.', false);
    if (p1 !== p2) return show('Konfirmasi kata sandi tidak cocok.', false);
    const b = document.getElementById('b'); b.disabled = true;
    try {
      const r = await fetch('/api/auth/reset', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: token, password: p1 }) });
      const d = await r.json();
      if (d.success) { document.getElementById('f').style.display = 'none'; show('Kata sandi berhasil diubah. Mengarahkan ke halaman masuk...', true); setTimeout(function(){ location.href = '/login'; }, 2000); }
      else { show(d.error || 'Gagal mengubah kata sandi.', false); b.disabled = false; }
    } catch (err) { show('Gagal menghubungi server: ' + err.message, false); b.disabled = false; }
  });
</script></body></html>
"""
