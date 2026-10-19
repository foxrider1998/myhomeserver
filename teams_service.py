#!/usr/bin/env python3
"""teams_service.py - Microsoft Teams Automation, Remote Browser & AI Digest for Westpro.

Features:
1. Headless Chromium session manager with persistent profile (/home/ares/.teams_profile).
2. Chrome DevTools Protocol (CDP) WebSocket client for remote viewing & login assistance (MFA).
3. Read-Only Harvester (DOM message reader with ABSOLUTE safety guarantee - never posts).
4. Daily AI Analysis via 9Router (gemini/gemini-3.8-flash) into 4 pillars:
   - 🚨 Incidents & System Bugs
   - 📌 Action Items & Tasks
   - 📅 Meetings & Scheduled Events
   - 💬 Channel & Discussion Summaries
5. SQLite storage for daily briefings and historical digests.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import websocket

# Ensure Asia/Jakarta timezone for accurate session scheduling
os.environ["TZ"] = "Asia/Jakarta"
try:
    time.tzset()
except Exception:
    pass

logger = logging.getLogger("teams_service")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] [Teams] %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.expanduser("~/.teams_profile")
DB_PATH = os.path.join(BASE_DIR, "teams.db")
CDP_PORT = 9222
CDP_BASE_URL = f"http://127.0.0.1:{CDP_PORT}"
AI_ROUTER_URL = os.environ.get("NINEROUTER_URL", "http://127.0.0.1:20128/v1/chat/completions")
AI_ROUTER_KEY = os.environ.get("NINEROUTER_KEY", "sk-6bf6c7c163dbf602-faqfeq-50da42f4")
AI_MODEL = "gemini/gemini-2.5-flash"
TEAMS_URL = "https://teams.cloud.microsoft"
SAVED_SESSION_FILE = os.path.join(BASE_DIR, "data", "teams_saved_session.json")

_proc_lock = threading.Lock()
_cdp_lock = threading.Lock()
_msg_seq = 0


def _get_seq() -> int:
    global _msg_seq
    with _cdp_lock:
        _msg_seq += 1
        return _msg_seq


# ---------------------------------------------------------------------------
# Database Initialization
# ---------------------------------------------------------------------------
def init_db():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS teams_digests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    date_str TEXT NOT NULL,
                    raw_messages_count INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'success',
                    digest_json TEXT,
                    briefing_html TEXT,
                    session_tag TEXT DEFAULT ''
                )
            """)
            try:
                conn.execute("ALTER TABLE teams_digests ADD COLUMN session_tag TEXT DEFAULT ''")
            except Exception:
                pass
            conn.execute("""
                CREATE TABLE IF NOT EXISTS teams_config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS teams_raw_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    total_chats INTEGER DEFAULT 0,
                    total_messages INTEGER DEFAULT 0,
                    snapshot_json TEXT
                )
            """)
            conn.commit()
    except Exception as e:
        logger.error(f"Error initializing teams database: {e}")


init_db()


def teams_get_config() -> dict:
    default_cfg = {
        "sprint_name": "Sprint 6 PNRI",
        "target_channels": "Sprint PNRI, Tech Team, Dev QA",
        "target_chats": "Aris, QA Lead, Tech Lead",
        "sprint_members": "Dimas, Bayu, Reza, Iqbal, Kevin",
        "user_name": "Aris",
        "sprint_focus_only": True,
        "include_incidents": True,
        "include_tasks": True,
        "include_meetings": True,
        "auto_detect_all": True
    }
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute("SELECT value FROM teams_config WHERE key = 'sprint_recap_config'").fetchone()
            if row and row[0]:
                d = json.loads(row[0])
                default_cfg.update(d)
    except Exception as e:
        logger.error(f"Error getting teams config: {e}")
    return default_cfg


