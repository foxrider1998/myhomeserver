#!/usr/bin/env python3
"""hermes_briefing.py - Briefing harian Hermes per-user (HTML ter-render di Workspace QA).

- Konteks dibangun dari data user yang login (cache SPEctrum miliknya, kalender, cuaca server).
- LLM (hermes-agent /api/chat) diminta menulis fragmen HTML; hasilnya SELALU disanitasi (allowlist tag/class)
  sebelum disimpan, jadi aman di-render dengan innerHTML.
- Jalan otomatis 02:00 WIB hari kerja (Senin-Jumat) untuk semua user, atau manual lewat tombol "Perbarui".
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import os
import random
import re
import threading
import time
import urllib.request
from datetime import datetime
from html.parser import HTMLParser

logger = logging.getLogger("hermes_briefing")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HERMES_CHAT_URL = os.environ.get("HERMES_CHAT_URL", "http://localhost:8000/api/chat")
MANUAL_COOLDOWN_SEC = 60
SCHEDULE_HOUR = 2          # jam lokal server
SCHEDULE_WINDOW_END = 6    # bila server baru hidup setelah jam 2, tetap dikejar sampai jam ini

DAYS_ID = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
MONTHS_ID = ["", "Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus",
             "September", "Oktober", "November", "Desember"]

# ---------------------------------------------------------------------------
# Sanitasi HTML (allowlist)
# ---------------------------------------------------------------------------
ALLOWED_TAGS = {"div", "p", "span", "b", "strong", "i", "em", "u", "br", "hr", "ul", "ol", "li",
                "h3", "h4", "small", "table", "thead", "tbody", "tr", "th", "td", "blockquote"}
VOID_TAGS = {"br", "hr"}
DROP_CONTENT_TAGS = {"script", "style", "iframe", "object", "embed", "noscript", "template", "svg", "math"}
ALLOWED_CLASS_RE = re.compile(r"^hb-[a-z0-9-]{1,30}$")


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.open_stack: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in DROP_CONTENT_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth or tag not in ALLOWED_TAGS:
            return
        classes = []
        for k, v in attrs:
            if k.lower() == "class" and v:
                classes = [c for c in v.split() if ALLOWED_CLASS_RE.match(c)]
        attr = f' class="{" ".join(classes)}"' if classes else ""
        self.out.append(f"<{tag}{attr}>")
        if tag not in VOID_TAGS:
            self.open_stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag.lower() in VOID_TAGS:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in DROP_CONTENT_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth or tag not in ALLOWED_TAGS or tag in VOID_TAGS:
            return
        if tag in self.open_stack:
            while self.open_stack:
                t = self.open_stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.skip_depth:
            self.out.append(html_lib.escape(data, quote=False))

    def result(self) -> str:
        while self.open_stack:
            self.out.append(f"</{self.open_stack.pop()}>")
        return "".join(self.out)


def sanitize_html(raw: str) -> str:
    raw = (raw or "").strip()
    raw = re.sub(r"```(?:html)?", "", raw)
    s = _Sanitizer()
    try:
        s.feed(raw)
        s.close()
    except Exception:
        return ""
    return s.result().strip()


# ---------------------------------------------------------------------------
# Penyimpanan (tabel dibuat sendiri; tidak mengubah skema lain)
# ---------------------------------------------------------------------------
_locks_guard = threading.Lock()
_user_locks: dict[int, threading.Lock] = {}


def _user_lock(user_id: int) -> threading.Lock:
    with _locks_guard:
        return _user_locks.setdefault(user_id, threading.Lock())


def ensure_table(db) -> None:
    with db.lock, db._get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_briefings (
                user_id INTEGER PRIMARY KEY,
                html TEXT NOT NULL DEFAULT '',
                generated_at TEXT,
                trigger TEXT,
                source TEXT,
                error TEXT
            )
        """)
        conn.commit()


