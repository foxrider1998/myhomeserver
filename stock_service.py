#!/usr/bin/env python3
"""stock_service.py - Analisis saham IHSG multi-metode untuk Westpro Home Server.

Alur harian (WIB, Senin-Jumat):
  08:20  Pra-pasar : ambil data live SEMUA saham IDX (TradingView scanner) + konteks global (Yahoo),
                     jalankan 8 metode analisis -> top 10 per metode, kumpulkan berita (Google News + RSS),
                     kirim ke AI untuk analisis sentimen per saham & rekomendasi gabungan (metode ke-9: AI + berita).
  16:30  Evaluasi  : ambil harga penutupan, hitung hasil tiap rekomendasi vs harga acuan (close sebelumnya),
                     bandingkan dengan pasar (rata-rata semua saham likuid), perbarui statistik akurasi metode.

Sumber data: TradingView scanner (semua saham IDX, tertunda 10 menit), Yahoo Finance (IHSG & indeks global),
Google News RSS + RSS CNBC Indonesia / Detik / CNN / Antara / IDNFinancials.
Bukan nasihat investasi; akurasi dihitung dari rekomendasi yang dicatat sebelum pasar buka.
"""

from __future__ import annotations

import calendar
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import feedparser
import pandas as pd
import requests

logger = logging.getLogger("stock_service")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "stocks.db")
WIB = ZoneInfo("Asia/Jakarta")

PRE_TIME = (8, 20)       # analisis pra-pasar (40 menit sebelum buka 09:00)
MARKET_OPEN = (9, 0)
EVAL_TIME = (16, 30)     # data tertunda ~15 menit; penutupan 16:00 + sesi pasca-penutupan 16:15
MAX_ATTEMPTS = 3
RETRY_GAP_SEC = 600
LIVE_INTERVAL_SEC = 60   # pelacak TP/CL selama jam bursa

# Filter likuiditas agar rekomendasi tidak berisi saham "gorengan"/tidak bisa ditransaksikan
MIN_PRICE = 50
MIN_VALUE = 2_000_000_000        # nilai transaksi harian >= Rp2 miliar
MIN_MCAP = 500_000_000_000       # kapitalisasi >= Rp500 miliar

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0 Safari/537.36"}

TV_COLUMNS = {
    "name": "name", "description": "desc", "close": "close", "change": "chg", "volume": "vol",
    "market_cap_basic": "mcap", "price_earnings_ttm": "pe", "sector": "sector", "industry": "industry",
    "RSI": "rsi", "MACD.macd": "macd", "MACD.signal": "macds", "SMA20": "sma20", "SMA50": "sma50",
    "SMA200": "sma200", "EMA20": "ema20", "Stoch.K": "stk", "Stoch.D": "stkd", "BB.upper": "bbu",
    "BB.lower": "bbl", "ADX": "adx", "relative_volume_10d_calc": "relvol", "price_52_week_high": "h52",
    "price_52_week_low": "l52", "Recommend.All": "rec", "open": "open", "high": "high", "low": "low",
    "return_on_equity": "roe", "net_margin": "nm", "dividends_yield_current": "dy",
    "earnings_per_share_basic_ttm": "eps", "debt_to_equity": "der", "price_book_fq": "pbv",
    "Perf.W": "pw", "Perf.1M": "p1m", "Perf.3M": "p3m", "ATR": "atr",
}

GLOBAL_SYMBOLS = [
    ("^JKSE", "IHSG"), ("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^DJI", "Dow Jones"),
    ("^N225", "Nikkei 225"), ("^HSI", "Hang Seng"), ("USDIDR=X", "USD/IDR"),
    ("BZ=F", "Minyak Brent"), ("GC=F", "Emas"),
]

NEWS_FEEDS = {
    "CNBC Indonesia": "https://www.cnbcindonesia.com/market/rss",
    "CNBC Indonesia News": "https://www.cnbcindonesia.com/news/rss",
    "Detik Finance": "https://finance.detik.com/rss",
    "CNN Indonesia": "https://www.cnnindonesia.com/ekonomi/rss",
    "Antara Ekonomi": "https://www.antaranews.com/rss/ekonomi-bisnis.xml",
    "IDNFinancials": "https://www.idnfinancials.com/feed",
}
GENERAL_QUERIES = ["IHSG", "ekonomi Indonesia", "BI rate rupiah", "bursa saham Indonesia", "OJK BEI emiten"]

# Claude Code (prefix cc/) lewat 9Router diprioritaskan; sisanya cadangan bila kuota/credential habis
AI_MODEL_CHAIN = [
    "cc/claude-haiku-4-5-20251001", "gpt-4o", "gemini/gemini-2.5-flash", "cc/claude-sonnet-5-5", "cc/claude-sonnet-5",
]

_db_lock = threading.Lock()
_run_lock = threading.Lock()
_state = {"running": False, "job": "", "stage": "", "started": 0.0, "error": "", "last_finished": 0.0}
_cache: dict = {}


# ---------------------------------------------------------------------------
# Util
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(WIB)


def _today() -> str:
    return _now().strftime("%Y-%m-%d")


def _num(x, default=None):
    try:
        if x is None:
            return default
        v = float(x)
        return default if (math.isnan(v) or math.isinf(v)) else v
    except Exception:
        return default


def _fmt(x, spec="{:.1f}", dash="-"):
    v = _num(x)
    return dash if v is None else spec.format(v)


