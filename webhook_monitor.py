"""Webhook & API Monitor Engine (Beeceptor / Webhook.site style).

Provides persistent request capture, mock response rules engine, replay,
and search/filter for incoming webhooks and API testing.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "webhook_monitor.db")


def get_db_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS webhook_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    url TEXT NOT NULL,
                    query_params TEXT,
                    headers TEXT,
                    body TEXT,
                    body_bytes_len INTEGER DEFAULT 0,
                    content_type TEXT,
                    client_ip TEXT,
                    status_code INTEGER DEFAULT 200,
                    response_headers TEXT,
                    response_body TEXT,
                    duration_ms REAL DEFAULT 0,
                    is_mocked INTEGER DEFAULT 0,
                    rule_name TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mock_rules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    path_pattern TEXT NOT NULL,
                    method TEXT NOT NULL DEFAULT 'ALL',
                    status_code INTEGER NOT NULL DEFAULT 200,
                    response_headers TEXT,
                    response_body TEXT,
                    delay_ms INTEGER DEFAULT 0,
                    enabled INTEGER DEFAULT 1,
                    priority INTEGER DEFAULT 0,
                    created_at TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_req_time ON webhook_requests(timestamp DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_req_method ON webhook_requests(method)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_req_path ON webhook_requests(path)")

            # Seed sample mock rules if empty
            cur = conn.execute("SELECT COUNT(*) FROM mock_rules")
            if cur.fetchone()[0] == 0:
                now_str = datetime.now(timezone.utc).isoformat()
                sample_rules = [
                    (
                        "rule_sample_payment",
                        "Mock Payment Webhook (200 OK)",
                        "/catch/payment*",
                        "POST",
                        200,
                        json.dumps({"Content-Type": "application/json"}),
                        json.dumps({
                            "status": "PAID",
                            "transaction_id": "TRX-9882194",
                            "order_id": "ORDER-5510",
                            "gross_amount": 150000,
                            "currency": "IDR",
                            "paid_at": now_str,
                            "message": "Payment verified successfully"
                        }, indent=2),
                        200,
                        1,
                        10,
                        now_str
                    ),
                    (
                        "rule_sample_error",
                        "Simulasi Bad Request 400",
                        "/catch/error*",
                        "ALL",
                        400,
                        json.dumps({"Content-Type": "application/json"}),
                        json.dumps({
                            "error": "BAD_REQUEST",
                            "message": "Missing required signature header: X-Webhook-Signature",
                            "code": 400
                        }, indent=2),
                        100,
                        0,
                        5,
                        now_str
                    )
                ]
                conn.executemany("""
                    INSERT INTO mock_rules (
                        id, name, path_pattern, method, status_code,
                        response_headers, response_body, delay_ms, enabled, priority, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, sample_rules)
    finally:
        conn.close()


def match_path_pattern(pattern: str, path: str) -> bool:
    pattern = pattern.strip()
    if not pattern or pattern == "*":
        return True
    if pattern.endswith("*"):
        prefix = pattern[:-1].rstrip("/")
        if not prefix:
            return True
        return path == prefix or path.startswith(prefix + "/") or path.startswith(prefix)
    return pattern.rstrip("/") == path.rstrip("/")


def find_matching_mock_rule(method: str, path: str) -> Optional[dict]:
    conn = get_db_connection()
    try:
        cur = conn.execute("""
            SELECT * FROM mock_rules
            WHERE enabled = 1
            ORDER BY priority DESC, created_at DESC
        """)
        rules = [dict(row) for row in cur.fetchall()]
        for r in rules:
            rule_method = (r.get("method") or "ALL").upper().strip()
            if rule_method != "ALL" and rule_method != method.upper().strip():
                continue
            if match_path_pattern(r.get("path_pattern", "*"), path):
                return r
        return None
    finally:
        conn.close()