def teams_save_config(cfg: dict) -> bool:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO teams_config (key, value)
                VALUES ('sprint_recap_config', ?)
            """, (json.dumps(cfg),))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"Error saving teams config: {e}")
        return False


# ---------------------------------------------------------------------------
# Chromium Process Lifecycle
# ---------------------------------------------------------------------------
def is_chromium_running() -> bool:
    try:
        req = urllib.request.Request(f"{CDP_BASE_URL}/json/version", headers={"User-Agent": "TeamsBot"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def start_chromium(target_url: str = TEAMS_URL) -> bool:
    if is_chromium_running():
        return True

    with _proc_lock:
        if is_chromium_running():
            return True

        os.makedirs(PROFILE_DIR, exist_ok=True)
        cmd = [
            "/usr/bin/chromium",
            "--headless=new",
            f"--remote-debugging-port={CDP_PORT}",
            "--remote-allow-origins=*",
            f"--user-data-dir={PROFILE_DIR}",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--window-size=1280,800",
            "--hide-scrollbars",
            target_url,
        ]

        logger.info(f"Starting Chromium headless for Teams with profile: {PROFILE_DIR}")
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            # Wait up to 10 seconds for CDP port to open
            for _ in range(20):
                time.sleep(0.5)
                if is_chromium_running():
                    logger.info("Chromium CDP port is live!")
                    return True
            logger.warning("Chromium started but CDP port not responsive within 10s")
            return False
        except Exception as e:
            logger.error(f"Failed to start Chromium: {e}")
            return False


def restart_chromium(target_url: str = TEAMS_URL) -> bool:
    try:
        subprocess.run(["pkill", "-f", f"remote-debugging-port={CDP_PORT}"], timeout=3)
        time.sleep(1)
    except Exception:
        pass
    return start_chromium(target_url)


# ---------------------------------------------------------------------------
# CDP Helpers
# ---------------------------------------------------------------------------
def _get_active_page_target() -> dict | None:
    try:
        req = urllib.request.Request(f"{CDP_BASE_URL}/json/list", headers={"User-Agent": "TeamsBot"})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            targets = json.loads(resp.read().decode("utf-8"))
            # Prefer 'page' targets that have teams or login or http
            pages = [t for t in targets if t.get("type") == "page"]
            if not pages:
                # Open a new tab
                new_req = urllib.request.Request(f"{CDP_BASE_URL}/json/new?{urllib.parse.quote(TEAMS_URL)}")
                with urllib.request.urlopen(new_req, timeout=3) as new_resp:
                    return json.loads(new_resp.read().decode("utf-8"))
            # Return first page or one that matches teams/microsoft
            for p in pages:
                url = p.get("url", "")
                if "teams.microsoft.com" in url or "login.microsoftonline.com" in url:
                    return p
            return pages[0]
    except Exception as e:
        logger.error(f"Error getting CDP active page: {e}")
        return None


def _send_cdp_command(method: str, params: dict | None = None, timeout: float = 6.0) -> dict | None:
    if not start_chromium():
        return None

    target = _get_active_page_target()
    if not target or "webSocketDebuggerUrl" not in target:
        logger.error("No valid webSocketDebuggerUrl found")
        return None

    ws_url = target["webSocketDebuggerUrl"]
    seq = _get_seq()
    payload = json.dumps({"id": seq, "method": method, "params": params or {}})

    try:
        ws = websocket.create_connection(ws_url, timeout=timeout)
        try:
            ws.send(payload)
            ws.settimeout(timeout)
            while True:
                raw = ws.recv()
                data = json.loads(raw)
                if data.get("id") == seq:
                    return data
        finally:
            ws.close()
    except Exception as e:
        logger.error(f"CDP command {method} failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Browser Interaction API
# ---------------------------------------------------------------------------
def teams_get_screenshot(quality: int = 70) -> bytes | None:
    """Captures a screenshot of the Teams browser page in JPEG format."""
    res = _send_cdp_command("Page.captureScreenshot", {
        "format": "jpeg",
        "quality": max(30, min(95, quality)),
    }, timeout=5.0)

    if res and "result" in res and "data" in res["result"]:
        return base64.b64decode(res["result"]["data"])
    return None


def teams_dispatch_click(x: int, y: int) -> bool:
    """Dispatches a mouse click at coordinates (x, y) with both DOM element trigger and CDP mouse events."""
    try:
        # 1. Run DOM element click / focus at coordinates (x, y)
        dom_click_js = f"""
        (() => {{
            const el = document.elementFromPoint({int(x)}, {int(y)});
            if (el) {{
                if (typeof el.focus === 'function') el.focus();
                if (typeof el.click === 'function') el.click();
                return {{ tag: el.tagName, id: el.id, className: el.className }};
            }}
            return null;
        }})()
        """
        _send_cdp_command("Runtime.evaluate", {"expression": dom_click_js, "returnByValue": True}, timeout=2.0)

        # 2. Also send CDP mousePressed and mouseReleased
        p_down = {
            "type": "mousePressed",
            "x": int(x),
            "y": int(y),
            "button": "left",
            "clickCount": 1,
        }
        p_up = {
            "type": "mouseReleased",
            "x": int(x),
            "y": int(y),
            "button": "left",
            "clickCount": 1,
        }
        _send_cdp_command("Input.dispatchMouseEvent", p_down, timeout=2.0)
        time.sleep(0.04)
        _send_cdp_command("Input.dispatchMouseEvent", p_up, timeout=2.0)
        return True
    except Exception as e:
        logger.error(f"Click failed: {e}")
        return False


def teams_dispatch_key(text: str | None = None, key: str | None = None) -> bool:
    """Dispatches text typing or special key press (Enter, Backspace, Tab, etc.)."""
    try:
        if key:
            key_codes = {
                "Enter": (13, "Enter", "Enter"),
                "Backspace": (8, "Backspace", "Backspace"),
                "Tab": (9, "Tab", "Tab"),
                "Escape": (27, "Escape", "Escape"),
                "ArrowDown": (40, "ArrowDown", "ArrowDown"),
                "ArrowUp": (38, "ArrowUp", "ArrowUp"),
            }
            code_info = key_codes.get(key, (0, key, key))

            if key == "Clear":
                clear_js = """
                (() => {
                    let target = document.activeElement;
                    if (!target || target === document.body || target.tagName === 'BODY') {
                        target = Array.from(document.querySelectorAll('input:not([type="hidden"]), textarea'))
                            .find(el => el.offsetParent !== null);
                    }
                    if (target) {
                        target.focus();
                        target.select();
                        document.execCommand('delete');
                        target.value = '';
                        target.dispatchEvent(new Event('input', { bubbles: true }));
                        target.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    }
                    return false;
                })()
                """
                _send_cdp_command("Runtime.evaluate", {"expression": clear_js, "returnByValue": True}, timeout=2.0)
                return True

            # If Enter, trigger click on Next / Submit button cleanly with mouse coordinates
            if key in ("Enter", "Submit"):
                enter_js = """
                (() => {
                    const act = document.activeElement;
                    if (act && act.tagName === 'INPUT') {
                        act.dispatchEvent(new Event('input', { bubbles: true }));
                        act.dispatchEvent(new Event('change', { bubbles: true }));
                    }

                    const submitBtn = document.querySelector(
                        '#idSIButton9, input[type="submit"], button[type="submit"], #idSubmit_SAOTCC_Continue, [data-report-event="Signin_Submit"]'
                    );
                    if (submitBtn && submitBtn.offsetParent !== null) {
                        const r = submitBtn.getBoundingClientRect();
                        submitBtn.focus();
                        submitBtn.click();
                        return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
                    }
                    return null;
                })()
                """
                res = _send_cdp_command("Runtime.evaluate", {"expression": enter_js, "returnByValue": True}, timeout=2.0)
                btn_coords = (res or {}).get("result", {}).get("result", {}).get("value")
                if isinstance(btn_coords, dict) and "x" in btn_coords and btn_coords["x"] > 0:
                    bx, by = btn_coords["x"], btn_coords["y"]
                    _send_cdp_command("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": bx, "y": by}, timeout=1.0)
                    _send_cdp_command("Input.dispatchMouseEvent", {"type": "mousePressed", "x": bx, "y": by, "button": "left", "clickCount": 1}, timeout=1.0)
                    time.sleep(0.04)
                    _send_cdp_command("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": bx, "y": by, "button": "left", "clickCount": 1}, timeout=1.0)
                if key == "Submit":
                    return True

            _send_cdp_command("Input.dispatchKeyEvent", {
                "type": "rawKeyDown",
                "windowsVirtualKeyCode": code_info[0],
                "key": code_info[1],
                "code": code_info[2],
            }, timeout=2.0)
            if key == "Enter":
                _send_cdp_command("Input.dispatchKeyEvent", {
                    "type": "char",
                    "text": "\r",
                    "unmodifiedText": "\r",
                }, timeout=2.0)
            time.sleep(0.03)
            _send_cdp_command("Input.dispatchKeyEvent", {
                "type": "keyUp",
                "windowsVirtualKeyCode": code_info[0],
                "key": code_info[1],
                "code": code_info[2],
            }, timeout=2.0)
            return True

        if text:
            # First, ensure a visible input has focus
            focus_dom_js = """
            (() => {
                let target = document.activeElement;
                if (!target || target === document.body || target.tagName === 'BODY') {
                    target = Array.from(document.querySelectorAll('input:not([type="hidden"]), textarea'))
                        .find(el => el.offsetParent !== null);
                    if (target) target.focus();
                }
                return !!target;
            })()
            """
            _send_cdp_command("Runtime.evaluate", {"expression": focus_dom_js, "returnByValue": True}, timeout=2.0)

            # Insert text cleanly without duplicate characters via CDP Input.insertText
            res = _send_cdp_command("Input.insertText", {"text": text}, timeout=3.0)
            return bool(res and "result" in res)
        return False
    except Exception as e:
        logger.error(f"Key dispatch failed: {e}")
        return False


def teams_navigate(url: str = TEAMS_URL) -> bool:
    """Navigates the browser to the given URL."""
    res = _send_cdp_command("Page.navigate", {"url": url}, timeout=5.0)
    return bool(res and "result" in res)


def teams_reload() -> bool:
    """Reloads the current page."""
    res = _send_cdp_command("Page.reload", {"ignoreCache": True}, timeout=5.0)
    return bool(res and "result" in res)


def teams_import_session(session_data: dict) -> dict:
    """Imports session (localStorage and cookies) into Chromium."""
    try:
        if not start_chromium():
            return {"success": False, "error": "Gagal memulai browser Chromium di server"}

        storage = session_data.get("storage") or {}
        raw_cookies = session_data.get("cookies") or session_data.get("cookie") or ""
        target_url = str(session_data.get("url") or "https://teams.microsoft.com/v2/")

        # 1. Enable Network domain and inject cookies
        _send_cdp_command("Network.enable", {})

        cookie_list = []
        if isinstance(raw_cookies, list):
            cookie_list = raw_cookies
        elif isinstance(raw_cookies, str) and raw_cookies.strip():
            domains = [".teams.microsoft.com", ".microsoft.com"]
            if "cloud.microsoft" in target_url:
                domains.extend([".teams.cloud.microsoft", ".cloud.microsoft"])
            for item in raw_cookies.split(";"):
                item = item.strip()
                if "=" in item:
                    k, v = item.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    if k:
                        for dom in domains:
                            cookie_list.append({
                                "name": k,
                                "value": v,
                                "domain": dom,
                                "path": "/",
                                "secure": True
                            })

        if cookie_list:
            _send_cdp_command("Network.setCookies", {"cookies": cookie_list})
            logger.info(f"Injected {len(cookie_list)} cookies into Teams browser session")

        # 2. Navigate to target URL first so we are on the exact origin
        teams_navigate(target_url)
        time.sleep(3)

        # 3. Inject localStorage
        if storage and isinstance(storage, dict):
            storage_json = json.dumps(storage)
            inject_js = f"""
            (() => {{
                try {{
                    const s = {storage_json};
                    for (const [k, v] of Object.entries(s)) {{
                        try {{
                            localStorage.setItem(k, typeof v === 'object' ? JSON.stringify(v) : String(v));
                        }} catch(e) {{}}
                    }}
                    return Object.keys(s).length;
                }} catch(err) {{
                    return 'err: ' + err.message;
                }}
            }})()
            """
            _send_cdp_command("Runtime.evaluate", {"expression": inject_js, "returnByValue": True})
            logger.info(f"Injected {len(storage)} localStorage keys into Teams browser")

        # 4. Reload page to let Teams load the new session
        time.sleep(1)
        _send_cdp_command("Page.reload", {"ignoreCache": False})
        time.sleep(4)

        status = teams_get_status()
        if status.get("logged_in") or storage or cookie_list:
            try:
                os.makedirs(os.path.dirname(SAVED_SESSION_FILE), exist_ok=True)
                with open(SAVED_SESSION_FILE + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(session_data, f, indent=2)
                os.replace(SAVED_SESSION_FILE + ".tmp", SAVED_SESSION_FILE)
                logger.info(f"Sesi Teams berhasil disimpan ke {SAVED_SESSION_FILE}")
            except Exception as se:
                logger.warning(f"Gagal menyimpan sesi ke file: {se}")

        return {"success": True, "status": status}
    except Exception as e:
        logger.error(f"Failed to import teams session: {e}")
        return {"success": False, "error": str(e)}


def teams_get_status() -> dict:
    """Checks the status of the Teams browser (running, logged in, needs login, page info)."""
    running = is_chromium_running()
    if not running:
        return {
            "running": False,
            "logged_in": False,
            "status": "offline",
            "message": "Chromium offline",
            "url": "",
            "title": "",
        }

    # Evaluate JS in page to detect state
    js_eval = """
    (() => {
        const url = window.location.href;
        const title = document.title;
        const bodyText = document.body ? document.body.innerText.slice(0, 500) : '';
        
        const isLogin = url.includes('login.microsoftonline.com') || 
                        url.includes('login.live.com') || 
                        !!document.querySelector('input[type="email"], input[name="loginfmt"], input[type="password"]');
                        
        const hasAuthNumber = !!document.querySelector('#idRichContext_DisplaySign, .displaySign, [data-bind*="displaySign"]');
        
        const isTeamsApp = url.includes('teams.microsoft.com') || url.includes('teams.live.com') || url.includes('teams.cloud.microsoft');
        
        const hasChatList = !!document.querySelector(
            '[data-tid="chat-list-item"], [data-tid="app-bar-chat"], .chat-list, [data-tid="messageBodyContent"], [role="treeitem"], [data-tid="team-channel-list"]'
        );
        
        const hasUserProfile = !!document.querySelector(
            '[data-tid="me-control-avatar-image"], [data-tid="user-avatar"], #person-avatar, [aria-label*="Profile"], [aria-label*="Profil"]'
        );
        
        return {
            url,
            title,
            isLogin,
            hasAuthNumber,
            isTeamsApp,
            hasChatList,
            hasUserProfile,
            loading: document.readyState !== 'complete'
        };
    })()
    """

    res = _send_cdp_command("Runtime.evaluate", {
        "expression": js_eval,
        "returnByValue": True,
    }, timeout=4.0)

    val = {}
    if res and "result" in res and "result" in res["result"] and "value" in res["result"]["result"]:
        val = res["result"]["result"]["value"]

    url = val.get("url", "")
    title = val.get("title", "")
    is_login = val.get("isLogin", False)
    has_auth = val.get("hasAuthNumber", False)
    has_chat = val.get("hasChatList", False)
    has_prof = val.get("hasUserProfile", False)
    is_teams = val.get("isTeamsApp", False)

    if (has_chat or (is_teams and (has_prof or "Chat" in title or "Teams" in title))) and not is_login and not has_auth:
        status = "logged_in"
        message = "Sesi Teams Aktif"
    elif has_auth:
        status = "needs_mfa"
        message = "Menunggu Persetujuan Microsoft Authenticator di HP"
    elif is_login:
        status = "needs_login"
        message = "Perlu Login Akun Microsoft"
    elif val.get("loading", False):
        status = "loading"
        message = "Memuat Halaman Teams..."
    else:
        status = "unknown"
        message = f"Halaman: {title or 'Teams'}"

    return {
        "running": True,
        "logged_in": (status == "logged_in"),
        "status": status,
        "message": message,
        "url": url,
        "title": title,
    }


def teams_backup_current_session() -> bool:
    """Mengambil cookies & localStorage dari sesi aktif Teams dan menyimpannya ke file cadangan."""
    if not is_chromium_running():
        return False
    try:
        status = teams_get_status()
        if not status.get("logged_in"):
            return False

        # 1. Ambil cookies dari CDP
        c_res = _send_cdp_command("Network.getCookies", {
            "urls": [
                "https://teams.cloud.microsoft",
                "https://teams.microsoft.com",
                "https://login.microsoftonline.com"
            ]
        })
        cookies = []
        if c_res and "result" in c_res and "cookies" in c_res["result"]:
            cookies = c_res["result"]["cookies"]

        # 2. Ambil localStorage dari page aktif
        storage_js = """
        (() => {
            try {
                const s = {};
                for (let i = 0; i < localStorage.length; i++) {
                    const k = localStorage.key(i);
                    s[k] = localStorage.getItem(k);
                }
                return s;
            } catch(e) { return {}; }
        })()
        """
        s_res = _send_cdp_command("Runtime.evaluate", {"expression": storage_js, "returnByValue": True})
        storage = {}
        if s_res and "result" in s_res and "result" in s_res["result"] and "value" in s_res["result"]["result"]:
            storage = s_res["result"]["result"]["value"] or {}

        if not cookies and not storage:
            return False

        payload = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "url": status.get("url") or TEAMS_URL,
            "cookies": cookies,
            "storage": storage
        }
        os.makedirs(os.path.dirname(SAVED_SESSION_FILE), exist_ok=True)
        with open(SAVED_SESSION_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(SAVED_SESSION_FILE + ".tmp", SAVED_SESSION_FILE)
        logger.info(f"Sesi Teams berhasil dicadangkan ke {SAVED_SESSION_FILE} ({len(cookies)} cookies, {len(storage)} storage keys)")
        return True
    except Exception as e:
        logger.warning(f"Gagal mencadangkan sesi Teams: {e}")
        return False


def teams_restore_session() -> bool:
    """Mengambil dan menyuntikkan ulang sesi yang tersimpan ke browser Chromium."""
    if not os.path.exists(SAVED_SESSION_FILE):
        logger.warning(f"File sesi cadangan tidak ditemukan di {SAVED_SESSION_FILE}")
        return False
    try:
        with open(SAVED_SESSION_FILE, "r", encoding="utf-8") as f:
            session_data = json.load(f)
        logger.info(f"Memulihkan sesi Teams dari cadangan ({session_data.get('timestamp')})...")
        res = teams_import_session(session_data)
        if res.get("success") and res.get("status", {}).get("logged_in"):
            logger.info("✓ Sesi Teams berhasil dipulihkan dari cadangan dan login aktif kembali!")
            return True
        else:
            logger.warning(f"Pemulihan sesi belum berhasil login: {res.get('status', {}).get('message')}")
            return False
    except Exception as e:
        logger.error(f"Error memulihkan sesi Teams: {e}")
        return False


def teams_ensure_logged_in(max_attempts: int = 2) -> bool:
    """Memastikan Teams dalam status logged in. Jika sesi hilang atau butuh login,
    otomatis mencoba memulihkan session cadangan.
    """
    if not is_chromium_running():
        logger.info("Chromium belum berjalan. Menjalankan Chromium...")
        start_chromium()
        time.sleep(5)

    status = teams_get_status()
    if status.get("logged_in"):
        return True

    # Jika gagal login / perlu login, coba ambil session ulang
    logger.warning(f"Teams belum login (status: {status.get('status')}, msg: {status.get('message')}). Mencoba ambil session ulang...")
    for attempt in range(max_attempts):
        if teams_restore_session():
            return True
        # Jika gagal, coba restart chromium dan restore lagi
        logger.info(f"Percobaan pemulihan sesi #{attempt+1} belum login. Merestart Chromium...")
        restart_chromium()
        time.sleep(6)
        if teams_restore_session():
            return True
        time.sleep(3)

    final_status = teams_get_status()
    return final_status.get("logged_in", False)


def teams_pull_chats(max_retries: int = 3, retry_delay: float = 6.0) -> dict:
    """Menarik obrolan Teams terbaru dengan mekanisme retry jika gagal.
    Jika kegagalan disebabkan sesi login terputus/kadaluarsa, otomatis mengambil session ulang.
    """
    logger.info("Memulai penarikan obrolan Teams otomatis...")
    # 1. Pastikan sesi login aktif (jika gagal login, ambil session ulang)
    logged_in = teams_ensure_logged_in()
    if not logged_in:
        logger.error("Penarikan obrolan gagal: Teams tidak login dan sesi cadangan gagal dipulihkan.")
        return {
            "success": False,
            "error": "login_failed",
            "message": "Teams belum login. Sesi cadangan dicoba namun tetap butuh persetujuan login akun Microsoft."
        }

    # 2. Tarik obrolan dengan mekanisme retry
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Menarik obrolan Teams (percobaan {attempt}/{max_retries})...")
            result = teams_harvest_all_chats(max_chats=15, max_messages=25)
            if result.get("success") and result.get("chats"):
                total_msgs = result.get("total_messages", 0)
                chats_count = len(result.get("chats", []))
                logger.info(f"✓ Berhasil menarik obrolan Teams: {chats_count} obrolan ({total_msgs} pesan).")

                # Backup sesi aktif yang terbukti bekerja dengan baik ke file cadangan
                try:
                    threading.Thread(target=teams_backup_current_session, daemon=True).start()
                except Exception:
                    pass

                return {
                    "success": True,
                    "attempt": attempt,
                    "chats_count": chats_count,
                    "total_messages": total_msgs,
                    "chats": result.get("chats", [])
                }
            else:
                last_err = result.get("error", "Data chat kosong / tidak ditemukan")
                logger.warning(f"Percobaan ke-{attempt} belum berhasil: {last_err}")
        except Exception as e:
            last_err = str(e)
            logger.warning(f"Exception saat penarikan obrolan percobaan ke-{attempt}: {e}")

        # Jika percobaan gagal, periksa apakah penyebabnya sesi login lepas di tengah jalan
        status = teams_get_status()
        if not status.get("logged_in"):
            logger.warning("Terdeteksi sesi terputus saat menarik obrolan. Mengambil session ulang...")
            teams_restore_session()

        if attempt < max_retries:
            logger.info(f"Menunggu {retry_delay} detik sebelum retry penarikan obrolan...")
            time.sleep(retry_delay)

    logger.error(f"Gagal menarik obrolan Teams setelah {max_retries} percobaan: {last_err}")
    return {
        "success": False,
        "error": "harvest_failed",
        "message": f"Gagal menarik obrolan setelah {max_retries} percobaan: {last_err}"
    }


_harvester_thread = None
_harvester_stop_event = threading.Event()


def _periodic_harvest_worker(interval_sec: int = 1800):
    logger.info(f"Background Teams periodic harvester aktif (setiap {interval_sec // 60} menit)...")
    time.sleep(15)  # Delay awal saat startup
    while not _harvester_stop_event.is_set():
        try:
            logger.info("[Auto-Harvester] Menjalankan penarikan obrolan Teams otomatis berkala (30 menit)...")
            res = teams_pull_chats(max_retries=3, retry_delay=6.0)
            if res.get("success"):
                logger.info(f"[Auto-Harvester] ✓ Berhasil menarik {res.get('chats_count')} obrolan ({res.get('total_messages')} pesan)")
            else:
                logger.warning(f"[Auto-Harvester] ⚠️ Gagal menarik obrolan: {res.get('message')}")
        except Exception as e:
            logger.error(f"[Auto-Harvester] Error loop penarikan berkala: {e}")

        if _harvester_stop_event.wait(timeout=interval_sec):
            break


def teams_start_periodic_harvester(interval_sec: int = 1800):
    global _harvester_thread
    if _harvester_thread and _harvester_thread.is_alive():
        return
    _harvester_thread = threading.Thread(target=_periodic_harvest_worker, args=(interval_sec,), daemon=True)
    _harvester_thread.start()
    logger.info("Teams periodic harvester thread berhasil distart.")


# ---------------------------------------------------------------------------
# READ-ONLY Harvester
# ---------------------------------------------------------------------------
def teams_harvest_messages() -> list[dict]:
    """Reads messages and channel discussions from Teams Web.
    
    SAFETY NOTICE:
    This function is 100% READ-ONLY. It NEVER clicks send buttons,
    never types into input boxes, and never triggers any modification.
    It purely queries the DOM for rendered text elements.
    """
    if not is_chromium_running():
        start_chromium()
        time.sleep(3)

    # JavaScript to extract conversation items and visible messages
    harvest_js = """
    (() => {
        const results = [];
        
        // 1. Gather Chat List Items (Recent Chats / Channels in left rail)
        const chatItems = document.querySelectorAll(
            '[data-tid="chat-list-item"], [data-tid="team-channel-item"], [role="treeitem"], .chat-list-item'
        );
        
        const chatListOverview = [];
        chatItems.forEach((el, idx) => {
            if (idx > 25) return; // Limit to 25 recent items
            const titleEl = el.querySelector('[data-tid="chat-list-item-title"], .chat-name, [data-tid="channel-name"]') || el;
            const snippetEl = el.querySelector('[data-tid="chat-list-item-snippet"], .last-message') || null;
            const unreadEl = el.querySelector('[data-tid="chat-unread-badge"], .unread-badge') || null;
            
            chatListOverview.push({
                type: 'chat_item',
                title: (titleEl.innerText || '').trim(),
                snippet: snippetEl ? (snippetEl.innerText || '').trim() : '',
                unread: !!unreadEl
            });
        });
        
        // 2. Gather Messages from currently active conversation pane
        const activeHeaderEl = document.querySelector(
            '[data-tid="chat-header-title"], [data-tid="channel-header-title"], h2[tabindex="-1"], .chat-header'
        );
        const activeTitle = activeHeaderEl ? (activeHeaderEl.innerText || '').trim() : 'Active Conversation';
        
        const msgNodes = document.querySelectorAll(
            '[data-tid="chat-pane-message"], [data-tid="messageBodyContent"], [role="listitem"], .ui-chat__message'
        );
        
        const activeMessages = [];
        msgNodes.forEach((node, idx) => {
            // Find author
            const authorEl = node.querySelector(
                '[data-tid="message-author-name"], .ui-chat__message__author, [aria-label*="from"], [data-tid="author"]'
            );
            // Find timestamp
            const timeEl = node.querySelector(
                '[data-tid="message-timestamp"], time, .ui-chat__message__timestamp'
            );
            // Find content body
            const bodyEl = node.querySelector(
                '[data-tid="messageBodyContent"], .ui-chat__message__content, [data-tid="message-text"]'
            ) || node;
            
            const author = authorEl ? (authorEl.innerText || '').trim() : '';
            const timestamp = timeEl ? (timeEl.innerText || timeEl.getAttribute('aria-label') || '').trim() : '';
            const body = (bodyEl.innerText || '').trim();
            
            if (body && body.length > 2 && !body.includes('Type a new message') && !body.includes('Ketik pesan baru')) {
                activeMessages.push({
                    author: author || 'Unknown',
                    time: timestamp,
                    text: body
                });
            }
        });
        
        return {
            active_title: activeTitle,
            chat_list: chatListOverview,
            active_messages: activeMessages.slice(-50), // last 50 messages
            scraped_at: new Date().toISOString()
        };
    })()
    """

    res = _send_cdp_command("Runtime.evaluate", {
        "expression": harvest_js,
        "returnByValue": True,
    }, timeout=8.0)

    if res and "result" in res and "result" in res["result"] and "value" in res["result"]["result"]:
        return [res["result"]["result"]["value"]]
    return []


def teams_harvest_all_chats(max_chats: int = 15, max_messages: int = 30) -> dict:
    """Clicks through each chat in the left rail (Personal & Group) and harvests recent messages."""
    if not is_chromium_running():
        return {"success": False, "error": "Chromium offline"}

    discover_js = """
    (() => {
        const skipList = ['Copilot', 'Quick views', 'Mentions', 'Discover', 'Drafts', 'Favorites', 'Chats', 'See more', 'Workflows'];
        const items = Array.from(document.querySelectorAll('[data-tid="chat-list-item"], [role="treeitem"]'));
        const candidates = [];
        const seen = new Set();

        items.forEach((el, idx) => {
            const text = (el.innerText || '').trim();
            const firstLine = text.split('\\n')[0].trim();
            if (!firstLine || firstLine.length < 2) return;
            if (skipList.some(s => firstLine === s || firstLine.startsWith(s + '\\n'))) return;
            if (seen.has(firstLine)) return;
            seen.add(firstLine);

            const isGroup = firstLine.startsWith('[') || firstLine.includes(',') || firstLine.startsWith('QA ') || firstLine.includes('SCRUM') || firstLine.includes('Team');
            candidates.push({
                index: idx,
                title: firstLine,
                type: isGroup ? 'group' : 'personal',
                snippet: text.split('\\n').slice(1).join(' ').slice(0, 80)
            });
        });

        return candidates;
    })()
    """

    res = _send_cdp_command("Runtime.evaluate", {"expression": discover_js, "returnByValue": True}, timeout=6.0)
    candidates = []
    if res and "result" in res and "result" in res["result"] and "value" in res["result"]["result"]:
        candidates = res["result"]["result"]["value"] or []

    if not candidates:
        return {"success": False, "error": "Tidak ada daftar obrolan yang ditemukan di antarmuka Teams"}

    # Prioritaskan obrolan grup sprint dan personal chat (PC) rekan pemegang task sprint
    cfg = teams_get_config()
    target_channels = [c.strip().lower() for c in cfg.get("target_channels", "").split(",") if c.strip()]
    target_chats = [c.strip().lower() for c in (cfg.get("target_chats", "") + "," + cfg.get("sprint_members", "")).split(",") if c.strip()]
    sprint_keyword = (cfg.get("sprint_name") or "").strip().lower()

    def candidate_score(c):
        title = (c.get("title") or "").lower()
        # Prioritas 1: Personal chat (PC) dengan orang yang memiliki task di sprint
        if any(tc in title for tc in target_chats if len(tc) >= 2):
            return 3
        # Prioritas 2: Obrolan grup/channel yang menyebut nama sprint
        if sprint_keyword and len(sprint_keyword) >= 3 and sprint_keyword in title:
            return 2
        # Prioritas 2b: Saluran/channel sprint target
        if any(tch in title for tch in target_channels if len(tch) >= 2):
            return 2
        # Prioritas 3: Grup umum
        if c.get("type") == "group":
            return 1
        return 0

    candidates.sort(key=candidate_score, reverse=True)
    candidates = candidates[:max_chats]
    harvested_chats = []
    total_messages_count = 0

    extract_js = f"""
    (() => {{
        const nodes = Array.from(document.querySelectorAll('[data-tid="chat-pane-message"]'));
        let lastAuthor = 'Unknown';
        let lastTime = '';
        const msgs = [];

        nodes.forEach(node => {{
            const mid = node.getAttribute('data-mid') || '';
            const authorEl = mid ? document.getElementById('author-' + mid) : node.querySelector('[data-tid="message-author-name"]');
            const timeEl = mid ? document.getElementById('timestamp-' + mid) : node.querySelector('[data-tid="message-timestamp"]');

            let author = authorEl ? (authorEl.innerText || '').trim() : '';
            let timeStr = timeEl ? (timeEl.innerText || timeEl.getAttribute('aria-label') || '').trim() : '';

            if (author) lastAuthor = author;
            else author = lastAuthor;

            if (timeStr) lastTime = timeStr;
            else timeStr = lastTime;

            const contentEl = mid ? document.getElementById('content-' + mid) : node;
            let text = (contentEl ? contentEl.innerText : node.innerText) || '';
            text = text.trim();

            // 1. File attachments
            const attachEl = mid ? document.getElementById('attachments-' + mid) : node.querySelector('[id^="attachments-"], [data-tid*="attachment"]');
            const attachments = [];
            if (attachEl) {{
                const fileItems = Array.from(attachEl.querySelectorAll('[aria-label], [title]'));
                const seenFiles = new Set();
                fileItems.forEach(fi => {{
                    const fname = (fi.getAttribute('title') || fi.getAttribute('aria-label') || fi.innerText || '').trim();
                    if (fname && /\\.[a-zA-Z0-9]{{2,5}}$/.test(fname) && !seenFiles.has(fname) && fname.length < 150) {{
                        seenFiles.add(fname);
                        const ext = fname.split('.').pop().toLowerCase();
                        attachments.push({{
                            type: 'file',
                            name: fname,
                            ext: ext
                        }});
                    }}
                }});

                if (!attachments.length && attachEl.innerText.trim()) {{
                    const fallbackName = attachEl.innerText.trim().split('\\n')[0].slice(0, 100);
                    if (fallbackName) {{
                        attachments.push({{
                            type: 'file',
                            name: fallbackName,
                            ext: fallbackName.includes('.') ? fallbackName.split('.').pop().toLowerCase() : 'file'
                        }});
                    }}
                }}
            }}

            // 2. Inline images
            const imgNodes = Array.from(node.querySelectorAll('img')).filter(img => {{
                const src = img.getAttribute('src') || '';
                return !src.includes('/emoticons/') && !src.includes('personal-expressions');
            }});
            imgNodes.forEach((img, idx) => {{
                attachments.push({{
                    type: 'image',
                    name: img.getAttribute('alt') || img.getAttribute('aria-label') || ('Gambar ' + (idx + 1)),
                    src: img.getAttribute('src') || ''
                }});
            }});

            // 3. Links
            const linkNodes = Array.from(node.querySelectorAll('a[href]'));
            const links = [];
            linkNodes.forEach(a => {{
                const href = a.getAttribute('href') || '';
                const lText = (a.innerText || '').trim();
                if (href && !href.startsWith('javascript:') && !href.startsWith('#')) {{
                    links.push({{
                        text: lText || href,
                        url: href
                    }});
                }}
            }});

            if (text && text.length > 1 && !text.includes('Type a message') && !text.includes('Ketik pesan')) {{
                msgs.push({{
                    mid: mid,
                    author: author,
                    time: timeStr,
                    text: text,
                    attachments: attachments,
                    links: links
                }});
            }}
        }});
        return msgs.slice(-{max_messages});
    }})()
    """

    for c in candidates:
        chat_title = c["title"]
        click_js = f"""
        (() => {{
            const items = Array.from(document.querySelectorAll('[data-tid="chat-list-item"], [role="treeitem"]'));
            const target = items.find(el => (el.innerText || '').startsWith({json.dumps(chat_title)}));
            if (target) {{
                target.click();
                return true;
            }}
            return false;
        }})()
        """
        _send_cdp_command("Runtime.evaluate", {"expression": click_js, "returnByValue": True}, timeout=4.0)
        time.sleep(1.2)

        res_msgs = _send_cdp_command("Runtime.evaluate", {"expression": extract_js, "returnByValue": True}, timeout=5.0)
        msgs = []
        if res_msgs and "result" in res_msgs and "result" in res_msgs["result"] and "value" in res_msgs["result"]["result"]:
            msgs = res_msgs["result"]["result"]["value"] or []

        total_messages_count += len(msgs)
        harvested_chats.append({
            "title": chat_title,
            "type": c["type"],
            "snippet": c.get("snippet", ""),
            "messages_count": len(msgs),
            "messages": msgs
        })

    snapshot_data = {
        "success": True,
        "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_chats": len(harvested_chats),
        "total_messages": total_messages_count,
        "chats": harvested_chats
    }

    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                INSERT INTO teams_raw_snapshots (total_chats, total_messages, snapshot_json)
                VALUES (?, ?, ?)
            """, (len(harvested_chats), total_messages_count, json.dumps(snapshot_data)))
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to save raw snapshot: {e}")

    return snapshot_data


