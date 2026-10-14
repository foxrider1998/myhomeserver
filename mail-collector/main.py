"""Poll banyak akun IMAP sekaligus (asyncio), forward email UNSEEN ke Hermes Agent.

config.json: list akun {account_name, imap_server, imap_port, email, password}.
Poll interval & webhook URL diatur lewat environment (lihat bawah).
"""

from __future__ import annotations

import asyncio
import email
import email.header
import json
import logging
import os
import re
from email.message import Message

import aioimaplib
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mail-collector")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.json")
WEBHOOK_URL = os.environ.get("HERMES_WEBHOOK_URL", "http://localhost:8000/webhook/mail")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
RECONNECT_BACKOFF_SECONDS = int(os.environ.get("RECONNECT_BACKOFF_SECONDS", "15"))
BODY_SNIPPET_CHARS = 500


def decode_mime_header(raw: str | None) -> str:
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def extract_body_snippet(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
                return text.strip()[:BODY_SNIPPET_CHARS]
        return ""
    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace").strip()[:BODY_SNIPPET_CHARS]


def parse_fetch_response(lines: list[bytes]) -> bytes | None:
    """aioimaplib mengembalikan fetch response sebagai list of bytes campur framing
    protokol IMAP (mis. b'1 FETCH (RFC822 {1234}') dan payload mentah. Blob email
    yang sebenarnya selalu jauh lebih panjang dari baris framing, jadi diambil
    yang paling panjang - trik pragmatis untuk quirk parsing aioimaplib ini."""
    if not lines:
        return None
    return max(lines, key=len)


async def send_webhook(client: httpx.AsyncClient, payload: dict) -> None:
    try:
        resp = await client.post(WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
    except Exception as exc:
        log.warning("Gagal kirim webhook untuk email dari %s: %s", payload.get("sender"), exc)


FILTER_CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "otp_filter_config.json"))

def load_filter_config() -> dict:
    if os.path.exists(FILTER_CONFIG_PATH):
        try:
            with open(FILTER_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "include_senders": [],
        "include_keywords": ["otp", "verifikasi", "verification", "kode", "code", "token", "pin", "sandi", "password", "konfirmasi", "confirm", "security code", "bni", "yap"],
        "exclude_senders": ["newsletter", "promo", "marketing"],
        "exclude_keywords": ["promo", "diskon", "cashback", "newsletter", "unsubscribe"]
    }

OTP_PATTERNS = [
    re.compile(r"(?:kode|code|otp|token|pin|verifikasi|verification|passcode|security code)[\s\S]{0,150}?[\*\s\:\-]*\b(\d{4,8})\b", re.IGNORECASE),
    re.compile(r"[\*\s\:\>]\b(\d{6})\b[\*\s\<\.]"),
    re.compile(r"\b(\d{6})\b"),
    re.compile(r"\b(\d{4})\b"),
    re.compile(r"\b(\d{8})\b"),
]


def extract_otp(subject: str, snippet: str) -> str | None:
    text = f"{subject} {snippet}"
    for pat in OTP_PATTERNS:
        match = pat.search(text)
        if match:
            return match.group(1)
    return None


seen_keys: set[str] = set()


async def process_messages(imap_client: aioimaplib.IMAP4_SSL, account_name: str, http_client: httpx.AsyncClient, search_mode: str = "UNSEEN", limit: int = 20) -> None:
    cfg = load_filter_config()
    include_senders = [s.lower().strip() for s in cfg.get("include_senders", []) if s.strip()]
    include_keywords = [k.lower().strip() for k in cfg.get("include_keywords", []) if k.strip()]
    exclude_senders = [s.lower().strip() for s in cfg.get("exclude_senders", []) if s.strip()]
    exclude_keywords = [k.lower().strip() for k in cfg.get("exclude_keywords", []) if k.strip()]

    _, search_data = await imap_client.search(search_mode)
    if not search_data or not search_data[0]:
        return
    msg_ids = search_data[0].split()
    if search_mode == "ALL":
        msg_ids = msg_ids[-max(1, min(limit, 50)):]

    for msg_id in msg_ids:
        msg_id_str = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
        result, fetch_data = await imap_client.fetch(msg_id_str, "(RFC822)")
        if result != "OK":
            continue
        raw = parse_fetch_response(fetch_data)
        if not raw:
            continue
        msg = email.message_from_bytes(raw)
        sender = decode_mime_header(msg.get("From"))
        sub = decode_mime_header(msg.get("Subject"))
        mid = msg.get("Message-ID", "").strip()
        dedup_key = f"{account_name}:{mid}" if mid else f"{account_name}:{msg_id_str}:{sub}"
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        snippet = extract_body_snippet(msg)
        full_text = f"{sub} {snippet}"

        # 1. Check Excludes
        if any(ex in sender.lower() for ex in exclude_senders):
            continue
        if any(ex in full_text.lower() for ex in exclude_keywords):
            continue

        # 2. Check Includes
        if include_senders and not any(inc in sender.lower() for inc in include_senders):
            continue

        otp = extract_otp(sub, snippet)
        has_inc_kw = any(kw in full_text.lower() for kw in include_keywords) if include_keywords else True

        if has_inc_kw or otp:
            payload = {
                "account": account_name,
                "sender": sender,
                "subject": sub,
                "date": msg.get("Date", ""),
                "snippet": snippet,
                "otp_code": otp,
            }
            log.info("[%s] email terdeteksi dari %s: %s | OTP: %s", account_name, payload["sender"], payload["subject"], otp or "tidak terdeteksi")
            await send_webhook(http_client, payload)


async def watch_account(account: dict, http_client: httpx.AsyncClient) -> None:
    name = account["account_name"]
    while True:
        try:
            imap_client = aioimaplib.IMAP4_SSL(host=account["imap_server"], port=account.get("imap_port", 993))
            await imap_client.wait_hello_from_server()
            await imap_client.login(account["email"], account["password"])
            await imap_client.select("INBOX")
            log.info("[%s] terhubung, mulai polling tiap %ss", name, POLL_SECONDS)

            # Pada saat awal terhubung, periksa juga 20 email terbaru (ALL)
            await process_messages(imap_client, name, http_client, search_mode="ALL", limit=20)

            while True:
                await process_messages(imap_client, name, http_client, search_mode="UNSEEN")
                await asyncio.sleep(POLL_SECONDS)

        except Exception as exc:
            log.warning("[%s] koneksi terputus/gagal: %s - reconnect dalam %ss", name, exc, RECONNECT_BACKOFF_SECONDS)
            await asyncio.sleep(RECONNECT_BACKOFF_SECONDS)


async def main() -> None:
    accounts = []
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                accounts = json.load(f)
        except Exception as e:
            log.warning("Gagal baca %s: %s", CONFIG_PATH, e)

    if not accounts:
        log.info("Menggunakan akun default: areskretindo@gmail.com")
        accounts = [
            {
                "account_name": "Gmail_Ares",
                "imap_server": "imap.gmail.com",
                "imap_port": 993,
                "email": "areskretindo@gmail.com",
                "password": "jugx ibxc tutj zbsk"
            }
        ]
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(accounts, f, indent=2)
        except Exception as e:
            log.warning("Gagal menulis config default: %s", e)

    log.info("Memulai pemantauan %s akun IMAP sekaligus (maks 20 akun)", len(accounts))
    async with httpx.AsyncClient() as http_client:
        await asyncio.gather(*(watch_account(acc, http_client) for acc in accounts))


if __name__ == "__main__":
    asyncio.run(main())
