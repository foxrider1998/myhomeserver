#!/usr/bin/env python3
"""stock_sim.py - Simulasi trading harian oleh AI (paper trading) di atas data stock_service.

Aturan simulasi:
  * Modal awal diatur pengguna; akun 'live' berjalan otomatis tiap 10 menit (09:10-15:50 WIB, Sen-Jum),
    akun 'uji' bisa dijalankan manual kapan saja dan terpisah dari akun live.
  * AI memilih saham dari kandidat analisis pra-pasar hari itu + pergerakan terkuat saat ini.
  * Eksekusi realistis sederhana: BELI di harga ask (= harga terakhir + 1 fraksi harga),
    JUAL di harga bid (= harga terakhir - 1 fraksi harga). Data gratis tidak punya order book,
    jadi spread dimodelkan 1 tick. Fee beli/jual dihitung (default 0,15% / 0,25%). Lot = 100 lembar.
  * Day trading: semua posisi dilikuidasi 15:50. Tidak ada short selling, tidak ada margin.
  * Batasan keras oleh kode (bukan AI): maks posisi, maks % ekuitas per saham, hanya saham likuid
    dari daftar kandidat, tidak beli setelah 15:20, auto jual bila menyentuh CL/TP yang ditetapkan AI.
Bukan nasihat investasi; fill sebenarnya (antrian, ARA/ARB, partial fill) tidak disimulasikan.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime

import stock_service as ss

logger = logging.getLogger("stock_sim")

DEFAULTS = {
    "enabled": 0, "capital": 10_000_000, "max_positions": 5, "max_pos_pct": 30.0,
    "fee_buy": 0.15, "fee_sell": 0.25, "last_buy": "15:20", "liquidate": "15:50",
}
FIRST_TICK = (9, 10)      # data tertunda 10 menit -> keputusan pertama saat data pukul 09:00 tersedia
LAST_TICK = (15, 50)
LOT = 100

_tick_lock = threading.Lock()
_state = {"running": False, "last_slot": "", "last_error": "", "last_tick_at": "", "stage": ""}


# ---------------------------------------------------------------------------
# DB & konfigurasi
# ---------------------------------------------------------------------------
def init_sim_db():
    ss.init_db()
    with ss._db_lock, ss._db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sim_config(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS sim_account(acct TEXT PRIMARY KEY, cash REAL, initial REAL, created TEXT);
        CREATE TABLE IF NOT EXISTS sim_positions(
            acct TEXT, ticker TEXT, name TEXT, lots INTEGER, avg_price REAL, cost REAL, tp REAL, cl REAL,
            opened_at TEXT, trade_date TEXT, last_price REAL, reason TEXT, PRIMARY KEY(acct, ticker));
        CREATE TABLE IF NOT EXISTS sim_trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT, acct TEXT, trade_date TEXT, ts TEXT, ticker TEXT, side TEXT,
            lots INTEGER, price REAL, fee REAL, amount REAL, pnl REAL, pnl_pct REAL, reason TEXT);
        CREATE TABLE IF NOT EXISTS sim_days(
            acct TEXT, trade_date TEXT, start_equity REAL, end_equity REAL, pnl REAL, pnl_pct REAL,
            trades INTEGER DEFAULT 0, wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0, status TEXT,
            PRIMARY KEY(acct, trade_date));
        CREATE TABLE IF NOT EXISTS sim_equity(acct TEXT, trade_date TEXT, ts TEXT, equity REAL, cash REAL);
        CREATE INDEX IF NOT EXISTS idx_sim_eq ON sim_equity(acct, trade_date);
        CREATE TABLE IF NOT EXISTS sim_ticks(trade_date TEXT, ts TEXT, ticker TEXT, price REAL, vol REAL, high REAL, low REAL, chg REAL);
        CREATE INDEX IF NOT EXISTS idx_sim_tk ON sim_ticks(trade_date, ticker);
        CREATE TABLE IF NOT EXISTS sim_log(id INTEGER PRIMARY KEY AUTOINCREMENT, acct TEXT, trade_date TEXT, ts TEXT, kind TEXT, text TEXT);
        """)
        c.commit()


def get_config() -> dict:
    cfg = dict(DEFAULTS)
    with ss._db_lock, ss._db() as c:
        for r in c.execute("SELECT key, value FROM sim_config").fetchall():
            if r["key"] in DEFAULTS:
                t = type(DEFAULTS[r["key"]])
                try:
                    cfg[r["key"]] = t(float(r["value"])) if t in (int, float) else r["value"]
                except Exception:
                    pass
    return cfg


def save_config(upd: dict) -> dict:
    cfg = get_config()
    for k, v in (upd or {}).items():
        if k not in DEFAULTS:
            continue
        t = type(DEFAULTS[k])
        try:
            cfg[k] = t(float(v)) if t in (int, float) else str(v)
        except Exception:
            continue
    cfg["capital"] = max(1_000_000, float(cfg["capital"]))
    cfg["max_positions"] = max(1, min(15, int(cfg["max_positions"])))
    cfg["max_pos_pct"] = max(5.0, min(100.0, float(cfg["max_pos_pct"])))
    cfg["fee_buy"] = max(0.0, min(2.0, float(cfg["fee_buy"])))
    cfg["fee_sell"] = max(0.0, min(2.0, float(cfg["fee_sell"])))
    cfg["enabled"] = 1 if int(cfg["enabled"]) else 0
    with ss._db_lock, ss._db() as c:
        for k, v in cfg.items():
            c.execute("INSERT OR REPLACE INTO sim_config(key, value) VALUES(?,?)", (k, str(v)))
        c.commit()
    return cfg


def _ensure_account(c, acct: str, cfg: dict):
    r = c.execute("SELECT * FROM sim_account WHERE acct=?", (acct,)).fetchone()
    if not r:
        c.execute("INSERT INTO sim_account VALUES(?,?,?,?)", (acct, cfg["capital"], cfg["capital"], ss._now().strftime("%Y-%m-%d %H:%M:%S")))
        r = c.execute("SELECT * FROM sim_account WHERE acct=?", (acct,)).fetchone()
    return r