def _set_stage(stage: str):
    _state["stage"] = stage
    logger.info(f"[Saham] {stage}")


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock, _db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots(
            id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, kind TEXT, taken_at TEXT, rows_json TEXT);
        CREATE INDEX IF NOT EXISTS idx_snap ON snapshots(trade_date, kind);
        CREATE TABLE IF NOT EXISTS runs(
            trade_date TEXT, job TEXT, status TEXT, attempts INTEGER DEFAULT 0,
            started_at TEXT, finished_at TEXT, detail TEXT, PRIMARY KEY(trade_date, job));
        CREATE TABLE IF NOT EXISTS picks(
            id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, run_tag TEXT, method TEXT, rank INTEGER,
            ticker TEXT, name TEXT, sector TEXT, score REAL, ref_price REAL, reason TEXT, data_json TEXT,
            result_close REAL, result_ret REAL, hit INTEGER, status TEXT DEFAULT 'open',
            tp REAL, cl REAL, outcome TEXT, note TEXT, day_high REAL, day_low REAL, first_touch TEXT, touch_at TEXT,
            UNIQUE(trade_date, run_tag, method, rank));
        CREATE INDEX IF NOT EXISTS idx_picks ON picks(trade_date, run_tag);
        CREATE TABLE IF NOT EXISTS stock_ai(
            trade_date TEXT, run_tag TEXT, ticker TEXT, sentiment TEXT, summary TEXT, catalyst TEXT, risk TEXT,
            PRIMARY KEY(trade_date, run_tag, ticker));
        CREATE TABLE IF NOT EXISTS news(
            id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, ticker TEXT, title TEXT, link TEXT,
            source TEXT, published TEXT, UNIQUE(trade_date, ticker, link));
        CREATE INDEX IF NOT EXISTS idx_news ON news(trade_date, ticker);
        CREATE TABLE IF NOT EXISTS analyses(
            trade_date TEXT, run_tag TEXT, kind TEXT, json TEXT, created_at TEXT,
            PRIMARY KEY(trade_date, run_tag, kind));
        CREATE TABLE IF NOT EXISTS days(
            trade_date TEXT PRIMARY KEY, status TEXT, ihsg_close REAL, ihsg_ret REAL, bench_ret REAL, detail_json TEXT);
        """)
        existing = {r[1] for r in c.execute("PRAGMA table_info(picks)").fetchall()}
        for col, typ in (("tp", "REAL"), ("cl", "REAL"), ("outcome", "TEXT"), ("note", "TEXT"), ("day_high", "REAL"), ("day_low", "REAL"), ("first_touch", "TEXT"), ("touch_at", "TEXT")):
            if col not in existing:
                c.execute(f"ALTER TABLE picks ADD COLUMN {col} {typ}")
        c.commit()


# ---------------------------------------------------------------------------
# Pengambilan data
# ---------------------------------------------------------------------------
def fetch_snapshot() -> list[dict]:
    """Data live semua saham IDX (843+ emiten) beserta indikator teknikal & fundamental."""
    cols = list(TV_COLUMNS.keys())
    payload = {
        "filter": [{"left": "type", "operation": "equal", "right": "stock"}],
        "options": {"lang": "en"}, "markets": ["indonesia"],
        "symbols": {"query": {"types": []}, "tickers": []},
        "columns": cols, "sort": {"sortBy": "volume", "sortOrder": "desc"}, "range": [0, 2500],
    }
    r = requests.post("https://scanner.tradingview.com/indonesia/scan", json=payload, headers=HTTP_HEADERS, timeout=40)
    r.raise_for_status()
    rows = []
    for item in r.json().get("data", []):
        d = dict(zip(cols, item.get("d", [])))
        row = {"ticker": item["s"].split(":")[-1]}
        for src, dst in TV_COLUMNS.items():
            v = d.get(src)
            row[dst] = v if isinstance(v, str) else _num(v)
        rows.append(row)
    if len(rows) < 300:
        raise RuntimeError(f"Snapshot terlalu sedikit ({len(rows)} saham)")
    return rows


def live_snapshot(max_age: int = 60) -> list[dict]:
    c = _cache.get("live")
    if c and time.time() - c[0] < max_age:
        return c[1]
    rows = fetch_snapshot()
    _cache["live"] = (time.time(), rows)
    return rows


def _yahoo_chart(symbol: str, rng: str = "5d", interval: str = "1d") -> dict | None:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range={rng}&interval={interval}"
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HTTP_HEADERS, timeout=12)
            if r.status_code == 429:
                time.sleep(2.5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()["chart"]["result"][0]
        except Exception as e:
            if attempt == 2:
                logger.warning(f"[Saham] Yahoo {symbol} gagal: {e}")
            else:
                time.sleep(1.5)
    return None


TV_GLOBAL = [
    ("IDX:COMPOSITE", "IHSG"), ("IDX:LQ45", "LQ45"), ("SP:SPX", "S&P 500"), ("NASDAQ:IXIC", "Nasdaq"),
    ("DJ:DJI", "Dow Jones"), ("TVC:NI225", "Nikkei 225"), ("TVC:HSI", "Hang Seng"),
    ("FX_IDC:USDIDR", "USD/IDR"), ("TVC:USOIL", "Minyak WTI"), ("TVC:GOLD", "Emas"),
]


def _fetch_global_tv() -> list[dict]:
    r = requests.post("https://scanner.tradingview.com/global/scan", headers=HTTP_HEADERS, timeout=20, json={
        "symbols": {"tickers": [s for s, _ in TV_GLOBAL]}, "columns": ["close", "change", "change_abs"]})
    r.raise_for_status()
    labels = dict(TV_GLOBAL)
    got = {}
    for it in r.json().get("data", []):
        last, chg, chg_abs = (it["d"] + [None, None, None])[:3]
        if last is None:
            continue
        got[it["s"]] = {"symbol": it["s"], "label": labels.get(it["s"], it["s"]), "last": round(last, 2),
                        "prev": round(last - (chg_abs or 0), 2), "chg_pct": round(chg or 0, 2), "date": _today()}
    return [got[s] for s, _ in TV_GLOBAL if s in got]


def fetch_global() -> list[dict]:
    try:
        out = _fetch_global_tv()
        if len(out) >= 4:
            return out
    except Exception as e:
        logger.warning(f"[Saham] Global via TradingView gagal, coba Yahoo: {e}")
    out = []

    def one(item):
        sym, label = item
        j = _yahoo_chart(sym, "5d", "1d")
        if not j:
            return None
        ts = j.get("timestamp") or []
        closes = j["indicators"]["quote"][0].get("close") or []
        pts = [(t, c) for t, c in zip(ts, closes) if c]
        if len(pts) < 2:
            return None
        last, prev = pts[-1][1], pts[-2][1]
        return {"symbol": sym, "label": label, "last": round(last, 2), "prev": round(prev, 2),
                "chg_pct": round((last / prev - 1) * 100, 2),
                "date": datetime.fromtimestamp(pts[-1][0], WIB).strftime("%Y-%m-%d")}

    with ThreadPoolExecutor(max_workers=2) as ex:
        for res in ex.map(one, GLOBAL_SYMBOLS):
            if res:
                out.append(res)
    return out


def live_global(max_age: int = 120) -> list[dict]:
    c = _cache.get("global")
    if c and time.time() - c[0] < max_age:
        return c[1]
    g = fetch_global()
    _cache["global"] = (time.time(), g)
    return g


def _short_name(desc: str) -> str:
    s = re.sub(r"^\s*PT\.?\s+", "", desc or "", flags=re.I)
    s = re.sub(r"\s+Tbk\.?\s*$", "", s, flags=re.I)
    s = re.sub(r"\s+(Class [A-Z]|Series [A-Z])$", "", s, flags=re.I)
    return s.strip()


def _clean_title(title: str, source: str) -> str:
    t = (title or "").strip()
    if source and t.endswith(" - " + source):
        t = t[: -len(source) - 3]
    return t


def _feed_entries(content: bytes, default_source: str, limit: int, max_age_days: int) -> list[dict]:
    f = feedparser.parse(content)
    out = []
    cutoff = time.time() - max_age_days * 86400
    for e in f.entries[:limit * 3]:
        pub_ts = None
        if getattr(e, "published_parsed", None):
            pub_ts = calendar.timegm(e.published_parsed)
        if pub_ts and pub_ts < cutoff - 86400:
            continue
        src = (e.get("source") or {}).get("title") or default_source
        out.append({
            "title": _clean_title(e.get("title", ""), src), "link": e.get("link", ""), "source": src,
            "published": datetime.fromtimestamp(pub_ts, WIB).strftime("%Y-%m-%d %H:%M") if pub_ts else "",
        })
        if len(out) >= limit:
            break
    return out


def google_news(query: str, limit: int = 6, days: int = 3) -> list[dict]:
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(f"{query} when:{days}d") + "&hl=id&gl=ID&ceid=ID:id"
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=12)
        r.raise_for_status()
        return _feed_entries(r.content, "Google News", limit, days)
    except Exception as e:
        logger.warning(f"[Saham] Google News '{query}' gagal: {e}")
        return []


def general_headlines() -> list[dict]:
    items: list[dict] = []

    def feed(kv):
        name, url = kv
        try:
            r = requests.get(url, headers=HTTP_HEADERS, timeout=12)
            r.raise_for_status()
            return _feed_entries(r.content, name, 40, 2)
        except Exception as e:
            logger.warning(f"[Saham] RSS {name} gagal: {e}")
            return []

    with ThreadPoolExecutor(max_workers=4) as ex:
        for res in ex.map(feed, NEWS_FEEDS.items()):
            items.extend(res)
        for res in ex.map(lambda q: google_news(q, 8, 2), GENERAL_QUERIES):
            items.extend(res)
    seen, out = set(), []
    for it in items:
        k = it["title"].lower()[:90]
        if k and k not in seen:
            seen.add(k)
            out.append(it)
    out.sort(key=lambda x: x.get("published", ""), reverse=True)
    return out


def news_for_tickers(tickers: list[dict], general: list[dict]) -> dict[str, list[dict]]:
    """tickers: [{ticker, desc}] -> {ticker: [berita...]} (Google News per saham + headline umum yang menyebut emiten)."""
    result: dict[str, list[dict]] = {}

    def one(t):
        code, desc = t["ticker"], _short_name(t.get("desc", ""))
        q = f'({code} OR "{desc}") saham' if len(desc) >= 4 else f"{code} saham"
        items = google_news(q, 10, 3) + general
        # hanya berita yang benar-benar menyebut kode atau nama emiten (hindari berita IHSG umum yang tidak relevan)
        pat_code = re.compile(r"\b" + re.escape(code) + r"\b")
        words = desc.split()
        name_keys = [desc.lower()] if len(desc) >= 5 else []
        if len(words) >= 2 and len(" ".join(words[:2])) >= 7:
            name_keys.append(" ".join(words[:2]).lower())
        items = [g for g in items if pat_code.search(g["title"]) or any(k in g["title"].lower() for k in name_keys)]
        seen, out = set(), []
        for it in items:
            k = it["title"].lower()[:90]
            if k not in seen:
                seen.add(k)
                out.append(it)
        out.sort(key=lambda x: x.get("published", ""), reverse=True)
        return code, out[:8]

    with ThreadPoolExecutor(max_workers=4) as ex:
        for code, items in ex.map(one, tickers):
            result[code] = items
    return result


# ---------------------------------------------------------------------------
# Metode analisis
# ---------------------------------------------------------------------------
def _frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    num_cols = [c for c in TV_COLUMNS.values() if c not in ("name", "desc", "sector", "industry")]
    for c in num_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce") if c in df else float("nan")
    df["value"] = df["close"] * df["vol"]
    return df


def _universe(df: pd.DataFrame) -> pd.DataFrame:
    return df[(df.close >= MIN_PRICE) & (df.value >= MIN_VALUE) & (df.mcap >= MIN_MCAP)].copy()


def _m_trend(u, prev):
    m = (u.close > u.sma20) & (u.sma20 > u.sma50) & (u.sma50 > u.sma200) & (u.adx >= 20) & u.rsi.between(45, 72)
    s = u.adx * 0.5 + u.p1m.fillna(0) * 0.8 + u.rec.fillna(0) * 10
    return m, s, lambda r: f"Tren naik SMA20>SMA50>SMA200, ADX {_fmt(r.adx, '{:.0f}')}, RSI {_fmt(r.rsi, '{:.0f}')}, 1 bulan {_fmt(r.p1m, '{:+.1f}')}%"


def _m_breakout(u, prev):
    m = (u.close >= 0.95 * u.h52) & (u.relvol >= 1.3) & (u.chg > 0)
    s = u.relvol.clip(upper=8) * 8 + u.chg.clip(upper=10) + (u.close / u.h52) * 20
    return m, s, lambda r: f"Dekat high 52-minggu ({_fmt(r.close / r.h52 * 100, '{:.0f}')}%), volume {_fmt(r.relvol, '{:.1f}')}x rata-rata, naik {_fmt(r.chg, '{:+.1f}')}%"


def _m_rsi(u, prev):
    m = (u.rsi <= 38) & (u.stk <= 30) & (u.close > u.l52 * 1.05)
    s = (40 - u.rsi) + (35 - u.stk) * 0.5 + (u.stk > u.stkd).astype(float) * 5
    return m, s, lambda r: f"Jenuh jual: RSI {_fmt(r.rsi, '{:.0f}')}, Stoch %K {_fmt(r.stk, '{:.0f}')} ({'berbalik naik' if _num(r.stk, 0) > _num(r.stkd, 0) else 'belum berbalik'}), potensi rebound"


def _m_macd(u, prev):
    hist = u.macd - u.macds
    if prev is not None and len(prev):
        pm = prev.set_index("ticker")
        prev_hist = u.ticker.map((pm.macd - pm.macds).to_dict())
    else:
        prev_hist = pd.Series([float("nan")] * len(u), index=u.index)
    fresh = (prev_hist <= 0) & (hist > 0)
    small = (hist > 0) & ((hist / u.close * 100) < 0.6)
    m = (hist > 0) & (fresh | small) & (u.close > u.ema20 * 0.98)
    s = fresh.astype(float) * 10 + (hist / u.close * 1000).clip(0, 10) + u.adx.fillna(0) / 10 + u.relvol.fillna(0).clip(upper=3)
    fresh_set = set(u.ticker[fresh.fillna(False)])
    return m, s, lambda r: (("MACD cross-up baru" if r.ticker in fresh_set else "MACD di atas signal") +
                            f", histogram {_fmt((r.macd - r.macds) / r.close * 100, '{:+.2f}')}% dari harga, ADX {_fmt(r.adx, '{:.0f}')}")


def _m_bollinger(u, prev):
    width = (u.bbu - u.bbl) / u.sma20
    bounce = (u.close <= u.bbl * 1.03) & (u.rsi <= 45)
    squeeze = (width < 0.10) & (u.close >= u.bbu * 0.99) & (u.chg > 0)
    m = bounce | squeeze
    s = bounce.astype(float) * ((u.bbl * 1.03 - u.close) / u.close * 100 + (45 - u.rsi) * 0.3 + 5) + \
        squeeze.astype(float) * (8 + u.relvol.fillna(0).clip(upper=4) * 2)
    return m, s, lambda r: ("Pantul dari Bollinger bawah" if r.close <= r.bbl * 1.03 else "Breakout setelah squeeze Bollinger") + f", RSI {_fmt(r.rsi, '{:.0f}')}"


def _m_volume(u, prev):
    m = (u.relvol >= 1.8) & (u.chg > 0) & (u.chg <= 8) & (u.close > u.sma20)
    s = u.relvol.clip(upper=10) * 5 + u.chg * 2 + u.value.apply(lambda v: math.log10(v) if v and v > 0 else 0)
    return m, s, lambda r: f"Lonjakan volume {_fmt(r.relvol, '{:.1f}')}x, harga naik {_fmt(r.chg, '{:+.1f}')}% di atas SMA20"


def _m_value(u, prev):
    m = (u.pe > 2) & (u.pe <= 15) & (u.roe >= 10) & ((u.der <= 1.5) | u.der.isna()) & (u.eps > 0)
    s = u.roe.clip(upper=40) * 0.6 + u.dy.fillna(0).clip(upper=12) * 2 + (15 - u.pe) + (1.5 - u.der.fillna(1)).clip(lower=0) * 3
    return m, s, lambda r: f"Value: PER {_fmt(r.pe)}, ROE {_fmt(r.roe)}%, DER {_fmt(r.der, '{:.2f}')}, dividen {_fmt(r.dy)}%"


def _m_consensus(u, prev):
    m = (u.rec >= 0.3) & (u.rsi < 75)
    s = u.rec * 100 + u.adx.fillna(0) * 0.2 + u.relvol.fillna(0).clip(upper=3)
    return m, s, lambda r: f"Konsensus indikator {_fmt(r.rec * 100, '{:.0f}')}/100 (rata-rata MA & osilator), RSI {_fmt(r.rsi, '{:.0f}')}"


METHODS = [
    {"key": "trend", "label": "Trend Following", "desc": "SMA20>SMA50>SMA200 + ADX kuat: mengikuti tren naik yang sudah terbentuk.", "fn": _m_trend},
    {"key": "breakout", "label": "Breakout 52 Minggu", "desc": "Harga dekat high 52-minggu dengan volume di atas rata-rata.", "fn": _m_breakout},
    {"key": "rsi", "label": "RSI Reversal", "desc": "RSI & Stochastic jenuh jual lalu berbalik: mencari rebound.", "fn": _m_rsi},
    {"key": "macd", "label": "MACD Crossover", "desc": "MACD memotong signal ke atas (cross-up baru) dengan momentum positif.", "fn": _m_macd},
    {"key": "bollinger", "label": "Bollinger Bands", "desc": "Pantulan dari band bawah atau breakout setelah squeeze.", "fn": _m_bollinger},
    {"key": "volume", "label": "Volume Spike", "desc": "Lonjakan volume relatif disertai kenaikan harga terkendali.", "fn": _m_volume},
    {"key": "value", "label": "Value & Fundamental", "desc": "PER rendah, ROE tinggi, utang rendah, dividen: kualitas fundamental.", "fn": _m_value},
    {"key": "consensus", "label": "Konsensus Teknikal", "desc": "Skor gabungan puluhan indikator (MA + osilator) TradingView.", "fn": _m_consensus},
    {"key": "ai_news", "label": "AI + Berita", "desc": "AI memilih dari kandidat semua metode dengan mempertimbangkan berita, makro & sentimen.", "fn": None},
]
METHOD_LABEL = {m["key"]: m["label"] for m in METHODS}


# (pengali ATR untuk TP, pengali ATR untuk CL) per metode; horizon 1 hari bursa
METHOD_RISK = {
    "trend": (1.0, 0.8), "breakout": (1.2, 0.8), "rsi": (0.8, 0.8), "macd": (1.0, 0.8),
    "bollinger": (0.8, 0.8), "volume": (1.2, 0.9), "value": (1.0, 1.0), "consensus": (1.0, 0.8), "ai_news": (1.0, 0.8),
}


def _tick(p: float) -> int:
    """Fraksi harga BEI."""
    return 1 if p < 200 else 2 if p < 500 else 5 if p < 2000 else 10 if p < 5000 else 25


def _round_tick(p: float) -> float:
    t = _tick(p)
    return round(round(p / t) * t, 2)


def trade_levels(method: str, ref: float, atr) -> tuple[float, float]:
    """Target profit & cut loss 1 hari dari ATR(14), dibulatkan ke fraksi harga, dibatasi ARA/ARB."""
    k_tp, k_cl = METHOD_RISK.get(method, (1.0, 0.8))
    a = _num(atr)
    if not a or a <= 0:
        a = ref * 0.03
    tp = max(ref + k_tp * a, ref * 1.015)          # minimal +1,5%
    cl = min(ref - k_cl * a, ref * 0.99)           # minimal -1%
    ara = 0.35 if ref < 200 else (0.25 if ref < 5000 else 0.20)
    tp = min(tp, ref * (1 + ara))
    cl = max(cl, ref * 0.85)                        # batas ARB 15%
    tp, cl = _round_tick(tp), _round_tick(cl)
    t = _tick(ref)
    if tp <= ref:
        tp = ref + t
    if cl >= ref:
        cl = ref - t
    return tp, max(cl, 1)


def _pick_data(row) -> dict:
    keys = ["close", "chg", "vol", "value", "mcap", "pe", "pbv", "rsi", "macd", "macds", "adx", "relvol", "sma20", "sma50", "sma200",
            "h52", "l52", "rec", "roe", "dy", "der", "p1m", "p3m", "atr"]
    return {k: (None if _num(row.get(k)) is None else round(float(row.get(k)), 4)) for k in keys}


def run_methods(rows: list[dict], prev_rows: list[dict] | None) -> dict[str, list[dict]]:
    df = _frame(rows)
    u = _universe(df)
    prev = _frame(prev_rows) if prev_rows else None
    out: dict[str, list[dict]] = {}
    for m in METHODS:
        if not m["fn"]:
            continue
        try:
            mask, score, reason_fn = m["fn"](u, prev)
            sel = u[mask.fillna(False)].copy()
            sel["score"] = score[sel.index]
            sel = sel.sort_values("score", ascending=False).head(10)
            picks = []
            for rank, (_, r) in enumerate(sel.iterrows(), 1):
                try:
                    reason = reason_fn(r)
                except Exception:
                    reason = ""
                tp, cl = trade_levels(m["key"], float(r.close), r.get("atr"))
                picks.append({"rank": rank, "ticker": r.ticker, "name": _short_name(r.get("desc") or r.get("name") or ""),
                              "sector": r.get("sector") or "", "score": round(float(r.score), 3),
                              "ref_price": float(r.close), "reason": reason, "data": _pick_data(r), "tp": tp, "cl": cl})
            out[m["key"]] = picks
        except Exception as e:
            logger.warning(f"[Saham] Metode {m['key']} gagal: {e}")
            out[m["key"]] = []
    return out


# ---------------------------------------------------------------------------
# AI
# ---------------------------------------------------------------------------
def _env(key: str, default: str = "") -> str:
    try:
        import teams_service as ts
        return ts._get_env_key(key, default)
    except Exception:
        return os.environ.get(key, default)


_ai_last_ok: list = []   # model yang terakhir berhasil, dicoba lebih dulu


def _ai_models() -> list[str]:
    chain: list[str] = list(_ai_last_ok)
    pref = _env("STOCK_AI_MODEL", "")
    if pref and pref not in chain:
        chain.append(pref)
    chain += [m for m in AI_MODEL_CHAIN if m not in chain]
    return chain


def _ai_call(model: str, messages: list, timeout: int) -> str:
    try:
        import teams_service as ts
        url, key = ts.AI_ROUTER_URL, ts.AI_ROUTER_KEY
    except Exception:
        url = _env("NINEROUTER_URL", "http://127.0.0.1:20128/v1/chat/completions")
        key = _env("NINEROUTER_KEY", "")
    last = None
    names = [model] if model.startswith(("cc/", "gh/")) else list(dict.fromkeys([model, model.split("/")[-1]]))
    for name in names:
        try:
            req = urllib.request.Request(
                url, data=json.dumps({"model": name, "messages": messages, "temperature": 0.2, "stream": False, "max_tokens": 8000}).encode(),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
            body, _ = json.JSONDecoder().raw_decode(raw.lstrip())   # sebagian model membalas text/event-stream dengan objek berurutan
            content = (body["choices"][0]["message"]["content"] or "").strip()
            if content:
                return content
        except Exception as e:
            last = e
    raise RuntimeError(str(last))


def _parse_json(text: str):
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        a = t.find("{")
        if a < 0:
            raise
        obj, _ = json.JSONDecoder().raw_decode(t[a:])   # abaikan teks tambahan setelah objek JSON
        return obj


def _ai(messages: list, timeout: int = 180) -> str:
    last = None
    for model in _ai_models():
        try:
            return _ai_call(model, messages, timeout)
        except Exception as e:
            last = e
            logger.warning(f"[Saham] Model {model} gagal: {e}")
    raise RuntimeError(f"Semua model AI gagal: {last}")


def _ai_json(system: str, user: str):
    """Panggil AI dan parse JSON; bila model mengembalikan JSON tak valid, lanjut ke model berikutnya."""
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    last = None
    for model in _ai_models():
        try:
            parsed = _parse_json(_ai_call(model, msgs, 180))
            if model in _ai_last_ok:
                _ai_last_ok.remove(model)
            _ai_last_ok.insert(0, model)
            del _ai_last_ok[1:]
            return parsed
        except Exception as e:
            last = e
            logger.warning(f"[Saham] Model {model} gagal/JSON tak valid: {str(e)[:120]}")
    raise RuntimeError(f"Semua model AI gagal: {last}")


def _stock_line(p: dict, methods: list[str], news: list[dict]) -> str:
    d = p["data"]
    head = (f"{p['ticker']} | {p['name']} | {p['sector']} | harga {_fmt(d.get('close'), '{:.0f}')} ({_fmt(d.get('chg'), '{:+.1f}')}%) | "
            f"RSI {_fmt(d.get('rsi'), '{:.0f}')} ADX {_fmt(d.get('adx'), '{:.0f}')} PER {_fmt(d.get('pe'))} ROE {_fmt(d.get('roe'))}% "
            f"1B {_fmt(d.get('p1m'), '{:+.1f}')}% | metode: {', '.join(METHOD_LABEL[m] for m in methods)}")
    if news:
        head += "\n   berita: " + " || ".join(f"{n['title']} ({n['source']}, {n['published'][:10]})" for n in news[:5])
    else:
        head += "\n   berita: (tidak ada berita spesifik)"
    return head


def ai_analyze_stocks(cands: list[dict], methods_by_ticker: dict, news_map: dict) -> dict[str, dict]:
    system = ("Anda analis saham Indonesia (BEI). Nilai dampak berita & data teknikal terhadap pergerakan saham 1 hari bursa ke depan. "
              "Jawab HANYA JSON valid: {\"stocks\":[{\"ticker\":\"XXXX\",\"sentiment\":\"positif|netral|negatif\","
              "\"summary\":\"1-2 kalimat analisis ringkas dalam Bahasa Indonesia\",\"catalyst\":\"katalis utama atau -\",\"risk\":\"risiko utama atau -\"}]}. "
              "Jika tidak ada berita, nilai dari data teknikal saja dan jangan mengarang berita.")
    out: dict[str, dict] = {}
    for i in range(0, len(cands), 20):
        batch = cands[i:i + 20]
        body = "\n".join(_stock_line(p, methods_by_ticker[p["ticker"]], news_map.get(p["ticker"], [])) for p in batch)
        try:
            res = _ai_json(system, "Analisis saham-saham berikut:\n" + body)
            for s in res.get("stocks", []):
                t = str(s.get("ticker", "")).upper().strip()
                if t:
                    out[t] = {"sentiment": str(s.get("sentiment", "netral")).lower(), "summary": s.get("summary", ""),
                              "catalyst": s.get("catalyst", ""), "risk": s.get("risk", "")}
        except Exception as e:
            logger.warning(f"[Saham] AI analisis batch {i // 20 + 1} gagal: {e}")
    return out


def ai_overview(glob: list[dict], general: list[dict], cands: list[dict], methods_by_ticker: dict, ai_stock: dict) -> dict:
    system = ("Anda strategis pasar modal Indonesia. Susun briefing pra-pasar BEI untuk hari ini dan pilih 10 saham terbaik untuk "
              "potensi kenaikan HARI INI dari daftar kandidat (WAJIB dari kandidat). Jawab HANYA JSON valid: "
              "{\"market_overview\":\"3-5 kalimat\",\"ihsg_outlook\":{\"bias\":\"naik|turun|sideways\",\"reason\":\"...\",\"support\":\"angka\",\"resistance\":\"angka\"},"
              "\"key_news\":[\"poin berita/ekonomi terpenting\"],\"watch_sectors\":[\"sektor\"],\"risks\":[\"risiko\"],"
              "\"ai_picks\":[{\"ticker\":\"XXXX\",\"confidence\":1-100,\"reason\":\"alasan singkat\"}]}. Bahasa Indonesia. Jangan mengarang berita.")
    g = "\n".join(f"{x['label']}: {x['last']} ({x['chg_pct']:+.2f}%)" for x in glob) or "(data global tidak tersedia)"
    h = "\n".join(f"- {x['title']} ({x['source']}, {x['published'][:10]})" for x in general[:25]) or "(tidak ada)"
    c = []
    for p in cands:
        a = ai_stock.get(p["ticker"], {})
        c.append(f"{p['ticker']} ({p['name']}, {p['sector']}) metode: {', '.join(METHOD_LABEL[m] for m in methods_by_ticker[p['ticker']])}"
                 f" | sentimen: {a.get('sentiment', '-')} | {a.get('summary', '')[:140]}")
    user = f"Tanggal: {_today()}\n\nINDEKS GLOBAL (penutupan terakhir):\n{g}\n\nHEADLINE EKONOMI/PASAR:\n{h}\n\nKANDIDAT:\n" + "\n".join(c)
    return _ai_json(system, user)


def ai_eval_comment(trade_date: str, ihsg_ret, bench_ret, day_stats: list[dict], overview: dict | None) -> str:
    system = ("Anda analis kuantitatif. Beri evaluasi singkat (4-6 kalimat, Bahasa Indonesia) mengapa metode tertentu "
              "akurat/meleset hari ini dibanding IHSG, dan apa pelajaran untuk besok. Jangan mengarang data.")
    lines = "\n".join(f"{METHOD_LABEL.get(s['method'], s['method'])}: naik {s['hit_rate']:.0f}%, TP {s['tp_rate']:.0f}%, CL {s['cl_rate']:.0f}%, rata-rata {s['avg_ret']:+.2f}% ({s['n']} saham)" for s in day_stats)
    pre = (overview or {}).get("market_overview", "")
    user = (f"Tanggal {trade_date}. IHSG {ihsg_ret:+.2f}%, rata-rata saham likuid {bench_ret:+.2f}%.\nHasil metode:\n{lines}\n\nBriefing pagi: {pre}")
    return _ai([{"role": "system", "content": system}, {"role": "user", "content": user}], timeout=120)


# ---------------------------------------------------------------------------
# Penyimpanan
# ---------------------------------------------------------------------------
def _save_snapshot(trade_date: str, kind: str, rows: list[dict]):
    slim = []
    for r in rows:
        slim.append({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})
    with _db_lock, _db() as c:
        c.execute("DELETE FROM snapshots WHERE trade_date=? AND kind=?", (trade_date, kind))
        c.execute("INSERT INTO snapshots(trade_date, kind, taken_at, rows_json) VALUES(?,?,?,?)",
                  (trade_date, kind, _now().strftime("%Y-%m-%d %H:%M:%S"), json.dumps(slim)))
        c.execute("DELETE FROM snapshots WHERE trade_date < ?", ((_now() - timedelta(days=60)).strftime("%Y-%m-%d"),))
        c.commit()


def _load_snapshot(trade_date: str | None = None, kind: str | None = None, before: str | None = None) -> list[dict] | None:
    q, a = "SELECT rows_json FROM snapshots WHERE 1=1", []
    if trade_date:
        q += " AND trade_date=?"; a.append(trade_date)
    if kind:
        q += " AND kind=?"; a.append(kind)
    if before:
        q += " AND trade_date<?"; a.append(before)
    q += " ORDER BY trade_date DESC, id DESC LIMIT 1"
    with _db_lock, _db() as c:
        row = c.execute(q, a).fetchone()
    return json.loads(row["rows_json"]) if row else None


def _save_analysis(trade_date: str, tag: str, kind: str, payload: dict):
    with _db_lock, _db() as c:
        c.execute("INSERT OR REPLACE INTO analyses(trade_date, run_tag, kind, json, created_at) VALUES(?,?,?,?,?)",
                  (trade_date, tag, kind, json.dumps(payload, ensure_ascii=False), _now().strftime("%Y-%m-%d %H:%M:%S")))
        c.commit()


def _get_analysis(trade_date: str, tag: str, kind: str) -> dict | None:
    with _db_lock, _db() as c:
        r = c.execute("SELECT json, created_at FROM analyses WHERE trade_date=? AND run_tag=? AND kind=?", (trade_date, tag, kind)).fetchone()
    if not r:
        return None
    d = json.loads(r["json"])
    d["_created_at"] = r["created_at"]
    return d


# ---------------------------------------------------------------------------
# Job: pra-pasar
# ---------------------------------------------------------------------------
def _in_premarket_window(now: datetime) -> bool:
    t = (now.hour, now.minute)
    return now.weekday() < 5 and (8, 0) <= t < MARKET_OPEN


def run_premarket(force_tag: str | None = None) -> dict:
    """Jalankan analisis lengkap. run_tag 'pre' (dihitung di statistik) bila dijalankan sebelum pasar buka, selain itu 'test'."""
    now = _now()
    trade_date = now.strftime("%Y-%m-%d")
    tag = force_tag or ("pre" if _in_premarket_window(now) else "test")

    _set_stage("Mengambil data live semua saham IDX...")
    rows = fetch_snapshot()
    _save_snapshot(trade_date, "pre" if tag == "pre" else "test", rows)
    prev_rows = _load_snapshot(kind="close", before=trade_date)

    _set_stage("Menjalankan 8 metode analisis...")
    picks = run_methods(rows, prev_rows)

    _set_stage("Mengambil konteks global & headline ekonomi...")
    glob = fetch_global()
    general = general_headlines()

    methods_by_ticker: dict[str, list[str]] = {}
    cand_map: dict[str, dict] = {}
    for mk, lst in picks.items():
        for p in lst:
            methods_by_ticker.setdefault(p["ticker"], []).append(mk)
            cand_map.setdefault(p["ticker"], p)
    cands = list(cand_map.values())
    desc_by_ticker = {r["ticker"]: r.get("desc") or r.get("name") or "" for r in rows}

    _set_stage(f"Mengumpulkan berita untuk {len(cands)} saham kandidat...")
    news_map = news_for_tickers([{"ticker": p["ticker"], "desc": desc_by_ticker.get(p["ticker"], "")} for p in cands], general)

    ai_stock: dict[str, dict] = {}
    overview: dict = {}
    ai_error = ""
    try:
        _set_stage("AI menganalisis berita & sentimen per saham...")
        ai_stock = ai_analyze_stocks(cands, methods_by_ticker, news_map)
        _set_stage("AI menyusun briefing pasar & rekomendasi gabungan...")
        overview = ai_overview(glob, general, cands, methods_by_ticker, ai_stock)
    except Exception as e:
        ai_error = str(e)
        logger.warning(f"[Saham] Analisis AI gagal: {e}")

    ai_picks = []
    for rank, ap in enumerate((overview.get("ai_picks") or [])[:10], 1):
        t = str(ap.get("ticker", "")).upper().strip()
        base = cand_map.get(t)
        if not base or any(x["ticker"] == t for x in ai_picks):
            continue
        ai_picks.append({"rank": len(ai_picks) + 1, "ticker": t, "name": base["name"], "sector": base["sector"],
                         "score": float(_num(ap.get("confidence"), 50)), "ref_price": base["ref_price"],
                         "reason": str(ap.get("reason", "")), "data": base["data"],
                         "tp": trade_levels("ai_news", base["ref_price"], base["data"].get("atr"))[0],
                         "cl": trade_levels("ai_news", base["ref_price"], base["data"].get("atr"))[1]})
    picks["ai_news"] = ai_picks

    _set_stage("Menyimpan hasil...")
    with _db_lock, _db() as c:
        c.execute("DELETE FROM picks WHERE trade_date=? AND run_tag=?", (trade_date, tag))
        c.execute("DELETE FROM stock_ai WHERE trade_date=? AND run_tag=?", (trade_date, tag))
        for mk, lst in picks.items():
            for p in lst:
                c.execute("""INSERT INTO picks(trade_date, run_tag, method, rank, ticker, name, sector, score, ref_price, reason, data_json, tp, cl)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (trade_date, tag, mk, p["rank"], p["ticker"], p["name"], p["sector"], p["score"], p["ref_price"],
                           p["reason"], json.dumps(p["data"]), p.get("tp"), p.get("cl")))
        for t, a in ai_stock.items():
            c.execute("INSERT OR REPLACE INTO stock_ai VALUES(?,?,?,?,?,?,?)",
                      (trade_date, tag, t, a["sentiment"], a["summary"], a["catalyst"], a["risk"]))
        for t, items in news_map.items():
            for n in items:
                c.execute("INSERT OR IGNORE INTO news(trade_date, ticker, title, link, source, published) VALUES(?,?,?,?,?,?)",
                          (trade_date, t, n["title"], n["link"], n["source"], n["published"]))
        for n in general[:40]:
            c.execute("INSERT OR IGNORE INTO news(trade_date, ticker, title, link, source, published) VALUES(?,?,?,?,?,?)",
                      (trade_date, "", n["title"], n["link"], n["source"], n["published"]))
        c.commit()
    overview["global"] = glob
    overview["ai_error"] = ai_error
    overview["universe"] = int(len(_universe(_frame(rows))))
    overview["total_stocks"] = len(rows)
    _save_analysis(trade_date, tag, "overview", overview)
    return {"trade_date": trade_date, "tag": tag, "stocks": len(rows), "candidates": len(cands), "ai_error": ai_error}


