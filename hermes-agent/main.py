"""Hermes Agent - Webhook Receiver + Orchestrator + LLM Controller.

Terima trigger dari mail-collector (email baru) dan dari solar watcher internal
(SOC baterai rendah/tinggi), lalu putuskan aksi lewat LLM eksternal (Sumopod -
OpenAI-compatible), bukan model lokal, supaya hemat RAM di ThinkCentre.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
import os
import re
import subprocess
import imaplib
import shlex
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("hermes-agent")

SUMOPOD_API_KEY = os.environ.get("SUMOPOD_API_KEY", "")
SUMOPOD_BASE_URL = os.environ.get("SUMOPOD_BASE_URL", "https://ai.sumopod.com/v1")
SUMOPOD_MODEL = os.environ.get("SUMOPOD_MODEL", "deepseek-v4-flash-0731:netra")

AVAILABLE_MODELS = [
    {
        "category": "DeepSeek (Rekomendasi Hemat & Cepat)",
        "models": [
            {"id": "deepseek-v4-flash-0731:netra", "name": "DeepSeek V4 Flash (Netra 90% Off) - $0.05/1M", "badge": "DEFAULT (Diskon 90%)"},
            {"id": "deepseek-v4.1-flash:netra", "name": "DeepSeek V4.1 Flash (Netra 90% Off) - $0.05/1M", "badge": "Diskon 90%"},
            {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash - $0.15/1M", "badge": "Fast"},
            {"id": "deepseek-v4-flash-vision-exp", "name": "DeepSeek V4 Flash Vision Exp - $0.15/1M", "badge": "Vision"},
            {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro - $0.66/1M", "badge": "Pro"},
            {"id": "deepseek-flash", "name": "DeepSeek Flash - $0.15/1M", "badge": ""},
        ]
    },
    {
        "category": "Anthropic Claude",
        "models": [
            {"id": "claude-sonnet-5", "name": "Claude Sonnet 5 - $2.00/1M", "badge": "Smart"},
            {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5 - $1.00/1M", "badge": "Fast"},
            {"id": "claude-opus-5", "name": "Claude Opus 5 - $5.00/1M", "badge": "Flagship"},
            {"id": "claude-opus-5-5", "name": "Claude Opus 5.5 - $4.00/1M", "badge": "Flagship"},
            {"id": "claude-opus-4-8", "name": "Claude Opus 4.8 - $5.00/1M", "badge": ""},
            {"id": "claude-fable-5-1", "name": "Claude Fable 5.1 - $10.00/1M", "badge": ""},
        ]
    },
    {
        "category": "Google Gemini",
        "models": [
            {"id": "gemini/gemini-3.5-flash", "name": "Gemini 3.5 Flash - $1.50/1M", "badge": "Populer"},
            {"id": "gemini/gemini-3.5-flash-lite", "name": "Gemini 3.5 Flash Lite - $0.30/1M", "badge": "Hemat"},
            {"id": "gemini/gemini-3.7-flash", "name": "Gemini 3.7 Flash - $0.75/1M", "badge": ""},
            {"id": "gemini/gemini-3.8-flash", "name": "Gemini 3.8 Flash - $0.75/1M", "badge": ""},
            {"id": "gemini/gemini-3.1-flash-lite", "name": "Gemini 3.1 Flash Lite - $0.25/1M", "badge": ""},
            {"id": "gemini/gemini-3.1-pro-preview", "name": "Gemini 3.1 Pro Preview - $2.00/1M", "badge": "Pro"},
            {"id": "gemini/gemini-3-flash-preview", "name": "Gemini 3 Flash Preview - $0.50/1M", "badge": ""},
        ]
    },
    {
        "category": "OpenAI GPT",
        "models": [
            {"id": "gpt-4o-mini", "name": "GPT-4o Mini - $0.15/1M", "badge": "Hemat"},
            {"id": "gpt-4o", "name": "GPT-4o - $2.50/1M", "badge": "Flagship"},
            {"id": "gpt-4.1-mini", "name": "GPT-4.1 Mini - $0.40/1M", "badge": ""},
            {"id": "gpt-4.1-nano", "name": "GPT-4.1 Nano - $0.10/1M", "badge": "Ultra Lite"},
            {"id": "gpt-4.1", "name": "GPT-4.1 - $2.00/1M", "badge": ""},
            {"id": "gpt-5-nano", "name": "GPT-5 Nano - $0.05/1M", "badge": "NextGen"},
            {"id": "gpt-5-mini", "name": "GPT-5 Mini - $0.25/1M", "badge": ""},
            {"id": "gpt-5", "name": "GPT-5 - $1.25/1M", "badge": ""},
            {"id": "gpt-5.4-nano", "name": "GPT-5.4 Nano - $0.20/1M", "badge": ""},
            {"id": "gpt-5.4-mini", "name": "GPT-5.4 Mini - $0.75/1M", "badge": ""},
            {"id": "gpt-5.4", "name": "GPT-5.4 - $2.50/1M", "badge": ""},
            {"id": "gpt-5.6-luna", "name": "GPT-5.6 Luna - $0.20/1M", "badge": ""},
            {"id": "gpt-6-luna", "name": "GPT-6 Luna - $0.10/1M", "badge": ""},
        ]
    },
    {
        "category": "Alibaba Qwen",
        "models": [
            {"id": "qwen3.6-flash", "name": "Qwen 3.6 Flash - $0.25/1M", "badge": "Fast"},
            {"id": "qwen3.6-plus", "name": "Qwen 3.6 Plus - $0.50/1M", "badge": ""},
            {"id": "qwen3.8-flash", "name": "Qwen 3.8 Flash - $0.15/1M", "badge": "Hemat"},
            {"id": "qwen3.8-max", "name": "Qwen 3.8 Max (50% Off) - $1.00/1M", "badge": "Diskon 50%"},
            {"id": "qwen3.7-max", "name": "Qwen 3.7 Max - $1.25/1M", "badge": ""},
            {"id": "qwen3.7-plus", "name": "Qwen 3.7 Plus - $0.32/1M", "badge": ""},
        ]
    },
    {
        "category": "Zhipu GLM & Lainnya",
        "models": [
            {"id": "glm-5.3-flash", "name": "GLM 5.3 Flash (50% Off) - $0.015/1M", "badge": "Super Hemat"},
            {"id": "glm-5.2", "name": "GLM 5.2 (25% Off) - $1.05/1M", "badge": ""},
            {"id": "glm-5", "name": "GLM 5 - $0.60/1M", "badge": ""},
            {"id": "glm-5-turbo", "name": "GLM 5 Turbo - $1.20/1M", "badge": ""},
            {"id": "MiniMax-M2.7-highspeed", "name": "MiniMax M2.7 Highspeed (90% Off) - $0.03/1M", "badge": "Diskon 90%"},
            {"id": "MiniMax-M3", "name": "MiniMax M3 - $0.30/1M", "badge": ""},
            {"id": "MiniMax-M3.1-Flash-Preview", "name": "MiniMax M3.1 Flash Preview - $0.03/1M", "badge": "Diskon 90%"},
            {"id": "mimo-v2.5", "name": "Mimo V2.5 - $0.14/1M", "badge": ""},
            {"id": "mimo-v2.6-flash", "name": "Mimo V2.6 Flash - $0.14/1M", "badge": ""},
            {"id": "grok-4.7", "name": "Grok 4.7 (xAI) - $2.00/1M", "badge": "xAI"},
            {"id": "kimi-k3", "name": "Kimi K3 (Moonshot) - $3.00/1M", "badge": ""},
            {"id": "hy3", "name": "Hunyuan 3 (Tencent) - $0.13/1M", "badge": ""},
        ]
    }
]

SOLAR_DASHBOARD_URL = os.environ.get("SOLAR_DASHBOARD_URL", "http://localhost:3005")
SOLAR_POLL_SECONDS = int(os.environ.get("SOLAR_POLL_SECONDS", "60"))
SOC_LOW_THRESHOLD = float(os.environ.get("SOC_LOW_THRESHOLD", "20"))
SOC_HIGH_THRESHOLD = float(os.environ.get("SOC_HIGH_THRESHOLD", "95"))

APPIUM_SERVER_URL = os.environ.get("APPIUM_SERVER_URL", "http://localhost:4723")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# In-memory log events untuk ditampilkan di dashboard UI
MAX_EVENTS = 100
events_log: list[dict[str, Any]] = []

# In-memory OTP Inbox (email masuk dengan deteksi OTP)
MAX_INBOX = 500
otp_inbox: list[dict[str, Any]] = []
_inbox_id_counter = 0


def record_event(category: str, title: str, details: Any = None):
    event = {
        "id": len(events_log) + 1,
        "timestamp": datetime.now().isoformat(),
        "category": category,  # 'solar', 'mail', 'llm', 'system'
        "title": title,
        "details": details or {},
    }
    events_log.insert(0, event)
    if len(events_log) > MAX_EVENTS:
        events_log.pop()


def add_to_inbox(account: str, sender: str, subject: str, date: str, snippet: str, otp_code: str | None, full_body: str = ""):
    global _inbox_id_counter

    # Deduplikasi ketat: jika sender, subject, dan (date atau snippet) sudah ada di inbox, jangan duplikasi
    for existing in otp_inbox:
        if (
            existing.get("subject") == subject
            and existing.get("sender") == sender
            and (existing.get("date") == date or existing.get("snippet") == snippet)
        ):
            # Jika sebelumnya belum ada OTP dan sekarang terdeteksi OTP, update
            if not existing.get("otp_code") and otp_code:
                existing["otp_code"] = otp_code
                existing["has_otp"] = True
                existing["highlighted"] = True
                if "otp" not in existing.get("labels", []):
                    existing.setdefault("labels", []).append("otp")
            return existing["id"]

    _inbox_id_counter += 1
    entry = {
        "id": _inbox_id_counter,
        "received_at": datetime.now().isoformat(),
        "date": date,
        "account": account,
        "sender": sender,
        "subject": subject,
        "snippet": snippet,
        "full_body": full_body,
        "otp_code": otp_code,
        "has_otp": bool(otp_code),
        "highlighted": bool(otp_code),  # OTP email auto-highlight
        "labels": ["otp"] if otp_code else [],
        "read": False,
    }
    otp_inbox.insert(0, entry)
    if len(otp_inbox) > MAX_INBOX:
        otp_inbox.pop()
    return _inbox_id_counter


class MailEvent(BaseModel):
    account: str
    sender: str
    subject: str
    date: str
    snippet: str
    otp_code: str | None = None


class ChatRequest(BaseModel):
    prompt: str
    model: str | None = None


class MeetingSummarizeRequest(BaseModel):
    title: str = "Meeting"
    transcript: str
    language: str = "id"
    model: str | None = None


current_state = {
    "last_soc": None,
    "last_alert": None,
    "last_poll_time": None,
    "connected_to_solar": False,
}


async def call_llm(prompt: str, model: str | None = None) -> str:
    chosen_model = model or SUMOPOD_MODEL
    if not SUMOPOD_API_KEY or SUMOPOD_API_KEY.startswith("sk-xxxx"):
        msg = f"[Simulasi LLM {chosen_model}] SUMOPOD_API_KEY belum diisi di .env. Prompt diterima: '{prompt}'"
        record_event("llm", "Panggilan LLM (Kunci Belum Dikonfigurasi)", {"prompt": prompt, "response": msg, "model": chosen_model})
        return msg

    models_to_try = [chosen_model]
    if chosen_model != "deepseek-v4-flash-0731:netra":
        models_to_try.append("deepseek-v4-flash-0731:netra")
    if "deepseek-v4-flash" not in models_to_try:
        models_to_try.append("deepseek-v4-flash")

    last_error = None
    for attempt_model in models_to_try:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{SUMOPOD_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {SUMOPOD_API_KEY}"},
                    json={
                        "model": attempt_model,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=50,
                )
                resp.raise_for_status()
                data = resp.json()
                reply = data["choices"][0]["message"]["content"]
                record_event("llm", f"Respon LLM ({attempt_model})", {"prompt": prompt, "response": reply[:200], "model": attempt_model})
                return reply
        except Exception as e:
            last_error = e
            log.warning("Panggilan LLM %s gagal (%s), mencoba model alternatif jika tersedia...", attempt_model, e)

    err_msg = f"Error menghubungi Sumopod LLM ({chosen_model}): {last_error}"
    log.error(err_msg)
    record_event("llm", "Error Panggilan LLM", {"error": str(last_error), "model": chosen_model})
    return err_msg


async def trigger_appium_task(task_name: str, **context) -> None:
    log.info("trigger_appium_task: %s context=%s (server=%s)", task_name, context, APPIUM_SERVER_URL)
    record_event("appium", f"Trigger Task: {task_name}", {"context": context, "server": APPIUM_SERVER_URL})


async def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    async with httpx.AsyncClient() as client:
        try:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
                timeout=10,
            )
            record_event("telegram", "Notifikasi Telegram Terkirim", {"text": text})
        except Exception as exc:
            log.warning("Gagal kirim Telegram: %s", exc)
            record_event("telegram", "Gagal Kirim Telegram", {"error": str(exc)})


async def solar_watcher() -> None:
    """Poll /api/state dari PowMr/solar-dashboard, trigger otomasi saat SOC melewati threshold."""
    record_event("system", "Solar Watcher Dimulai", {"target": SOLAR_DASHBOARD_URL, "interval": SOLAR_POLL_SECONDS})
    async with httpx.AsyncClient() as client:
        while True:
            try:
                resp = await client.get(f"{SOLAR_DASHBOARD_URL}/api/state", timeout=10)
                resp.raise_for_status()
                state = resp.json()
                soc = state.get("soc")
                current_state["last_soc"] = soc
                current_state["last_poll_time"] = datetime.now().isoformat()
                current_state["connected_to_solar"] = True

                last_alert = current_state["last_alert"]

                if soc is not None and soc <= SOC_LOW_THRESHOLD and last_alert != "low":
                    msg = f"⚠️ Baterai rendah ({soc:.1f}%) - trigger mode hemat daya"
                    log.info(msg)
                    record_event("solar", "Otomasi: Baterai Rendah", {"soc": soc, "action": "battery_low_powersave"})
                    await trigger_appium_task("battery_low_powersave", soc=soc)
                    await send_telegram(msg)
                    current_state["last_alert"] = "low"
                elif soc is not None and soc >= SOC_HIGH_THRESHOLD and last_alert != "high":
                    msg = f"⚡ Baterai penuh/surplus ({soc:.1f}%) - trigger task berdaya tinggi"
                    log.info(msg)
                    record_event("solar", "Otomasi: Baterai Surplus", {"soc": soc, "action": "battery_high_surplus_task"})
                    await trigger_appium_task("battery_high_surplus_task", soc=soc)
                    current_state["last_alert"] = "high"
                elif soc is not None and SOC_LOW_THRESHOLD < soc < SOC_HIGH_THRESHOLD:
                    current_state["last_alert"] = None

            except Exception as exc:
                log.warning("Gagal polling solar service: %s", exc)
                current_state["connected_to_solar"] = False

            await asyncio.sleep(SOLAR_POLL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(solar_watcher())
    yield
    task.cancel()


app = FastAPI(title="Hermes Agent", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/status")
async def get_status():
    return {
        "status": "online",
        "llm_model": SUMOPOD_MODEL,
        "llm_configured": bool(SUMOPOD_API_KEY and not SUMOPOD_API_KEY.startswith("sk-xxxx")),
        "soc_low_threshold": SOC_LOW_THRESHOLD,
        "soc_high_threshold": SOC_HIGH_THRESHOLD,
        "solar_poll_seconds": SOLAR_POLL_SECONDS,
        "solar_dashboard_url": SOLAR_DASHBOARD_URL,
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "appium_url": APPIUM_SERVER_URL,
        "state": current_state,
        "events": events_log[:30],
    }


class SelectModelRequest(BaseModel):
    model: str


@app.get("/api/models")
async def get_models():
    return {
        "current_model": SUMOPOD_MODEL,
        "categories": AVAILABLE_MODELS
    }


@app.post("/api/model/select")
async def select_model(req: SelectModelRequest):
    global SUMOPOD_MODEL
    SUMOPOD_MODEL = req.model
    env_path = "/home/ares/Documents/myserver/.env"
    try:
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                content = f.read()
            if "SUMOPOD_MODEL=" in content:
                content = re.sub(r"SUMOPOD_MODEL=.*", f"SUMOPOD_MODEL={req.model}", content)
            else:
                content += f"\nSUMOPOD_MODEL={req.model}\n"
            with open(env_path, "w", encoding="utf-8") as f:
                f.write(content)
    except Exception as e:
        log.warning("Gagal simpan SUMOPOD_MODEL ke .env: %s", e)

    record_event("system", f"Model LLM Aktif Diubah: {req.model}", {"model": req.model})
    return {"status": "ok", "current_model": SUMOPOD_MODEL}


@app.post("/api/chat")
async def chat_with_agent(req: ChatRequest):
    if not req.prompt:
        raise HTTPException(status_code=400, detail="Prompt tidak boleh kosong")
    reply = await call_llm(req.prompt, req.model)
    return {"prompt": req.prompt, "reply": reply, "model": req.model or SUMOPOD_MODEL}


@app.post("/api/summarize-meeting")
async def summarize_meeting_endpoint(req: MeetingSummarizeRequest):
    if not req.transcript.strip():
        raise HTTPException(status_code=400, detail="Transkrip meeting kosong")

    prompt = f"""Kamu adalah notulen meeting profesional dan asisten AI Hermes di Westpro Home Server.
