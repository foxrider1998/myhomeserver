"""stt.py - Transkripsi file rekaman (WAV/MP3/M4A/WEBM/OGG/FLAC/...) untuk Notulen Rapat.

Alur: ffmpeg -> audio mono 16 kHz -> mesin STT:
  1. "ai"     : endpoint OpenAI-compatible `{SUMOPOD_BASE_URL}/audio/transcriptions` (Whisper dsb.).
                File dikirim UTUH (dikompres Opus/MP3 mono agar muat satu permintaan, +/-11 MB per jam),
                sehingga konteks tidak terpotong. Hanya bila hasil kompresi masih di atas batas ukuran,
                audio dipotong menjadi segmen BERTUMPANG TINDIH dan teksnya digabung tanpa duplikat.
  2. "google" : SpeechRecognition + Google Web Speech (cadangan). Mesin ini hanya menerima klip pendek,
                jadi dipotong per segmen bertumpang tindih lalu digabung tanpa duplikat.
  3. "gemini" : model multimodal via chat/completions + input_audio (mp3). Dipakai karena Sumopod tidak punya
                model Whisper/STT; biayanya mengikuti harga token model (audio ~32 token/detik).
  "auto" mencoba STT_ENGINES berurutan (default gemini, ai, google) sampai ada yang berhasil.
Berjalan sebagai job latar belakang supaya rekaman panjang tidak kena timeout HTTP.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid

import httpx

log = logging.getLogger("hermes-agent.stt")

SUMOPOD_API_KEY = os.environ.get("SUMOPOD_API_KEY", "")
SUMOPOD_BASE_URL = os.environ.get("SUMOPOD_BASE_URL", "https://ai.sumopod.com/v1")
# Endpoint khusus audio (transkripsi & analisa audio). Boleh diarahkan ke router lain (mis. 9Router
# http://localhost:20128/v1) tanpa mengubah LLM teks. Default: sama dengan Sumopod.
STT_BASE_URL = os.environ.get("STT_BASE_URL", "").strip() or SUMOPOD_BASE_URL
STT_API_KEY = os.environ.get("STT_API_KEY", "").strip() or SUMOPOD_API_KEY
STT_MODEL = os.environ.get("STT_MODEL", "whisper-1")
# Sumopod tidak menyediakan model Whisper/STT; model multimodal (Gemini) bisa menerima audio lewat chat.
STT_CHAT_MODEL = os.environ.get("STT_CHAT_MODEL", "gemini/gemini-3.1-flash-lite-preview")
# Rantai model audio: dicoba berurutan; bila kuota/limit/error, otomatis pindah ke model berikutnya.
# Isi STT_CHAT_MODELS (pisah koma) dengan model multimodal-audio dari provider LAIN agar tidak ikut habis
# saat kuota satu provider habis (mis. "openai/gpt-4o-audio-preview"). Bisa juga nama "combo" 9Router.
_DEFAULT_CHAT_MODELS = ["gemini/gemini-3.1-flash-lite-preview", "gemini/gemini-3.5-flash-lite", "gemini/gemini-2.5-flash-lite", "gemini/gemini-3-flash-preview", "gemini/gemini-3.8-flash", "gemini/gemini-2.5-flash"]
STT_CHAT_MODELS = [m.strip() for m in os.environ.get("STT_CHAT_MODELS", "").split(",") if m.strip()] or _DEFAULT_CHAT_MODELS
STT_ENGINES = [e.strip() for e in os.environ.get("STT_ENGINES", "gemini,ai,google").split(",") if e.strip()]
AI_MAX_BYTES = 24 * 1024 * 1024   # batas Whisper API 25 MB, sisakan margin
AI_SEGMENT_SEC = 1500             # hanya dipakai bila satu file utuh tetap kelewat besar
AI_OVERLAP_SEC = 30
GOOGLE_SEGMENT_SEC = 50           # batas praktis Google Web Speech
GOOGLE_OVERLAP_SEC = 6
MAX_UPLOAD_BYTES = 120 * 1024 * 1024
JOB_TTL_SEC = 3600

JOBS: dict[str, dict] = {}


def _cleanup_jobs() -> None:
    now = time.time()
    for jid in [j for j, v in JOBS.items() if now - v.get("created", now) > JOB_TTL_SEC]:
        JOBS.pop(jid, None)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _ffmpeg(args: list[str], timeout: int = 1800) -> None:
    proc = subprocess.run(["ffmpeg", "-y", "-loglevel", "error"] + args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg gagal: " + (proc.stderr or "")[-300:])


def _duration_sec(path: str) -> float:
    proc = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                           "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=60)
    try:
        return float(proc.stdout.strip())
    except ValueError:
        raise RuntimeError("Audio kosong atau format tidak dikenali.")


# (ekstensi, argumen codec) dari paling hemat ukuran ke paling kompatibel
_COMPACT_CODECS = [
    (".ogg", ["-c:a", "libopus", "-b:a", "24k", "-application", "voip"]),
    (".mp3", ["-c:a", "libmp3lame", "-b:a", "32k"]),
    (".wav", ["-c:a", "pcm_s16le"]),
]
_MIME = {".ogg": "audio/ogg", ".mp3": "audio/mpeg", ".wav": "audio/wav"}


def _encode(src: str, dst_base: str, start: float | None = None, dur: float | None = None, wav_only: bool = False, mp3_only: bool = False) -> str:
    """Encode (sebagian) audio ke mono 16 kHz dengan codec pertama yang tersedia. Mengembalikan path hasil."""
    codecs = [c for c in _COMPACT_CODECS if (c[0] == ".mp3" if mp3_only else (c[0] == ".wav" or not wav_only))]
    last = ""
    for ext, codec in codecs:
        out = dst_base + ext
        args = []
        if start is not None:
            args += ["-ss", f"{start:.2f}"]
        args += ["-i", src]
        if dur is not None:
            args += ["-t", f"{dur:.2f}"]
        args += ["-vn", "-ac", "1", "-ar", "16000"] + codec + [out]
        try:
            _ffmpeg(args)
            return out
        except RuntimeError as e:
            last = str(e)
    raise RuntimeError(last or "Konversi audio gagal.")


def _overlapping_segments(src: str, out_dir: str, seg_sec: int, overlap_sec: int, wav_only: bool, mp3_only: bool = False) -> list[str]:
    """Potong menjadi segmen berdurasi seg_sec yang saling tumpang tindih overlap_sec detik."""
    total = _duration_sec(src)
    step = max(1, seg_sec - overlap_sec)
    segs, start, i = [], 0.0, 0
    while start < total:
        segs.append(_encode(src, os.path.join(out_dir, f"seg_{i:04d}"), start, seg_sec, wav_only, mp3_only))
        i += 1
        if start + seg_sec >= total:
            break
        start += step
    return segs


def _merge_overlapping(texts: list[str], max_overlap_words: int = 60, min_overlap_words: int = 3) -> str:
    """Gabung transkrip segmen bertumpang tindih: buang kata awal segmen berikut yang sama dengan akhir sebelumnya."""
    merged: list[str] = []
    for t in texts:
        words = t.split()
        if not words:
            continue
        if merged:
            tail = [w.lower().strip(".,!?;:") for w in merged[-max_overlap_words:]]
            head = [w.lower().strip(".,!?;:") for w in words[:max_overlap_words]]
            best = 0
            for n in range(min(len(tail), len(head)), min_overlap_words - 1, -1):
                if tail[-n:] == head[:n]:
                    best = n
                    break
            words = words[best:]
        merged.extend(words)
    return " ".join(merged)


def _transcribe_file_ai(path: str, language: str) -> str:
    if not STT_API_KEY or STT_API_KEY.startswith("sk-xxxx"):
        raise RuntimeError("STT_API_KEY / SUMOPOD_API_KEY belum diisi.")
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as f:
        audio = f.read()
    data = {"model": STT_MODEL, "response_format": "json"}
    if language:
        data["language"] = language
    resp = httpx.post(f"{STT_BASE_URL}/audio/transcriptions",
                      headers={"Authorization": f"Bearer {STT_API_KEY}"},
                      data=data, files={"file": ("audio" + ext, audio, _MIME.get(ext, "application/octet-stream"))}, timeout=300.0)
    if resp.status_code >= 400:
        raise RuntimeError(f"STT AI HTTP {resp.status_code}: {resp.text[:200]}")
    return (resp.json().get("text") or "").strip()


def _chat_audio_one(model: str, b64: str, instruction: str) -> str:
    import json
    body = {"model": model, "temperature": 0, "stream": False,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": instruction},
                {"type": "input_audio", "input_audio": {"data": b64, "format": "mp3"}}]}]}
    resp = httpx.post(f"{STT_BASE_URL}/chat/completions", json=body,
                      headers={"Authorization": f"Bearer {STT_API_KEY}"}, timeout=900.0)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    text_resp = resp.text.strip()
    if text_resp.startswith("data:"):
        collected = []
        for line in text_resp.splitlines():
            line = line.strip()
            if line.startswith("data:") and line != "data: [DONE]":
                try:
                    chunk = json.loads(line[5:].strip())
                    delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    if delta:
                        collected.append(delta)
                except Exception:
                    pass
        out = "".join(collected).strip()
    else:
        out = (resp.json()["choices"][0]["message"]["content"] or "").strip()
    if not out:
        raise RuntimeError("respons kosong")
    return out


def _chat_audio(path: str, instruction: str, used: list | None = None) -> str:
    """Kirim audio mp3 + instruksi ke model multimodal; otomatis rotasi ke model berikutnya bila gagal/kuota habis."""
    import base64
    if not STT_API_KEY or STT_API_KEY.startswith("sk-xxxx"):
        raise RuntimeError("STT_API_KEY / SUMOPOD_API_KEY belum diisi.")
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    chain = list(dict.fromkeys([STT_CHAT_MODEL] + STT_CHAT_MODELS)) if os.environ.get("STT_CHAT_MODEL") else list(STT_CHAT_MODELS)
    errors = []
    for model in chain:
        try:
            out = _chat_audio_one(model, b64, instruction)
            if used is not None:
                used.append(model)
            if errors:
                log.info("Audio AI berhasil via model cadangan %s (gagal: %s)", model, "; ".join(errors))
            return out
        except Exception as e:
            errors.append(f"{model}: {str(e)[:120]}")
            log.warning("Audio AI model %s gagal: %s. Mencoba model berikutnya...", model, e)
    raise RuntimeError("Semua model audio gagal -> " + " | ".join(errors))


MIX_NOTE = "Bahasa Indonesia yang bercampur istilah teknis bahasa Inggris (code-switching); tulis istilah Inggris apa adanya, jangan diterjemahkan"


def _lang_name(language: str) -> str:
    if language in ("", "mix"):
        return MIX_NOTE
    return {"id": "Bahasa Indonesia", "en": "English"}.get(language, language)


def _transcribe_file_gemini(path: str, language: str, used: list | None = None) -> str:
    """Transkripsi lewat model multimodal."""
    return _chat_audio(path, (
        f"Transkripsikan rekaman rapat ini secara persis kata per kata dalam {_lang_name(language)}. "
        "Jangan merangkum, jangan menerjemahkan, jangan menambah komentar. Pertahankan istilah teknis dan nama apa adanya. "
        "Pisahkan paragraf saat topik atau pembicara berganti. Bila bagian tidak terdengar jelas tulis [tidak jelas]. "
        "Keluarkan hanya teksnya."), used)


def _analyze_audio(path: str, language: str, title: str, context: str = "", used: list | None = None) -> str:
    """Analisa & notulen langsung dari audio (tanpa transkrip perantara)."""
    return _chat_audio(path, (
        f"Kamu notulen meeting profesional. Dengarkan rekaman rapat tim IT/QA berjudul \"{title or 'Meeting'}\" ini "
        f"dan susun notulen dalam {_lang_name(language)} berformat Markdown:\n"
        "1. **Ringkasan Eksekutif & Topik Utama**\n2. **Poin Kunci Diskusi & Keputusan Teknis**\n"
        "3. **Keputusan yang Disepakati**\n4. **Daftar Tindak Lanjut** (sebut PIC dan tugas spesifik bila disebut)\n"
        "5. **Catatan Khusus, Risiko, & Kendala**\n"
        + (f"\nKONTEKS SPRINT & TASK TERKAIT (rujukan; hubungkan tindak lanjut ke kode task bila memang dibahas):\n{context}\n" if context else "") +
        "Hanya tulis hal yang benar-benar terdengar; jangan mengarang nama, angka, atau keputusan. "
        "Bila bagian tidak jelas, tulis [tidak jelas]. Perhatikan penekanan nada untuk menilai mana yang mendesak."), used)


def _transcribe_segment_google(path: str, language_full: str) -> str:
    import speech_recognition as sr
    r = sr.Recognizer()
    with sr.AudioFile(path) as source:
        audio = r.record(source)
    try:
        return r.recognize_google(audio, language=language_full).strip()
    except sr.UnknownValueError:
        return ""


def _run_job(job_id: str, raw: bytes, filename: str, language_full: str, engine: str, mode: str = "transcribe", title: str = "", context: str = "") -> None:
    job = JOBS[job_id]
    # 'mix' = Indonesia + istilah teknis Inggris: biarkan Whisper mendeteksi bahasa (tanpa hint), Google pakai id-ID
    lang_short = "" if language_full == "mix" else (language_full or "id-ID").split("-")[0]
    if language_full == "mix":
        language_full = "id-ID"
        job["mix"] = True
    tmp = tempfile.mkdtemp(prefix="stt_")
    try:
        ext = os.path.splitext(filename or "")[1].lower()[:8] or ".bin"
        src = os.path.join(tmp, "input" + ext)
        with open(src, "wb") as f:
            f.write(raw)
        del raw
        if not ffmpeg_available():
            raise RuntimeError("ffmpeg belum terpasang di server (apt install ffmpeg).")

        if mode == "analyze":
            job.update(status="running", engine="gemini", total=1, done=0, message="Menyiapkan audio...")
            whole = _encode(src, os.path.join(tmp, "whole"), mp3_only=True)
            if os.path.getsize(whole) > AI_MAX_BYTES:
                raise RuntimeError("Rekaman terlalu panjang untuk analisa audio langsung (di atas ~1,7 jam). Gunakan dasar analisa Transkrip.")
            job.update(message="Menganalisis audio langsung (AI)...")
            used: list = []
            summary = _analyze_audio(whole, "mix" if job.get("mix") else lang_short, title, context, used)
            if used:
                job["model"] = used[-1]
            if len(summary.strip()) < 40:
                raise RuntimeError("Hasil analisa audio kosong.")
            job.update(status="done", done=1, summary=summary, text="", message="Selesai")
            return

        engines = STT_ENGINES if engine == "auto" else [engine]
        last_err = ""
        for eng in engines:
            seg_dir = os.path.join(tmp, eng)
            os.makedirs(seg_dir, exist_ok=True)
            try:
                job.update(status="running", engine=eng, done=0, message=f"Menyiapkan audio ({eng})...")
                if eng in ("ai", "gemini"):
                    file_fn = _transcribe_file_gemini if eng == "gemini" else _transcribe_file_ai
                    mp3 = eng == "gemini"
                    whole = _encode(src, os.path.join(seg_dir, "whole"), mp3_only=mp3)
                    if os.path.getsize(whole) <= AI_MAX_BYTES:
                        # Satu permintaan utuh: konteks tidak terpotong
                        job.update(total=1, message="Mentranskripsi file utuh (AI)...")
                        text = file_fn(whole, lang_short)
                        job.update(done=1, partial=text)
                    else:
                        segs = _overlapping_segments(src, seg_dir, AI_SEGMENT_SEC, AI_OVERLAP_SEC, wav_only=False, mp3_only=mp3)
                        job.update(total=len(segs), message=f"File sangat panjang: {len(segs)} segmen tumpang tindih (AI)...")
                        texts = []
                        for i, seg in enumerate(segs):
                            texts.append(file_fn(seg, lang_short))
                            job.update(done=i + 1, partial=_merge_overlapping(texts))
                        text = _merge_overlapping(texts)
                else:
                    segs = _overlapping_segments(src, seg_dir, GOOGLE_SEGMENT_SEC, GOOGLE_OVERLAP_SEC, wav_only=True)
                    job.update(total=len(segs), message=f"Mentranskripsi {len(segs)} segmen tumpang tindih (Google)...")
                    texts = []
                    for i, seg in enumerate(segs):
                        texts.append(_transcribe_segment_google(seg, language_full))
                        job.update(done=i + 1, partial=_merge_overlapping(texts))
                    text = _merge_overlapping(texts)
                if not text.strip():
                    raise RuntimeError("Tidak ada ucapan yang terdeteksi.")
                job.update(status="done", text=text, message="Selesai")
                return
            except Exception as e:  # coba mesin berikutnya
                last_err = f"{eng}: {e}"
                log.warning("STT %s gagal: %s", eng, e)
        raise RuntimeError(last_err or "Transkripsi gagal.")
    except Exception as e:
        job.update(status="error", error=str(e)[:400], message="Gagal")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def start_job(raw: bytes, filename: str, language: str = "id-ID", engine: str = "auto", mode: str = "transcribe", title: str = "", context: str = "") -> str:
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError(f"File terlalu besar (maks {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
    if engine not in ("auto", "ai", "gemini", "google"):
        engine = "auto"
    _cleanup_jobs()
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"created": time.time(), "status": "queued", "engine": engine, "total": 0, "done": 0,
                    "message": "Menunggu giliran...", "filename": filename, "mode": mode}
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _run_job, job_id, raw, filename, language, engine, "analyze" if mode == "analyze" else "transcribe", title[:200], context[:4000])
    return job_id


def get_job(job_id: str) -> dict | None:
    j = JOBS.get(job_id)
    if not j:
        return None
    return {k: v for k, v in j.items() if k != "created"}
