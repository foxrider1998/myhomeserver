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
TEAMS_URL = "https://teams.microsoft.com"

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
                    briefing_html TEXT
                )
            """)
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
# Daily AI Analyzer (Gemini 3.8 Flash via 9Router)
# ---------------------------------------------------------------------------
def teams_run_analysis() -> dict:
    """Scrapes recent messages and uses 9Router Gemini 3.8 Flash to generate a daily digest."""
    status = teams_get_status()
    if not status.get("logged_in"):
        return {
            "status": "error",
            "message": "Teams belum login. Buka Remote Browser di Dashboard untuk login akun Microsoft terlebih dahulu.",
        }

    harvest_data = teams_harvest_messages()
    if not harvest_data:
        return {
            "status": "error",
            "message": "Gagal membaca obrolan Teams (tidak ada data DOM yang ditemukan).",
        }

    raw_payload = harvest_data[0]
    chat_list = raw_payload.get("chat_list", [])
    active_msgs = raw_payload.get("active_messages", [])
    active_title = raw_payload.get("active_title", "Percakapan")

    cfg = teams_get_config()
    sprint_name = cfg.get("sprint_name") or "Sprint Berjalan"
    target_channels = cfg.get("target_channels") or "Grup tim & sprint"
    target_chats = cfg.get("target_chats") or "Personal chat terkait"

    # Format text for prompt
    text_corpus = f"=== KONTEKS SPRINT: {sprint_name} ===\n"
    text_corpus += f"Target Group Chat: {target_channels}\n"
    text_corpus += f"Target Personal Chat: {target_chats}\n\n"
    text_corpus += f"=== PERCAKAPAN AKTIF: {active_title} ===\n"
    for m in active_msgs:
        text_corpus += f"[{m.get('time', '')}] {m.get('author', 'User')}: {m.get('text', '')}\n"

    text_corpus += "\n=== DAFTAR CHAT / CHANNEL TERAKHIR & SNIPPET ===\n"
    for c in chat_list:
        text_corpus += f"- [{c.get('title', '')}]: {c.get('snippet', '')} (Unread: {c.get('unread')})\n"

    system_prompt = (
        "Anda adalah AI Asisten Analis Operasional Westpro. Anda membaca obrolan & percakapan Microsoft Teams "
        "tim internal untuk memberikan rekap meeting harian & rangkuman perkembangan sprint berjalan kepada user (Aris).\n"
        f"Konteks Aktif: {sprint_name}.\n"
        f"Grup Chat Target: {target_channels}.\n"
        f"Personal Chat Target: {target_chats}.\n\n"
        "Tugas Anda: Ekstrak fakta secara ringkas, akurat, dan profesional ke dalam 4 kategori utama:\n"
        "1. INCIDENTS (🚨 Masalah sistem, bug kritis, issue deployment, server down, error, komplain urgen terkait sprint)\n"
        "2. TASKS (📌 Tugas baru, action item, review pending, follow-up yang perlu dikerjakan user atau tim dalam sprint)\n"
        "3. MEETINGS (📅 Jadwal rapat, sprint sync, standup, demo, release, atau meeting yang disebutkan di chat grup/PC)\n"
        "4. CHANNEL_SUMMARIES (💬 Rangkuman topik inti dari masing-masing channel/obrolan target yang ada)\n\n"
        "OUTPUT HARUS BERUPA JSON VALID SAJA (tanpa pembuka/penutup markdown seperti ```json):\n"
        "{\n"
        '  "date": "YYYY-MM-DD",\n'
        f'  "sprint_name": "{sprint_name}",\n'
        '  "briefing_narrative": "Ringkasan eksekutif 2-3 paragraf ramah dan jelas mengenai situasi sprint dan meeting hari ini.",\n'
        '  "incidents": [\n'
        '    {"title": "...", "severity": "high|medium|low", "channel": "...", "details": "...", "time": "..."}\n'
        "  ],\n"
        '  "tasks": [\n'
        '    {"task": "...", "assignee": "...", "due": "...", "channel": "...", "priority": "high|medium|low"}\n'
        "  ],\n"
        '  "meetings": [\n'
        '    {"title": "...", "when": "...", "participants": "...", "channel": "..."}\n'
        "  ],\n"
        '  "channel_summaries": [\n'
        '    {"channel": "...", "summary": "...", "key_points": ["..."]}\n'
        "  ]\n"
        "}"
    )

    candidate_models = [AI_MODEL, "gemini/gemini-2.5-flash", "gemini/gemini-1.5-flash", "gemini/gemini-3.8-flash"]
    # Deduplicate while preserving order
    candidate_models = list(dict.fromkeys(candidate_models))

    digest_data = None
    last_err = None

    for m in candidate_models:
        req_body = {
            "model": m,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Berikut adalah data pesan & obrolan Teams terkini:\n\n{text_corpus}"},
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
            with urllib.request.urlopen(req, timeout=45) as resp:
                ai_resp = json.loads(resp.read().decode("utf-8"))
                content = ai_resp["choices"][0]["message"]["content"].strip()
                # Clean markdown code block if present
                if content.startswith("```"):
                    content = re.sub(r"^```[a-zA-Z]*\n?", "", content)
                    content = re.sub(r"\n?```$", "", content)

                digest_data = json.loads(content)
                logger.info(f"AI analysis successfully generated using model: {m}")
                break
        except Exception as e:
            last_err = e
            logger.warning(f"AI analysis with model {m} failed: {e}. Trying next fallback...")

    if not digest_data:
        logger.error(f"All candidate models failed: {last_err}")
        # Fallback digest if LLM fails or timeout
        digest_data = {
            "date": time.strftime("%Y-%m-%d"),
            "briefing_narrative": f"Analisis AI gagal diproses: {last_err}. Pesan mentah tersimpan.",
            "incidents": [],
            "tasks": [],
            "meetings": [],
            "channel_summaries": [{"channel": active_title, "summary": f"{len(active_msgs)} pesan terbaca.", "key_points": []}],
        }

    # Save to SQLite database
    date_str = digest_data.get("date", time.strftime("%Y-%m-%d"))
    total_raw = len(active_msgs) + len(chat_list)
    briefing_html = _generate_html_briefing(digest_data)

    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO teams_digests (date_str, raw_messages_count, status, digest_json, briefing_html)
                VALUES (?, ?, 'success', ?, ?)
            """, (date_str, total_raw, json.dumps(digest_data), briefing_html))
            conn.commit()
            digest_id = cur.lastrowid
    except Exception as e:
        logger.error(f"Failed to save digest to DB: {e}")
        digest_id = None

    return {
        "status": "success",
        "digest_id": digest_id,
        "date": date_str,
        "digest": digest_data,
        "html": briefing_html,
        "raw_count": total_raw,
    }


