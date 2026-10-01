"""Hermes Agent - webhook receiver + orchestrator.

Terima trigger dari mail-collector (email baru) dan dari solar watcher internal
(SOC baterai rendah/tinggi), lalu putuskan aksi lewat LLM eksternal (Sumopod -
OpenAI-compatible), bukan model lokal, supaya hemat RAM di ThinkCentre.

CATATAN: SUMOPOD_BASE_URL di bawah ini masih dugaan berdasarkan konvensi domain
Sumopod (ai.sumopod.com untuk dashboard, api-gate.sumopod.com untuk API publik) -
belum terverifikasi ke dokumentasi resmi mereka. Cek ulang di dashboard
(ai.sumopod.com -> AI -> API Keys, biasanya ada contoh base_url di situ) sebelum
deploy. Kalau ternyata beda, cukup ubah SUMOPOD_BASE_URL di .env, tidak perlu
ubah kode ini.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("hermes-agent")

SUMOPOD_API_KEY = os.environ.get("SUMOPOD_API_KEY", "")
SUMOPOD_BASE_URL = os.environ.get("SUMOPOD_BASE_URL", "https://ai.sumopod.com/v1")
SUMOPOD_MODEL = os.environ.get("SUMOPOD_MODEL", "gpt-4o-mini")

SOLAR_DASHBOARD_URL = os.environ.get("SOLAR_DASHBOARD_URL", "http://solar-dashboard:3000")
SOLAR_POLL_SECONDS = int(os.environ.get("SOLAR_POLL_SECONDS", "60"))
SOC_LOW_THRESHOLD = float(os.environ.get("SOC_LOW_THRESHOLD", "20"))
SOC_HIGH_THRESHOLD = float(os.environ.get("SOC_HIGH_THRESHOLD", "95"))

APPIUM_SERVER_URL = os.environ.get("APPIUM_SERVER_URL", "http://localhost:4723")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


class MailEvent(BaseModel):
    account: str
    sender: str
    subject: str
    date: str
    snippet: str


async def call_llm(prompt: str) -> str:
    if not SUMOPOD_API_KEY:
        log.warning("SUMOPOD_API_KEY kosong, skip pemanggilan LLM")
        return ""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SUMOPOD_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {SUMOPOD_API_KEY}"},
            json={
                "model": SUMOPOD_MODEL,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def trigger_appium_task(task_name: str, **context) -> None:
    """Stub - Appium tidak punya endpoint generik "jalankan task X". Isi fungsi ini
    dengan appium-python-client: buat W3C session ke APPIUM_SERVER_URL dengan
    capabilities device fisik (udid, platformName, automationName=UiAutomator2),
    jalankan langkah sesuai task_name, lalu quit session. Untuk sekarang cuma log."""
    log.info("TODO trigger_appium_task: %s context=%s (server=%s)", task_name, context, APPIUM_SERVER_URL)


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
        except Exception as exc:
            log.warning("Gagal kirim Telegram: %s", exc)


async def solar_watcher() -> None:
    """Poll /api/state dari solar-dashboard, trigger otomasi saat SOC melewati
    threshold. State in-memory sederhana supaya tidak spam trigger tiap poll."""
    last_alert: str | None = None
    async with httpx.AsyncClient() as client:
        while True:
            try:
                resp = await client.get(f"{SOLAR_DASHBOARD_URL}/api/state", timeout=10)
                resp.raise_for_status()
                state = resp.json()
                soc = state.get("soc")

                if soc is not None and soc <= SOC_LOW_THRESHOLD and last_alert != "low":
                    log.info("SOC rendah (%.1f%%) - trigger mode hemat daya", soc)
                    await trigger_appium_task("battery_low_powersave", soc=soc)
                    await send_telegram(f"⚠️ Baterai rendah: {soc:.1f}% - mode hemat daya diaktifkan")
                    last_alert = "low"
                elif soc is not None and soc >= SOC_HIGH_THRESHOLD and last_alert != "high":
                    log.info("SOC tinggi/surplus (%.1f%%) - trigger task berdaya tinggi", soc)
                    await trigger_appium_task("battery_high_surplus_task", soc=soc)
                    last_alert = "high"
                elif soc is not None and SOC_LOW_THRESHOLD < soc < SOC_HIGH_THRESHOLD:
                    last_alert = None

            except Exception as exc:
                log.warning("Gagal polling solar-dashboard: %s", exc)

            await asyncio.sleep(SOLAR_POLL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(solar_watcher())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


@app.post("/webhook/mail")
async def webhook_mail(event: MailEvent):
    log.info("[mail] %s <%s>: %s", event.account, event.sender, event.subject)
    # TODO: sesuaikan logic keputusan (filter subjek/pengirim, panggil LLM untuk
    # klasifikasi, lalu trigger_appium_task kalau perlu aksi di HP Android).
    return {"status": "received"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