def reset_account(acct: str, capital: float | None = None) -> dict:
    cfg = save_config({"capital": capital}) if capital else get_config()
    with ss._db_lock, ss._db() as c:
        for t in ("sim_account", "sim_positions", "sim_trades", "sim_days", "sim_equity", "sim_log"):
            c.execute(f"DELETE FROM {t} WHERE acct=?", (acct,))
        _ensure_account(c, acct, cfg)
        c.commit()
    return {"success": True, "capital": cfg["capital"]}


def _log(c, acct: str, kind: str, text: str):
    c.execute("INSERT INTO sim_log(acct, trade_date, ts, kind, text) VALUES(?,?,?,?,?)",
              (acct, ss._today(), ss._now().strftime("%H:%M:%S"), kind, text[:1500]))


# ---------------------------------------------------------------------------
# Harga & eksekusi
# ---------------------------------------------------------------------------
def _ask(last: float) -> float:
    return last + ss._tick(last)


def _bid(last: float) -> float:
    return max(last - ss._tick(last), 1.0)


def _equity(c, acct: str, cash: float) -> tuple[float, list]:
    pos = c.execute("SELECT * FROM sim_positions WHERE acct=?", (acct,)).fetchall()
    value = sum(p["lots"] * LOT * _bid(p["last_price"] or p["avg_price"]) for p in pos)
    return cash + value, pos