def record_incoming_request(
    method: str,
    path: str,
    raw_query: str = "",
    headers: Dict[str, str] = None,
    raw_body: bytes = b"",
    client_ip: str = "127.0.0.1",
    body: bytes = None,
    ip: str = None
) -> tuple[int, Dict[str, str], bytes]:
    """Capture request, execute matching mock rule if any, save to DB, and return (status, headers, body)."""
    if headers is None:
        headers = {}
    if body is not None:
        raw_body = body
    if ip is not None:
        client_ip = ip
    if not raw_query and "?" in path:
        parts = path.split("?", 1)
        clean_path = parts[0]
        raw_query = parts[1]
    else:
        clean_path = path.split("?")[0]

    start_time = time.perf_counter()
    now_iso = datetime.now(timezone.utc).isoformat()
    full_url = path if not raw_query else f"{clean_path}?{raw_query}"

    # Parse query params
    query_dict = {}
    if raw_query:
        for k, v in urllib.parse.parse_qs(raw_query).items():
            query_dict[k] = v[0] if len(v) == 1 else v

    # Decode body
    body_text = ""
    if raw_body:
        try:
            body_text = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            try:
                body_text = raw_body.decode("latin-1")
            except Exception:
                body_text = f"<binary data: {len(raw_body)} bytes>"

    content_type = headers.get("content-type", headers.get("Content-Type", "application/octet-stream"))

    # Match Mock Rule
    rule = find_matching_mock_rule(method, clean_path)
    is_mocked = 1 if rule else 0
    rule_name = rule.get("name") if rule else None

    if rule:
        status_code = int(rule.get("status_code", 200))
        delay_ms = max(0, min(10000, int(rule.get("delay_ms", 0))))
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)

        # Parse response headers
        resp_headers = {"Content-Type": "application/json", "X-Mocked-By": "Beeceptor-Style-Monitor"}
        try:
            custom_h = json.loads(rule.get("response_headers") or "{}")
            if isinstance(custom_h, dict):
                resp_headers.update(custom_h)
        except Exception:
            pass

        resp_body_str = rule.get("response_body") or ""
        resp_bytes = resp_body_str.encode("utf-8")
    else:
        # Default mock response
        status_code = 200
        resp_headers = {"Content-Type": "application/json", "X-Captured-By": "Beeceptor-Style-Monitor"}
        default_resp = {
            "status": "success",
            "message": "Request successfully captured by Webhook Monitor",
            "method": method,
            "path": clean_path,
            "timestamp": now_iso
        }
        resp_body_str = json.dumps(default_resp, indent=2)
        resp_bytes = resp_body_str.encode("utf-8")

    duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

    # Persist in DB
    conn = get_db_connection()
    try:
        with conn:
            conn.execute("""
                INSERT INTO webhook_requests (
                    timestamp, method, path, url, query_params, headers,
                    body, body_bytes_len, content_type, client_ip, status_code,
                    response_headers, response_body, duration_ms, is_mocked, rule_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now_iso,
                method.upper(),
                clean_path,
                full_url,
                json.dumps(query_dict),
                json.dumps(headers),
                body_text,
                len(raw_body),
                content_type,
                client_ip,
                status_code,
                json.dumps(resp_headers),
                resp_body_str,
                duration_ms,
                is_mocked,
                rule_name
            ))
            # Keep max 500 records to prevent bloating
            conn.execute("""
                DELETE FROM webhook_requests
                WHERE id NOT IN (
                    SELECT id FROM webhook_requests ORDER BY id DESC LIMIT 500
                )
            """)
    finally:
        conn.close()

    return status_code, resp_headers, resp_bytes


def get_requests_list(
    limit: int = 50,
    offset: int = 0,
    method: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None
) -> dict:
    conn = get_db_connection()
    try:
        where_clauses = []
        params = []

        if method and method.upper() != "ALL":
            where_clauses.append("method = ?")
            params.append(method.upper())

        if status:
            if status == "2xx":
                where_clauses.append("status_code >= 200 AND status_code < 300")
            elif status == "4xx":
                where_clauses.append("status_code >= 400 AND status_code < 500")
            elif status == "5xx":
                where_clauses.append("status_code >= 500")
            elif status.isdigit():
                where_clauses.append("status_code = ?")
                params.append(int(status))

        if search:
            search_pattern = f"%{search.strip()}%"
            where_clauses.append("(path LIKE ? OR body LIKE ? OR client_ip LIKE ? OR query_params LIKE ?)")
            params.extend([search_pattern, search_pattern, search_pattern, search_pattern])

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        # Total count
        count_cur = conn.execute(f"SELECT COUNT(*) FROM webhook_requests {where_sql}", params)
        total = count_cur.fetchone()[0]

        # Fetch records
        query = f"""
            SELECT id, timestamp, method, path, url, client_ip, status_code,
                   duration_ms, is_mocked, rule_name, body_bytes_len, content_type
            FROM webhook_requests
            {where_sql}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """
        cur = conn.execute(query, params + [max(1, min(limit, 200)), max(0, offset)])
        rows = [dict(r) for r in cur.fetchall()]

        return {
            "total": total,
            "count": len(rows),
            "requests": rows
        }
    finally:
        conn.close()


def get_request_detail(req_id: int) -> Optional[dict]:
    conn = get_db_connection()
    try:
        cur = conn.execute("SELECT * FROM webhook_requests WHERE id = ?", (req_id,))
        row = cur.fetchone()
        if not row:
            return None
        d = dict(row)
        # Parse JSON fields
        for field in ("query_params", "headers", "response_headers"):
            if d.get(field):
                try:
                    d[field] = json.loads(d[field])
                except Exception:
                    pass
        return d
    finally:
        conn.close()


def clear_all_requests() -> int:
    conn = get_db_connection()
    try:
        with conn:
            cur = conn.execute("DELETE FROM webhook_requests")
            return cur.rowcount
    finally:
        conn.close()


def delete_single_request(req_id: int) -> bool:
    conn = get_db_connection()
    try:
        with conn:
            cur = conn.execute("DELETE FROM webhook_requests WHERE id = ?", (req_id,))
            return cur.rowcount > 0
    finally:
        conn.close()


def get_all_rules() -> List[dict]:
    conn = get_db_connection()
    try:
        cur = conn.execute("SELECT * FROM mock_rules ORDER BY priority DESC, created_at DESC")
        rules = []
        for row in cur.fetchall():
            d = dict(row)
            if d.get("response_headers"):
                try:
                    d["response_headers"] = json.loads(d["response_headers"])
                except Exception:
                    pass
            rules.append(d)
        return rules
    finally:
        conn.close()


def save_rule(rule_data: dict) -> dict:
    rule_id = rule_data.get("id") or f"rule_{int(time.time() * 1000)}"
    name = rule_data.get("name") or "Aturan Mock Baru"
    path_pattern = rule_data.get("path_pattern") or "/catch/*"
    method = (rule_data.get("method") or "ALL").upper()
    status_code = int(rule_data.get("status_code", 200))

    resp_headers = rule_data.get("response_headers", {"Content-Type": "application/json"})
    if isinstance(resp_headers, dict):
        resp_headers_str = json.dumps(resp_headers)
    else:
        resp_headers_str = str(resp_headers)

    response_body = rule_data.get("response_body", '{"status": "ok"}')
    delay_ms = max(0, min(10000, int(rule_data.get("delay_ms", 0))))
    enabled = 1 if rule_data.get("enabled", True) else 0
    priority = int(rule_data.get("priority", 0))
    created_at = rule_data.get("created_at") or datetime.now(timezone.utc).isoformat()

    conn = get_db_connection()
    try:
        with conn:
            conn.execute("""
                INSERT OR REPLACE INTO mock_rules (
                    id, name, path_pattern, method, status_code,
                    response_headers, response_body, delay_ms, enabled, priority, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                rule_id, name, path_pattern, method, status_code,
                resp_headers_str, response_body, delay_ms, enabled, priority, created_at
            ))
        return {
            "id": rule_id,
            "name": name,
            "path_pattern": path_pattern,
            "method": method,
            "status_code": status_code,
            "enabled": bool(enabled)
        }
    finally:
        conn.close()