# ---------------------------------------------------------------------------
# Job: evaluasi penutupan
# ---------------------------------------------------------------------------
def run_close_eval(trade_date: str | None = None) -> dict:
    trade_date = trade_date or _today()
    _set_stage("Mengambil harga penutupan semua saham...")
    rows = fetch_snapshot()
    pre_rows = _load_snapshot(trade_date=trade_date, kind="pre") or _load_snapshot(kind="close", before=trade_date) or []

    # Bursa libur bila total volume snapshot sekarang identik dengan snapshot pra-pasar (sesi sebelumnya)
    vol_now = sum((r.get("vol") or 0) for r in rows)
    vol_ref = sum((r.get("vol") or 0) for r in pre_rows)
    if pre_rows and abs(vol_now - vol_ref) < 1:
        with _db_lock, _db() as c:
            c.execute("UPDATE picks SET status='void' WHERE trade_date=? AND status='open'", (trade_date,))
            c.execute("INSERT OR REPLACE INTO days VALUES(?,?,?,?,?,?)", (trade_date, "no_session", None, None, None, "{}"))
            c.commit()
        return {"trade_date": trade_date, "status": "no_session"}
    _save_snapshot(trade_date, "close", rows)
    now_by = {r["ticker"]: r for r in rows}
    ihsg = next((g for g in fetch_global() if g["symbol"] == "IDX:COMPOSITE"), None)
    ihsg_close = ihsg["last"] if ihsg else 0.0
    ihsg_ret = ihsg["chg_pct"] if ihsg else 0.0

    rets = []  # benchmark: rata-rata return semua saham likuid (acuan = close sebelum buka)
    for r in pre_rows:
        n = now_by.get(r["ticker"])
        ref = r.get("close") or 0
        if not n or ref < MIN_PRICE or (r.get("mcap") or 0) < MIN_MCAP:
            continue
        if (n.get("close") or 0) * (n.get("vol") or 0) >= MIN_VALUE:
            rets.append((n["close"] / ref - 1) * 100)
    bench = sum(rets) / len(rets) if rets else 0.0

    _set_stage("Menghitung hasil tiap rekomendasi...")
    with _db_lock, _db() as c:
        picks = c.execute("SELECT id, ticker, ref_price, tp, cl, first_touch FROM picks WHERE trade_date=? AND status IN ('open','live')", (trade_date,)).fetchall()
        for p in picks:
            n = now_by.get(p["ticker"])
            if not n or not p["ref_price"] or (n.get("vol") or 0) <= 0:
                c.execute("UPDATE picks SET status='no_trade', outcome='NO_TRADE', note='Tidak ada transaksi hari ini' WHERE id=?", (p["id"],))
                continue
            ret = (n["close"] / p["ref_price"] - 1) * 100
            hi, lo, tp, cl = n.get("high"), n.get("low"), p["tp"], p["cl"]
            outcome, note = "NETRAL", f"TP/CL tidak tersentuh, ditutup {ret:+.2f}%"
            if tp and cl and hi and lo:
                hit_tp, hit_cl = hi >= tp, lo <= cl
                if hit_tp and hit_cl:
                    if p["first_touch"] in ("TP", "CL"):
                        outcome = p["first_touch"]
                        note = f"TP {tp:g} dan CL {cl:g} sama-sama tersentuh; yang lebih dulu terdeteksi: {outcome} (high {hi:g}, low {lo:g})"
                    else:
                        outcome, note = "CL", f"TP {tp:g} dan CL {cl:g} sama-sama tersentuh (high {hi:g}, low {lo:g}); urutan tidak diketahui, dihitung CL"
                elif hit_tp:
                    outcome, note = "TP", f"TP {tp:g} tercapai (high {hi:g}), ditutup {ret:+.2f}%"
                elif hit_cl:
                    outcome, note = "CL", f"CL {cl:g} tersentuh (low {lo:g}), ditutup {ret:+.2f}%"
                else:
                    note = f"TP/CL tidak tersentuh (high {hi:g}, low {lo:g}), ditutup {ret:+.2f}%"
            c.execute("UPDATE picks SET result_close=?, result_ret=?, hit=?, status='done', outcome=?, note=?, day_high=?, day_low=? WHERE id=?",
                      (n["close"], round(ret, 3), 1 if ret > 0 else 0, outcome, note, hi, lo, p["id"]))
        c.execute("INSERT OR REPLACE INTO days VALUES(?,?,?,?,?,?)",
                  (trade_date, "done", round(ihsg_close, 2), round(ihsg_ret, 3), round(bench, 3), json.dumps({"universe": len(rets)})))
        c.commit()

    day = method_stats(date=trade_date)["methods"]
    comment = ""
    try:
        _set_stage("AI menyusun evaluasi hari ini...")
        comment = ai_eval_comment(trade_date, ihsg_ret, bench, day, _get_analysis(trade_date, "pre", "overview"))
    except Exception as e:
        logger.warning(f"[Saham] Evaluasi AI gagal: {e}")
    _save_analysis(trade_date, "pre", "eval", {"comment": comment, "ihsg_ret": ihsg_ret, "bench_ret": bench, "methods": day})
    return {"trade_date": trade_date, "status": "done", "ihsg_ret": round(ihsg_ret, 2), "bench_ret": round(bench, 2)}


