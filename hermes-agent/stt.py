"""stt.py - Transkripsi file rekaman (WAV/MP3/M4A/WEBM/OGG/FLAC/...) untuk Notulen Rapat.

Alur: ffmpeg -> audio mono 16 kHz -> mesin STT:
  1. "gemini" : model multimodal via chat/completions + input_audio (mp3/wav). Default diarahkan ke
                9Router lokal (http://127.0.0.1:20128/v1) dengan rantai model Gemini aktif.
  2. "ai"     : endpoint OpenAI-compatible `{STT_BASE_URL}/audio/transcriptions` (Whisper dsb.).
                File dikirim UTUH (dikompres Opus/MP3 mono agar muat satu permintaan, +/-11 MB per jam),
                sehingga konteks tidak terpotong. Hanya bila hasil kompresi masih di atas batas ukuran,
                audio dipotong menjadi segmen BERTUMPANG TINDIH dan teksnya digabung tanpa duplikat.
  3. "google" : SpeechRecognition + Google Web Speech (cadangan). Mesin ini hanya menerima klip pendek,
                jadi dipotong per segmen bertumpang tindih lalu digabung tanpa duplikat.
  "auto" mencoba STT_ENGINES berurutan (default gemini,ai,google) sampai ada yang berhasil.
Berjalan sebagai job latar belakang supaya rekaman panjang tidak kena timeout HTTP.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid

import httpx

log = logging.getLogger("hermes-agent.stt")


def _get_env_key(key: str, default: str = "") -> str:
    v = os.environ.get(key, "").strip()
    if v:
        return v
    base_dir = os.path.dirname(os.path.abspath(__file__))
    for env_path in [
        os.path.join(base_dir, ".env"),
        os.path.join(os.path.dirname(base_dir), ".env"),
        "/home/ares/Documents/myserver/.env",
    ]:
        if os.path.isfile(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, val = line.split("=", 1)
                            if k.strip() == key:
                                val = val.strip().strip("'\"")
                                if val:
                                    return val
            except Exception:
                pass
    return default


SUMOPOD_API_KEY = _get_env_key("SUMOPOD_API_KEY", "")
SUMOPOD_BASE_URL = _get_env_key("SUMOPOD_BASE_URL", "https://ai.sumopod.com/v1").rstrip("/")
NINEROUTER_RAW_URL = _get_env_key("NINEROUTER_URL", "http://127.0.0.1:20128/v1")
NINEROUTER_BASE_URL = NINEROUTER_RAW_URL.split("/chat/completions")[0].rstrip("/")
NINEROUTER_KEY = _get_env_key("NINEROUTER_KEY", "sk-6bf6c7c163dbf602-faqfeq-50da42f4")

# Endpoint khusus audio (transkripsi & analisa audio). Default ke 9Router lokal (http://127.0.0.1:20128/v1)
# agar model multimodal Gemini di 9Router tetap jalan walaupun STT_BASE_URL belum ditulis di .env.
STT_BASE_URL = (_get_env_key("STT_BASE_URL", "") or NINEROUTER_BASE_URL).rstrip("/")
STT_API_KEY = _get_env_key("STT_API_KEY", "") or NINEROUTER_KEY or SUMOPOD_API_KEY
STT_MODEL = _get_env_key("STT_MODEL", "whisper-1")
STT_CHAT_MODEL = _get_env_key("STT_CHAT_MODEL", "gemini/gemini-3.5-flash-lite")

# Rantai model audio: diurutkan dari yang paling cepat & stabil di 9Router; bila kuota/limit/error,
# otomatis pindah ke model berikutnya.
_DEFAULT_CHAT_MODELS = [
    "gemini/gemini-3.5-flash-lite",
    "gemini/gemini-3.1-flash-lite-preview",
    "gemini/gemini-3.6-flash",
    "gemini/gemini-3-flash-preview",
    "gemini/gemini-3.7-flash",
    "gemini/gemini-3.8-flash",
    "gemini/gemini-2.5-flash",
]
_DEAD_AUDIO_MODELS = {"gemini/gemini-2.5-flash-lite", "gemini-2.5-flash-lite"}
_env_chat_models = [
    m.strip()
    for m in _get_env_key("STT_CHAT_MODELS", "").split(",")
    if m.strip() and m.strip() not in _DEAD_AUDIO_MODELS
]
STT_CHAT_MODELS = list(dict.fromkeys(_env_chat_models + _DEFAULT_CHAT_MODELS))
STT_ENGINES = [e.strip() for e in _get_env_key("STT_ENGINES", "gemini,ai,google").split(",") if e.strip()]
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
    if wav_only:
        codecs = [c for c in _COMPACT_CODECS if c[0] == ".wav"]
    elif mp3_only:
        # Prioritaskan MP3; bila libmp3lame tidak tersedia di ffmpeg, fallback ke WAV agar tidak gagal
        codecs = [c for c in _COMPACT_CODECS if c[0] in (".mp3", ".wav")]
    else:
        codecs = list(_COMPACT_CODECS)
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
            if os.path.exists(out) and os.path.getsize(out) > 44:
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


_MODEL_COOLDOWN: dict[str, float] = {}


def _chat_audio_one(model: str, b64: str, instruction: str, audio_format: str = "mp3", base_url: str | None = None, api_key: str | None = None, system_prompt: str | None = None) -> str:
    target_base = (base_url or STT_BASE_URL).rstrip("/")
    target_key = api_key or STT_API_KEY
    # Timeout adaptif (45s untuk rekaman pendek, hingga 240s untuk rekaman panjang) agar rotasi model tidak macet 15 menit
    req_timeout = max(45.0, min(240.0, 35.0 + len(b64) / (1024 * 50)))
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    # Penting: letakkan input_audio SEBELUM instruksi teks agar model multimodal membaca audio dulu lalu mematuhi instruksi di akhir
    messages.append({"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": b64, "format": audio_format}},
        {"type": "text", "text": instruction},
    ]})
    body = {"model": model, "temperature": 0, "stream": False, "messages": messages}
    resp = httpx.post(f"{target_base}/chat/completions", json=body,
                      headers={"Authorization": f"Bearer {target_key}"}, timeout=req_timeout)
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


def _clean_stt_output(text: str) -> str:
    """Bersihkan respons halusinasi LLM bila rekaman hening / tidak ada ucapan."""
    t = (text or "").strip()
    low = t.lower().strip(".,!?;: ")
    if low in (
        "[tidak ada ucapan]", "[tidak ada suara]", "[hening]", "[tidak jelas]",
        "[musik]", "[suara latar]", "[noise]",
        "tidak ada ucapan", "tidak ada suara", "hening", "tidak jelas",
    ):
        return ""
    if "belum melampirkan" in low or "silakan unggah file audio" in low or "tidak ada file audio" in low or "tidak ada suara pembicaraan" in low:
        return ""
    return t


def _chat_audio(path: str, instruction: str, used: list | None = None, system_prompt: str | None = None) -> str:
    """Kirim audio + instruksi ke model multimodal; otomatis rotasi ke model berikutnya bila gagal/kuota habis."""
    if not STT_API_KEY or STT_API_KEY.startswith("sk-xxxx"):
        raise RuntimeError("STT_API_KEY / NINEROUTER_KEY belum diisi.")
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    audio_format = ext if ext in ("mp3", "wav", "ogg") else "mp3"

    first_models = [STT_CHAT_MODEL] if STT_CHAT_MODEL and STT_CHAT_MODEL not in _DEAD_AUDIO_MODELS else []
    raw_chain = list(dict.fromkeys(first_models + STT_CHAT_MODELS))
    now = time.time()
    active_chain = [m for m in raw_chain if _MODEL_COOLDOWN.get(m, 0) < now]
    cooled_chain = [m for m in raw_chain if _MODEL_COOLDOWN.get(m, 0) >= now]
    chain = active_chain + cooled_chain

    # Daftar endpoint yang dicoba: STT_BASE_URL utama, lalu fallback ke 9Router lokal bila STT_BASE_URL diarahkan ke tempat lain
    endpoints = [(STT_BASE_URL, STT_API_KEY)]
    if NINEROUTER_BASE_URL and NINEROUTER_BASE_URL != STT_BASE_URL and NINEROUTER_KEY:
        endpoints.append((NINEROUTER_BASE_URL, NINEROUTER_KEY))

    errors = []
    for base_url, api_key in endpoints:
        for model in chain:
            try:
                out = _chat_audio_one(model, b64, instruction, audio_format=audio_format, base_url=base_url, api_key=api_key, system_prompt=system_prompt)
                _MODEL_COOLDOWN.pop(model, None)
                if used is not None:
                    used.append(model)
                if errors:
                    log.info("Audio AI berhasil via model %s (sebelumnya gagal: %s)", model, "; ".join(errors))
                return out
            except Exception as e:
                _MODEL_COOLDOWN[model] = time.time() + 180.0
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
    raw_out = _chat_audio(
        path,
        (
            f"Transkripsikan seluruh ucapan dalam rekaman audio di atas secara persis kata per kata dalam {_lang_name(language)}. "
            "Jangan merangkum, jangan menerjemahkan, jangan menambah komentar atau pengantar. Pertahankan istilah teknis dan nama apa adanya. "
            "Pisahkan paragraf saat topik atau pembicara berganti. Bila ada kata yang kurang terdengar jelas tulis [tidak jelas]. "
            "Jika audio benar-benar hening atau tidak ada suara pembicaraan sama sekali, tulis tepat: [tidak ada ucapan]. "
            "Keluarkan hanya teks transkripnya."
        ),
        used,
        system_prompt="Kamu adalah mesin Speech-to-Text (STT) presisi tinggi. Tugasmu hanya menuliskan transkrip kata per kata dari audio yang dilampirkan tanpa tambahan komentar.",
    )
    return _clean_stt_output(raw_out)


def _analyze_audio(path: str, language: str, title: str, context: str = "", used: list | None = None) -> str:
    """Analisa & notulen langsung dari audio."""
    return _chat_audio(
        path,
        (
            f"Berdasarkan rekaman audio rapat berjudul \"{title or 'Meeting'}\" di atas, "
            f"susun notulen rapat resmi dalam {_lang_name(language)} berformat Markdown dengan 5 bagian bernomor berikut:\n"
            "1. **Ringkasan Eksekutif & Topik Utama**\n"
            "2. **Poin Kunci Diskusi & Keputusan Teknis**\n"
            "3. **Keputusan yang Disepakati**\n"
            "4. **Daftar Tindak Lanjut** (sebut PIC dan tugas spesifik bila disebut)\n"
            "5. **Catatan Khusus, Risiko, & Kendala**\n"
            + (f"\nKONTEKS SPRINT & TASK TERKAIT (rujukan; hubungkan tindak lanjut ke kode task bila memang dibahas):\n{context}\n" if context else "")
            + "Jangan hanya menulis transkrip! Wajib gunakan format 5 bagian bernomor di atas. "
            "Hanya tulis hal yang benar-benar terdengar; jangan mengarang nama, angka, atau keputusan. "
            "Bila bagian tidak jelas, tulis [tidak jelas]. Perhatikan penekanan nada untuk menilai mana yang mendesak."
        ),
        used,
        system_prompt="Kamu adalah penyusun notulen rapat IT/QA profesional. Kamu WAJIB selalu menjawab dalam format Markdown yang memiliki 5 bagian bernomor (1. Ringkasan Eksekutif, 2. Poin Kunci, 3. Keputusan, 4. Daftar Tindak Lanjut, 5. Catatan Khusus). Jangan pernah hanya mengeluarkan transkrip mentah.",
    )


def _ensure_flac_via_ffmpeg() -> None:
    """Pastikan SpeechRecognition bisa mengonversi WAV->FLAC via ffmpeg bila paket `flac` 64-bit belum terpasang."""
    import speech_recognition as sr
    if getattr(sr.AudioData, "_ffmpeg_flac_patched", False):
        return
    orig_get_flac = sr.AudioData.get_flac_data

    def _patched_get_flac_data(self, convert_rate=None, convert_width=None):
        try:
            if shutil.which("flac"):
                return orig_get_flac(self, convert_rate=convert_rate, convert_width=convert_width)
        except Exception:
            pass
        wav_data = self.get_wav_data(convert_rate=convert_rate, convert_width=convert_width)
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-c:a", "flac", "-f", "flac", "pipe:1"],
            input=wav_data,
            capture_output=True,
            timeout=60,
        )
        if proc.returncode != 0 or not proc.stdout:
            return orig_get_flac(self, convert_rate=convert_rate, convert_width=convert_width)
        return proc.stdout

    sr.AudioData.get_flac_data = _patched_get_flac_data
    sr.AudioData._ffmpeg_flac_patched = True


def _transcribe_segment_google(path: str, language_full: str) -> str:
    import speech_recognition as sr
    _ensure_flac_via_ffmpeg()
    r = sr.Recognizer()
    with sr.AudioFile(path) as source:
        audio = r.record(source)
    try:
        return r.recognize_google(audio, language=language_full).strip()
    except sr.UnknownValueError:
        return ""


def _transcribe_with_engines(src: str, tmp: str, lang_short: str, language_full: str, engine: str = "auto", job: dict | None = None, allow_empty: bool = False) -> tuple[str, str]:
    """Jalankan transkripsi suara menggunakan rantai mesin STT (gemini -> ai -> google). Mengembalikan (text, engine_used)."""
    engines = STT_ENGINES if engine == "auto" else [engine]
    last_err = ""
    for eng in engines:
        seg_dir = os.path.join(tmp, f"tr_{eng}")
        os.makedirs(seg_dir, exist_ok=True)
        try:
            if job is not None:
                job.update(status="running", engine=eng, done=0, message=f"Menyiapkan audio ({eng})...")
            if eng in ("ai", "gemini"):
                file_fn = _transcribe_file_gemini if eng == "gemini" else _transcribe_file_ai
                mp3 = eng == "gemini"
                whole = _encode(src, os.path.join(seg_dir, "whole"), mp3_only=mp3)
                if os.path.getsize(whole) <= AI_MAX_BYTES:
                    if job is not None:
                        job.update(total=1, message=f"Mentranskripsi suara ({eng.upper()})...")
                    used_models: list = []
                    text = _transcribe_file_gemini(whole, lang_short, used_models) if eng == "gemini" else _transcribe_file_ai(whole, lang_short)
                    if job is not None:
                        if used_models:
                            job["model"] = used_models[-1]
                        job.update(done=1, partial=text)
                else:
                    segs = _overlapping_segments(src, seg_dir, AI_SEGMENT_SEC, AI_OVERLAP_SEC, wav_only=False, mp3_only=mp3)
                    if job is not None:
                        job.update(total=len(segs), message=f"File panjang: {len(segs)} segmen ({eng.upper()})...")
                    texts = []
                    for i, seg in enumerate(segs):
                        texts.append(file_fn(seg, lang_short))
                        if job is not None:
                            job.update(done=i + 1, partial=_merge_overlapping(texts))
                    text = _merge_overlapping(texts)
            else:
                segs = _overlapping_segments(src, seg_dir, GOOGLE_SEGMENT_SEC, GOOGLE_OVERLAP_SEC, wav_only=True)
                if job is not None:
                    job.update(total=len(segs), message=f"Mentranskripsi {len(segs)} segmen (Google)...")
                texts = []
                for i, seg in enumerate(segs):
                    texts.append(_transcribe_segment_google(seg, language_full))
                    if job is not None:
                        job.update(done=i + 1, partial=_merge_overlapping(texts))
                text = _merge_overlapping(texts)
            if not text.strip():
                if allow_empty:
                    return "", eng
                raise RuntimeError("Tidak ada ucapan yang terdeteksi.")
            return text.strip(), eng
        except Exception as e:
            last_err = f"{eng}: {e}"
            log.warning("STT %s gagal: %s", eng, e)
    if allow_empty:
        return "", "none"
    raise RuntimeError(last_err or "Transkripsi gagal.")


def transcribe_bytes_sync(raw: bytes, filename: str = "rekaman.webm", language_full: str = "id-ID", engine: str = "google") -> str:
    """Helper sinkron untuk mentranskripsi potongan suara live (default: murni Google STT tanpa AI LLM)."""
    if not raw:
        return ""
    lang_short = "" if language_full == "mix" else (language_full or "id-ID").split("-")[0]
    if language_full == "mix" or not language_full:
        language_full = "id-ID"
    tmp = tempfile.mkdtemp(prefix="stt_sync_")
    try:
        ext = os.path.splitext(filename or "")[1].lower()[:8] or ".webm"
        src = os.path.join(tmp, "input" + ext)
        with open(src, "wb") as f:
            f.write(raw)
        if not ffmpeg_available():
            raise RuntimeError("ffmpeg belum terpasang di server (apt install ffmpeg).")
        if engine == "google":
            wav_path = _encode(src, os.path.join(tmp, "whole"), wav_only=True)
            return _transcribe_segment_google(wav_path, language_full)
        text, _ = _transcribe_with_engines(src, tmp, "mix" if not lang_short else lang_short, language_full, engine=engine, job=None, allow_empty=True)
        return text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
            job.update(status="running", engine="gemini", total=2, done=0, message="Menyiapkan audio...")
            whole = _encode(src, os.path.join(tmp, "whole"), mp3_only=True)
            if os.path.getsize(whole) > AI_MAX_BYTES:
                raise RuntimeError("Rekaman terlalu panjang untuk analisa audio langsung (di atas ~1,7 jam). Gunakan dasar analisa Transkrip.")
            target_lang = "mix" if job.get("mix") else lang_short
            # 1) Transkripsikan suara dengan STT murni (Google STT) terlebih dahulu agar kotak Transkrip Suara Real-time terisi tanpa memanggil AI dua kali
            transcript_text = ""
            try:
                job.update(message="Mentranskripsi suara rekaman (STT)...")
                transcript_text, _ = _transcribe_with_engines(src, tmp, target_lang, language_full, engine="google", job=None, allow_empty=True)
                if not transcript_text.strip():
                    used_tr: list = []
                    transcript_text = _transcribe_file_gemini(whole, target_lang, used_tr)
                if transcript_text:
                    job.update(done=1, text=transcript_text, partial=transcript_text)
            except Exception as tr_err:
                log.warning("Transkrip pendamping pada mode analyze gagal: %s", tr_err)

            # 2) Analisa audio langsung untuk menyusun notulen
            job.update(message="Menganalisis audio & menyusun notulen (AI)...")
            used: list = []
            summary = _analyze_audio(whole, target_lang, title, context, used)
            if used:
                job["model"] = used[-1]
            if len(summary.strip()) < 20:
                raise RuntimeError("Hasil analisa audio kosong.")
            job.update(status="done", done=2, summary=summary, text=transcript_text, message="Selesai")
            return

        text, eng_used = _transcribe_with_engines(src, tmp, "mix" if job.get("mix") else lang_short, language_full, engine=engine, job=job)
        job.update(status="done", engine=eng_used, text=text, message="Selesai")
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