def teams_get_latest_raw_chats() -> dict:
    """Returns the most recent raw snapshot of harvested chats and messages."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT snapshot_json FROM teams_raw_snapshots
                ORDER BY id DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row and row[0]:
                return json.loads(row[0])
    except Exception as e:
        logger.error(f"Failed to read latest raw snapshot: {e}")
    return {"success": False, "total_chats": 0, "total_messages": 0, "chats": []}


def teams_select_chat(title: str) -> dict:
    """Switches active chat in Teams remote browser to the given title and returns its messages."""
    if not is_chromium_running():
        return {"success": False, "error": "Chromium offline"}

    click_js = f"""
    (() => {{
        const items = Array.from(document.querySelectorAll('[data-tid="chat-list-item"], [role="treeitem"]'));
        const target = items.find(el => (el.innerText || '').startsWith({json.dumps(title)}));
        if (target) {{
            target.click();
            return true;
        }}
        return false;
    }})()
    """
    _send_cdp_command("Runtime.evaluate", {"expression": click_js, "returnByValue": True}, timeout=4.0)
    time.sleep(1.2)

    extract_js = """
    (() => {
        const nodes = Array.from(document.querySelectorAll('[data-tid="chat-pane-message"]'));
        let lastAuthor = 'Unknown';
        let lastTime = '';
        const msgs = [];

        nodes.forEach(node => {
            const mid = node.getAttribute('data-mid') || '';
            const authorEl = mid ? document.getElementById('author-' + mid) : node.querySelector('[data-tid="message-author-name"]');
            const timeEl = mid ? document.getElementById('timestamp-' + mid) : node.querySelector('[data-tid="message-timestamp"]');

            let author = authorEl ? (authorEl.innerText || '').trim() : '';
            let timeStr = timeEl ? (timeEl.innerText || timeEl.getAttribute('aria-label') || '').trim() : '';

            if (author) lastAuthor = author;
            else author = lastAuthor;

            if (timeStr) lastTime = timeStr;
            else timeStr = lastTime;

            const contentEl = mid ? document.getElementById('content-' + mid) : node;
            let text = (contentEl ? contentEl.innerText : node.innerText) || '';
            text = text.trim();

            // 1. File attachments
            const attachEl = mid ? document.getElementById('attachments-' + mid) : node.querySelector('[id^="attachments-"], [data-tid*="attachment"]');
            const attachments = [];
            if (attachEl) {
                const fileItems = Array.from(attachEl.querySelectorAll('[aria-label], [title]'));
                const seenFiles = new Set();
                fileItems.forEach(fi => {
                    const fname = (fi.getAttribute('title') || fi.getAttribute('aria-label') || fi.innerText || '').trim();
                    if (fname && /\\.[a-zA-Z0-9]{2,5}$/.test(fname) && !seenFiles.has(fname) && fname.length < 150) {
                        seenFiles.add(fname);
                        const ext = fname.split('.').pop().toLowerCase();
                        attachments.push({
                            type: 'file',
                            name: fname,
                            ext: ext
                        });
                    }
                });

                if (!attachments.length && attachEl.innerText.trim()) {
                    const fallbackName = attachEl.innerText.trim().split('\\n')[0].slice(0, 100);
                    if (fallbackName) {
                        attachments.push({
                            type: 'file',
                            name: fallbackName,
                            ext: fallbackName.includes('.') ? fallbackName.split('.').pop().toLowerCase() : 'file'
                        });
                    }
                }
            }

            // 2. Inline images
            const imgNodes = Array.from(node.querySelectorAll('img')).filter(img => {
                const src = img.getAttribute('src') || '';
                return !src.includes('/emoticons/') && !src.includes('personal-expressions');
            });
            imgNodes.forEach((img, idx) => {
                attachments.push({
                    type: 'image',
                    name: img.getAttribute('alt') || img.getAttribute('aria-label') || ('Gambar ' + (idx + 1)),
                    src: img.getAttribute('src') || ''
                });
            });

            // 3. Links
            const linkNodes = Array.from(node.querySelectorAll('a[href]'));
            const links = [];
            linkNodes.forEach(a => {
                const href = a.getAttribute('href') || '';
                const lText = (a.innerText || '').trim();
                if (href && !href.startsWith('javascript:') && !href.startsWith('#')) {
                    links.push({
                        text: lText || href,
                        url: href
                    });
                }
            });

            if (text && text.length > 1 && !text.includes('Type a message') && !text.includes('Ketik pesan')) {
                msgs.push({
                    mid: mid,
                    author: author,
                    time: timeStr,
                    text: text,
                    attachments: attachments,
                    links: links
                });
            }
        });
        return msgs.slice(-40);
    })()
    """
    res_msgs = _send_cdp_command("Runtime.evaluate", {"expression": extract_js, "returnByValue": True}, timeout=5.0)
    msgs = []
    if res_msgs and "result" in res_msgs and "result" in res_msgs["result"] and "value" in res_msgs["result"]["result"]:
        msgs = res_msgs["result"]["result"]["value"] or []

    return {"success": True, "title": title, "messages_count": len(msgs), "messages": msgs}


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Session & Rolling Context Helpers
# ---------------------------------------------------------------------------
def teams_get_session_info() -> dict:
    """Calculates the current workday session tag based on hour of day."""
    now = time.localtime()
    hour = now.tm_hour
    date_str = time.strftime("%Y-%m-%d", now)
    time_str = time.strftime("%H:%M", now)
    weekday = now.tm_wday  # 0=Mon, 4=Fri, 5=Sat, 6=Sun
    day_names = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
    day_name = day_names[weekday]

    if hour < 10:
        session_name = "Sesi Pagi (08:00 WIB)"
        icon = "🌅"
        code = "morning"
    elif hour < 14:
        session_name = "Sesi Siang (11:00 WIB)"
        icon = "☀️"
        code = "midday"
    else:
        session_name = "Sesi Sore (17:00 WIB)"
        icon = "🌆"
        code = "afternoon"

    return {
        "date_str": date_str,
        "time_str": time_str,
        "day_name": day_name,
        "session_tag": f"{icon} {session_name}",
        "session_code": code,
        "is_workday": weekday < 5,
        "display_header": f"{day_name}, {date_str} &bull; {session_name}"
    }


# ---------------------------------------------------------------------------
# Daily & Scheduled AI Analyzer (Gemini via 9Router with Continuous Memory)
# ---------------------------------------------------------------------------
def teams_run_analysis() -> dict:
    """Gathers all Teams chats (Group & Personal / PC), incorporates the previous
    session's digest as rolling context, and synthesizes an AI analysis partitioned
    by project with status tracking.
    """
    if not teams_ensure_logged_in():
        return {
            "status": "error",
            "message": "Teams belum login dan sesi cadangan gagal dipulihkan. Buka Remote Browser di Dashboard untuk menyetujui login akun Microsoft terlebih dahulu.",
        }

    session_info = teams_get_session_info()
    logger.info(f"Starting Teams analysis for {session_info['session_tag']} ({session_info['date_str']})...")

    # 1. Fetch Previous Digest for Rolling Continuous Context
    prev_record = teams_get_latest_digest()
    prev_context_str = "Belum ada rangkuman sebelumnya (Ini adalah sesi inisialisasi / rangkuman pertama)."
    prev_summary_brief = ""
    if prev_record and prev_record.get("digest_json"):
        try:
            prev_d = json.loads(prev_record["digest_json"])
            p_date = prev_record.get("date_str") or prev_d.get("date", "-")
            p_tag = prev_record.get("session_tag") or prev_d.get("session_tag", "")
            p_narrative = prev_d.get("briefing_narrative", "-")
            prev_summary_brief = p_narrative

            p_projects = prev_d.get("projects", [])
            p_proj_lines = []
            for p in p_projects:
                p_name = p.get("project_name", "Proyek")
                p_stat = p.get("status", "on_track")
                p_sum = p.get("summary", "")[:150]
                p_proj_lines.append(f"- [{p_stat.upper()}] {p_name} ({p.get('chat_source', '-')}): {p_sum}")

            p_incidents = prev_d.get("global_incidents") or prev_d.get("incidents", [])
            p_inc_lines = [f"- [{i.get('severity', 'med').upper()}] {i.get('title')}: {i.get('details', '')[:120]}" for i in p_incidents[:5]]

            p_tasks = prev_d.get("global_tasks") or prev_d.get("tasks", [])
            p_task_lines = [f"- {t.get('task')} (Assignee: {t.get('assignee', '-')}, Due: {t.get('due', '-')})" for t in p_tasks[:6]]

            prev_context_str = (
                f"Waktu Sesi Rangkuman Sebelumnya: {p_date} {p_tag}\n"
                f"Ringkasan Eksekutif Sesi Lalu:\n{p_narrative}\n\n"
                f"Status Proyek Sesi Lalu:\n" + ("\n".join(p_proj_lines) if p_proj_lines else "- Tidak ada proyek tercatat") + "\n\n"
                f"Kendala/Isu Terbuka Sesi Lalu:\n" + ("\n".join(p_inc_lines) if p_inc_lines else "- Tidak ada insiden terbuka") + "\n\n"
                f"Action Items/Tugas Sesi Lalu:\n" + ("\n".join(p_task_lines) if p_task_lines else "- Tidak ada tugas tercatat")
            )
        except Exception as pe:
            logger.warning(f"Failed to parse previous digest JSON: {pe}")

    # 2. Harvest chats across Teams (both Group and Personal Chats)
    raw_snapshot = teams_harvest_all_chats(max_chats=15, max_messages=25)
    harvested_chats = []
    total_raw_msgs = 0

    if raw_snapshot.get("success") and raw_snapshot.get("chats"):
        harvested_chats = raw_snapshot["chats"]
        total_raw_msgs = raw_snapshot.get("total_messages", 0)
    else:
        logger.warning("Harvest all chats failed or returned empty. Falling back to single active chat...")
        fallback_data = teams_harvest_messages()
        if fallback_data:
            fb = fallback_data[0]
            active_t = fb.get("active_title", "Percakapan Aktif")
            msgs = fb.get("active_messages", [])
            harvested_chats = [{
                "title": active_t,
                "type": "group" if any(w in active_t.lower() for w in ["sprint", "retro", "team", "qa", "dev", ","]) else "personal",
                "snippet": "",
                "messages_count": len(msgs),
                "messages": msgs
            }]
            total_raw_msgs = len(msgs)

    if not harvested_chats:
        return {
            "status": "error",
            "message": "Gagal membaca obrolan Teams (tidak ada data DOM yang ditemukan).",
        }

    # 3. Format Corpus partitioned into Group Chats vs Personal Chats (PC)
    group_chats = [c for c in harvested_chats if c.get("type") == "group"]
    personal_chats = [c for c in harvested_chats if c.get("type") != "group"]

    text_corpus = f"=== INFORMASI SESI SAAT INI ===\n"
    text_corpus += f"Jadwal Sesi: {session_info['session_tag']}\n"
    text_corpus += f"Tanggal & Jam: {session_info['date_str']} {session_info['time_str']} WIB ({session_info['day_name']})\n"
    text_corpus += f"Total Obrolan Dibaca: {len(harvested_chats)} ({len(group_chats)} Grup, {len(personal_chats)} Personal Chat / PC)\n\n"

    text_corpus += f"=== KONTEKS RANGKUMAN SESI SEBELUMNYA (CONTINUOUS DIGEST MEMORY) ===\n"
    text_corpus += f"{prev_context_str}\n\n"

    text_corpus += f"=== OBROLAN GRUP & CHANNEL (GROUP CHATS) ===\n"
    if group_chats:
        for idx, gc in enumerate(group_chats, 1):
            text_corpus += f"\n[GRUP #{idx}: {gc.get('title')}]\n"
            msgs = gc.get("messages", [])
            if msgs:
                for m in msgs[-20:]:
                    text_corpus += f"  [{m.get('time', '')}] {m.get('author', 'User')}: {m.get('text', '')}\n"
            else:
                text_corpus += f"  (Snippet: {gc.get('snippet', 'Tidak ada pesan baru')})\n"
    else:
        text_corpus += "(Tidak ada grup chat yang terbaca)\n"

    text_corpus += f"\n=== OBROLAN PERSONAL (1:1 / PERSONAL CHAT / PC) ===\n"
    if personal_chats:
        for idx, pc in enumerate(personal_chats, 1):
            text_corpus += f"\n[PC #{idx}: {pc.get('title')}]\n"
            msgs = pc.get("messages", [])
            if msgs:
                for m in msgs[-20:]:
                    text_corpus += f"  [{m.get('time', '')}] {m.get('author', 'User')}: {m.get('text', '')}\n"
            else:
                text_corpus += f"  (Snippet: {pc.get('snippet', 'Tidak ada pesan baru')})\n"
    else:
        text_corpus += "(Tidak ada personal chat yang terbaca)\n"

    cfg = teams_get_config()
    sprint_name = cfg.get("sprint_name") or "Sprint Berjalan"
    target_channels = cfg.get("target_channels") or ""
    sprint_members = cfg.get("sprint_members") or cfg.get("target_chats") or ""

    system_prompt = (
        "Anda adalah AI Senior Intelligence & Operational Analyst Westpro yang bertugas menganalisis seluruh "
        "komunikasi Microsoft Teams (Grup Chat maupun Personal Chat/PC) untuk Aris (User).\n\n"
        "JADWAL RANGKUMAN:\n"
        "Sistem berjalan 3x sehari pada hari kerja: Jam 08:00 (Sesi Pagi), Jam 11:00 (Sesi Siang), dan Jam 17:00 (Sesi Sore).\n\n"
        "TARGET & FOKUS UTAMA ANALISIS:\n"
        f"1. SPRINT BERJALAN ARIS ({sprint_name}):\n"
        f"   - Analisis difokuskan pada sprint yang sedang aktif berjalan bagi Aris ({sprint_name}).\n"
        f"   - Lacak tiket Jira/SPEctrum, user story, task dev/QA, commit, bug fixing, testing, dan deployment yang dikerjakan oleh Aris maupun tim di channel terkait ({target_channels}).\n"
        f"2. PERSONAL CHAT (PC) DENGAN ORANG-ORANG YANG TASK-NYA ADA DI SPRINT BERJALAN:\n"
        f"   - Prioritaskan dan gali obrolan 1:1 (Personal Chat / PC) secara spesifik dengan rekan kerja yang memegang task di sprint ini: [{sprint_members}].\n"
        f"   - Identifikasi status pengerjaan task masing-masing orang: sedang dikerjakan (in progress), butuh review/QA handoff, selesai, atau terkendala (blocked).\n"
        f"   - Lacak ketergantungan dan sinergi antara pekerjaan Aris dan pekerjaan masing-masing rekan tim sprint.\n"
        f"3. FILTER OBROLAN DI LUAR SPRINT:\n"
        f"   - Obrolan di luar konteks sprint (obrolan santai atau proyek di luar sprint aktif) difilter/diabaikan agar laporan padat, terarah, dan actionable.\n\n"
        "PRINSIP UTAMA ANALISIS:\n"
        "1. CONTINUOUS CONTEXT (MEMORI BERJALAN):\n"
        "   - Pelajari 'KONTEKS RANGKUMAN SESI SEBELUMNYA' di atas.\n"
        "   - Bandingkan dengan data chat saat ini. Evaluasi apa saja isu/tugas sebelumnya yang SUDAH SELESAI/TERATASI.\n"
        "   - Evaluasi apa saja perkembangan atau progres baru yang terjadi sejak sesi sebelumnya.\n"
        "   - Evaluasi apa saja isu atau blocker baru yang muncul di sesi ini.\n"
        "2. PARTISI & RANGKUM PER PROYEK (GROUP & PC):\n"
        "   - Kelompokkan temuan berdasarkan PROYEK/INISIATIF terkait sprint berjalan.\n"
        "   - Untuk setiap proyek, nyatakan asal komunikasinya apakah dari Grup Chat atau Personal Chat (PC) atau keduanya.\n"
        "   - Tentukan status proyek: 'on_track' (hijau/lancar), 'attention' (kuning/butuh pantauan), atau 'blocked' (merah/terkendala).\n"
        "3. FORMAT OUTPUT:\n"
        "   - KELUARKAN HANYA JSON VALID MURNI (tanpa markdown backticks, tanpa text lain):\n"
        "{\n"
        f'  "date": "{session_info["date_str"]}",\n'
        f'  "session_tag": "{session_info["session_tag"]}",\n'
        f'  "sprint_name": "{sprint_name}",\n'
        '  "briefing_narrative": "Ringkasan eksekutif 2-3 paragraf ramah, profesional, dan komprehensif mengenai situasi operasional sprint hari ini serta perbandingannya dengan sesi sebelumnya.",\n'
        '  "progress_comparison": {\n'
        '    "resolved_since_last_session": ["Isu/task A berhasil diselesaikan...", "..."],\n'
        '    "new_blockers_or_issues": ["Kendala baru B muncul di channel...", "..."],\n'
        '    "key_milestones_reached": ["Fitur X lolos QA...", "..."]\n'
        '  },\n'
        '  "sprint_team_tasks": [\n'
        '    {\n'
        '      "member_name": "Nama Rekan / Aris",\n'
        '      "task_desc": "Ringkasan task sprint yang dikerjakan...",\n'
        '      "status": "in_progress|done|blocked|need_review",\n'
        '      "source_chat": "PC Rekan / Grup Sprint",\n'
        '      "notes": "Update status/kendala dari obrolan..."\n'
        '    }\n'
        '  ],\n'
        '  "projects": [\n'
        '    {\n'
        '      "project_name": "Nama Proyek / Fitur",\n'
        '      "chat_source": "Nama channel / nama rekan PC",\n'
        '      "chat_type": "group|personal|mixed",\n'
        '      "status": "on_track|attention|blocked",\n'
        '      "status_label": "ON TRACK|PERLU PERHATIAN|TERKENDALA",\n'
        '      "summary": "Rangkuman lengkap progres dan kondisi terkini proyek ini...",\n'
        '      "updates_since_last_digest": "Perkembangan spesifik sejak sesi sebelumnya...",\n'
        '      "key_discussions": ["Poin penting 1", "Poin penting 2"],\n'
        '      "tasks": [\n'
        '        {"task": "...", "assignee": "...", "due": "...", "priority": "high|medium|low"}\n'
        '      ],\n'
        '      "incidents": [\n'
        '        {"title": "...", "severity": "high|medium|low", "details": "..."}\n'
        '      ]\n'
        '    }\n'
        '  ],\n'
        '  "global_incidents": [\n'
        '    {"title": "...", "severity": "high|medium|low", "project": "...", "channel": "...", "details": "...", "time": "..."}\n'
        '  ],\n'
        '  "global_tasks": [\n'
        '    {"task": "...", "project": "...", "assignee": "...", "due": "...", "priority": "high|medium|low"}\n'
        '  ],\n'
        '  "meetings": [\n'
        '    {"title": "...", "when": "...", "project": "...", "participants": "...", "channel": "..."}\n'
        '  ]\n'
        "}"
    )

    candidate_models = [AI_MODEL, "gemini/gemini-2.5-flash", "gemini/gemini-1.5-flash", "gemini/gemini-3.8-flash"]
    candidate_models = list(dict.fromkeys(candidate_models))

    digest_data = None
    last_err = None

    for m in candidate_models:
        req_body = {
            "model": m,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Berikut adalah data obrolan Teams terbaru untuk dianalisis:\n\n{text_corpus}"},
            ],
            "temperature": 0.2,
            "stream": False,
        }

        try:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {AI_ROUTER_KEY}",
            }
            req = urllib.request.Request(
                AI_ROUTER_URL,
                data=json.dumps(req_body).encode("utf-8"),
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                ai_resp = json.loads(resp.read().decode("utf-8"))
                content = ai_resp["choices"][0]["message"]["content"].strip()
                if content.startswith("```"):
                    content = re.sub(r"^```[a-zA-Z]*\n?", "", content)
                    content = re.sub(r"\n?```$", "", content)

                digest_data = json.loads(content)
                logger.info(f"AI Teams analysis successfully generated using model: {m}")
                break
        except Exception as e:
            last_err = e
            logger.warning(f"AI analysis with model {m} failed: {e}. Trying next fallback...")

    if not digest_data:
        logger.error(f"All candidate models failed: {last_err}")
        digest_data = {
            "date": session_info["date_str"],
            "session_tag": session_info["session_tag"],
            "sprint_name": sprint_name,
            "briefing_narrative": f"Analisis AI gagal diproses: {last_err}. Pesan mentah tetap tersimpan.",
            "progress_comparison": {
                "resolved_since_last_session": [],
                "new_blockers_or_issues": [str(last_err)],
                "key_milestones_reached": []
            },
            "projects": [
                {
                    "project_name": "Microsoft Teams Raw Scrape",
                    "chat_source": "Semua Obrolan",
                    "chat_type": "mixed",
                    "status": "attention",
                    "status_label": "PERLU PERHATIAN",
                    "summary": f"Berhasil membaca {len(harvested_chats)} obrolan dengan total {total_raw_msgs} pesan.",
                    "updates_since_last_digest": "Koleksi data berhasil disimpan.",
                    "key_discussions": [c.get("title") for c in harvested_chats[:5]],
                    "tasks": [],
                    "incidents": []
                }
            ],
            "global_incidents": [],
            "global_tasks": [],
            "meetings": []
        }

    # Ensure backwards-compatibility keys
    digest_data.setdefault("session_tag", session_info["session_tag"])
    digest_data.setdefault("date", session_info["date_str"])
    digest_data["incidents"] = digest_data.get("global_incidents") or digest_data.get("incidents", [])
    digest_data["tasks"] = digest_data.get("global_tasks") or digest_data.get("tasks", [])
    if "channel_summaries" not in digest_data:
        digest_data["channel_summaries"] = [
            {"channel": p.get("project_name", "Proyek"), "summary": p.get("summary", ""), "key_points": p.get("key_discussions", [])}
            for p in digest_data.get("projects", [])
        ]

    # Generate modern HTML briefing
    date_str = digest_data.get("date", session_info["date_str"])
    session_tag = digest_data.get("session_tag", session_info["session_tag"])
    briefing_html = _generate_html_briefing(digest_data)

    # Save to SQLite database
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO teams_digests (date_str, raw_messages_count, status, digest_json, briefing_html, session_tag)
                VALUES (?, ?, 'success', ?, ?, ?)
            """, (date_str, total_raw_msgs, json.dumps(digest_data), briefing_html, session_tag))
            conn.commit()
            digest_id = cur.lastrowid
            logger.info(f"Teams digest #{digest_id} saved successfully to teams.db.")
    except Exception as e:
        logger.error(f"Failed to save digest to DB: {e}")
        digest_id = None

    return {
        "status": "success",
        "digest_id": digest_id,
        "date": date_str,
        "session_tag": session_tag,
        "digest": digest_data,
        "html": briefing_html,
        "raw_count": total_raw_msgs,
        "total_chats": len(harvested_chats)
    }