# ---------------------------------------------------------------------------
# Pelacak live: tandai TP/CL begitu tersentuh selama jam bursa
# ---------------------------------------------------------------------------
_live_lock = threading.Lock()
_live_state = {"last": 0.0, "touched": 0, "error": ""}


def live_tick(trade_date: str | None = None) -> dict:
    """Perbarui high/low/harga terakhir semua rekomendasi hari ini dan tandai TP/CL yang baru tersentuh."""
    if not _live_lock.acquire(blocking=False):
        return {"skipped": True}
    try:
        trade_date = trade_date or _today()
        with _db_lock, _db() as c:
            picks = c.execute("SELECT id, ticker, ref_price, tp, cl, first_touch FROM picks WHERE trade_date=? AND status IN ('open','live')",
                              (trade_date,)).fetchall()
        if not picks:
            return {"picks": 0}
        rows = live_snapshot(50)   # dibagi dengan pengumpul tick per menit (satu request per menit)
        now_by = {r["ticker"]: r for r in rows}
        stamp = _now().strftime("%H:%M")
        new_touch = 0
        with _db_lock, _db() as c:
            for p in picks:
                n = now_by.get(p["ticker"])
                if not n or not p["ref_price"] or (n.get("vol") or 0) <= 0:
                    continue
                hi, lo, last = n.get("high"), n.get("low"), n.get("close")
                if not hi or not lo or not last:
                    continue
                ret = (last / p["ref_price"] - 1) * 100
                hit_tp, hit_cl = bool(p["tp"]) and hi >= p["tp"], bool(p["cl"]) and lo <= p["cl"]
                first = p["first_touch"]
                if not first and (hit_tp or hit_cl):
                    first = "BOTH" if (hit_tp and hit_cl) else ("TP" if hit_tp else "CL")
                    new_touch += 1
                    c.execute("UPDATE picks SET first_touch=?, touch_at=? WHERE id=?", (first, stamp, p["id"]))
                if first == "TP":
                    outcome, note = "TP", f"LIVE: TP {p['tp']:g} tercapai (terdeteksi {stamp}, high {hi:g}), harga kini {last:g} ({ret:+.2f}%)"
                elif first in ("CL", "BOTH"):
                    outcome, note = "CL", f"LIVE: CL {p['cl']:g} tersentuh (terdeteksi {stamp}, low {lo:g}), harga kini {last:g} ({ret:+.2f}%)"
                else:
                    outcome, note = "BERJALAN", f"LIVE: belum menyentuh TP/CL, harga kini {last:g} ({ret:+.2f}%)"
                c.execute("UPDATE picks SET status='live', outcome=?, note=?, result_close=?, result_ret=?, day_high=?, day_low=? WHERE id=?",
                          (outcome, note, last, round(ret, 3), hi, lo, p["id"]))
            c.commit()
        _live_state.update(last=time.time(), touched=_live_state["touched"] + new_touch, error="")
        return {"picks": len(picks), "new_touch": new_touch}
    except Exception as e:
        _live_state["error"] = str(e)
        logger.warning(f"[Saham] Live tick gagal: {e}")
        return {"error": str(e)}
    finally:
        _live_lock.release()