def get_briefing(db, user_id: int) -> dict | None:
    ensure_table(db)
    with db.lock, db._get_conn() as conn:
        row = conn.execute(
            "SELECT html, generated_at, trigger, source, error FROM user_briefings WHERE user_id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def save_briefing(db, user_id: int, html: str, trigger: str, source: str, error: str = "") -> dict:
    ensure_table(db)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db.lock, db._get_conn() as conn:
        conn.execute("""
            INSERT INTO user_briefings (user_id, html, generated_at, trigger, source, error)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET html = excluded.html, generated_at = excluded.generated_at,
                trigger = excluded.trigger, source = excluded.source, error = excluded.error
        """, (user_id, html, now, trigger, source, error))
        conn.commit()
    return {"html": html, "generated_at": now, "trigger": trigger, "source": source, "error": error}


# ---------------------------------------------------------------------------
# Konteks per-user
# ---------------------------------------------------------------------------
def _parse_dt(s: str):
    try:
        return datetime.strptime((s or "")[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except Exception:
        try:
            return datetime.strptime((s or "")[:10], "%Y-%m-%d")
        except Exception:
            return None


def _is_my_task(t: dict, name: str, email: str) -> bool:
    name, email = name.lower().strip(), email.lower().strip()
    a, e, c = (t.get("assignee") or "").lower(), (t.get("assignee_email") or "").lower(), (t.get("created_by") or "").lower()
    if email and e and (e == email or email in e or e in email):
        return True
    if name and a and (name in a or a in name):
        return True
    tokens = {w for w in re.split(r"\s+", name) if len(w) >= 3} | {w for w in re.split(r"[._-]", email.split("@")[0]) if len(w) >= 3}
    return any(tok in a or tok in e for tok in tokens)


def build_context(db, user: dict, extra: dict | None = None) -> dict:
    now = datetime.now()
    cfg = db.get_user_spectrum_config(user["id"]) or {}
    name = (cfg.get("member_name") or user.get("name") or user.get("email", "").split("@")[0]).strip()
    ctx: dict = {
        "nama": name,
        "tanggal": f"{DAYS_ID[now.weekday()]}, {now.day} {MONTHS_ID[now.month]} {now.year}",
        "jam": now.strftime("%H:%M"),
        "hari_kerja": now.weekday() < 5,
        "spectrum_terhubung": bool(cfg.get("spectrum_email")),
    }

    path = os.path.join(BASE_DIR, "data", f"spectrum_user_{user['id']}.json")
    data = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        pass
    if data:
        sprint = data.get("active_sprint") or {}
        tasks = data.get("tasks") or []
        mine = [t for t in tasks if _is_my_task(t, name, user.get("email", ""))]
        end = _parse_dt(sprint.get("end_date", ""))
        ctx["sprint"] = {
            "nama": sprint.get("name"),
            "proyek": (data.get("active_project") or {}).get("name"),
            "target": (sprint.get("description") or "")[:300],
            "sisa_hari": max(0, (end.date() - now.date()).days) if end else None,
            "total_task": len(tasks),
            "ready_qa_dev_done": sum(1 for t in tasks if t.get("status") in ("Done-Development", "Task-Finished")),
            "on_progress": sum(1 for t in tasks if t.get("status") == "On-Progress"),
        }
        ctx["task_saya"] = [{"kode": t.get("code"), "nama": t.get("name"), "status": t.get("status"),
                             "bobot": t.get("weight"), "proyek": t.get("project_name")} for t in mine[:12]]
        today = now.strftime("%Y-%m-%d")
        events = []
        for ev in data.get("calendar_events") or []:
            start = (ev.get("execute_at") or ev.get("start") or "")
            if start[:10] >= today:
                events.append({"nama": ev.get("name"), "mulai": start[:16].replace("T", " ")})
        events.sort(key=lambda x: x["mulai"])
        ctx["agenda"] = events[:6]
    else:
        ctx["catatan"] = "Data SPEctrum belum tersedia untuk pengguna ini."

    # Cuaca lokasi server (cache 15 menit; gagal -> dilewati)
    try:
        from solar_weather import get_solar_weather_data
        w = get_solar_weather_data()
        cur, fc = w.get("current", {}), w.get("forecast_3d", [])
        ctx["cuaca"] = {
            "lokasi": w.get("location"),
            "sekarang": f"{cur.get('condition')} {cur.get('temp')}°C, lembap {cur.get('humidity')}%",
            "prakiraan": [f"{f.get('label')}: {f.get('condition')} {f.get('temp_min')}-{f.get('temp_max')}°C, "
                          f"hujan {f.get('precipitation_prob')}%" for f in fc],
        }
        pa = w.get("planting_advice")
        if pa:
            ctx["cuaca"]["hujan_14_hari_mm"] = pa.get("rain_14d_mm")
    except Exception as e:
        logger.info("Cuaca tidak tersedia untuk briefing: %s", e)

    if extra:
        ctx.update(extra)
    return ctx


# ---------------------------------------------------------------------------
# Generate (LLM) + fallback
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """Kamu Hermes, asisten pribadi di Westpro Home Server. Tulis BRIEFING PAGI untuk pengguna berdasarkan data JSON.
Keluarkan HANYA fragmen HTML (tanpa <html>/<body>, tanpa markdown, tanpa code fence). Bahasa Indonesia santai, hangat, ringkas.

Tag yang boleh: div, p, span, b, strong, i, em, br, ul, ol, li, h3, h4, small, table/thead/tbody/tr/th/td, blockquote.
Class yang boleh (HANYA ini): hb-section, hb-title, hb-joke, hb-grid, hb-card, hb-chip, hb-ok, hb-warn, hb-muted, hb-list.
Dilarang: script, style, atribut style, link, gambar, emoji berlebihan (maks 1 per judul).

Struktur (pakai <div class="hb-section"> per bagian, judul <h3 class="hb-title">):
1. Sapaan hangat memakai nama + hari/tanggal, lalu 1 kalimat motivasi dan 1 candaan ringan yang sopan (class hb-joke). Candaan baru, jangan generik, boleh bertema QA/testing/kerja/cuaca.
2. Cuaca: ringkas kondisi hari ini dan 2 hari ke depan, plus 1 saran praktis (bawa jas hujan, jemur, dll). Lewati bila data cuaca tidak ada.
3. Pekerjaan: urutkan prioritas hari ini dari task_saya, agenda terdekat, dan task siap QA. Sebut kode task. Jika tidak ada data, katakan jujur dan sarankan hubungkan akun SPEctrum.
4. Anjuran: 2-3 poin singkat yang bisa dilakukan hari ini (fokus, istirahat, risiko sprint bila sisa hari sedikit).
Jangan mengarang task, angka, atau jadwal yang tidak ada di data. Maksimal sekitar 220 kata."""


def _call_hermes(prompt: str, system_prompt: str, timeout: float = 75.0) -> str:
    body = json.dumps({"prompt": prompt, "system_prompt": system_prompt}).encode("utf-8")
    req = urllib.request.Request(HERMES_CHAT_URL, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return (json.loads(resp.read().decode("utf-8")).get("reply") or "").strip()


JOKES = [
    "Bug itu seperti mantan: kelihatannya sudah selesai, tapi tiba-tiba muncul lagi di production.",
    "QA itu profesi unik: makin banyak yang ditemukan, makin ada yang bilang kita bikin kerjaan.",
    "Kopi dulu, baru test case. Urutannya jangan dibalik, nanti expected result-nya error.",
    "Server rumah ini 24/7 online. Semoga semangat kita juga, minimal sampai jam pulang.",
    "Kalau semua test case hijau di percobaan pertama, curigai test case-nya, bukan keberuntunganmu.",
]
MOTIVASI = [
    "Pelan tapi pasti: satu task selesai lebih baik daripada lima task setengah jalan.",
    "Kualitas dibangun dari hal kecil yang dicek berulang. Hari ini kamu bagian dari itu.",
    "Mulai dari task yang paling bikin ragu; sisanya akan terasa lebih ringan.",
]


def fallback_html(ctx: dict) -> str:
    e = html_lib.escape
    parts = [f'<div class="hb-section"><h3 class="hb-title">Selamat pagi, {e(ctx["nama"])}</h3>'
             f'<p>{e(ctx["tanggal"])}. {e(random.choice(MOTIVASI))}</p>'
             f'<blockquote class="hb-joke">{e(random.choice(JOKES))}</blockquote></div>']
    cw = ctx.get("cuaca")
    if cw:
        items = "".join(f"<li>{e(x)}</li>" for x in cw.get("prakiraan", []))
        parts.append(f'<div class="hb-section"><h3 class="hb-title">Cuaca</h3><p>{e(str(cw.get("sekarang", "")))}'
                     f' <span class="hb-muted">({e(str(cw.get("lokasi", "")))})</span></p><ul class="hb-list">{items}</ul></div>')
    work = []
    for t in ctx.get("task_saya", [])[:6]:
        work.append(f"<li><b>{e(str(t.get('kode') or ''))}</b> {e(str(t.get('nama') or ''))} "
                    f'<span class="hb-chip">{e(str(t.get("status") or ""))}</span></li>')
    for a in ctx.get("agenda", [])[:3]:
        work.append(f"<li>Agenda: {e(str(a['nama']))} <span class=\"hb-muted\">({e(a['mulai'])})</span></li>")
    sp = ctx.get("sprint")
    if sp and sp.get("ready_qa_dev_done"):
        work.append(f"<li>{sp['ready_qa_dev_done']} task sudah dev done dan siap di-QA.</li>")
    if work:
        parts.append(f'<div class="hb-section"><h3 class="hb-title">Pekerjaan hari ini</h3><ul class="hb-list">{"".join(work)}</ul></div>')
    else:
        parts.append('<div class="hb-section"><h3 class="hb-title">Pekerjaan hari ini</h3>'
                     '<p class="hb-muted">Belum ada data task. Hubungkan akun SPEctrum di menu Profil &amp; SPEctrum, lalu klik Perbarui.</p></div>')
    parts.append('<div class="hb-section"><h3 class="hb-title">Anjuran</h3><ul class="hb-list">'
                 "<li>Kerjakan satu hal paling penting lebih dulu.</li><li>Istirahat 5 menit setiap 50 menit fokus.</li>"
                 "<li>Catat temuan segera supaya tidak lupa saat daily.</li></ul></div>")
    return "".join(parts)


def generate_for_user(db, user: dict, trigger: str = "manual", extra_ctx: dict | None = None) -> dict:
    """Bangun & simpan briefing satu user. trigger: 'manual' | 'auto'."""
    lock = _user_lock(user["id"])
    if not lock.acquire(blocking=False):
        cur = get_briefing(db, user["id"]) or {}
        cur["busy"] = True
        return cur
    try:
        ctx = build_context(db, user, extra_ctx)
        prompt = "Data pengguna (JSON):\n" + json.dumps(ctx, ensure_ascii=False, indent=1)
        source, err, html = "llm", "", ""
        try:
            html = sanitize_html(_call_hermes(prompt, SYSTEM_PROMPT))
            if len(re.sub(r"<[^>]+>", "", html).strip()) < 40:
                raise ValueError("Balasan LLM kosong/tidak valid")
        except Exception as e:
            source, err = "fallback", str(e)[:200]
            logger.warning("Briefing LLM gagal untuk user %s: %s", user["id"], e)
            html = sanitize_html(fallback_html(ctx))
        return save_briefing(db, user["id"], html, trigger, source, err)
    finally:
        lock.release()


_HB_INLINE = {
    "hb-section": "margin:0 0 14px;padding:12px 14px;border:1px solid #e2e8f0;border-left:3px solid #38bdf8;border-radius:8px;",
    "hb-title": "margin:0 0 6px;font-size:15px;color:#0369a1;",
    "hb-joke": "margin:8px 0;padding:8px 12px;border-left:3px solid #f59e0b;background:#fffbeb;color:#92400e;font-style:italic;",
    "hb-chip": "display:inline-block;font-size:11px;padding:1px 8px;border-radius:10px;background:#e0f2fe;color:#0369a1;",
    "hb-ok": "color:#047857;", "hb-warn": "color:#b45309;", "hb-muted": "color:#64748b;",
    "hb-list": "margin:6px 0 0 18px;padding:0;", "hb-card": "padding:8px 10px;border:1px solid #e2e8f0;border-radius:8px;", "hb-grid": "",
}


def email_html(html: str) -> str:
    """Ubah class hb-* menjadi style inline (klien email sering membuang <style>). Masukan harus sudah disanitasi."""
    return re.sub(r'class="([^"]*)"', lambda m: 'style="' + "".join(_HB_INLINE.get(c, "") for c in m.group(1).split()) + '"', html)


def send_briefing_email(db, user: dict, html: str) -> bool:
    """Kirim briefing ke email user bila SMTP aktif dan user memilih menerima (opt-in)."""
    try:
        import mailer
        if not mailer.is_configured():
            return False
        prefs = db.get_email_prefs(user["id"])
        if not prefs.get("briefing") or not mailer.valid_address(prefs.get("email", "")):
            return False
        now = datetime.now()
        title = f"Briefing Hermes - {DAYS_ID[now.weekday()]}, {now.day} {MONTHS_ID[now.month]} {now.year}"
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"[ \t]+", " ", text).strip()
        body = mailer.wrap_html(title, email_html(html), button=("Buka Workspace", mailer.cfg()["base_url"] + "/hermes"),
                                footer="Anda menerima email ini karena mengaktifkannya di Profil. Matikan kapan saja di menu Profil & SPEctrum.")
        return mailer.send_async(db, prefs["email"], title, text, body, kind="briefing")
    except Exception as e:
        logger.warning("Email briefing user %s gagal: %s", user.get("id"), e)
        return False


def manual_allowed(db, user_id: int) -> tuple[bool, int]:
    """Cooldown agar tombol Perbarui tidak membakar kuota LLM."""
    b = get_briefing(db, user_id)
    if not b or not b.get("generated_at"):
        return True, 0
    last = _parse_dt(b["generated_at"])
    if not last:
        return True, 0
    wait = MANUAL_COOLDOWN_SEC - int((datetime.now() - last).total_seconds())
    return wait <= 0, max(0, wait)


# ---------------------------------------------------------------------------
# Penjadwal 02:00 hari kerja
# ---------------------------------------------------------------------------
def _list_users(db) -> list[dict]:
    with db.lock, db._get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT id, email, name, role FROM users").fetchall()]


def run_daily_batch(db, sync_fn=None) -> int:
    """Generate briefing semua user yang belum punya briefing 'auto' hari ini. Mengembalikan jumlah yang dibuat."""
    today = datetime.now().strftime("%Y-%m-%d")
    done = 0
    for user in _list_users(db):
        b = get_briefing(db, user["id"])
        if b and b.get("trigger") == "auto" and (b.get("generated_at") or "").startswith(today):
            continue
        if sync_fn:
            try:
                sync_fn(user)  # segarkan data SPEctrum dulu (best-effort)
            except Exception as e:
                logger.info("Sync SPEctrum user %s dilewati: %s", user["id"], e)
        try:
            res = generate_for_user(db, user, trigger="auto")
            done += 1
            if res and res.get("html") and not res.get("busy"):
                send_briefing_email(db, user, res["html"])
        except Exception as e:
            logger.warning("Briefing auto user %s gagal: %s", user["id"], e)
        time.sleep(1.0)
    return done


def start_scheduler(db, sync_fn=None) -> threading.Thread:
    ensure_table(db)

    def loop():
        last_batch_day = ""
        while True:
            try:
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                if now.weekday() < 5 and SCHEDULE_HOUR <= now.hour < SCHEDULE_WINDOW_END and last_batch_day != today:
                    n = run_daily_batch(db, sync_fn)
                    last_batch_day = today
                    logger.info("Briefing harian otomatis selesai: %d user", n)
            except Exception as e:
                logger.warning("Scheduler briefing error: %s", e)
            time.sleep(60)

    t = threading.Thread(target=loop, name="hermes-briefing-scheduler", daemon=True)
    t.start()
    return t