def _generate_html_briefing(d: dict) -> str:
    """Generates a modern styled HTML fragment for rendering in the Westpro dashboard.
    Features:
    - Workday Session Badge (08:00 Pagi, 11:00 Siang, 17:00 Sore)
    - Continuous memory diff (Resolved since last session vs New blockers)
    - Partitioned Project Cards (Group & Personal Chat / PC breakdown)
    - Global Incidents, Action Items, and Meeting Syncs
    """
    session_tag = d.get("session_tag") or "Sesi Analisis Teams"
    sprint_name = d.get("sprint_name") or "Sprint Berjalan"
    date_str = d.get("date", "")
    narrative = d.get("briefing_narrative", "")
    comparison = d.get("progress_comparison", {})
    resolved_items = comparison.get("resolved_since_last_session", [])
    new_blockers = comparison.get("new_blockers_or_issues", [])
    milestones = comparison.get("key_milestones_reached", [])
    projects = d.get("projects", [])
    incidents = d.get("global_incidents") or d.get("incidents", [])
    tasks = d.get("global_tasks") or d.get("tasks", [])
    meetings = d.get("meetings", [])

    html = []

    # 1. Header Card with Session Badge
    html.append('<div class="teams-digest-header" style="background:#18181b; border:1px solid #27272a; border-radius:10px; padding:1.25rem; margin-bottom:1.5rem; box-shadow:0 4px 12px rgba(0,0,0,0.3); position:relative; overflow:hidden;">')
    html.append('<div style="position:absolute; top:0; left:0; width:4px; height:100%; background:linear-gradient(to bottom, #a855f7, #6366f1);"></div>')
    html.append('<div style="display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:0.75rem; margin-bottom:0.85rem;">')
    html.append('  <div style="display:flex; align-items:center; gap:0.5rem;">')
    html.append(f'    <span style="background:rgba(124,58,237,0.25); color:#c084fc; border:1px solid rgba(168,85,247,0.4); font-size:0.78rem; font-weight:700; padding:0.25rem 0.75rem; border-radius:6px; letter-spacing:0.3px;">{session_tag}</span>')
    html.append(f'    <span style="background:rgba(59,130,246,0.18); color:#60a5fa; border:1px solid rgba(59,130,246,0.3); font-size:0.75rem; font-weight:600; padding:0.25rem 0.65rem; border-radius:6px;">🎯 {sprint_name.upper()}</span>')
    html.append('  </div>')
    html.append(f'  <div style="font-size:0.78rem; color:#94a3b8; display:flex; align-items:center; gap:0.35rem;">📅 {date_str} &bull; 🤖 Gemini AI</div>')
    html.append('</div>')
    html.append(f'<p style="color:#e2e8f0; font-size:0.92rem; line-height:1.65; margin:0; font-weight:400;">{narrative}</p>')
    html.append('</div>')

    # 2. Continuous Memory Diff (Perkembangan Sejak Sesi Sebelumnya)
    if resolved_items or new_blockers or milestones:
        html.append('<div style="background:#131316; border:1px solid #27272a; border-radius:10px; padding:1rem 1.25rem; margin-bottom:1.5rem;">')
        html.append('<div style="font-size:0.8rem; font-weight:700; color:#cbd5e1; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:0.75rem; display:flex; align-items:center; gap:0.4rem;">')
        html.append('<span>🔄 PERKEMBANGAN SEJAK SESI SEBELUMNYA</span>')
        html.append('</div>')
        html.append('<div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(240px, 1fr)); gap:0.75rem;">')

        # Resolved
        if resolved_items:
            html.append('<div style="background:#092e20; border:1px solid #059669; border-radius:8px; padding:0.75rem;">')
            html.append('<div style="color:#34d399; font-size:0.76rem; font-weight:700; margin-bottom:0.4rem; display:flex; align-items:center; gap:0.3rem;">✅ TERATASI / SELESAI</div>')
            html.append('<ul style="margin:0; padding-left:1.1rem; color:#a7f3d0; font-size:0.78rem; line-height:1.5;">')
            for r in resolved_items:
                html.append(f'<li>{r}</li>')
            html.append('</ul></div>')

        # New Blockers
        if new_blockers:
            html.append('<div style="background:#2e0b0e; border:1px solid #dc2626; border-radius:8px; padding:0.75rem;">')
            html.append('<div style="color:#f87171; font-size:0.76rem; font-weight:700; margin-bottom:0.4rem; display:flex; align-items:center; gap:0.3rem;">⚠️ KENDALA / ISU BARU</div>')
            html.append('<ul style="margin:0; padding-left:1.1rem; color:#fecaca; font-size:0.78rem; line-height:1.5;">')
            for b in new_blockers:
                html.append(f'<li>{b}</li>')
            html.append('</ul></div>')

        # Milestones
        if milestones:
            html.append('<div style="background:#1e1b4b; border:1px solid #6366f1; border-radius:8px; padding:0.75rem;">')
            html.append('<div style="color:#818cf8; font-size:0.76rem; font-weight:700; margin-bottom:0.4rem; display:flex; align-items:center; gap:0.3rem;">🚀 CAPAIAN & PROGRES</div>')
            html.append('<ul style="margin:0; padding-left:1.1rem; color:#c7d2fe; font-size:0.78rem; line-height:1.5;">')
            for m in milestones:
                html.append(f'<li>{m}</li>')
            html.append('</ul></div>')

        html.append('</div></div>')

    # 2b. Sprint Team Tasks from PC & Groups
    sprint_team_tasks = d.get("sprint_team_tasks", [])
    if sprint_team_tasks:
        html.append('<div style="background:#18181b; border:1px solid #27272a; border-radius:10px; padding:1rem 1.25rem; margin-bottom:1.5rem;">')
        html.append('<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.75rem;">')
        html.append('  <div style="font-size:0.8rem; font-weight:700; color:#38bdf8; text-transform:uppercase; letter-spacing:0.5px; display:flex; align-items:center; gap:0.4rem;">')
        html.append(f'    <span>👥 KOORDINASI TASK TIM & PC SPRINT ({sprint_name})</span>')
        html.append('  </div>')
        html.append(f'  <span style="font-size:0.72rem; color:#94a3b8;">{len(sprint_team_tasks)} rekan terpantau</span>')
        html.append('</div>')
        html.append('<div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); gap:0.65rem;">')
        for st in sprint_team_tasks:
            m_stat = (st.get("status") or "in_progress").lower()
            if m_stat in ("done", "selesai"):
                st_color = "#34d399"
                st_bg = "rgba(16,185,129,0.15)"
                st_badge = "✓ SELESAI"
            elif m_stat in ("blocked", "terkendala"):
                st_color = "#f87171"
                st_bg = "rgba(239,68,68,0.15)"
                st_badge = "⚠️ KENDALA"
            elif m_stat in ("need_review", "review"):
                st_color = "#fbbf24"
                st_bg = "rgba(245,158,11,0.15)"
                st_badge = "👀 REVIEW"
            else:
                st_color = "#60a5fa"
                st_bg = "rgba(59,130,246,0.15)"
                st_badge = "⏳ PROGRES"
            m_name = st.get("member_name", "Rekan Tim")
            m_desc = st.get("task_desc", "-")
            m_src = st.get("source_chat", "PC")
            m_notes = st.get("notes", "")
            html.append('<div style="background:#131316; border:1px solid #27272a; border-radius:8px; padding:0.75rem;">')
            html.append('  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.35rem;">')
            html.append(f'    <strong style="color:#f1f5f9; font-size:0.84rem;">👤 {m_name}</strong>')
            html.append(f'    <span style="background:{st_bg}; color:{st_color}; border:1px solid {st_color}44; font-size:0.68rem; font-weight:700; padding:0.12rem 0.45rem; border-radius:4px;">{st_badge}</span>')
            html.append('  </div>')
            html.append(f'  <div style="color:#cbd5e1; font-size:0.78rem; line-height:1.45; margin-bottom:0.35rem;">{m_desc}</div>')
            if m_notes:
                html.append(f'  <div style="color:#94a3b8; font-size:0.72rem; line-height:1.4; border-top:1px dashed #27272a; padding-top:0.3rem;">💬 <em>{m_notes}</em> ({m_src})</div>')
            html.append('</div>')
        html.append('</div></div>')

    # 3. Partitioned Projects (Separated by Project: Group & Personal Chat / PC)
    html.append('<div class="teams-digest-section" style="margin-bottom:1.5rem;">')
    html.append('<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.85rem;">')
    html.append('  <div style="display:flex; align-items:center; gap:0.5rem;">')
    html.append('    <span style="background:rgba(16,185,129,0.2); color:#34d399; border:1px solid rgba(16,185,129,0.3); font-size:0.75rem; font-weight:700; padding:0.25rem 0.65rem; border-radius:6px;">📁 RANGKUMAN & ANALISIS PER PROYEK</span>')
    html.append(f'    <span style="color:#94a3b8; font-size:0.8rem;">({len(projects)} proyek dianalisis)</span>')
    html.append('  </div>')
    html.append('</div>')

    if projects:
        html.append('<div style="display:flex; flex-direction:column; gap:0.85rem;">')
        for p in projects:
            p_stat = (p.get("status") or "on_track").lower()
            if p_stat == "blocked":
                accent_color = "#ef4444"
                badge_bg = "rgba(239,68,68,0.2)"
                badge_fg = "#f87171"
                status_txt = "🔴 TERKENDALA"
            elif p_stat == "attention":
                accent_color = "#f59e0b"
                badge_bg = "rgba(245,158,11,0.2)"
                badge_fg = "#fbbf24"
                status_txt = "🟡 PERLU PERHATIAN"
            else:
                accent_color = "#10b981"
                badge_bg = "rgba(16,185,129,0.2)"
                badge_fg = "#34d399"
                status_txt = "🟢 ON TRACK"

            c_type = (p.get("chat_type") or "group").lower()
            if c_type == "personal":
                type_badge = f'<span style="background:rgba(14,165,233,0.18); color:#38bdf8; border:1px solid rgba(14,165,233,0.3); font-size:0.72rem; padding:0.15rem 0.55rem; border-radius:4px; font-weight:600;">👤 PC / 1:1: {p.get("chat_source", "")}</span>'
            elif c_type == "mixed":
                type_badge = f'<span style="background:rgba(168,85,247,0.18); color:#c084fc; border:1px solid rgba(168,85,247,0.3); font-size:0.72rem; padding:0.15rem 0.55rem; border-radius:4px; font-weight:600;">👥👤 Grup & PC: {p.get("chat_source", "")}</span>'
            else:
                type_badge = f'<span style="background:rgba(99,102,241,0.18); color:#818cf8; border:1px solid rgba(99,102,241,0.3); font-size:0.72rem; padding:0.15rem 0.55rem; border-radius:4px; font-weight:600;">👥 Grup: {p.get("chat_source", "")}</span>'

            html.append(f'<div style="background:#18181b; border:1px solid #27272a; border-left:4px solid {accent_color}; border-radius:8px; padding:1rem 1.15rem;">')
            html.append('  <div style="display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:0.5rem; margin-bottom:0.6rem;">')
            html.append('    <div style="display:flex; align-items:center; gap:0.5rem; flex-wrap:wrap;">')
            html.append(f'      <strong style="color:#f8fafc; font-size:0.95rem; font-weight:700;">{p.get("project_name", "Proyek")}</strong>')
            html.append(f'      {type_badge}')
            html.append('    </div>')
            html.append(f'    <span style="background:{badge_bg}; color:{badge_fg}; border:1px solid {accent_color}55; font-size:0.72rem; font-weight:700; padding:0.2rem 0.55rem; border-radius:4px;">{status_txt}</span>')
            html.append('  </div>')

            # Summary
            html.append(f'  <p style="color:#cbd5e1; font-size:0.85rem; line-height:1.55; margin:0 0 0.65rem 0;">{p.get("summary", "")}</p>')

            # Updates since last digest
            if p.get("updates_since_last_digest"):
                html.append(f'<div style="background:rgba(255,255,255,0.03); border:1px dashed #3f3f46; border-radius:6px; padding:0.5rem 0.75rem; margin-bottom:0.65rem; font-size:0.8rem; color:#e4e4e7;">')
                html.append(f'<span style="color:#a78bfa; font-weight:600;">⚡ Update Sesi Ini:</span> {p.get("updates_since_last_digest")}')
                html.append('</div>')

            # Key discussions
            discussions = p.get("key_discussions", [])
            if discussions:
                html.append('<div style="margin-bottom:0.5rem;">')
                html.append('  <div style="color:#a1a1aa; font-size:0.74rem; font-weight:600; text-transform:uppercase; margin-bottom:0.25rem;">💬 Pembahasan Utama:</div>')
                html.append('  <ul style="margin:0; padding-left:1.15rem; color:#94a3b8; font-size:0.8rem; line-height:1.45;">')
                for d_item in discussions:
                    html.append(f'<li>{d_item}</li>')
                html.append('  </ul>')
                html.append('</div>')

            # Project tasks or incidents
            p_tasks = p.get("tasks", [])
            if p_tasks:
                html.append('<div style="margin-top:0.5rem; display:flex; flex-direction:column; gap:0.35rem;">')
                for pt in p_tasks:
                    prio_color = "#ef4444" if pt.get("priority") == "high" else "#60a5fa"
                    html.append(f'<div style="background:#27272a; padding:0.4rem 0.65rem; border-radius:4px; font-size:0.78rem; display:flex; justify-content:space-between; align-items:center;">')
                    html.append(f'  <span style="color:#f1f5f9;">📌 {pt.get("task")}</span>')
                    html.append(f'  <span style="color:{prio_color}; font-size:0.72rem;">PIC: {pt.get("assignee", "-")}</span>')
                    html.append('</div>')
                html.append('</div>')

            html.append('</div>')
        html.append('</div>')
    else:
        html.append('<div style="color:#71717a; font-size:0.82rem; font-style:italic;">Belum ada proyek yang teridentifikasi dalam sesi ini.</div>')
    html.append('</div>')

    # 4. Global Incidents & Blockers
    html.append('<div class="teams-digest-section" style="margin-bottom:1.5rem;">')
    html.append('<div style="display:flex; align-items:center; gap:0.5rem; margin-bottom:0.75rem;">')
    html.append('  <span style="background:rgba(239,68,68,0.2); color:#ef4444; border:1px solid rgba(239,68,68,0.3); font-size:0.75rem; font-weight:700; padding:0.2rem 0.6rem; border-radius:6px;">🚨 INSIDEN & KENDALA SISTEM</span>')
    html.append(f'  <span style="color:#94a3b8; font-size:0.8rem;">({len(incidents)} terdeteksi)</span>')
    html.append('</div>')
    if incidents:
        html.append('<div style="display:flex; flex-direction:column; gap:0.5rem;">')
        for inc in incidents:
            sev_color = "#ef4444" if inc.get("severity") == "high" else "#f59e0b"
            html.append(f'<div style="background:#18181b; border-left:4px solid {sev_color}; padding:0.75rem 1rem; border-radius:6px; border:1px solid #27272a; border-left-width:4px;">')
            html.append('  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.25rem;">')
            html.append(f'    <strong style="color:#f87171; font-size:0.88rem;">{inc.get("title", "Insiden")}</strong>')
            html.append(f'    <span style="font-size:0.72rem; color:#a1a1aa;">{inc.get("time", "")} &bull; {inc.get("channel", "")}</span>')
            html.append('  </div>')
            html.append(f'  <p style="color:#cbd5e1; font-size:0.82rem; margin:0; line-height:1.5;">{inc.get("details", "")}</p>')
            html.append('</div>')
        html.append('</div>')
    else:
        html.append('<div style="color:#71717a; font-size:0.82rem; font-style:italic;">Tidak ada laporan insiden kritis atau bug urgent yang terdeteksi.</div>')
    html.append('</div>')

    # 5. Global Action Items & Tasks
    html.append('<div class="teams-digest-section" style="margin-bottom:1.5rem;">')
    html.append('<div style="display:flex; align-items:center; gap:0.5rem; margin-bottom:0.75rem;">')
    html.append('  <span style="background:rgba(59,130,246,0.2); color:#60a5fa; border:1px solid rgba(59,130,246,0.3); font-size:0.75rem; font-weight:700; padding:0.2rem 0.6rem; border-radius:6px;">📌 TUGAS & ACTION ITEMS TIM</span>')
    html.append(f'  <span style="color:#94a3b8; font-size:0.8rem;">({len(tasks)} item)</span>')
    html.append('</div>')
    if tasks:
        html.append('<div style="display:flex; flex-direction:column; gap:0.5rem;">')
        for t in tasks:
            html.append('<div style="background:#18181b; padding:0.75rem 1rem; border-radius:6px; border:1px solid #27272a; display:flex; justify-content:space-between; align-items:flex-start;">')
            html.append('  <div>')
            html.append(f'    <div style="color:#f1f5f9; font-size:0.85rem; font-weight:600;">{t.get("task", "")}</div>')
            html.append(f'    <div style="color:#94a3b8; font-size:0.75rem; margin-top:0.25rem;">Channel/PC: <b>{t.get("channel", t.get("project", "-"))}</b> &bull; Assignee: <span style="color:#38bdf8;">{t.get("assignee", "-")}</span></div>')
            html.append('  </div>')
            if t.get("due"):
                html.append(f'  <span style="background:#27272a; color:#fbbf24; font-size:0.72rem; padding:0.15rem 0.5rem; border-radius:4px; white-space:nowrap;">Batas: {t.get("due")}</span>')
            html.append('</div>')
        html.append('</div>')
    else:
        html.append('<div style="color:#71717a; font-size:0.82rem; font-style:italic;">Belum ada tugas atau action item spesifik yang terdata.</div>')
    html.append('</div>')

    # 6. Meetings
    html.append('<div class="teams-digest-section">')
    html.append('<div style="display:flex; align-items:center; gap:0.5rem; margin-bottom:0.75rem;">')
    html.append('  <span style="background:rgba(168,85,247,0.2); color:#c084fc; border:1px solid rgba(168,85,247,0.3); font-size:0.75rem; font-weight:700; padding:0.2rem 0.6rem; border-radius:6px;">📅 AGENDA & MEETING SYNC</span>')
    html.append(f'  <span style="color:#94a3b8; font-size:0.8rem;">({len(meetings)} agenda)</span>')
    html.append('</div>')
    if meetings:
        html.append('<div style="display:flex; flex-direction:column; gap:0.5rem;">')
        for m in meetings:
            html.append('<div style="background:#18181b; padding:0.75rem 1rem; border-radius:6px; border:1px solid #27272a;">')
            html.append('  <div style="display:flex; justify-content:space-between; align-items:center;">')
            html.append(f'    <strong style="color:#e9d5ff; font-size:0.85rem;">{m.get("title", "Meeting")}</strong>')
            html.append(f'    <span style="color:#c084fc; font-size:0.78rem; font-weight:600;">{m.get("when", "")}</span>')
            html.append('  </div>')
            if m.get("participants"):
                html.append(f'  <div style="color:#94a3b8; font-size:0.75rem; margin-top:0.25rem;">Peserta: {m.get("participants")} &bull; Sumber: {m.get("channel", "-")}</div>')
            html.append('</div>')
        html.append('</div>')
    else:
        html.append('<div style="color:#71717a; font-size:0.82rem; font-style:italic;">Tidak ada agenda pertemuan mendesak dalam obrolan terbaru.</div>')
    html.append('</div>')

    return "".join(html)