# ---------------------------------------------------------------------------
# Statistik akurasi
# ---------------------------------------------------------------------------
def method_stats(date: str | None = None, window: int | None = None) -> dict:
    """Akurasi per metode dari rekomendasi 'pre' yang sudah dievaluasi."""
    q = ("SELECT p.method, p.trade_date, p.ticker, p.result_ret, p.hit, p.outcome, d.bench_ret FROM picks p "
         "LEFT JOIN days d ON d.trade_date=p.trade_date WHERE p.run_tag='pre' AND p.status='done'")
    a: list = []
    if date:
        q += " AND p.trade_date=?"; a.append(date)
    elif window:
        q += " AND p.trade_date>=?"; a.append((_now() - timedelta(days=window)).strftime("%Y-%m-%d"))
    with _db_lock, _db() as c:
        rows = c.execute(q, a).fetchall()
    agg: dict[str, dict] = {}
    for r in rows:
        s = agg.setdefault(r["method"], {"n": 0, "hits": 0, "tp": 0, "cl": 0, "ret": 0.0, "alpha": 0.0, "beat": 0, "dates": set(), "best": None, "worst": None})
        s["n"] += 1
        s["hits"] += r["hit"] or 0
        s["tp"] += 1 if r["outcome"] == "TP" else 0
        s["cl"] += 1 if r["outcome"] == "CL" else 0
        s["ret"] += r["result_ret"] or 0
        bench = r["bench_ret"] or 0
        s["alpha"] += (r["result_ret"] or 0) - bench
        s["beat"] += 1 if (r["result_ret"] or 0) > bench else 0
        s["dates"].add(r["trade_date"])
        if s["best"] is None or r["result_ret"] > s["best"][1]:
            s["best"] = (r["ticker"], r["result_ret"])
        if s["worst"] is None or r["result_ret"] < s["worst"][1]:
            s["worst"] = (r["ticker"], r["result_ret"])
    methods = []
    for m in METHODS:
        s = agg.get(m["key"])
        if not s or not s["n"]:
            methods.append({"method": m["key"], "label": m["label"], "n": 0, "days": 0, "hit_rate": 0.0, "avg_ret": 0.0,
                            "avg_alpha": 0.0, "beat_rate": 0.0, "tp_rate": 0.0, "cl_rate": 0.0, "best": None, "worst": None})
            continue
        methods.append({"method": m["key"], "label": m["label"], "n": s["n"], "days": len(s["dates"]),
                        "hit_rate": s["hits"] / s["n"] * 100, "avg_ret": s["ret"] / s["n"], "avg_alpha": s["alpha"] / s["n"],
                        "beat_rate": s["beat"] / s["n"] * 100, "tp_rate": s["tp"] / s["n"] * 100, "cl_rate": s["cl"] / s["n"] * 100,
                        "best": s["best"], "worst": s["worst"]})
    methods.sort(key=lambda x: (x["n"] > 0, x["tp_rate"] - x["cl_rate"], x["hit_rate"], x["avg_ret"]), reverse=True)
    best = next((m for m in methods if m["n"] >= (3 if date else 10)), None)
    return {"methods": methods, "best": best["method"] if best else None}


