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
STT_MODEL = os.environ.get("STT_MODEL", "whisper-1")
# Sumopod tidak menyediakan model Whisper/STT; model multimodal (Gemini) bisa menerima audio lewat chat.
STT_CHAT_MODEL = os.environ.get("STT_CHAT_MODEL", "gemini/gemini-3.1-flash-lite")
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
    if not SUMOPOD_API_KEY or SUMOPOD_API_KEY.startswith("sk-xxxx"):
        raise RuntimeError("SUMOPOD_API_KEY belum diisi.")
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as f:
        audio = f.read()
    data = {"model": STT_MODEL, "response_format": "json"}
    if language:
        data["language"] = language
    resp = httpx.post(f"{SUMOPOD_BASE_URL}/audio/transcriptions",
                      headers={"Authorization": f"Bearer {SUMOPOD_API_KEY}"},
                      data=data, files={"file": ("audio" + ext, audio, _MIME.get(ext, "application/octet-stream"))}, timeout=300.0)
    if resp.status_code >= 400:
        raise RuntimeError(f"STT AI HTTP {resp.status_code}: {resp.text[:200]}")
    return (resp.json().get("text") or "").strip()


def _transcribe_file_gemini(path: str, language: str) -> str:
    """Transkripsi lewat model multimodal (chat/completions + input_audio, format mp3)."""
    import base64
    if not SUMOPOD_API_KEY or SUMOPOD_API_KEY.startswith("sk-xxxx"):
        raise RuntimeError("SUMOPOD_API_KEY belum diisi.")
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    lang_name = {"id": "Bahasa Indonesia", "en": "English"}.get(language, language or "Bahasa Indonesia")
    instruction = (f"Transkripsikan rekaman rapat ini secara persis kata per kata dalam {lang_name}. "
                   "Jangan merangkum, jangan menerjemahkan, jangan menambah komentar. Pertahankan istilah teknis dan nama apa adanya. "
                   "Pisahkan paragraf saat topik atau pembicara berganti. Bila bagian tidak terdengar jelas tulis [tidak jelas]. "
                   "Keluarkan hanya teksnya.")
    body = {"model": STT_CHAT_MODEL, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": instruction},
                {"type": "input_audio", "input_audio": {"data": b64, "format": "mp3"}}]}]}
    resp = httpx.post(f"{SUMOPOD_BASE_URL}/chat/completions", json=body,
                      headers={"Authorization": f"Bearer {SUMOPOD_API_KEY}"}, timeout=600.0)
    if resp.status_code >= 400:
        raise RuntimeError(f"STT Gemini HTTP {resp.status_code}: {resp.text[:200]}")
    return (resp.json()["choices"][0]["message"]["content"] or "").strip()


def _transcribe_segment_google(path: str, language_full: str) -> str:
    import speech_recognition as sr
    r = sr.Recognizer()
    with sr.AudioFile(path) as source:
        audio = r.record(source)
    try:
        return r.recognize_google(audio, language=language_full).strip()
    except sr.UnknownValueError:
        return ""


def _run_job(job_id: str, raw: bytes, filename: str, language_full: str, engine: str) -> None:
    job = JOBS[job_id]
    lang_short = (language_full or "id-ID").split("-")[0]
    tmp = tempfile.mkdtemp(prefix="stt_")
    try:
        ext = os.path.splitext(filename or "")[1].lower()[:8] or ".bin"
        src = os.path.join(tmp, "input" + ext)
        with open(src, "wb") as f:
            f.write(raw)
        del raw
        if not ffmpeg_available():
            raise RuntimeError("ffmpeg belum terpasang di server (apt install ffmpeg).")

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


async def start_job(raw: bytes, filename: str, language: str = "id-ID", engine: str = "auto") -> str:
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError(f"File terlalu besar (maks {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
    if engine not in ("auto", "ai", "gemini", "google"):
        engine = "auto"
    _cleanup_jobs()
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"created": time.time(), "status": "queued", "engine": engine, "total": 0, "done": 0,
                    "message": "Menunggu giliran...", "filename": filename}
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _run_job, job_id, raw, filename, language, engine)
    return job_id


def get_job(job_id: str) -> dict | None:
    j = JOBS.get(job_id)
    if not j:
        return None
    return {k: v for k, v in j.items() if k != "created"}