Berikut adalah transkrip rekaman suara pembicaraan meeting:
---
{req.transcript}
---

Tolong buatkan dokumen notulen & rangkuman meeting yang sangat rapi, jelas, dan terstruktur dalam Bahasa Indonesia:
1. 🎯 **Ringkasan Eksekutif & Topik Utama**
2. 💡 **Poin-Poin Kunci Diskusi**
3. ✅ **Keputusan yang Disepakati (Decisions Made)**
4. 📌 **Daftar Tindak Lanjut / Action Items** (Sebutkan PIC dan tugas spesifik jika ada)
5. ⚠️ **Catatan Khusus & Resiko** (jika ada)

Gunakan format Markdown dengan bullet points yang rapi dan mudah dibaca."""

    summary = await call_llm(prompt, req.model)
    record_event("meeting", f"Rangkuman Meeting: {req.title}", {"length": len(req.transcript), "model": req.model or SUMOPOD_MODEL})
    return {"title": req.title, "transcript": req.transcript, "summary": summary, "model": req.model or SUMOPOD_MODEL}


@app.post("/webhook/mail")
async def webhook_mail(event: MailEvent):
    log.info("[mail] %s <%s>: %s", event.account, event.sender, event.subject)
    otp = event.otp_code

    # Simpan ke OTP Inbox
    add_to_inbox(
        account=event.account,
        sender=event.sender,
        subject=event.subject,
        date=event.date,
        snippet=event.snippet,
        otp_code=otp,
    )

    if otp:
        msg = f"🔑 KODE OTP DITERIMA: {otp} (dari {event.sender})"
        log.info(msg)
        record_event("otp", f"OTP Diterima: {otp}", {
            "otp": otp,
            "sender": event.sender,
            "subject": event.subject,
            "account": event.account,
            "time": datetime.now().isoformat()
        })
        await send_telegram(f"🔐 *KODE OTP BARU*\n\nKode: `{otp}`\nPengirim: {event.sender}\nSubjek: {event.subject}")
    else:
        record_event("mail", f"Email Baru: {event.subject}", {
            "account": event.account,
            "sender": event.sender,
            "snippet": event.snippet,
        })
    return {"status": "received", "otp": otp, "inbox_id": otp_inbox[0]["id"] if otp_inbox else None}


@app.get("/api/otp/inbox")
async def get_otp_inbox(
    otp_only: bool = False,
    sender: str = "",
    keyword: str = "",
    limit: int = 100,
    account: str = "",
):
    """Endpoint internal: semua email, dengan filter opsional."""
    result = list(otp_inbox)
    if otp_only:
        result = [e for e in result if e["has_otp"]]
    if sender:
        result = [e for e in result if sender.lower() in e["sender"].lower()]
    if account:
        result = [e for e in result if account.lower() in e["account"].lower()]
    if keyword:
        kw = keyword.lower()
        result = [e for e in result if kw in e["subject"].lower() or kw in e["snippet"].lower()]
    return result[:max(1, min(limit, 500))]


@app.delete("/api/otp/inbox")
@app.post("/api/otp/inbox/clear")
async def clear_otp_inbox():
    otp_inbox.clear()
    return {"status": "cleared", "count": 0}


@app.get("/api/otp/public")
async def get_otp_public(
    otp_only: bool = True,
    sender: str = "",
    keyword: str = "",
    limit: int = 20,
    account: str = "",
):
    """Endpoint publik: secara default hanya tampilkan email ber-OTP. Tidak ada autentikasi.
    
    Query params:
      - otp_only=true    : hanya email yang punya kode OTP (default: true)
      - sender=          : filter berdasarkan alamat pengirim
      - keyword=         : filter berdasarkan kata kunci di subjek/snippet
      - account=         : filter berdasarkan akun IMAP
      - limit=20         : jumlah maksimum hasil (max 50)
    """
    result = list(otp_inbox)
    if otp_only:
        result = [e for e in result if e["has_otp"]]
    if sender:
        result = [e for e in result if sender.lower() in e["sender"].lower()]
    if account:
        result = [e for e in result if account.lower() in e["account"].lower()]
    if keyword:
        kw = keyword.lower()
        result = [e for e in result if kw in e["subject"].lower() or kw in e["snippet"].lower()]
    # Public response: sembunyikan full_body dan account detail, tampilkan OTP saja
    public_result = []
    for e in result[:max(1, min(limit, 50))]:
        public_result.append({
            "id": e["id"],
            "received_at": e["received_at"],
            "date": e["date"],
            "sender": e["sender"],
            "subject": e["subject"],
            "snippet": e["snippet"],
            "otp_code": e["otp_code"],
            "has_otp": e["has_otp"],
            "highlighted": e["highlighted"],
        })
    return public_result


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/api/tailscale/status")
async def get_tailscale_status():
    ts_bin = "/home/ares/bin/tailscale"
    sock = "/home/ares/.tailscale/tailscaled.sock"
    try:
        proc = subprocess.run([ts_bin, f"--socket={sock}", "status", "--json"], capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            data = json.loads(proc.stdout)
            state = data.get("BackendState", "Unknown")
            ips = data.get("TailscaleIPs") or []
            auth_url = data.get("AuthURL")
            return {
                "running": True,
                "state": state,
                "ips": ips,
                "auth_url": auth_url,
                "is_logged_in": state == "Running" and len(ips) > 0
            }
        return {"running": False, "state": "Error", "error": proc.stderr}
    except Exception as exc:
        return {"running": False, "state": "Error", "error": str(exc)}


@app.get("/api/devices")
async def get_devices():
    appium_status = {"ready": False, "version": None}
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{APPIUM_SERVER_URL}/status")
            if resp.status_code == 200:
                data = resp.json().get("value", {})
                appium_status = {"ready": data.get("ready", False), "version": data.get("build", {}).get("version")}
    except Exception as e:
        appium_status = {"ready": False, "error": str(e)}

    adb_bin = "/home/ares/bin/adb" if os.path.exists("/home/ares/bin/adb") else "adb"
    devices = []
    try:
        proc = subprocess.run([adb_bin, "devices", "-l"], capture_output=True, text=True, timeout=5)
        for line in proc.stdout.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial = parts[0]
            state = parts[1]
            model = "Unknown"
            product = ""
            for p in parts[2:]:
                if p.startswith("model:"):
                    model = p.split(":", 1)[1]
                elif p.startswith("product:"):
                    product = p.split(":", 1)[1]

            battery_level = None
            screen_w = 1080
            screen_h = 2400
            wifi_ip = None

            if state == "device":
                # Baterai
                try:
                    bat_proc = subprocess.run([adb_bin, "-s", serial, "shell", "dumpsys", "battery"], capture_output=True, text=True, timeout=3)
                    for bat_line in bat_proc.stdout.splitlines():
                        if "level:" in bat_line:
                            battery_level = int(bat_line.split("level:")[1].strip())
                            break
                except Exception:
                    pass

                # Resolusi Layar
                try:
                    wm_proc = subprocess.run([adb_bin, "-s", serial, "shell", "wm", "size"], capture_output=True, text=True, timeout=3)
                    matches = re.findall(r"(\d+)x(\d+)", wm_proc.stdout)
                    if matches:
                        # Ambil yang terakhir (Override size jika ada, atau Physical size)
                        screen_w = int(matches[-1][0])
                        screen_h = int(matches[-1][1])
                except Exception:
                    pass

                # WiFi IP
                try:
                    ip_proc = subprocess.run([adb_bin, "-s", serial, "shell", "ip", "addr", "show", "wlan0"], capture_output=True, text=True, timeout=3)
                    ip_match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", ip_proc.stdout)
                    if ip_match:
                        wifi_ip = ip_match.group(1)
                except Exception:
                    pass

            devices.append({
                "serial": serial,
                "state": state,
                "model": model,
                "product": product,
                "battery": battery_level,
                "width": screen_w,
                "height": screen_h,
                "wifi_ip": wifi_ip,
            })
    except Exception as exc:
        log.warning("Gagal list adb devices: %s", exc)

    return {
        "appium": appium_status,
        "appium_url": APPIUM_SERVER_URL,
        "devices": devices,
    }


@app.get("/api/devices/screenshot")
async def get_device_screenshot(serial: str):
    adb_bin = "/home/ares/bin/adb" if os.path.exists("/home/ares/bin/adb") else "adb"
    try:
        proc = subprocess.run([adb_bin, "-s", serial, "exec-out", "screencap", "-p"], capture_output=True, timeout=8)
        if proc.returncode == 0 and len(proc.stdout) > 0:
            return Response(content=proc.stdout, media_type="image/png")
        return JSONResponse(status_code=500, content={"error": "Gagal capture screen", "stderr": proc.stderr.decode(errors="ignore")})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


class DeviceActionRequest(BaseModel):
    serial: str
    action: str  # 'wake', 'power', 'home', 'back', 'recent', 'volup', 'voldown', 'tap', 'swipe', 'text', 'open_url', 'tcpip', 'shell', 'battery'
    x: Optional[int] = None
    y: Optional[int] = None
    x1: Optional[int] = None
    y1: Optional[int] = None
    x2: Optional[int] = None
    y2: Optional[int] = None
    duration: Optional[int] = 300
    text: Optional[str] = None
    url: Optional[str] = None
    cmd: Optional[str] = None
    port: Optional[int] = 5555


@app.post("/api/devices/action")
async def device_action_endpoint(req: DeviceActionRequest):
    adb_bin = "/home/ares/bin/adb" if os.path.exists("/home/ares/bin/adb") else "adb"
    try:
        if req.action == "wake":
            proc = subprocess.run([adb_bin, "-s", req.serial, "shell", "input", "keyevent", "224"], capture_output=True, text=True, timeout=5)
            record_event("appium", f"Perangkat {req.serial}: Wake Layar", {"serial": req.serial})
            return {"success": True, "action": req.action, "output": proc.stdout}
        elif req.action == "power":
            proc = subprocess.run([adb_bin, "-s", req.serial, "shell", "input", "keyevent", "26"], capture_output=True, text=True, timeout=5)
            record_event("appium", f"Perangkat {req.serial}: Power Toggle", {"serial": req.serial})
            return {"success": True, "action": req.action, "output": proc.stdout}
        elif req.action == "home":
            proc = subprocess.run([adb_bin, "-s", req.serial, "shell", "input", "keyevent", "3"], capture_output=True, text=True, timeout=5)
            record_event("appium", f"Perangkat {req.serial}: Tombol Home", {"serial": req.serial})
            return {"success": True, "action": req.action, "output": proc.stdout}
        elif req.action == "back":
            proc = subprocess.run([adb_bin, "-s", req.serial, "shell", "input", "keyevent", "4"], capture_output=True, text=True, timeout=5)
            record_event("appium", f"Perangkat {req.serial}: Tombol Back", {"serial": req.serial})
            return {"success": True, "action": req.action, "output": proc.stdout}
        elif req.action in ("recent", "menu"):
            proc = subprocess.run([adb_bin, "-s", req.serial, "shell", "input", "keyevent", "187"], capture_output=True, text=True, timeout=5)
            record_event("appium", f"Perangkat {req.serial}: Tombol Recent Apps", {"serial": req.serial})
            return {"success": True, "action": req.action, "output": proc.stdout}
        elif req.action == "volup":
            proc = subprocess.run([adb_bin, "-s", req.serial, "shell", "input", "keyevent", "24"], capture_output=True, text=True, timeout=5)
            return {"success": True, "action": req.action, "output": proc.stdout}
        elif req.action == "voldown":
            proc = subprocess.run([adb_bin, "-s", req.serial, "shell", "input", "keyevent", "25"], capture_output=True, text=True, timeout=5)
            return {"success": True, "action": req.action, "output": proc.stdout}
        elif req.action == "tap":
            if req.x is None or req.y is None:
                return {"success": False, "error": "Parameter x dan y diperlukan untuk tap"}
            proc = subprocess.run([adb_bin, "-s", req.serial, "shell", "input", "tap", str(req.x), str(req.y)], capture_output=True, text=True, timeout=5)
            return {"success": True, "action": "tap", "x": req.x, "y": req.y}
        elif req.action == "swipe":
            x1 = req.x1 or 500
            y1 = req.y1 or 1500
            x2 = req.x2 or 500
            y2 = req.y2 or 500
            dur = req.duration or 300
            proc = subprocess.run([adb_bin, "-s", req.serial, "shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(dur)], capture_output=True, text=True, timeout=5)
            return {"success": True, "action": "swipe"}
        elif req.action == "text":
            if not req.text:
                return {"success": False, "error": "Parameter text diperlukan"}
            # Ganti spasi dengan %s untuk adb input text
            safe_text = req.text.replace(" ", "%s")
            proc = subprocess.run([adb_bin, "-s", req.serial, "shell", "input", "text", safe_text], capture_output=True, text=True, timeout=5)
            record_event("appium", f"Perangkat {req.serial}: Input Text", {"text": req.text})
            return {"success": True, "action": "text", "output": proc.stdout}
        elif req.action == "open_url":
            if not req.url:
                return {"success": False, "error": "Parameter url diperlukan"}
            proc = subprocess.run([adb_bin, "-s", req.serial, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", req.url], capture_output=True, text=True, timeout=5)
            return {"success": True, "action": "open_url", "output": proc.stdout}
        elif req.action == "tcpip":
            port_to_open = req.port or 5555
            proc = subprocess.run([adb_bin, "-s", req.serial, "tcpip", str(port_to_open)], capture_output=True, text=True, timeout=8)
            record_event("appium", f"Perangkat {req.serial}: Aktifkan ADB TCP/IP port {port_to_open}", {"output": proc.stdout})
            return {"success": True, "action": "tcpip", "port": port_to_open, "output": proc.stdout.strip() or "restarting in TCP mode port " + str(port_to_open)}
        elif req.action == "shell":
            if not req.cmd:
                return {"success": False, "error": "Parameter cmd diperlukan"}
            proc = subprocess.run([adb_bin, "-s", req.serial, "shell", req.cmd], capture_output=True, text=True, timeout=10)
            return {"success": True, "action": "shell", "stdout": proc.stdout, "stderr": proc.stderr, "exit_code": proc.returncode}
        elif req.action == "battery":
            proc = subprocess.run([adb_bin, "-s", req.serial, "shell", "dumpsys", "battery"], capture_output=True, text=True, timeout=5)
            info = {}
            for line in proc.stdout.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    info[k.strip()] = v.strip()
            record_event("appium", f"Perangkat {req.serial}: Cek Status Baterai", info)
            return {"success": True, "action": req.action, "battery_info": info}
        else:
            return {"success": False, "error": f"Aksi tidak dikenal: {req.action}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


class MailAccountConfig(BaseModel):
    account_name: str = "Gmail_OTP"
    imap_server: str = "imap.gmail.com"
    imap_port: int = 993
    email: str
    password: str


class SaveMailConfigRequest(BaseModel):
    accounts: list[MailAccountConfig]


class TestMailRequest(BaseModel):
    imap_server: str = "imap.gmail.com"
    imap_port: int = 993
    email: str
    password: str


@app.get("/api/mail/config")
async def get_mail_config():
    config_path = "/home/ares/Documents/myserver/mail-collector/config.json"
    if not os.path.exists(config_path):
        return {"accounts": []}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            accounts = json.load(f)
        masked = []
        for a in accounts:
            masked.append({
                "account_name": a.get("account_name", ""),
                "imap_server": a.get("imap_server", "imap.gmail.com"),
                "imap_port": a.get("imap_port", 993),
                "email": a.get("email", ""),
                "has_password": bool(a.get("password") and not a.get("password").startswith("app_password_anda")),
            })
        return {"accounts": masked}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/mail/config")
async def save_mail_config(req: SaveMailConfigRequest):
    config_path = "/home/ares/Documents/myserver/mail-collector/config.json"
    try:
        data = [acc.model_dump() for acc in req.accounts]
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        subprocess.run(["systemctl", "--user", "restart", "mail-collector.service"], check=False)
        record_event("mail", "Konfigurasi Akun IMAP Diperbarui", {"accounts": [a["email"] for a in data]})
        return {"status": "saved", "restarted": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/mail/test")
async def test_mail_connection(req: TestMailRequest):
    import ssl
    try:
        loop = asyncio.get_event_loop()
        def _test():
            ctx = ssl.create_default_context()
            with imaplib.IMAP4_SSL(req.imap_server, req.imap_port, ssl_context=ctx) as imap:
                imap.login(req.email, req.password)
                typ, _ = imap.select("INBOX")
                return typ
        res = await loop.run_in_executor(None, _test)
        return {"success": True, "message": f"Koneksi IMAP berhasil! Folder INBOX terakses ({res})"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


class TranscribeAudioRequest(BaseModel):
    audio_b64: str
    language: str = "id-ID"


@app.post("/api/transcribe")
async def transcribe_audio_endpoint(req: TranscribeAudioRequest):
    import base64
    import io
    import speech_recognition as sr
    try:
        raw_bytes = base64.b64decode(req.audio_b64)
        r = sr.Recognizer()
        with io.BytesIO(raw_bytes) as buf:
            with sr.AudioFile(buf) as source:
                audio = r.record(source)
                loop = asyncio.get_event_loop()
                text = await loop.run_in_executor(None, lambda: r.recognize_google(audio, language=req.language))
                return {"success": True, "text": text}
    except sr.UnknownValueError:
        return {"success": True, "text": "", "message": "Suara tidak terdengar jelas atau terlalu sunyi"}
    except Exception as exc:
        return {"success": False, "error": str(exc), "text": ""}


