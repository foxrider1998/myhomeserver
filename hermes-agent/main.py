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
import email
import email.header
import shlex
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import urllib.parse

import agent_tools
import stt

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

# Log events untuk ditampilkan di dashboard UI (Persisted to disk)
MAX_EVENTS = 100
DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
os.makedirs(DATA_DIR, exist_ok=True)
EVENTS_FILE = os.path.join(DATA_DIR, "events_log.json")
INBOX_FILE = os.path.join(DATA_DIR, "otp_inbox.json")

events_log: list[dict[str, Any]] = []

def _load_events():
    global events_log
    try:
        if os.path.exists(EVENTS_FILE):
            with open(EVENTS_FILE, "r", encoding="utf-8") as f:
                events_log = json.load(f)
    except Exception as e:
        log.warning("Gagal memuat events_log dari disk: %s", e)

def _save_events():
    try:
        with open(EVENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(events_log[:MAX_EVENTS], f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("Gagal menyimpan events_log ke disk: %s", e)

_load_events()

# OTP Inbox (email masuk dengan deteksi OTP - Persisted to disk)
MAX_INBOX = 500
otp_inbox: list[dict[str, Any]] = []
_inbox_id_counter = 0

def _load_inbox():
    global otp_inbox, _inbox_id_counter
    try:
        if os.path.exists(INBOX_FILE):
            with open(INBOX_FILE, "r", encoding="utf-8") as f:
                otp_inbox = json.load(f)
                if otp_inbox:
                    _inbox_id_counter = max((item.get("id", 0) for item in otp_inbox), default=0)
    except Exception as e:
        log.warning("Gagal memuat otp_inbox dari disk: %s", e)

def _save_inbox():
    try:
        with open(INBOX_FILE, "w", encoding="utf-8") as f:
            json.dump(otp_inbox[:MAX_INBOX], f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("Gagal menyimpan otp_inbox ke disk: %s", e)

_load_inbox()


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
    _save_events()


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
                _save_inbox()
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
    _save_inbox()
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
    system_prompt: str | None = None


class MeetingSummarizeRequest(BaseModel):
    title: str = "Meeting"
    transcript: str
    language: str = "id"
    model: str | None = None
    context: str = ""


class MeetingCleanTranscriptRequest(BaseModel):
    transcript: str
    model: str | None = None
    language: str = "id-ID"
    title: str = "Meeting"


current_state = {
    "last_soc": None,
    "last_alert": None,
    "last_poll_time": None,
    "connected_to_solar": False,
}


SPECTRUM_DATA_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "spectrum_sprint_data.json"))


async def call_llm(prompt: str, model: str | None = None, system_prompt: str | None = None) -> str:
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

    # Konteks SPEctrum dibangun per-user oleh gateway (powmr_service); jangan pernah memakai data global.
    sys_ctx = system_prompt or ""
    conversation = []
    if sys_ctx:
        conversation.append({"role": "system", "content": sys_ctx})
    conversation.append({"role": "user", "content": prompt})

    last_error = None
    for attempt_model in models_to_try:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{SUMOPOD_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {SUMOPOD_API_KEY}"},
                    json={
                        "model": attempt_model,
                        "messages": conversation,
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
    reply = await call_llm(req.prompt, req.model, system_prompt=req.system_prompt or "")
    return {"prompt": req.prompt, "reply": reply, "model": req.model or SUMOPOD_MODEL}


@app.get("/api/spectrum/data")
async def get_spectrum_data_endpoint():
    try:
        if os.path.exists(SPECTRUM_DATA_FILE):
            with open(SPECTRUM_DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"error": "spectrum_sprint_data.json tidak ditemukan"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/spectrum/sync")
async def sync_spectrum_endpoint():
    try:
        sync_script = "/home/ares/Documents/myserver/sync_spectrum.py"
        res = subprocess.run(["python3", sync_script], capture_output=True, text=True, timeout=35)
        if res.returncode == 0:
            record_event("system", "Sinkronisasi SPEctrum Berhasil", {"tasks": 30})
            if os.path.exists(SPECTRUM_DATA_FILE):
                with open(SPECTRUM_DATA_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            return {"success": True}
        else:
            raise HTTPException(status_code=500, detail=f"Gagal sync: {res.stderr}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/clean-transcript")
async def clean_transcript_endpoint(req: MeetingCleanTranscriptRequest):
    if not req.transcript.strip():
        return {"success": False, "error": "Transkrip kosong", "cleaned": ""}

    system_prompt = """Kamu adalah AI Spesialis Restorasi & Normalisasi Transkrip Suara (Speech-to-Text Cleaner) untuk Tim IT, QA, & Software Engineering di Westpro Server.
Tugasmu adalah merekonstruksi dan merapikan transkrip suara mentah yang 'ngaco', tidak ada tanda baca, atau mengalami salah dengar fonetik (acoustic misrecognition) dari mikrofon.

Konteks Khusus Tim & Domain:
- Domain Kerja: Testing QA SPEctrum, BNI YAP!, PNRI, Mobile App Android/iOS, Backend API, Billing, VA (Virtual Account), Payment Gateway, Staging, Production, Inverter PLTS PowMr, dsb.
- Nama yang sering disebut: Aris, Mas Roji, Mas Niko, Mbak, dsb.

Aturan Restorasi & Koreksi:
1. Koreksi kata-kata bahasa Inggris/IT yang salah didengar secara fonetik bahasa Indonesia:
   - 'haid' / 'tidak haid' / 'di haid' -> 'hide' / 'tidak di-hide' / 'di-hide'
   - 'diut' / 'di ut' -> 'di-mute'
   - 'refer' / 'revert' -> 'revert' atau 'reference' (sesuai konteks)
   - 'bubble' -> 'publish' / 'build'
   - 'dikonsumsi' -> 'dikonfirmasi'
   - 'penari' -> 'PNRI'
   - 'siap' -> 'YAP!' (jika terkait BNI YAP!)
   - 'tes ke' / 'test ke' -> 'test case'
   - 'mbak di va' -> 'bug di VA' (bedakan dengan sapaan rekan kerja 'Mbak')
   - 'stasiun' -> 'staging' (jika konteks environment/server)
   - 'kcek' -> 'cek'
   - 'copas' -> 'kolom Copas'
2. Bersihkan stuttering / gagap / pengulangan kata tak disengaja dari mic (misal: 'aku aku aku', 'itu itu aja', 'dari dari').
3. Bubuhkan tanda baca lengkap (titik, koma, tanda tanya, tanda seru) dan huruf kapital pada awal kalimat serta nama produk/entitas (BNI, YAP!, PNRI, QA, API, VA, ID, dll).
4. Rapikan menjadi paragraf-paragraf dialog atau topik yang mengalir teratur dan mudah dibaca.
5. JANGAN mengubah esensi, keputusan, angka, atau fakta yang dibicarakan oleh peserta meeting.

Keluarkan HANYA teks transkrip bersih hasil perbaikan, tanpa salam pembuka, penutup, atau penjelasan pengantar."""

    prompt = f"Perbaiki dan rapikan transkrip mentah berikut:\n---\n{req.transcript}\n---"
    cleaned = await call_llm(prompt, model=req.model, system_prompt=system_prompt)
    cleaned = cleaned.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()

    return {"success": True, "cleaned": cleaned, "model": req.model or SUMOPOD_MODEL}


@app.post("/api/summarize-meeting")
async def summarize_meeting_endpoint(req: MeetingSummarizeRequest):
    if not req.transcript.strip():
        raise HTTPException(status_code=400, detail="Transkrip meeting kosong")

    context_block = ""
    if req.context.strip():
        context_block = ("KONTEKS SPRINT & TASK TERKAIT (rujukan saja; hubungkan keputusan/tindak lanjut ke kode task "
                         "bila memang dibahas, jangan mengarang):\n" + req.context[:3500] + "\n---")
    prompt = f"""Kamu adalah notulen meeting profesional dan asisten AI Hermes di Westpro Home Server.
Berikut adalah transkrip rekaman suara pembicaraan meeting tim IT/QA (SPEctrum, BNI YAP!, PNRI, Testing):
---
{req.transcript}
---
{context_block}

PENTING - Catatan Koreksi Fonetik STT:
Transkrip di atas berasal dari Speech-to-Text mikrofon yang mungkin masih mengandung salah dengar fonetik:
- 'haid' = 'hide' (fitur di-hide)
- 'diut' = 'di-mute'
- 'refer' = 'revert'
- 'bubble' = 'publish'
- 'dikonsumsi' = 'dikonfirmasi'
- 'penari' = 'PNRI'
- 'siap' = 'YAP!'
- 'tes ke' = 'test case'
- 'mbak' = bisa berarti panggilan rekan kerja atau 'bug' sesuai konteks kalimat.
Pahami konteks asli pembicaraan teknis di atas dengan benar sebelum membuat notulen.

Tolong buatkan dokumen notulen & rangkuman meeting yang sangat rapi, jelas, dan terstruktur dalam Bahasa Indonesia:
1. 🎯 **Ringkasan Eksekutif & Topik Utama**
2. 💡 **Poin-Poin Kunci Diskusi & Keputusan Teknis**
3. ✅ **Keputusan yang Disepakati (Decisions Made)**
4. 📌 **Daftar Tindak Lanjut / Action Items** (Sebutkan PIC seperti Aris, Mas Roji, Mas Niko, Mbak, dll beserta tugas spesifik)
5. ⚠️ **Catatan Khusus, Resiko, & Kendala**

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
    _save_inbox()
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
def get_tailscale_status():
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
def get_devices():
    appium_status = {"ready": False, "version": None}
    try:
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(f"{APPIUM_SERVER_URL}/status")
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
def get_device_screenshot(serial: Optional[str] = None, quality: str = "medium"):
    adb_bin = "/home/ares/bin/adb" if os.path.exists("/home/ares/bin/adb") else "adb"
    if not serial:
        try:
            p = subprocess.run([adb_bin, "devices"], capture_output=True, text=True, timeout=4)
            for line in p.stdout.strip().split("\n")[1:]:
                parts = line.strip().split()
                if len(parts) >= 2 and parts[1] == "device":
                    serial = parts[0]
                    break
        except Exception:
            pass
    if not serial:
        raise HTTPException(status_code=400, detail="Tidak ada device Android yang terhubung")

    try:
        proc = subprocess.run([adb_bin, "-s", serial, "exec-out", "screencap", "-p"], capture_output=True, timeout=8)
        if proc.returncode == 0 and len(proc.stdout) > 0:
            if quality == "original":
                return Response(content=proc.stdout, media_type="image/png")
            try:
                import io
                from PIL import Image
                with Image.open(io.BytesIO(proc.stdout)) as img:
                    rgb_img = img.convert("RGB")
                    cur_w, cur_h = rgb_img.size

                    if quality == "low":
                        max_w = 600
                        jpg_q = 60
                    elif quality == "medium":
                        max_w = 840
                        jpg_q = 76
                    else:  # high
                        max_w = 1080
                        jpg_q = 88

                    if cur_w > max_w:
                        new_h = int(cur_h * (max_w / cur_w))
                        rgb_img = rgb_img.resize((max_w, new_h), Image.Resampling.BILINEAR)

                    buf = io.BytesIO()
                    rgb_img.save(buf, format="JPEG", quality=jpg_q, optimize=True)
                    return Response(content=buf.getvalue(), media_type="image/jpeg")
            except Exception as pil_err:
                log.warning("Pillow compress error, returning raw PNG: %s", pil_err)
                return Response(content=proc.stdout, media_type="image/png")

        return JSONResponse(status_code=500, content={"error": "Gagal capture screen", "stderr": proc.stderr.decode(errors="ignore")})
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


class DeviceActionRequest(BaseModel):
    serial: Optional[str] = None
    action: str  # 'wake', 'power', 'home', 'back', 'recent', 'volup', 'voldown', 'tap', 'swipe', 'swipe_left', 'swipe_right', 'swipe_up', 'swipe_down', 'text', 'open_url', 'tcpip', 'shell', 'battery'
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
def device_action_endpoint(req: DeviceActionRequest):
    adb_bin = "/home/ares/bin/adb" if os.path.exists("/home/ares/bin/adb") else "adb"
    if not req.serial:
        try:
            p = subprocess.run([adb_bin, "devices"], capture_output=True, text=True, timeout=4)
            for line in p.stdout.strip().split("\n")[1:]:
                parts = line.strip().split()
                if len(parts) >= 2 and parts[1] == "device":
                    req.serial = parts[0]
                    break
        except Exception:
            pass
    if not req.serial:
        raise HTTPException(status_code=400, detail="Tidak ada device Android yang terhubung")
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
        elif req.action == "swipe_left":
            x1 = req.x1 or 850
            y1 = req.y1 or 1000
            x2 = req.x2 or 150
            y2 = req.y2 or 1000
            dur = req.duration or 250
            proc = subprocess.run([adb_bin, "-s", req.serial, "shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(dur)], capture_output=True, text=True, timeout=5)
            return {"success": True, "action": "swipe_left"}
        elif req.action == "swipe_right":
            x1 = req.x1 or 150
            y1 = req.y1 or 1000
            x2 = req.x2 or 850
            y2 = req.y2 or 1000
            dur = req.duration or 250
            proc = subprocess.run([adb_bin, "-s", req.serial, "shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(dur)], capture_output=True, text=True, timeout=5)
            return {"success": True, "action": "swipe_right"}
        elif req.action == "swipe_up":
            x1 = req.x1 or 500
            y1 = req.y1 or 1500
            x2 = req.x2 or 500
            y2 = req.y2 or 500
            dur = req.duration or 250
            proc = subprocess.run([adb_bin, "-s", req.serial, "shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(dur)], capture_output=True, text=True, timeout=5)
            return {"success": True, "action": "swipe_up"}
        elif req.action == "swipe_down":
            x1 = req.x1 or 500
            y1 = req.y1 or 500
            x2 = req.x2 or 500
            y2 = req.y2 or 1500
            dur = req.duration or 250
            proc = subprocess.run([adb_bin, "-s", req.serial, "shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(dur)], capture_output=True, text=True, timeout=5)
            return {"success": True, "action": "swipe_down"}
        elif req.action in ("key", "keyevent"):
            code = req.cmd or (str(req.x) if req.x is not None else None) or req.text
            if not code:
                return {"success": False, "error": "Parameter key/code diperlukan"}
            proc = subprocess.run([adb_bin, "-s", req.serial, "shell", "input", "keyevent", str(code)], capture_output=True, text=True, timeout=5)
            return {"success": True, "action": "keyevent", "key": str(code), "output": proc.stdout}
        elif req.action == "text":
            if not req.text:
                return {"success": False, "error": "Parameter text diperlukan"}
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


class ConnectDeviceRequest(BaseModel):
    target: Optional[str] = None  # e.g. "100.85.12.34:5555" or "192.168.1.50:5555"
    ip: Optional[str] = None
    port: Optional[int] = 5555


@app.post("/api/devices/connect")
def connect_adb_device(req: ConnectDeviceRequest):
    adb_bin = "/home/ares/bin/adb" if os.path.exists("/home/ares/bin/adb") else "adb"
    target = (req.target or "").strip()
    if not target and req.ip:
        target = f"{req.ip.strip()}:{req.port or 5555}"
    if not target:
        return {"success": False, "error": "Target IP/Host diperlukan"}
    if ":" not in target:
        target = f"{target}:5555"
    try:
        proc = subprocess.run([adb_bin, "connect", target], capture_output=True, text=True, timeout=10)
        output = (proc.stdout + " " + proc.stderr).strip()
        success = "connected" in output.lower() or "already connected" in output.lower()
        record_event("appium", f"Koneksi ADB Network/Tailscale: {target}", {"success": success, "output": output})
        return {"success": success, "output": output, "target": target}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@app.post("/api/devices/disconnect")
def disconnect_adb_device(req: ConnectDeviceRequest):
    adb_bin = "/home/ares/bin/adb" if os.path.exists("/home/ares/bin/adb") else "adb"
    target = (req.target or "").strip()
    if not target and req.ip:
        target = f"{req.ip.strip()}:{req.port or 5555}"
    if not target:
        return {"success": False, "error": "Target IP/Host diperlukan"}
    if ":" not in target:
        target = f"{target}:5555"
    try:
        proc = subprocess.run([adb_bin, "disconnect", target], capture_output=True, text=True, timeout=10)
        output = (proc.stdout + " " + proc.stderr).strip()
        return {"success": True, "output": output, "target": target}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@app.post("/api/devices/scan-wifi")
def scan_and_connect_wifi_adb():
    """
    Auto-scan subnet Wi-Fi lokal dan mDNS untuk mencari HP Android dengan port ADB 5555,
    lalu secara otomatis menyambungkannya via `adb connect`.
    Murni menggunakan Python standard library (ipaddress, socket, concurrent.futures).
    """
    adb_bin = "/home/ares/bin/adb" if os.path.exists("/home/ares/bin/adb") else "adb"
    import ipaddress
    import socket
    from concurrent.futures import ThreadPoolExecutor

    # 1. Deteksi subnet lokal dari perintah sistem (wlan0 / eth0)
    subnets = []
    my_ips = set()
    try:
        proc = subprocess.run(["ip", "-4", "addr", "show"], capture_output=True, text=True, timeout=3)
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("inet ") and not line.startswith("inet 127."):
                cidr = line.split()[1]
                my_ips.add(cidr.split("/")[0])
                net = ipaddress.ip_network(cidr, strict=False)
                if net not in subnets:
                    subnets.append(net)
    except Exception:
        pass

    if not subnets:
        subnets.append(ipaddress.ip_network("172.16.5.0/24", strict=False))

    found_targets = []

    # 2. Cek perangkat yang sudah tersambung di `adb devices` & bersihkan status offline yang macet
    try:
        dev_proc = subprocess.run([adb_bin, "devices"], capture_output=True, text=True, timeout=5)
        for line in dev_proc.stdout.splitlines():
            line = line.strip()
            if "\toffline" in line and ":" in line:
                # Bersihkan target offline yang macet agar tidak mengganggu koneksi baru
                offline_target = line.split("\t")[0].strip()
                subprocess.run([adb_bin, "disconnect", offline_target], capture_output=True, timeout=3)
            elif "\tdevice" in line and ":" in line:
                dev_target = line.split("\t")[0].strip()
                if dev_target not in found_targets:
                    found_targets.append(dev_target)
    except Exception:
        pass

    # 3. Cek perangkat USB: aktifkan tcpip 5555 dan deteksi wlan0 IP HP langsung
    try:
        usb_proc = subprocess.run([adb_bin, "devices", "-l"], capture_output=True, text=True, timeout=5)
        for line in usb_proc.stdout.splitlines():
            if "usb:" in line and "device" in line:
                serial = line.split()[0]
                subprocess.run([adb_bin, "-s", serial, "tcpip", "5555"], capture_output=True, timeout=5)
                ip_proc = subprocess.run([adb_bin, "-s", serial, "shell", "ip -4 addr show wlan0"], capture_output=True, text=True, timeout=5)
                for ip_line in ip_proc.stdout.splitlines():
                    if "inet " in ip_line:
                        phone_ip = ip_line.strip().split()[1].split("/")[0]
                        if phone_ip and phone_ip not in my_ips:
                            target_candidate = f"{phone_ip}:5555"
                            if target_candidate not in found_targets:
                                found_targets.append(target_candidate)
                            try:
                                net_cand = ipaddress.ip_network(f"{phone_ip}/24", strict=False)
                                if net_cand not in subnets:
                                    subnets.append(net_cand)
                            except Exception:
                                pass
    except Exception:
        pass

    # 4. Bangunkan perangkat Wi-Fi yang sleep via fping & ambil kandidat IP dari ARP Cache sistem
    for net in subnets:
        try:
            subprocess.run(["fping", "-a", "-g", "-r", "0", "-t", "80", str(net)], capture_output=True, timeout=3.5)
        except Exception:
            pass

    arp_candidates = []
    try:
        with open("/proc/net/arp", "r") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[2] == "0x2" and parts[3] != "00:00:00:00:00:00":
                    ip_str = parts[0]
                    if ip_str not in my_ips and ip_str not in arp_candidates:
                        arp_candidates.append(ip_str)
    except Exception:
        pass

    try:
        n_proc = subprocess.run(["ip", "neigh", "show"], capture_output=True, text=True, timeout=3)
        for line in n_proc.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 1 and "." in parts[0]:
                ip_str = parts[0]
                if ip_str not in my_ips and ip_str not in arp_candidates:
                    arp_candidates.append(ip_str)
    except Exception:
        pass

    # Probe cepat prioritas ke semua IP aktif di ARP cache (port 5555, 5554, 5556, 5557, 5558)
    def probe_arp(target_ip):
        found = []
        for port in (5555, 5554, 5556, 5557, 5558):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                try:
                    if sock.connect_ex((target_ip, port)) == 0:
                        found.append(f"{target_ip}:{port}")
                except Exception:
                    pass
        return found

    with ThreadPoolExecutor(max_workers=25) as ex:
        for res_list in ex.map(probe_arp, arp_candidates):
            for res in res_list:
                if res and res not in found_targets:
                    found_targets.append(res)

    # 5. Cek mDNS services Android 11+ dengan ADB_MDNS_OPENSCREEN=1
    try:
        mdns_env = {**os.environ, "ADB_MDNS_OPENSCREEN": "1"}
        mdns_proc = subprocess.run([adb_bin, "mdns", "services"], capture_output=True, text=True, timeout=5, env=mdns_env)
        for line in mdns_proc.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 3 and ":" in parts[-1]:
                target = parts[-1]
                if target not in found_targets:
                    found_targets.append(target)
    except Exception:
        pass

    # 6. Multi-threaded TCP probe port 5555 pada seluruh host subnet
    def probe_host(host_ip):
        str_ip = str(host_ip)
        if str_ip in my_ips:
            return None
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.6)
            try:
                if sock.connect_ex((str_ip, 5555)) == 0:
                    return f"{str_ip}:5555"
            except Exception:
                pass
        return None

    all_hosts = []
    for net in subnets:
        all_hosts.extend(list(net.hosts()))

    with ThreadPoolExecutor(max_workers=40) as executor:
        for res in executor.map(probe_host, all_hosts):
            if res and res not in found_targets:
                found_targets.append(res)

    # 7. Otomatis jalankan `adb connect <target>` untuk semua target yang ditemukan
    connected = []
    for target in found_targets:
        try:
            conn_proc = subprocess.run([adb_bin, "connect", target], capture_output=True, text=True, timeout=10)
            out = (conn_proc.stdout + " " + conn_proc.stderr).strip()
            is_ok = "connected" in out.lower() or "already connected" in out.lower()

            model = "Android Device"
            if is_ok:
                try:
                    m_proc = subprocess.run([adb_bin, "-s", target, "shell", "getprop ro.product.model"], capture_output=True, text=True, timeout=4)
                    if m_proc.stdout.strip():
                        model = m_proc.stdout.strip()
                except Exception:
                    pass

            connected.append({
                "target": target,
                "status": "connected" if is_ok else "failed",
                "message": out,
                "model": model
            })
            record_event("appium", f"Auto-Scan ADB Wi-Fi: {target} ({model})", {"success": is_ok, "output": out})
        except Exception as e:
            connected.append({
                "target": target,
                "status": "error",
                "message": str(e)
            })

    return {
        "success": True,
        "scanned_subnets": [str(s) for s in subnets],
        "total_hosts_scanned": len(all_hosts),
        "found_targets": found_targets,
        "connected": connected,
        "count": len(connected)
    }


class MailAccountConfig(BaseModel):
    account_name: str = "Gmail_OTP"
    imap_server: str = "imap.gmail.com"
    imap_port: int = 993
    email: str
    password: str = ""


class SaveMailConfigRequest(BaseModel):
    accounts: list[MailAccountConfig]


class TestMailRequest(BaseModel):
    imap_server: str = "imap.gmail.com"
    imap_port: int = 993
    email: str
    password: str


DEFAULT_IMAP_ACCOUNTS = [
    {
        "account_name": "Gmail_Ares",
        "imap_server": "imap.gmail.com",
        "imap_port": 993,
        "email": "areskretindo@gmail.com",
        "password": "jugx ibxc tutj zbsk"
    }
]


@app.get("/api/mail/config")
async def get_mail_config():
    config_path = "/home/ares/Documents/myserver/mail-collector/config.json"
    accounts = []
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                accounts = json.load(f)
        except Exception:
            pass

    if not accounts:
        accounts = list(DEFAULT_IMAP_ACCOUNTS)
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(accounts, f, indent=2)
        except Exception:
            pass

    try:
        masked = []
        for a in accounts:
            masked.append({
                "account_name": a.get("account_name", ""),
                "imap_server": a.get("imap_server", "imap.gmail.com"),
                "imap_port": a.get("imap_port", 993),
                "email": a.get("email", ""),
                "has_password": bool(a.get("password") and not a.get("password").startswith("app_password_anda")),
                "is_default": a.get("email", "").lower() == "areskretindo@gmail.com"
            })
        return {"accounts": masked, "max_accounts": 20}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/mail/config")
async def save_mail_config(req: SaveMailConfigRequest):
    if len(req.accounts) > 20:
        raise HTTPException(status_code=400, detail="Maksimal 20 akun IMAP yang didukung.")

    config_path = "/home/ares/Documents/myserver/mail-collector/config.json"
    try:
        # Load existing passwords to preserve them if not provided
        existing_passwords = {}
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    for a in json.load(f):
                        if a.get("email"):
                            existing_passwords[a["email"]] = a.get("password", "")
            except Exception:
                pass

        # Fallback default password for areskretindo@gmail.com if empty
        existing_passwords.setdefault("areskretindo@gmail.com", "jugx ibxc tutj zbsk")

        data = []
        for acc in req.accounts:
            d = acc.model_dump()
            if not d.get("password") and d.get("email") in existing_passwords:
                d["password"] = existing_passwords[d["email"]]
            data.append(d)

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        subprocess.run(["systemctl", "--user", "restart", "mail-collector.service"], check=False)
        record_event("mail", "Konfigurasi Akun IMAP Diperbarui", {"accounts": [a["email"] for a in data]})
        return {"status": "saved", "restarted": True, "accounts": len(data), "max_accounts": 20}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/mail/config")
async def delete_mail_account(email: str):
    config_path = "/home/ares/Documents/myserver/mail-collector/config.json"
    if not os.path.exists(config_path):
        return {"status": "ok"}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            accounts = json.load(f)
        accounts = [a for a in accounts if a.get("email") != email]
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2)
        subprocess.run(["systemctl", "--user", "restart", "mail-collector.service"], check=False)
        record_event("mail", f"Akun IMAP Dihapus: {email}")
        return {"status": "deleted", "accounts": len(accounts)}
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


# ==================== OTP RULES & IMAP DIRECT SYNC ====================
RULES_PATH = "/home/ares/Documents/myserver/data/otp_filter_config.json"

DEFAULT_FILTER_RULES = {
    "include_senders": [],
    "include_keywords": [
        "otp", "verifikasi", "verification", "kode", "code", "token", "pin", "sandi", "password",
        "konfirmasi", "confirm", "security code", "passcode", "access code", "two-factor", "2fa",
        "autentikasi", "authentication", "bni", "yap"
    ],
    "exclude_senders": [
        "newsletter", "promo", "marketing", "noreply-offer", "updates"
    ],
    "exclude_keywords": [
        "promo", "diskon", "cashback", "newsletter", "webinar", "unsubscribe", "potongan harga", "voucher"
    ],
    "fetch_limit": 30
}

def load_filter_rules() -> dict:
    if os.path.exists(RULES_PATH):
        try:
            with open(RULES_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return dict(DEFAULT_FILTER_RULES)

def decode_mime_header(raw: str | None) -> str:
    if not raw:
        return ""
    import email.header
    parts = email.header.decode_header(raw)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)

def extract_otp_code(text: str) -> str | None:
    # 1. Pola kata kunci diikuti kode (bisa lewat baris baru, bintang, spasi, dsb.)
    pat = re.compile(r'(?:kode|code|otp|token|pin|verifikasi|verification|passcode|security code)[\s\S]{0,150}?[\*\s\:\-]*\b(\d{4,8})\b', re.IGNORECASE)
    m = pat.search(text)
    if m:
        return m.group(1)
    # 2. Pola 6 digit terisolasi (misal *181964* atau baris baru)
    star_pat = re.compile(r'[\*\s\:\>]\b(\d{6})\b[\*\s\<\.]')
    m2 = star_pat.search(text)
    if m2:
        return m2.group(1)
    return None

class OtpFilterRulesRequest(BaseModel):
    include_senders: list[str] = []
    include_keywords: list[str] = []
    exclude_senders: list[str] = []
    exclude_keywords: list[str] = []
    fetch_limit: int = 30

@app.get("/api/otp/rules")
async def get_otp_rules():
    return load_filter_rules()

@app.post("/api/otp/rules")
async def save_otp_rules(req: OtpFilterRulesRequest):
    data = req.model_dump()
    try:
        with open(RULES_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        subprocess.run(["systemctl", "--user", "restart", "mail-collector.service"], check=False)
        record_event("mail", "Aturan Filter OTP Diperbarui", data)
        return {"status": "saved", "rules": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/otp/sync")
async def sync_otp_inbox_now(limit: int = 30):
    """Pindai ulang IMAP inbox secara langsung dan terapkan filter rules."""
    config_path = "/home/ares/Documents/myserver/mail-collector/config.json"
    if not os.path.exists(config_path):
        return {"status": "no_accounts", "added": 0, "total": len(otp_inbox)}

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            accounts = json.load(f)
    except Exception as e:
        return {"status": "error", "message": str(e), "added": 0}

    rules = load_filter_rules()
    include_senders = [s.lower().strip() for s in rules.get("include_senders", []) if s.strip()]
    include_keywords = [k.lower().strip() for k in rules.get("include_keywords", []) if k.strip()]
    exclude_senders = [s.lower().strip() for s in rules.get("exclude_senders", []) if s.strip()]
    exclude_keywords = [k.lower().strip() for k in rules.get("exclude_keywords", []) if k.strip()]

    loop = asyncio.get_event_loop()
    def _do_sync():
        added_count = 0
        for acc in accounts:
            email_addr = acc.get("email")
            passwd = acc.get("password")
            server = acc.get("imap_server", "imap.gmail.com")
            port = acc.get("imap_port", 993)
            acc_name = acc.get("account_name", email_addr)
            if not email_addr or not passwd:
                continue
            try:
                with imaplib.IMAP4_SSL(server, port) as imap:
                    imap.login(email_addr, passwd)
                    imap.select("INBOX")
                    typ, data = imap.search(None, "ALL")
                    if typ != "OK" or not data or not data[0]:
                        continue
                    ids = data[0].split()
                    recent_ids = ids[-max(1, min(limit, 100)):]
                    for mid in reversed(recent_ids):
                        try:
                            t, fetch_data = imap.fetch(mid, "(RFC822)")
                            if t != "OK" or not fetch_data or not fetch_data[0]:
                                continue
                            raw = fetch_data[0][1]
                            msg = email.message_from_bytes(raw)
                            sender = decode_mime_header(msg.get("From", ""))
                            subject = decode_mime_header(msg.get("Subject", ""))
                            date_str = msg.get("Date", "")

                            body = ""
                            if msg.is_multipart():
                                for p in msg.walk():
                                    if p.get_content_type() == "text/plain":
                                        payload = p.get_payload(decode=True) or b""
                                        body = payload.decode(p.get_content_charset() or "utf-8", errors="replace")
                                        break
                            else:
                                payload = msg.get_payload(decode=True) or b""
                                body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")

                            full_text = f"{subject} {body}"
                            snippet = body.strip()[:500] if body else ""

                            # Check Excludes
                            if any(ex in sender.lower() for ex in exclude_senders):
                                continue
                            if any(ex in full_text.lower() for ex in exclude_keywords):
                                continue

                            # Check Includes
                            if include_senders and not any(inc in sender.lower() for inc in include_senders):
                                continue

                            otp = extract_otp_code(full_text)
                            has_inc_kw = any(kw in full_text.lower() for kw in include_keywords) if include_keywords else True

                            if has_inc_kw or otp:
                                new_id = add_to_inbox(
                                    account=acc_name,
                                    sender=sender,
                                    subject=subject,
                                    date=date_str,
                                    snippet=snippet,
                                    otp_code=otp,
                                    full_body=body[:2000]
                                )
                                if new_id:
                                    added_count += 1
                        except Exception as ex_item:
                            log.warning("Sync item error: %s", ex_item)
            except Exception as ex_acc:
                log.warning("Sync account %s error: %s", email_addr, ex_acc)
        return added_count

    synced_added = await loop.run_in_executor(None, _do_sync)
    record_event("mail", f"Penyelarasan IMAP Selesai", {"synced": synced_added, "total": len(otp_inbox)})
    return {"status": "ok", "synced": synced_added, "total": len(otp_inbox)}

@app.on_event("startup")
async def on_startup_sync():
    asyncio.create_task(sync_otp_inbox_now(limit=30))


# ==================== TRANSKRIPSI FILE REKAMAN (WAV/MP3/M4A/WEBM/...) ====================
@app.post("/api/transcribe-file")
async def transcribe_file_start(request: Request):
    """Body = bytes audio mentah. Header: X-Filename, X-Language (id-ID), X-Engine (auto|ai|google)."""
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="File audio kosong")
    try:
        job_id = await stt.start_job(
            raw,
            request.headers.get("X-Filename", "rekaman"),
            request.headers.get("X-Language", "id-ID"),
            request.headers.get("X-Engine", "auto"),
            request.headers.get("X-Mode", "transcribe"),
            urllib.parse.unquote(request.headers.get("X-Title", "")),
            urllib.parse.unquote(request.headers.get("X-Context", "")),
        )
    except ValueError as e:
        raise HTTPException(status_code=413, detail=str(e))
    return {"success": True, "job_id": job_id}


@app.get("/api/transcribe-file/{job_id}")
async def transcribe_file_status(job_id: str):
    job = stt.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job tidak ditemukan atau sudah kedaluwarsa")
    return job


# ==================== AGEN: HERMES MENJALANKAN ALAT (adb, jaringan, appium, status server) ====================
agent_tools.register_hooks(
    get_devices=get_devices,
    scan_wifi=scan_and_connect_wifi_adb,
    tailscale_status=get_tailscale_status,
)


class AgentRequest(BaseModel):
    prompt: str
    model: str | None = None


class AgentActionRequest(BaseModel):
    id: str


@app.post("/api/agent")
async def agent_run(req: AgentRequest):
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="Perintah tidak boleh kosong")
    result = await agent_tools.run_agent(req.prompt.strip(), call_llm, req.model)
    record_event("agent", "Perintah agen: " + req.prompt[:80], {"steps": [s["tool"] for s in result["steps"]]})
    result["model"] = req.model or SUMOPOD_MODEL
    return result


@app.post("/api/agent/confirm")
async def agent_confirm(req: AgentActionRequest):
    res = await agent_tools.confirm_action(req.id)
    record_event("agent", "Aksi agen disetujui: " + str(res.get("tool")), {"ok": res.get("success")})
    return res


@app.post("/api/agent/cancel")
async def agent_cancel(req: AgentActionRequest):
    return agent_tools.cancel_action(req.id)