def _generate_html_briefing(d: dict) -> str:
    """Generates a modern styled HTML fragment for rendering in the Westpro dashboard."""
    incidents = d.get("incidents", [])
    tasks = d.get("tasks", [])
    meetings = d.get("meetings", [])
    summaries = d.get("channel_summaries", [])
    narrative = d.get("briefing_narrative", "")

    html = []
    sprint_name = d.get("sprint_name") or "Sprint Berjalan"
    html.append(f'<div class="teams-digest-header" style="margin-bottom:1.25rem;">')
    html.append(f'<div style="display:flex; align-items:center; gap:0.5rem; margin-bottom:0.6rem;">')
    html.append(f'<span style="background:rgba(124,58,237,0.25); color:#a78bfa; border:1px solid rgba(124,58,237,0.4); font-size:0.75rem; font-weight:700; padding:0.2rem 0.65rem; border-radius:6px;">🎯 REKAP {sprint_name.upper()}</span>')
    html.append(f'<span style="font-size:0.75rem; color:#94a3b8;">📅 {d.get("date", "")}</span>')
    html.append('</div>')
    html.append(f'<p style="color:#e2e8f0; font-size:0.92rem; line-height:1.6; margin:0 0 1rem 0;">{narrative}</p>')
    html.append('</div>')

    # 1. Incidents
    html.append('<div class="teams-digest-section" style="margin-bottom:1.5rem;">')
    html.append('<div style="display:flex; align-items:center; gap:0.5rem; margin-bottom:0.75rem;">')
    html.append('<span style="background:rgba(239,68,68,0.2); color:#ef4444; border:1px solid rgba(239,68,68,0.3); font-size:0.75rem; font-weight:700; padding:0.2rem 0.6rem; border-radius:6px;">🚨 INSIDEN & KENDALA</span>')
    html.append(f'<span style="color:#94a3b8; font-size:0.8rem;">({len(incidents)} terdeteksi)</span>')
    html.append('</div>')
    if incidents:
        html.append('<div style="display:flex; flex-direction:column; gap:0.5rem;">')
        for inc in incidents:
            sev_color = "#ef4444" if inc.get("severity") == "high" else "#f59e0b"
            html.append(f'<div style="background:#18181b; border-left:4px solid {sev_color}; padding:0.75rem 1rem; border-radius:6px; border:1px solid #27272a; border-left-width:4px;">')
            html.append(f'<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.25rem;">')
            html.append(f'<strong style="color:#f87171; font-size:0.88rem;">{inc.get("title", "Insiden")}</strong>')
            html.append(f'<span style="font-size:0.72rem; color:#a1a1aa;">{inc.get("time", "")} &bull; {inc.get("channel", "")}</span>')
            html.append('</div>')
            html.append(f'<p style="color:#cbd5e1; font-size:0.82rem; margin:0; line-height:1.5;">{inc.get("details", "")}</p>')
            html.append('</div>')
        html.append('</div>')
    else:
        html.append('<div style="color:#71717a; font-size:0.82rem; font-style:italic;">Tidak ada laporan insiden kritis atau bug urgent yang terdeteksi hari ini.</div>')
    html.append('</div>')

    # 2. Tasks & Action Items
    html.append('<div class="teams-digest-section" style="margin-bottom:1.5rem;">')
    html.append('<div style="display:flex; align-items:center; gap:0.5rem; margin-bottom:0.75rem;">')
    html.append('<span style="background:rgba(59,130,246,0.2); color:#60a5fa; border:1px solid rgba(59,130,246,0.3); font-size:0.75rem; font-weight:700; padding:0.2rem 0.6rem; border-radius:6px;">📌 TUGAS & ACTION ITEMS</span>')
    html.append(f'<span style="color:#94a3b8; font-size:0.8rem;">({len(tasks)} item)</span>')
    html.append('</div>')
    if tasks:
        html.append('<div style="display:flex; flex-direction:column; gap:0.5rem;">')
        for t in tasks:
            html.append(f'<div style="background:#18181b; padding:0.75rem 1rem; border-radius:6px; border:1px solid #27272a; display:flex; justify-content:space-between; align-items:flex-start;">')
            html.append('<div>')
            html.append(f'<div style="color:#f1f5f9; font-size:0.85rem; font-weight:600;">{t.get("task", "")}</div>')
            html.append(f'<div style="color:#94a3b8; font-size:0.75rem; margin-top:0.25rem;">Channel: <b>{t.get("channel", "-")}</b> &bull; Assignee: <span style="color:#38bdf8;">{t.get("assignee", "-")}</span></div>')
            html.append('</div>')
            if t.get("due"):
                html.append(f'<span style="background:#27272a; color:#fbbf24; font-size:0.72rem; padding:0.15rem 0.5rem; border-radius:4px; white-space:nowrap;">Batas: {t.get("due")}</span>')
            html.append('</div>')
        html.append('</div>')
    else:
        html.append('<div style="color:#71717a; font-size:0.82rem; font-style:italic;">Belum ada tugas atau action item spesifik yang terdata.</div>')
    html.append('</div>')

    # 3. Meetings
    html.append('<div class="teams-digest-section" style="margin-bottom:1.5rem;">')
    html.append('<div style="display:flex; align-items:center; gap:0.5rem; margin-bottom:0.75rem;">')
    html.append('<span style="background:rgba(168,85,247,0.2); color:#c084fc; border:1px solid rgba(168,85,247,0.3); font-size:0.75rem; font-weight:700; padding:0.2rem 0.6rem; border-radius:6px;">📅 AGENDA & MEETING</span>')
    html.append(f'<span style="color:#94a3b8; font-size:0.8rem;">({len(meetings)} agenda)</span>')
    html.append('</div>')
    if meetings:
        html.append('<div style="display:flex; flex-direction:column; gap:0.5rem;">')
        for m in meetings:
            html.append(f'<div style="background:#18181b; padding:0.75rem 1rem; border-radius:6px; border:1px solid #27272a;">')
            html.append(f'<div style="display:flex; justify-content:space-between; align-items:center;">')
            html.append(f'<strong style="color:#e9d5ff; font-size:0.85rem;">{m.get("title", "Meeting")}</strong>')
            html.append(f'<span style="color:#c084fc; font-size:0.78rem; font-weight:600;">{m.get("when", "")}</span>')
            html.append('</div>')
            if m.get("participants"):
                html.append(f'<div style="color:#94a3b8; font-size:0.75rem; margin-top:0.25rem;">Peserta: {m.get("participants")}</div>')
            html.append('</div>')
        html.append('</div>')
    else:
        html.append('<div style="color:#71717a; font-size:0.82rem; font-style:italic;">Tidak ada agenda pertemuan mendesak dalam obrolan terbaru.</div>')
    html.append('</div>')

    # 4. Summaries
    html.append('<div class="teams-digest-section">')
    html.append('<div style="display:flex; align-items:center; gap:0.5rem; margin-bottom:0.75rem;">')
    html.append('<span style="background:rgba(16,185,129,0.2); color:#34d399; border:1px solid rgba(16,185,129,0.3); font-size:0.75rem; font-weight:700; padding:0.2rem 0.6rem; border-radius:6px;">💬 RANGKUMAN CHANNEL & DISKUSI</span>')
    html.append('</div>')
    if summaries:
        html.append('<div style="display:flex; flex-direction:column; gap:0.75rem;">')
        for s in summaries:
            html.append(f'<div style="background:#18181b; padding:0.75rem 1rem; border-radius:6px; border:1px solid #27272a;">')
            html.append(f'<div style="color:#34d399; font-weight:700; font-size:0.85rem; margin-bottom:0.35rem;">#{s.get("channel", "Diskusi")}</div>')
            html.append(f'<div style="color:#cbd5e1; font-size:0.82rem; line-height:1.5;">{s.get("summary", "")}</div>')
            points = s.get("key_points", [])
            if points:
                html.append('<ul style="margin:0.5rem 0 0 1.25rem; padding:0; color:#94a3b8; font-size:0.78rem;">')
                for p in points:
                    html.append(f'<li>{p}</li>')
                html.append('</ul>')
            html.append('</div>')
        html.append('</div>')
    else:
        html.append('<div style="color:#71717a; font-size:0.82rem; font-style:italic;">Belum ada rangkuman obrolan tambahan.</div>')
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
                SELECT id, created_at, date_str, raw_messages_count, status
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