def teams_get_latest_digest() -> dict | None:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("""
                SELECT * FROM teams_digests ORDER BY id DESC LIMIT 1
            """).fetchone()
            if row:
                return dict(row)
    except Exception as e:
        logger.error(f"Error fetching latest digest: {e}")
    return None


def teams_get_digest_history(limit: int = 15) -> list[dict]:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT id, created_at, date_str, raw_messages_count, status, session_tag
                FROM teams_digests ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Error fetching digest history: {e}")
        return []


def teams_get_digest_by_id(digest_id: int) -> dict | None:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("""
                SELECT * FROM teams_digests WHERE id = ?
            """, (digest_id,)).fetchone()
            if row:
                return dict(row)
    except Exception as e:
        logger.error(f"Error fetching digest {digest_id}: {e}")
    return None


# ---------------------------------------------------------------------------
# CLI Entrypoint for Scheduled Timers & Manual Invocation
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    if "--run-digest" in sys.argv:
        print("[Teams CLI] Starting scheduled/manual AI Digest run...")
        res = teams_run_analysis()
        print(f"[Teams CLI] Result Status: {res.get('status')}")
        if res.get("status") == "success":
            print(f"[Teams CLI] Digest ID: {res.get('digest_id')}")
            print(f"[Teams CLI] Session: {res.get('session_tag')}")
            print(f"[Teams CLI] Total Chats: {res.get('total_chats')} | Raw Msgs: {res.get('raw_count')}")
            briefing = res.get("digest", {}).get("briefing_narrative", "")
            print(f"[Teams CLI] Narrative: {briefing[:200]}...")
        else:
            print(f"[Teams CLI] Error: {res.get('message')}")
    elif "--status" in sys.argv:
        print(json.dumps(teams_get_status(), indent=2, default=str))
    elif "--pull" in sys.argv:
        p = teams_pull_chats(max_retries=3)
        print(json.dumps(p, indent=2, default=str))
    elif "--backup-session" in sys.argv:
        ok = teams_backup_current_session()
        print(f"Backup session result: {ok}")
    elif "--restore-session" in sys.argv:
        ok = teams_restore_session()
        print(f"Restore session result: {ok}")
    elif "--harvest" in sys.argv:
        h = teams_harvest_all_chats()
        print(json.dumps(h, indent=2, default=str))
    else:
        print("Usage: python3 teams_service.py [--run-digest | --pull | --status | --backup-session | --restore-session | --harvest]")

