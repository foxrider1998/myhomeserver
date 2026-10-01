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
WEBHOOK_URL = os.environ.get("HERMES_WEBHOOK_URL", "http://hermes-agent:8000/webhook/mail")
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


async def process_unseen(imap_client: aioimaplib.IMAP4_SSL, account_name: str, http_client: httpx.AsyncClient) -> None:
    _, search_data = await imap_client.search("UNSEEN")
    if not search_data or not search_data[0]:
        return
    msg_ids = search_data[0].split()
    for msg_id in msg_ids:
        msg_id_str = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
        result, fetch_data = await imap_client.fetch(msg_id_str, "(RFC822)")
        if result != "OK":
            continue
        raw = parse_fetch_response(fetch_data)
        if not raw:
            continue
        msg = email.message_from_bytes(raw)
        payload = {
            "account": account_name,
            "sender": decode_mime_header(msg.get("From")),
            "subject": decode_mime_header(msg.get("Subject")),
            "date": msg.get("Date", ""),
            "snippet": extract_body_snippet(msg),
        }
        log.info("[%s] email baru dari %s: %s", account_name, payload["sender"], payload["subject"])
        await send_webhook(http_client, payload)
        await imap_client.store(msg_id_str, "+FLAGS", "\\Seen")


async def watch_account(account: dict, http_client: httpx.AsyncClient) -> None:
    name = account["account_name"]
    while True:
        try:
            imap_client = aioimaplib.IMAP4_SSL(host=account["imap_server"], port=account.get("imap_port", 993))
            await imap_client.wait_hello_from_server()
            await imap_client.login(account["email"], account["password"])
            await imap_client.select("INBOX")
            log.info("[%s] terhubung, mulai polling tiap %ss", name, POLL_SECONDS)

            while True:
                await process_unseen(imap_client, name, http_client)
                await asyncio.sleep(POLL_SECONDS)

        except Exception as exc:
            log.warning("[%s] koneksi terputus/gagal: %s - reconnect dalam %ss", name, exc, RECONNECT_BACKOFF_SECONDS)
            await asyncio.sleep(RECONNECT_BACKOFF_SECONDS)


async def main() -> None:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        accounts = json.load(f)

    if not accounts:
        log.error("config.json kosong - tidak ada akun untuk dipantau")
        return

    async with httpx.AsyncClient() as http_client:
        await asyncio.gather(*(watch_account(acc, http_client) for acc in accounts))


if __name__ == "__main__":
    asyncio.run(main())