def daily_history(limit: int = 30) -> list[dict]:
    with _db_lock, _db() as c:
        days = c.execute("SELECT * FROM days ORDER BY trade_date DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for d in days:
        item = {"date": d["trade_date"], "status": d["status"], "ihsg_ret": d["ihsg_ret"], "bench_ret": d["bench_ret"], "methods": {}}
        if d["status"] == "done":
            for m in method_stats(date=d["trade_date"])["methods"]:
                if m["n"]:
                    item["methods"][m["method"]] = {"hit_rate": round(m["hit_rate"], 1), "avg_ret": round(m["avg_ret"], 2)}
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Scheduler & eksekusi
# ---------------------------------------------------------------------------
def _job_row(trade_date: str, job: str):
    with _db_lock, _db() as c:
        return c.execute("SELECT * FROM runs WHERE trade_date=? AND job=?", (trade_date, job)).fetchone()


def _record_run(trade_date: str, job: str, status: str, detail: str = "", attempt_inc: bool = False):
    with _db_lock, _db() as c:
        row = c.execute("SELECT attempts FROM runs WHERE trade_date=? AND job=?", (trade_date, job)).fetchone()
        attempts = (row["attempts"] if row else 0) + (1 if attempt_inc else 0)
        c.execute("INSERT OR REPLACE INTO runs(trade_date, job, status, attempts, started_at, finished_at, detail) VALUES(?,?,?,?,?,?,?)",
                  (trade_date, job, status, attempts, _now().strftime("%Y-%m-%d %H:%M:%S"),
                   _now().strftime("%Y-%m-%d %H:%M:%S") if status != "running" else "", detail[:500]))
        c.commit()


def run_job(job: str, scheduled: bool = False) -> dict:
    """Jalankan job ('pre' | 'close') dengan kunci tunggal. Mengembalikan ringkasan atau {'error':...}."""
    if not _run_lock.acquire(blocking=False):
        return {"error": "Job lain sedang berjalan"}
    trade_date = _today()
    try:
        _state.update(running=True, job=job, started=time.time(), error="", stage="Mulai...")
        if scheduled:
            _record_run(trade_date, job, "running", attempt_inc=True)
        res = run_premarket() if job == "pre" else run_close_eval()
        if scheduled:
            _record_run(trade_date, job, "done", json.dumps(res))
        return res
    except Exception as e:
        logger.exception(f"[Saham] Job {job} gagal")
        _state["error"] = str(e)
        if scheduled:
            _record_run(trade_date, job, "failed", str(e))
        return {"error": str(e)}
    finally:
        _state.update(running=False, last_finished=time.time(), stage="Selesai" if not _state["error"] else "Gagal")
        _run_lock.release()


def start_job_async(job: str) -> dict:
    if _state["running"]:
        return {"success": False, "error": "Job lain sedang berjalan"}
    threading.Thread(target=run_job, args=(job, False), name=f"saham-{job}", daemon=True).start()
    return {"success": True, "message": f"Job '{job}' dimulai"}


def _due(trade_date: str, job: str) -> bool:
    row = _job_row(trade_date, job)
    if not row:
        return True
    if row["status"] == "done":
        return False
    if row["status"] == "failed" and row["attempts"] < MAX_ATTEMPTS:
        try:
            last = datetime.strptime(row["finished_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=WIB)
            return (_now() - last).total_seconds() >= RETRY_GAP_SEC
        except Exception:
            return True
    return False


def _scheduler_loop():
    logger.info("[Saham] Scheduler aktif: pra-pasar 08:20 WIB, evaluasi 16:30 WIB (Sen-Jum)")
    time.sleep(20)
    while True:
        try:
            now = _now()
            if now.weekday() < 5 and not _state["running"]:
                t = (now.hour, now.minute)
                td = now.strftime("%Y-%m-%d")
                if PRE_TIME <= t < MARKET_OPEN and _due(td, "pre"):
                    run_job("pre", scheduled=True)
                elif t >= EVAL_TIME and _job_row(td, "pre") and _due(td, "close"):
                    run_job("close", scheduled=True)
            if now.weekday() < 5 and MARKET_OPEN <= (now.hour, now.minute) < (16, 20) and not _state["running"] \
                    and time.time() - _live_state["last"] >= LIVE_INTERVAL_SEC:
                _live_state["last"] = time.time()
                live_tick()
            try:
                import stock_sim
                stock_sim.maybe_collect()
                stock_sim.maybe_tick()
            except Exception as se:
                logger.warning(f"[Saham] Sim error: {se}")
        except Exception as e:
            logger.warning(f"[Saham] Scheduler error: {e}")
        time.sleep(30)


_sched_thread: threading.Thread | None = None


def start_scheduler():
    global _sched_thread
    init_db()
    if _sched_thread and _sched_thread.is_alive():
        return
    _sched_thread = threading.Thread(target=_scheduler_loop, name="saham-scheduler", daemon=True)
    _sched_thread.start()


# ---------------------------------------------------------------------------
# API untuk halaman web
# ---------------------------------------------------------------------------
def _pick_rows(trade_date: str, tag: str) -> list[sqlite3.Row]:
    with _db_lock, _db() as c:
        return c.execute("SELECT * FROM picks WHERE trade_date=? AND run_tag=? ORDER BY method, rank", (trade_date, tag)).fetchall()


def available_runs() -> list[dict]:
    with _db_lock, _db() as c:
        rows = c.execute("SELECT DISTINCT trade_date, run_tag FROM picks ORDER BY trade_date DESC, run_tag ASC LIMIT 60").fetchall()
    return [{"date": r["trade_date"], "tag": r["run_tag"]} for r in rows]


def get_picks(date: str | None, tag: str | None) -> dict:
    runs = available_runs()
    if not date:
        if not runs:
            return {"runs": [], "date": None, "tag": None, "methods": [], "overview": None, "eval": None}
        pre = next((r for r in runs if r["tag"] == "pre"), None)
        sel = pre or runs[0]
        date, tag = sel["date"], sel["tag"]
    tag = tag or "pre"
    with _db_lock, _db() as c:
        ai = {r["ticker"]: dict(r) for r in c.execute("SELECT * FROM stock_ai WHERE trade_date=? AND run_tag=?", (date, tag)).fetchall()}
        ncount = {r["ticker"]: r["n"] for r in c.execute("SELECT ticker, COUNT(*) n FROM news WHERE trade_date=? GROUP BY ticker", (date,)).fetchall()}
        day = c.execute("SELECT * FROM days WHERE trade_date=?", (date,)).fetchone()
    by_method: dict[str, list] = {}
    for r in _pick_rows(date, tag):
        a = ai.get(r["ticker"], {})
        by_method.setdefault(r["method"], []).append({
            "rank": r["rank"], "ticker": r["ticker"], "name": r["name"], "sector": r["sector"], "score": r["score"],
            "ref_price": r["ref_price"], "reason": r["reason"], "data": json.loads(r["data_json"] or "{}"),
            "result_close": r["result_close"], "result_ret": r["result_ret"], "hit": r["hit"], "status": r["status"],
            "tp": r["tp"], "cl": r["cl"], "outcome": r["outcome"], "note": r["note"], "day_high": r["day_high"], "day_low": r["day_low"],
            "first_touch": r["first_touch"], "touch_at": r["touch_at"],
            "sentiment": a.get("sentiment"), "ai_summary": a.get("summary"), "catalyst": a.get("catalyst"), "risk": a.get("risk"),
            "news_count": ncount.get(r["ticker"], 0)})
    methods = [{"key": m["key"], "label": m["label"], "desc": m["desc"], "picks": by_method.get(m["key"], [])} for m in METHODS]
    return {"runs": runs, "date": date, "tag": tag, "methods": methods,
            "overview": _get_analysis(date, tag, "overview"), "eval": _get_analysis(date, "pre", "eval") if tag == "pre" else None,
            "day": dict(day) if day else None}


def get_news(ticker: str, date: str | None) -> list[dict]:
    with _db_lock, _db() as c:
        if not date:
            r = c.execute("SELECT MAX(trade_date) d FROM news").fetchone()
            date = r["d"] if r else None
        if not date:
            return []
        rows = c.execute("SELECT title, link, source, published FROM news WHERE trade_date=? AND ticker=? ORDER BY published DESC",
                         (date, ticker.upper())).fetchall()
    return [dict(r) for r in rows]


def get_status() -> dict:
    td = _today()
    with _db_lock, _db() as c:
        runs = [dict(r) for r in c.execute("SELECT * FROM runs ORDER BY trade_date DESC, job LIMIT 6").fetchall()]
    now = _now()
    return {"state": dict(_state), "now": now.strftime("%Y-%m-%d %H:%M:%S"), "weekday": now.weekday(), "today": td,
            "schedule": {"pre": "08:20 WIB", "close": "16:30 WIB"}, "runs": runs,
            "scheduler_alive": bool(_sched_thread and _sched_thread.is_alive()), "live": dict(_live_state),
            "market_hours": now.weekday() < 5 and MARKET_OPEN <= (now.hour, now.minute) < (16, 20)}


def get_all_stocks(live: bool) -> dict:
    try:
        rows = live_snapshot(60 if live else 3600) if live else (_load_snapshot() or live_snapshot(3600))
    except Exception as e:
        rows = _load_snapshot() or []
        if not rows:
            raise e
    keys = ["ticker", "name", "desc", "sector", "close", "chg", "vol", "mcap", "pe", "pbv", "rsi", "macd", "macds", "adx", "relvol",
            "sma20", "sma50", "sma200", "stk", "stkd", "bbu", "bbl", "h52", "l52", "rec", "roe", "nm", "dy", "eps", "der", "pw", "p1m", "p3m"]
    out = [{k: r.get(k) for k in keys} for r in rows]
    return {"count": len(out), "fetched": _now().strftime("%H:%M:%S"), "stocks": out}


def handle_api(method: str, sub: str, q: dict, body: dict) -> tuple[int, dict]:
    try:
        init_db()
        if sub.startswith("sim/"):
            import stock_sim
            return stock_sim.handle_api(method, sub[4:], q, body)
        if method == "GET":
            if sub == "status":
                return 200, get_status()
            if sub == "picks":
                return 200, {"success": True, **get_picks(q.get("date"), q.get("tag"))}
            if sub == "news":
                return 200, {"success": True, "news": get_news(q.get("ticker", ""), q.get("date"))}
            if sub == "stats":
                w = q.get("window", "all")
                return 200, {"success": True, **method_stats(window=None if w == "all" else int(w)), "history": daily_history(30)}
            if sub == "stocks":
                return 200, {"success": True, **get_all_stocks(q.get("live") == "1")}
            if sub == "live":
                return 200, {"success": True, "live": dict(_live_state), "tick": live_tick() if q.get("force") == "1" else None}
            if sub == "global":
                return 200, {"success": True, "global": live_global()}
        elif method == "POST":
            if sub == "run":
                job = body.get("job")
                if job not in ("pre", "close"):
                    return 400, {"success": False, "error": "job harus 'pre' atau 'close'"}
                res = start_job_async(job)
                return (200 if res.get("success") else 409), res
        return 404, {"success": False, "error": "Tidak ditemukan"}
    except Exception as e:
        logger.exception("[Saham] API error")
        return 500, {"success": False, "error": str(e)}


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["snap", "methods", "pre", "close", "stats", "live"])
    args = ap.parse_args()
    init_db()
    if args.cmd == "snap":
        rows = fetch_snapshot()
        print(len(rows), rows[0])
    elif args.cmd == "methods":
        rows = fetch_snapshot()
        res = run_methods(rows, _load_snapshot(kind="close", before=_today()))
        for k, v in res.items():
            print("==", k, len(v))
            for p in v[:3]:
                print("  ", p["ticker"], p["score"], p["reason"])
    elif args.cmd == "pre":
        print(run_job("pre"))
    elif args.cmd == "close":
        print(run_job("close"))
    elif args.cmd == "live":
        print(live_tick())
    else:
        print(json.dumps(method_stats(), indent=1, default=str))