def delete_rule(rule_id: str) -> bool:
    conn = get_db_connection()
    try:
        with conn:
            cur = conn.execute("DELETE FROM mock_rules WHERE id = ?", (rule_id,))
            return cur.rowcount > 0
    finally:
        conn.close()


def toggle_rule(rule_id: str) -> Optional[bool]:
    conn = get_db_connection()
    try:
        with conn:
            cur = conn.execute("SELECT enabled FROM mock_rules WHERE id = ?", (rule_id,))
            row = cur.fetchone()
            if not row:
                return None
            new_val = 0 if row[0] == 1 else 1
            conn.execute("UPDATE mock_rules SET enabled = ? WHERE id = ?", (new_val, rule_id))
            return bool(new_val)
    finally:
        conn.close()


def replay_captured_request(req_id: int, target_url: Optional[str] = None) -> dict:
    """Replay a captured request to target URL or to localhost."""
    req = get_request_detail(req_id)
    if not req:
        raise ValueError(f"Request ID {req_id} not found")

    dest_url = target_url
    if not dest_url:
        dest_url = f"http://localhost:3005{req['path']}"

    headers = dict(req.get("headers") or {})
    # Strip host and content-length to avoid conflict
    headers.pop("host", None)
    headers.pop("Host", None)
    headers.pop("content-length", None)
    headers.pop("Content-Length", None)
    headers["X-Replayed-From"] = f"RequestID-{req_id}"

    body_bytes = (req.get("body") or "").encode("utf-8") if req.get("body") else None

    req_obj = urllib.request.Request(
        dest_url,
        data=body_bytes if req["method"] in ("POST", "PUT", "PATCH") else None,
        headers=headers,
        method=req["method"]
    )

    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req_obj, timeout=15.0) as resp:
            dur = round((time.perf_counter() - start) * 1000, 2)
            resp_body = resp.read().decode("utf-8", errors="replace")
            return {
                "success": True,
                "status_code": resp.status,
                "duration_ms": dur,
                "target_url": dest_url,
                "response_body": resp_body[:2000]
            }
    except urllib.error.HTTPError as he:
        dur = round((time.perf_counter() - start) * 1000, 2)
        err_body = he.read().decode("utf-8", errors="replace")
        return {
            "success": False,
            "status_code": he.code,
            "duration_ms": dur,
            "target_url": dest_url,
            "error": str(he),
            "response_body": err_body[:2000]
        }
    except Exception as ex:
        dur = round((time.perf_counter() - start) * 1000, 2)
        return {
            "success": False,
            "status_code": 500,
            "duration_ms": dur,
            "target_url": dest_url,
            "error": str(ex)
        }


# Aliases for seamless compatibility
get_requests = get_requests_list
get_request_by_id = get_request_detail
clear_requests = clear_all_requests
delete_request = delete_single_request
get_mock_rules = get_all_rules
add_mock_rule = save_rule
delete_mock_rule = delete_rule
toggle_mock_rule = toggle_rule


def replay_request(request_id: int = None, target_url: Optional[str] = None, custom_headers: Optional[dict] = None, custom_body: Optional[str] = None, req_id: int = None) -> dict:
    actual_id = request_id if request_id is not None else req_id
    return replay_captured_request(req_id=actual_id, target_url=target_url)


# Auto-init DB upon import
init_db()