def _do_buy(c, acct, cfg, cash, ticker, name, lots, last, tp, cl, reason, equity) -> tuple[float, str]:
    price = _ask(last)
    fee_rate = cfg["fee_buy"] / 100
    pos = c.execute("SELECT * FROM sim_positions WHERE acct=?", (acct,)).fetchall()
    held = next((p for p in pos if p["ticker"] == ticker), None)
    if not held and len(pos) >= cfg["max_positions"]:
        return cash, f"BELI {ticker} ditolak: sudah {len(pos)} posisi (maks {cfg['max_positions']})"
    cap_idr = equity * cfg["max_pos_pct"] / 100 - (held["lots"] * LOT * _bid(held["last_price"] or held["avg_price"]) if held else 0)
    budget = min(cash, cap_idr)
    max_lots = int(budget // (price * LOT * (1 + fee_rate)))
    lots = min(int(lots), max_lots)
    if lots < 1:
        return cash, f"BELI {ticker} ditolak: dana/porsi tidak cukup untuk 1 lot (harga {price:g}/lembar)"
    amount = lots * LOT * price
    fee = amount * fee_rate
    cost = amount + fee
    cash -= cost
    now = ss._now()
    if held:
        new_lots = held["lots"] + lots
        new_cost = held["cost"] + cost
        c.execute("UPDATE sim_positions SET lots=?, cost=?, avg_price=?, tp=?, cl=?, last_price=? WHERE acct=? AND ticker=?",
                  (new_lots, new_cost, new_cost / (new_lots * LOT), tp or held["tp"], cl or held["cl"], last, acct, ticker))
    else:
        c.execute("INSERT INTO sim_positions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (acct, ticker, name, lots, cost / (lots * LOT), cost, tp, cl, now.strftime("%H:%M"), ss._today(), last, reason))
    c.execute("INSERT INTO sim_trades(acct, trade_date, ts, ticker, side, lots, price, fee, amount, pnl, pnl_pct, reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (acct, ss._today(), now.strftime("%H:%M:%S"), ticker, "BUY", lots, price, fee, amount, None, None, reason[:300]))
    return cash, f"BELI {ticker} {lots} lot @ {price:g} (ask) = Rp{cost:,.0f} | TP {tp or '-'} CL {cl or '-'} | {reason}"


def _do_sell(c, acct, cfg, cash, p, last, reason) -> tuple[float, str, float]:
    price = _bid(last)
    amount = p["lots"] * LOT * price
    fee = amount * cfg["fee_sell"] / 100
    proceeds = amount - fee
    pnl = proceeds - p["cost"]
    pnl_pct = pnl / p["cost"] * 100 if p["cost"] else 0.0
    cash += proceeds
    c.execute("DELETE FROM sim_positions WHERE acct=? AND ticker=?", (acct, p["ticker"]))
    c.execute("INSERT INTO sim_trades(acct, trade_date, ts, ticker, side, lots, price, fee, amount, pnl, pnl_pct, reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (acct, ss._today(), ss._now().strftime("%H:%M:%S"), p["ticker"], "SELL", p["lots"], price, fee, amount,
               round(pnl, 2), round(pnl_pct, 3), reason[:300]))
    return cash, f"JUAL {p['ticker']} {p['lots']} lot @ {price:g} (bid) | P&L Rp{pnl:,.0f} ({pnl_pct:+.2f}%) | {reason}", pnl


# ---------------------------------------------------------------------------
# Kandidat & prompt AI
# ---------------------------------------------------------------------------
def _candidates(rows: list[dict], now_by: dict) -> list[dict]:
    today = ss._today()
    data = None
    for tag in ("pre", "test"):
        d = ss.get_picks(today, tag)
        if any(m["picks"] for m in d.get("methods", [])):
            data = d
            break
    pool: dict[str, dict] = {}
    if data:
        for m in data["methods"]:
            for p in m["picks"]:
                e = pool.setdefault(p["ticker"], {"ticker": p["ticker"], "name": p["name"], "ref": p["ref_price"], "radar": 0,
                                                  "tp": p.get("tp"), "cl": p.get("cl"), "sent": p.get("sentiment"),
                                                  "sum": (p.get("ai_summary") or "")[:110], "src": "pra-pasar"})
                e["radar"] += 1
    ranked = sorted(pool.values(), key=lambda x: -x["radar"])[:40]
    pool = {e["ticker"]: e for e in ranked}
    # pergerakan terkuat saat ini (likuid) yang belum ada di daftar
    try:
        df = ss._frame(rows)
        u = ss._universe(df)
        u = u[(u.chg > 0.5) & (u.chg < 20)].copy()
        u["mv"] = u.chg * u.relvol.fillna(1).clip(upper=5)
        for _, r in u.sort_values("mv", ascending=False).head(8).iterrows():
            if r.ticker not in pool:
                pool[r.ticker] = {"ticker": r.ticker, "name": ss._short_name(r.get("desc") or ""), "ref": float(r.close - (r.close * r.chg / 100) / (1 + r.chg / 100)) if r.chg else float(r.close),
                                  "radar": 0, "tp": None, "cl": None, "sent": None, "sum": "", "src": "mover"}
    except Exception as e:
        logger.warning(f"[Sim] mover gagal: {e}")
    return list(pool.values())


def _line(e: dict, n: dict) -> str:
    last = n.get("close") or 0
    ref = e.get("ref") or last
    vs = (last / ref - 1) * 100 if ref else 0
    value = last * (n.get("vol") or 0)
    return (f"{e['ticker']} | {e['name'][:22]} | kini {last:g} | hari {n.get('chg') or 0:+.1f}% | vs acuan {vs:+.1f}% | "
            f"hi {n.get('high') or 0:g} lo {n.get('low') or 0:g} | relvol {n.get('relvol') or 0:.1f}x | nilai {value / 1e9:.1f}M | "
            f"RSI {n.get('rsi') or 0:.0f} | radar {e['radar']}/9 | sent {e.get('sent') or '-'} | saran TP {e.get('tp') or '-'} CL {e.get('cl') or '-'} | {e['src']}"
            + (f" | data per menit: {e['trail']}" if e.get("trail") else "")
            + (f" | {e['sum']}" if e.get("sum") else ""))


SYSTEM_PROMPT = (
    "Anda trader harian (day trader) saham BEI yang mengelola dana simulasi. Tujuan: maksimalkan profit HARI INI, "
    "semua posisi akan dilikuidasi otomatis pukul 15:50. Data tertunda 10 menit. Eksekusi: BELI di harga ask (harga kini + 1 tick), "
    "JUAL di harga bid (harga kini - 1 tick), ada fee beli/jual, jadi hindari trading berlebihan (churning) dan cut loss cepat. "
    "Gunakan BRIEFING PAGI, arah IHSG kini, tren harga 10 menitan tiap saham, dan keputusan Anda sebelumnya untuk konsisten (jangan membolak-balik posisi tanpa alasan baru). "
    "Hanya boleh membeli saham dari daftar KANDIDAT. Tetapkan TP dan CL (harga) setiap BUY. "
    "Jawab HANYA JSON valid: {\"analysis\":\"1-2 kalimat kondisi pasar/portofolio\",\"actions\":[{\"action\":\"BUY\",\"ticker\":\"XXXX\","
    "\"pct\":persen_ekuitas_untuk_posisi_ini_antara_5_dan_30,\"tp\":harga,\"cl\":harga,\"reason\":\"alasan singkat\"},{\"action\":\"SELL\",\"ticker\":\"XXXX\",\"reason\":\"alasan singkat\"}]}. "
    "Ukuran posisi ditentukan lewat pct (persen dari EKUITAS); sistem yang menghitung jumlah lot (1 lot = 100 lembar), jadi jangan menghitung lot sendiri. "
    "Daftar actions boleh kosong (tahan posisi / tunggu). Jangan mengarang data."
)


def _context(acct: str, cands: list, now_by: dict) -> str:
    """Konteks pagi (briefing AI), arah IHSG kini, dan keputusan AI sebelumnya hari ini."""
    today = ss._today()
    parts = []
    ov = ss._get_analysis(today, "pre", "overview") or ss._get_analysis(today, "test", "overview")
    if ov:
        out = ov.get("ihsg_outlook") or {}
        parts.append("BRIEFING PAGI (analisis AI pra-pasar): " + str(ov.get("market_overview", ""))[:700])
        parts.append(f"Bias IHSG pagi: {out.get('bias', '-')} ({str(out.get('reason', ''))[:200]}); support {out.get('support', '-')}, resistance {out.get('resistance', '-')}")
        if ov.get("risks"):
            parts.append("Risiko: " + "; ".join(str(x)[:100] for x in ov["risks"][:4]))
        if ov.get("watch_sectors"):
            parts.append("Sektor dipantau: " + ", ".join(str(x) for x in ov["watch_sectors"][:5]))
    try:
        g = {x["symbol"]: x for x in ss.live_global(60)}
        ih = g.get("IDX:COMPOSITE")
        if ih:
            parts.append(f"IHSG kini (tertunda 10 mnt): {ih['last']:g} ({ih['chg_pct']:+.2f}% hari ini)")
    except Exception:
        pass
    with ss._db_lock, ss._db() as c:
        prev = c.execute("SELECT ts, text FROM sim_log WHERE acct=? AND trade_date=? AND kind IN ('AI','BUY','SELL') ORDER BY id DESC LIMIT 6", (acct, today)).fetchall()
    if prev:
        parts.append("KEPUTUSAN/ANALISIS ANDA SEBELUMNYA HARI INI (terbaru di atas):\n" + "\n".join(f"{r['ts']} {r['text'][:220]}" for r in prev))
    return "\n".join(parts)


_collect = {"last": 0.0, "tracked": (0.0, set()), "running": False}


def _min_of(ts: str) -> int:
    h, m = ts.split(":")[:2]
    return int(h) * 60 + int(m)


def _tracked_tickers() -> set:
    """Saham yang dipantau per menit: kandidat analisis pagi + posisi + yang sudah punya tick hari ini (mis. top mover)."""
    cached_at, cached = _collect["tracked"]
    if time.time() - cached_at < 240 and cached:
        return cached
    today = ss._today()
    with ss._db_lock, ss._db() as c:
        t = {r[0] for r in c.execute("SELECT DISTINCT ticker FROM picks WHERE trade_date=?", (today,)).fetchall()}
        t |= {r[0] for r in c.execute("SELECT DISTINCT ticker FROM sim_positions").fetchall()}
        t |= {r[0] for r in c.execute("SELECT DISTINCT ticker FROM sim_ticks WHERE trade_date=?", (today,)).fetchall()}
    _collect["tracked"] = (time.time(), t)
    return t


def _store_ticks(rows: list[dict], tickers: set, hhmm: str) -> int:
    today = ss._today()
    n = 0
    with ss._db_lock, ss._db() as c:
        last = {r["ticker"]: (r["price"], r["vol"]) for r in c.execute(
            "SELECT ticker, price, vol FROM sim_ticks WHERE rowid IN (SELECT MAX(rowid) FROM sim_ticks WHERE trade_date=? GROUP BY ticker)", (today,)).fetchall()}
        for r in rows:
            tk = r["ticker"]
            if tk not in tickers or not r.get("close"):
                continue
            if last.get(tk) == (r["close"], r.get("vol")):
                continue  # data belum berubah sejak menit lalu (feed tertunda membeku / istirahat)
            c.execute("INSERT INTO sim_ticks VALUES(?,?,?,?,?,?,?,?)", (today, hhmm, tk, r["close"], r.get("vol"), r.get("high"), r.get("low"), r.get("chg")))
            n += 1
        c.execute("DELETE FROM sim_ticks WHERE trade_date < ?", ((ss._now() - __import__("datetime").timedelta(days=5)).strftime("%Y-%m-%d"),))
        c.commit()
    return n


def collect_minute():
    """Ambil snapshot TradingView (dibagi dengan pelacak TP/CL lewat cache) & simpan tick saham yang dipantau."""
    if _collect["running"]:
        return
    _collect["running"] = True
    try:
        now = ss._now()
        rows = ss.live_snapshot(50)
        _store_ticks(rows, _tracked_tickers(), now.strftime("%H:%M"))
    except Exception as e:
        logger.warning(f"[Sim] kumpul tick gagal: {e}")
    finally:
        _collect["running"] = False


def maybe_collect():
    """Dipanggil scheduler: kumpulkan tick tiap ~60 dtk selama jam data bursa (tertunda 10 mnt)."""
    now = ss._now()
    t = (now.hour, now.minute)
    if now.weekday() >= 5 or not ((9, 1) <= t <= (16, 15)) or _in_break(now) or time.time() - _collect["last"] < 55 or _collect["running"]:
        return
    _collect["last"] = time.time()
    init_sim_db()
    threading.Thread(target=collect_minute, name="saham-ticks", daemon=True).start()


def _micro(c, ticker: str, today: str, now_min: int) -> str:
    """Ringkas data per menit: momentum 10/30 menit, lonjakan volume, rentang 30 menit, tren harga."""
    rows = c.execute("SELECT ts, price, vol FROM sim_ticks WHERE trade_date=? AND ticker=? ORDER BY rowid DESC LIMIT 60", (today, ticker)).fetchall()
    pts = [(_min_of(r["ts"]), r["price"], r["vol"] or 0) for r in reversed(rows)]
    if len(pts) < 3:
        return ""
    t_now, p_now, v_now = pts[-1]

    def at(minutes_ago):
        target = t_now - minutes_ago
        cand = [p for p in pts if p[0] <= target]
        return cand[-1] if cand else pts[0]

    parts = []
    p10, p30 = at(10), at(30)
    if p10[1]:
        parts.append(f"10m {(p_now / p10[1] - 1) * 100:+.2f}%")
    if p30[1] and p30[0] != p10[0]:
        parts.append(f"30m {(p_now / p30[1] - 1) * 100:+.2f}%")
    d10 = max(v_now - p10[2], 0)
    d_prev = max(p10[2] - at(40)[2], 0)
    if d10 > 0:
        txt = f"vol10m Rp{d10 * p_now / 1e9:.2f}M"
        if d_prev > 0:
            txt += f" ({d10 / (d_prev / 3):.1f}x rata-rata 30m sebelumnya)"
        parts.append(txt)
    recent = [p[1] for p in pts if p[0] >= t_now - 30]
    if recent:
        parts.append(f"rentang30m {min(recent):g}-{max(recent):g}")
    trail = [f"{p[1]:g}" for p in pts[-8:]]
    parts.append("tren " + "→".join(trail))
    return ", ".join(parts)


def _record_prices(acct: str, cands: list, now_by: dict, hhmm: str) -> None:
    """Pastikan tick terbaru kandidat tersimpan, lalu isi e['trail'] dari data per menit yang terkumpul."""
    today = ss._today()
    tickers = {e["ticker"] for e in cands}
    _store_ticks([now_by[t] for t in tickers if t in now_by], tickers, hhmm)
    _collect["tracked"] = (0.0, set())  # paksa muat ulang agar top mover ikut dipantau menit berikutnya
    now_min = _min_of(hhmm)
    with ss._db_lock, ss._db() as c:
        for e in cands:
            txt = _micro(c, e["ticker"], today, now_min)
            if txt:
                e["trail"] = txt


def _ai_decide(cfg, cash, equity, positions, cands, now_by, t_now, can_buy: bool, context: str = "") -> tuple[str, list]:
    pos_lines = []
    for p in positions:
        n = now_by.get(p["ticker"], {})
        last = n.get("close") or p["last_price"] or p["avg_price"]
        pnl = (_bid(last) * p["lots"] * LOT * (1 - cfg["fee_sell"] / 100) - p["cost"])
        pos_lines.append(f"{p['ticker']} {p['lots']} lot, avg {p['avg_price']:.1f}, kini {last:g}, P&L {pnl:+,.0f} ({pnl / p['cost'] * 100:+.2f}%), TP {p['tp'] or '-'} CL {p['cl'] or '-'}, sejak {p['opened_at']}")
    c_lines = [_line(e, now_by[e["ticker"]]) for e in cands if e["ticker"] in now_by]
    user = (f"Waktu data: {t_now} WIB (tertunda 10 menit). Beli baru {'DIPERBOLEHKAN' if can_buy else 'TIDAK diperbolehkan (mendekati penutupan)'}.\n"
            f"Kas Rp{cash:,.0f} | Ekuitas Rp{equity:,.0f} | maks {cfg['max_positions']} posisi | maks {cfg['max_pos_pct']:.0f}% ekuitas per saham.\n\n"
            + (context + "\n\n" if context else "") +
            f"POSISI SAAT INI:\n" + ("\n".join(pos_lines) if pos_lines else "(kosong)") +
            "\n\nKANDIDAT (data live):\n" + "\n".join(c_lines))
    res = ss._ai_json(SYSTEM_PROMPT, user)
    return str(res.get("analysis", ""))[:500], [a for a in (res.get("actions") or []) if isinstance(a, dict)]


# ---------------------------------------------------------------------------
# Siklus 10 menit
# ---------------------------------------------------------------------------
def _hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def _in_break(now: datetime) -> bool:
    """Istirahat sesi bursa (data tertunda 10 menit, jadi harga membeku mulai 10 menit setelah jam istirahat):
    Sen-Kam 12:00-13:30, Jumat 11:30-14:00."""
    t = now.hour * 60 + now.minute
    if now.weekday() == 4:
        return 11 * 60 + 40 <= t <= 14 * 60
    return 12 * 60 + 10 <= t <= 13 * 60 + 30


def _market_closed_today(rows: list[dict]) -> bool:
    """Hari libur bursa: total volume snapshot identik dengan penutupan sesi sebelumnya."""
    prev = ss._load_snapshot(kind="close", before=ss._today())
    if not prev:
        return False
    return abs(sum((r.get("vol") or 0) for r in rows) - sum((r.get("vol") or 0) for r in prev)) < 1


def run_tick(acct: str = "live", force: bool = False, liquidate_all: bool = False) -> dict:
    """Satu siklus: tandai harga, auto-exit CL/TP, minta keputusan AI, eksekusi, catat ekuitas.
    Tiga fase agar kunci DB tidak dipegang saat menunggu AI/jaringan.
    force=True (akun uji) mengabaikan jam bursa."""
    if not _tick_lock.acquire(blocking=False):
        return {"error": "siklus lain sedang berjalan"}
    _state.update(running=True, last_error="", stage="Mengambil data...")
    try:
        init_sim_db()
        cfg = get_config()
        now = ss._now()
        t = (now.hour, now.minute)
        test = acct != "live"
        if not test and not liquidate_all and _in_break(now):
            return {"skipped": "istirahat sesi bursa"}
        rows = ss.fetch_snapshot()
        if not test and _market_closed_today(rows):
            return {"skipped": "bursa libur hari ini (volume tidak berubah dari penutupan sebelumnya)"}
        now_by = {r["ticker"]: r for r in rows}
        today = ss._today()
        end_of_day = liquidate_all or (not test and t >= _hhmm(cfg["liquidate"]))
        can_buy = test or t < _hhmm(cfg["last_buy"])
        sells = 0

        # ---- Fase A: tandai harga, auto-exit, likuidasi ----
        with ss._db_lock, ss._db() as c:
            acc = _ensure_account(c, acct, cfg)
            cash = acc["cash"]
            equity0, _ = _equity(c, acct, cash)
            if not c.execute("SELECT 1 FROM sim_days WHERE acct=? AND trade_date=?", (acct, today)).fetchone():
                c.execute("INSERT INTO sim_days(acct, trade_date, start_equity, end_equity, pnl, pnl_pct, status) VALUES(?,?,?,?,0,0,'running')",
                          (acct, today, equity0, equity0))
            for p in c.execute("SELECT * FROM sim_positions WHERE acct=?", (acct,)).fetchall():
                n = now_by.get(p["ticker"])
                if n and n.get("close"):
                    c.execute("UPDATE sim_positions SET last_price=? WHERE acct=? AND ticker=?", (n["close"], acct, p["ticker"]))
            for p in c.execute("SELECT * FROM sim_positions WHERE acct=?", (acct,)).fetchall():
                n = now_by.get(p["ticker"])
                if not n or not n.get("close") or (n.get("vol") or 0) <= 0:
                    continue
                last = n["close"]
                reason = None
                if p["trade_date"] != today:
                    reason = "posisi sisa hari sebelumnya, dilikuidasi saat pembukaan"
                elif p["cl"] and last <= p["cl"]:
                    reason = f"AUTO CL {p['cl']:g} tersentuh"
                elif p["tp"] and last >= p["tp"]:
                    reason = f"AUTO TP {p['tp']:g} tercapai"
                elif end_of_day:
                    reason = "likuidasi akhir hari"
                if reason:
                    cash, msg, _ = _do_sell(c, acct, cfg, cash, p, last, reason)
                    _log(c, acct, "SELL", msg)
                    sells += 1
            c.execute("UPDATE sim_account SET cash=? WHERE acct=?", (cash, acct))
            equity, _ = _equity(c, acct, cash)
            positions = c.execute("SELECT * FROM sim_positions WHERE acct=?", (acct,)).fetchall()
            c.commit()

        # ---- Fase B: kandidat + keputusan AI (tanpa kunci DB) ----
        analysis, actions, cands, ai_err = "", [], [], ""
        if not end_of_day:
            _state["stage"] = "AI mengambil keputusan..."
            cands = _candidates(rows, now_by)
            try:
                _record_prices(acct, cands, now_by, now.strftime("%H:%M"))
                ctx = _context(acct, cands, now_by)
            except Exception as ce:
                logger.warning(f"[Sim] konteks gagal: {ce}")
                ctx = ""
            try:
                analysis, actions = _ai_decide(cfg, cash, equity, positions, cands, now_by, now.strftime("%H:%M"), can_buy, ctx)
            except Exception as e:
                ai_err = str(e)
                _state["last_error"] = ai_err

        # ---- Fase C: eksekusi & rekap ----
        with ss._db_lock, ss._db() as c:
            cash = c.execute("SELECT cash FROM sim_account WHERE acct=?", (acct,)).fetchone()["cash"]
            if not end_of_day:
                _log(c, acct, "ERROR" if ai_err else "AI", (f"AI gagal: {ai_err[:200]} - tahan posisi (guard CL/TP tetap aktif)" if ai_err else (analysis or "(tanpa analisis)")))
            cand_by = {e["ticker"]: e for e in cands}
            for a in actions[:8]:
                act = str(a.get("action", "")).upper()
                tk = str(a.get("ticker", "")).upper().strip()
                n = now_by.get(tk)
                reason = str(a.get("reason", ""))[:200]
                if not n or not n.get("close") or (n.get("vol") or 0) <= 0:
                    _log(c, acct, "SKIP", f"{act} {tk} diabaikan: tidak ada data/transaksi")
                    continue
                last = n["close"]
                if act == "BUY":
                    if not can_buy:
                        _log(c, acct, "SKIP", f"BELI {tk} diabaikan: lewat batas beli ({cfg['last_buy']})")
                        continue
                    if tk not in cand_by:
                        _log(c, acct, "SKIP", f"BELI {tk} ditolak: bukan saham kandidat")
                        continue
                    ara = 0.35 if last < 200 else (0.25 if last < 5000 else 0.20)
                    if (n.get("chg") or 0) >= ara * 100 * 0.97:
                        _log(c, acct, "SKIP", f"BELI {tk} ditolak: harga di zona ARA (tidak realistis terisi)")
                        continue
                    try:
                        tp = float(a.get("tp")) if a.get("tp") else None
                        cl = float(a.get("cl")) if a.get("cl") else None
                    except Exception:
                        tp = cl = None
                    if not tp or not cl or cl >= last or tp <= last:
                        tp, cl = ss.trade_levels("ai_news", last, n.get("atr"))
                    equity, _ = _equity(c, acct, cash)
                    try:
                        if a.get("pct"):
                            pct = max(1.0, min(float(a["pct"]), cfg["max_pos_pct"]))
                            lots = int((equity * pct / 100) // (_ask(last) * LOT * (1 + cfg["fee_buy"] / 100)))
                        else:
                            lots = int(float(a.get("lots", 1) or 1))
                    except Exception:
                        lots = 1
                    cash, msg = _do_buy(c, acct, cfg, cash, tk, cand_by[tk]["name"], lots, last, tp, cl, reason, equity)
                    _log(c, acct, "SKIP" if "ditolak" in msg else "BUY", msg)
                elif act == "SELL":
                    p = c.execute("SELECT * FROM sim_positions WHERE acct=? AND ticker=?", (acct, tk)).fetchone()
                    if not p:
                        _log(c, acct, "SKIP", f"JUAL {tk} diabaikan: tidak punya posisi")
                        continue
                    cash, msg, _ = _do_sell(c, acct, cfg, cash, p, last, reason or "keputusan AI")
                    _log(c, acct, "SELL", msg)
                    sells += 1

            equity, positions = _equity(c, acct, cash)
            c.execute("UPDATE sim_account SET cash=? WHERE acct=?", (cash, acct))
            c.execute("INSERT INTO sim_equity VALUES(?,?,?,?,?)", (acct, today, now.strftime("%H:%M"), round(equity, 2), round(cash, 2)))
            tr = c.execute("SELECT COUNT(*) n, SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) w, SUM(CASE WHEN pnl<=0 THEN 1 ELSE 0 END) l "
                           "FROM sim_trades WHERE acct=? AND trade_date=? AND side='SELL'", (acct, today)).fetchone()
            start_eq = c.execute("SELECT start_equity FROM sim_days WHERE acct=? AND trade_date=?", (acct, today)).fetchone()["start_equity"]
            status = "done" if (end_of_day and not positions) else "running"
            c.execute("UPDATE sim_days SET end_equity=?, pnl=?, pnl_pct=?, trades=?, wins=?, losses=?, status=? WHERE acct=? AND trade_date=?",
                      (round(equity, 2), round(equity - start_eq, 2), round((equity / start_eq - 1) * 100, 3) if start_eq else 0,
                       tr["n"] or 0, tr["w"] or 0, tr["l"] or 0, status, acct, today))
            c.commit()
        _state.update(last_tick_at=now.strftime("%Y-%m-%d %H:%M:%S"))
        return {"acct": acct, "equity": round(equity, 2), "cash": round(cash, 2), "positions": len(positions), "sells": sells, "status": status}
    except Exception as e:
        logger.exception("[Sim] tick gagal")
        _state["last_error"] = str(e)
        return {"error": str(e)}
    finally:
        _state.update(running=False, stage="")
        _tick_lock.release()


def maybe_tick():
    """Dipanggil scheduler tiap ~30 dtk: jalankan siklus akun live tepat tiap 10 menit di jam bursa."""
    try:
        now = ss._now()
        t = (now.hour, now.minute)
        if now.weekday() >= 5 or not (FIRST_TICK <= t <= LAST_TICK) or now.minute % 10 != 0 or _in_break(now):
            return
        slot = now.strftime("%Y-%m-%d %H:%M")
        if slot == _state["last_slot"] or _state["running"]:
            return
        init_sim_db()
        if not get_config()["enabled"]:
            return
        _state["last_slot"] = slot
        threading.Thread(target=run_tick, args=("live",), name="saham-sim", daemon=True).start()
    except Exception as e:
        logger.warning(f"[Sim] maybe_tick error: {e}")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def get_state(acct: str) -> dict:
    init_sim_db()
    cfg = get_config()
    today = ss._today()
    with ss._db_lock, ss._db() as c:
        acc = _ensure_account(c, acct, cfg)
        c.commit()
        equity, pos = _equity(c, acct, acc["cash"])
        positions = []
        for p in pos:
            last = p["last_price"] or p["avg_price"]
            val = p["lots"] * LOT * _bid(last) * (1 - cfg["fee_sell"] / 100)
            positions.append({"ticker": p["ticker"], "name": p["name"], "lots": p["lots"], "avg": round(p["avg_price"], 2), "last": last,
                              "tp": p["tp"], "cl": p["cl"], "value": round(val, 0), "pnl": round(val - p["cost"], 0),
                              "pnl_pct": round((val / p["cost"] - 1) * 100, 2) if p["cost"] else 0, "since": p["opened_at"], "reason": p["reason"]})
        day = c.execute("SELECT * FROM sim_days WHERE acct=? AND trade_date=?", (acct, today)).fetchone()
        trades = [dict(r) for r in c.execute("SELECT * FROM sim_trades WHERE acct=? AND trade_date=? ORDER BY id DESC LIMIT 60", (acct, today)).fetchall()]
        log = [dict(r) for r in c.execute("SELECT * FROM sim_log WHERE acct=? AND trade_date=? ORDER BY id DESC LIMIT 60", (acct, today)).fetchall()]
        curve = [dict(r) for r in c.execute("SELECT ts, equity FROM sim_equity WHERE acct=? AND trade_date=? ORDER BY rowid", (acct, today)).fetchall()]
        hist = [dict(r) for r in c.execute("SELECT * FROM sim_days WHERE acct=? ORDER BY trade_date DESC LIMIT 60", (acct,)).fetchall()]
    done_days = [h for h in hist if h["status"] == "done"]
    now = ss._now()
    return {"success": True, "acct": acct, "config": cfg,
            "account": {"cash": round(acc["cash"], 0), "equity": round(equity, 0), "initial": acc["initial"],
                        "total_pnl": round(equity - acc["initial"], 0), "total_pnl_pct": round((equity / acc["initial"] - 1) * 100, 2) if acc["initial"] else 0,
                        "since": acc["created"]},
            "today": dict(day) if day else None, "positions": positions, "trades": trades, "log": log, "curve": curve, "history": hist,
            "stats": {"days": len(done_days), "win_days": sum(1 for h in done_days if h["pnl"] > 0),
                      "avg_daily_pct": round(sum(h["pnl_pct"] for h in done_days) / len(done_days), 3) if done_days else 0},
            "market_hours": now.weekday() < 5 and FIRST_TICK <= (now.hour, now.minute) <= LAST_TICK,
            "ticks": _tick_stats(),
            "runtime": dict(_state)}


def _range_sql(frm: str | None, to: str | None) -> tuple[str, list]:
    q, a = "", []
    if frm:
        q += " AND trade_date>=?"; a.append(frm)
    if to:
        q += " AND trade_date<=?"; a.append(to)
    return q, a


def get_report(acct: str, frm: str | None = None, to: str | None = None) -> dict:
    """Ringkasan realized P&L + deret harian untuk periode bebas (kosong = sejak awal)."""
    init_sim_db()
    cfg = get_config()
    rq, ra = _range_sql(frm, to)
    with ss._db_lock, ss._db() as c:
        acc = _ensure_account(c, acct, cfg)
        c.commit()
        days = [dict(r) for r in c.execute("SELECT * FROM sim_days WHERE acct=?" + rq + " ORDER BY trade_date", [acct] + ra).fetchall()]
        sells = [dict(r) for r in c.execute("SELECT ticker, pnl, pnl_pct, trade_date, ts, lots, price FROM sim_trades WHERE acct=? AND side='SELL' AND pnl IS NOT NULL" + rq + " ORDER BY id", [acct] + ra).fetchall()]
        n_all = c.execute("SELECT COUNT(*) n FROM sim_trades WHERE acct=?" + rq, [acct] + ra).fetchone()["n"]
        span = c.execute("SELECT MIN(trade_date) a, MAX(trade_date) b FROM sim_trades WHERE acct=?", (acct,)).fetchone()
    base = days[0]["start_equity"] if days else acc["initial"]
    series, peak, mdd = [], base, 0.0
    for d in days:
        peak = max(peak, d["end_equity"])
        dd = (d["end_equity"] / peak - 1) * 100 if peak else 0.0
        mdd = min(mdd, dd)
        series.append({"date": d["trade_date"], "start": d["start_equity"], "end": d["end_equity"], "pnl": d["pnl"], "pnl_pct": d["pnl_pct"],
                       "cum_pnl": round(d["end_equity"] - base, 2), "cum_pct": round((d["end_equity"] / base - 1) * 100, 3) if base else 0,
                       "trades": d["trades"], "wins": d["wins"], "losses": d["losses"], "status": d["status"]})
    wins = [x["pnl"] for x in sells if x["pnl"] > 0]
    losses = [x["pnl"] for x in sells if x["pnl"] <= 0]
    gross_w, gross_l = sum(wins), -sum(losses)
    per: dict[str, dict] = {}
    for x in sells:
        e = per.setdefault(x["ticker"], {"ticker": x["ticker"], "n": 0, "wins": 0, "pnl": 0.0})
        e["n"] += 1
        e["wins"] += 1 if x["pnl"] > 0 else 0
        e["pnl"] += x["pnl"]
    realized = sum(x["pnl"] for x in sells)
    ended = [d for d in series if d["status"] == "done"]
    return {"success": True, "acct": acct, "from": frm, "to": to, "initial": acc["initial"], "base": base,
            "first_trade": span["a"], "last_trade": span["b"], "series": series,
            "summary": {
                "realized_pnl": round(realized, 0), "realized_pct": round(realized / base * 100, 3) if base else 0,
                "sells": len(sells), "total_trades": n_all, "win_rate": round(len(wins) / len(sells) * 100, 1) if sells else 0,
                "avg_win": round(gross_w / len(wins), 0) if wins else 0, "avg_loss": round(-gross_l / len(losses), 0) if losses else 0,
                "profit_factor": round(gross_w / gross_l, 2) if gross_l else None,
                "best": max(sells, key=lambda x: x["pnl"]) if sells else None, "worst": min(sells, key=lambda x: x["pnl"]) if sells else None,
                "days": len(ended), "win_days": sum(1 for d in ended if d["pnl"] > 0), "loss_days": sum(1 for d in ended if d["pnl"] <= 0),
                "avg_daily_pct": round(sum(d["pnl_pct"] for d in ended) / len(ended), 3) if ended else 0,
                "best_day": max(ended, key=lambda d: d["pnl"]) if ended else None, "worst_day": min(ended, key=lambda d: d["pnl"]) if ended else None,
                "max_drawdown_pct": round(mdd, 2)},
            "per_ticker": sorted(per.values(), key=lambda e: -e["pnl"])}


def get_trades(acct: str, frm=None, to=None, ticker=None, offset: int = 0, limit: int = 200, order: str = "asc") -> dict:
    init_sim_db()
    rq, ra = _range_sql(frm, to)
    if ticker:
        rq += " AND ticker=?"; ra.append(ticker.upper())
    with ss._db_lock, ss._db() as c:
        total = c.execute("SELECT COUNT(*) n FROM sim_trades WHERE acct=?" + rq, [acct] + ra).fetchone()["n"]
        rows = c.execute("SELECT * FROM sim_trades WHERE acct=?" + rq + " ORDER BY id " + ("DESC" if order == "desc" else "ASC") + " LIMIT ? OFFSET ?",
                         [acct] + ra + [max(1, min(int(limit), 1000)), max(0, int(offset))]).fetchall()
    return {"success": True, "total": total, "trades": [dict(r) for r in rows]}


def get_day(acct: str, date: str) -> dict:
    init_sim_db()
    with ss._db_lock, ss._db() as c:
        day = c.execute("SELECT * FROM sim_days WHERE acct=? AND trade_date=?", (acct, date)).fetchone()
        curve = [dict(r) for r in c.execute("SELECT ts, equity FROM sim_equity WHERE acct=? AND trade_date=? ORDER BY rowid", (acct, date)).fetchall()]
        trades = [dict(r) for r in c.execute("SELECT * FROM sim_trades WHERE acct=? AND trade_date=? ORDER BY id", (acct, date)).fetchall()]
        log = [dict(r) for r in c.execute("SELECT * FROM sim_log WHERE acct=? AND trade_date=? ORDER BY id", (acct, date)).fetchall()]
    return {"success": True, "date": date, "day": dict(day) if day else None, "curve": curve, "trades": trades, "log": log}


def _tick_stats() -> dict:
    with ss._db_lock, ss._db() as c:
        r = c.execute("SELECT COUNT(*) n, COUNT(DISTINCT ticker) t, MAX(ts) last FROM sim_ticks WHERE trade_date=?", (ss._today(),)).fetchone()
    return {"points": r["n"], "tickers": r["t"], "last": r["last"]}


def handle_api(method: str, sub: str, q: dict, body: dict) -> tuple[int, dict]:
    try:
        acct = "test" if (q.get("acct") or body.get("acct")) == "test" else "live"
        if method == "GET" and sub == "state":
            return 200, get_state(acct)
        if method == "GET" and sub == "report":
            return 200, get_report(acct, q.get("from") or None, q.get("to") or None)
        if method == "GET" and sub == "trades":
            return 200, get_trades(acct, q.get("from") or None, q.get("to") or None, q.get("ticker") or None,
                                   int(q.get("offset") or 0), int(q.get("limit") or 200), q.get("order") or "asc")
        if method == "GET" and sub == "day":
            return 200, get_day(acct, q.get("date") or ss._today())
        if method == "POST":
            if sub == "config":
                cfg = save_config(body)
                return 200, {"success": True, "config": cfg}
            if sub == "reset":
                if str(body.get("confirm")) != "yes":
                    return 400, {"success": False, "error": "konfirmasi diperlukan"}
                return 200, reset_account(acct, float(body["capital"]) if body.get("capital") else None)
            if sub in ("tick", "liquidate"):
                if acct == "live" and sub == "tick" and not (get_state("live")["market_hours"]):
                    return 400, {"success": False, "error": "Akun live hanya berjalan di jam bursa (09:10-15:50 WIB). Gunakan akun uji."}
                if _state["running"]:
                    return 409, {"success": False, "error": "siklus sedang berjalan"}
                threading.Thread(target=run_tick, args=(acct, True, sub == "liquidate"), name="saham-sim-manual", daemon=True).start()
                return 200, {"success": True, "message": "Siklus dimulai"}
        return 404, {"success": False, "error": "Tidak ditemukan"}
    except Exception as e:
        logger.exception("[Sim] API error")
        return 500, {"success": False, "error": str(e)}


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["tick", "liquidate", "state", "reset"])
    ap.add_argument("--acct", default="test")
    ap.add_argument("--capital", type=float, default=0)
    a = ap.parse_args()
    init_sim_db()
    if a.cmd == "reset":
        print(reset_account(a.acct, a.capital or None))
    elif a.cmd == "state":
        s = get_state(a.acct)
        print(json.dumps({k: s[k] for k in ("account", "today", "positions", "stats")}, indent=1, default=str))
        for l in s["log"][:12][::-1]:
            print(l["ts"], l["kind"], l["text"][:200])
    else:
        print(run_tick(a.acct, True, a.cmd == "liquidate"))
