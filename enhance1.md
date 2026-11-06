# enhance1.md — Catatan Perubahan (5–6 Nov 2026)

Server: `192.168.1.10` (`westpro.my.id`), repo `foxrider1998/myhomeserver`, branch `main`.

## 1. Meeting: Transkripsi Suara (STT)
| Perubahan | File | Commit |
|---|---|---|
| Hermes STT lewat 9Router (`STT_CHAT_MODEL=gemini/gemini-3.5-flash-lite`), cooldown model, `input_audio` sebelum `text` | `hermes-agent/stt.py`, `.env` server | 3776076, 07eefd2 |
| Konversi FLAC via `ffmpeg` agar `recognize_google` jalan di Ubuntu 64-bit | `hermes-agent/stt.py` | b8ff0fe |
| Fix JS: `'\n'` di dalam `HTML_PAGE` Python → `String.fromCharCode(10)` | `powmr_service.py` | 3105517 |
| **Live STT murni (tanpa AI)**: `/api/transcribe` default `engine="google"`; AI hanya setelah rekaman (Selesai & Rangkum / Analisa / Koreksi AI) | `hermes-agent/main.py`, `stt.py`, `powmr_service.py` | 21e8f61 |
| Mic: `webkitSpeechRecognition` selalu aktif walau share screen+audio | `powmr_service.py` | 21e8f61 |
| Hapus gate `peak < 0.006` (AnalyserNode tak terhubung → semua chunk setelah pertama dibuang) | `powmr_service.py` | 21e8f61 |
| **2 jalur STT overlap** (segmen 8 dtk, jalur B digeser 4 dtk) + `mergeLiveChunkText` (dedup overlap suffix/prefix) agar tidak ada kata terpotong | `powmr_service.py` | 42e6797 |
| Upload/poll job audio tahan balasan HTML dari tunnel (`Unexpected token '<'`): parse aman, retry polling, pesan HTTP status | `powmr_service.py` | — |
| `waitAudioJob`/`runAudioAnalysis` tidak menimpa transkrip live dengan hasil lebih pendek | `powmr_service.py` | 21e8f61 |

Default "Dasar analisa" tetap `audio` (Rekaman audio langsung).

## 2. Optimasi Performa Server
Temuan: server 2 core; `/api/energy/analytics` memakan ±13 dtk CPU per panggilan (308 rb baris telemetri, dipanggil tiap 30 dtk/tab); `xfce4-screensaver-dialog` macet ±92% CPU selama 4 jam.

| Perubahan | Hasil |
|---|---|
| `get_energy_analytics`: cache 5 menit, stale-while-revalidate (hitung ulang di thread latar) | tidak lagi menghitung per request |
| `strptime` → `fromisoformat` | 13,4 dtk → 4,4 dtk |
| Cache per-hari untuk hari yang sudah lewat + agregasi SQL (`AVG/MIN/MAX`) untuk statistik 7 hari | 4,4 dtk → **0,26 dtk** |
| Polling dashboard 30 dtk → 120 dtk, berhenti saat tab tersembunyi | beban ±4× lebih kecil |
| Screensaver XFCE dimatikan (`saver/enabled`, `lock/enabled` = false), proses macet di-kill | load avg ±3 → ±1,8 |

File: `history_db.py`, `powmr_service.py`. Keluaran lama vs baru sudah dibandingkan di server (identik kecuali data live hari berjalan).

## 3. Simulasi Saham — Analisis Kerugian & Guard Baru
### Hasil analisis akun `live` (22 Okt – 5 Nov, 9 hari trading)
- Modal Rp10.000.000 → Rp7.138.263 (**−28,6%**), hanya 1 dari 9 hari untung.
- 116 jual: menang 17 / kalah 99 (win rate **14,7%**); rata-rata +1,89% vs −2,41% → ekspektasi **−1,78%/transaksi** (impas butuh win rate ±56%).
- Fee ±Rp640 rb. Pick: CL 185× (−2,7%) vs TP 88× (+4,45%). Metode positif hanya `value` & `breakout`.
- IHSG turun 5.387 → 4.920 (−8,7%) sementara strategi long-only dan selalu penuh 5 posisi (porsi tersimpan di DB 80%/posisi).
- Entri banyak di 09:10 (52 dari 121 beli, dekat puncak pembukaan); 13/20 posisi yang bisa dicek kena CL lalu harga pulih (hold-to-close +0,52% vs aktual −0,87%).
- Saham < Rp1.000: 45 transaksi, rugi ±Rp1,63 jt (tick = 0,2–0,5%).
- Tidak ada data pick/analisis untuk 26 Okt, padahal simulasi tetap transaksi 19× (−8,97%) → **perlu diselidiki**.

### Perubahan kode — `stock_sim.py` (commit "Stock sim: guard anti-rugi")
Semua bisa diatur lewat config (`sim_config`), default:

| Guard | Config | Default |
|---|---|---|
| Jangan beli saat gap pembukaan | `first_buy` | `09:45` (sebelumnya mulai 09:10) |
| Batas harga minimum (kandidat disaring + beli ditolak) | `min_price` | 500 |
| Stop beli bila IHSG melemah | `halt_ihsg_pct` | −1,5% |
| Mode probe setelah rugi beruntun: maks 1 posisi, porsi kecil | `loss_streak_days`, `probe_pct` | 2 hari, 15% |
| AI tidak boleh jual posisi baru dibuka (CL/TP otomatis tetap aktif) | `min_hold_min` | 30 menit |
| CL/TP minimum dari harga masuk (dibulatkan ke tick) | `min_cl_pct`, `min_tp_pct` | 4,5% / 5% |
| Larang beli ulang saham yang sudah dijual rugi hari itu | `block_reentry` | 1 |
| Maks posisi | `max_positions` | 3 (sebelumnya 5) |
| Porsi per saham | `max_pos_pct` | 30% (DB sebelumnya 80%) |

Prompt AI juga diperbarui (beri ruang posisi, hindari gap pembukaan/saham murah/re-entry, jangan beli saat IHSG lemah). Semua guard tercatat di `sim_log` (kind `SKIP`).
Config live di server sudah di-set `max_positions=3`, `max_pos_pct=30`. Saat deploy IHSG −1,89% → beli baru otomatis ditahan hari itu.

### Belum dikerjakan / saran lanjutan
- Backtest lebih panjang sebelum dana simulasi `live` dipercaya; akun `test` masih kosong.
- Pembobotan metode pick (naikkan `value`/`breakout`, turunkan `volume`/`rsi`/`consensus`) di `_candidates`.
- Selidiki data kosong 26 Okt.
- Tabel agregat telemetri per jam bila 0,26 dtk masih dirasa berat.
- `teams.db` 429 MB belum diperiksa.

> Hasil simulasi 9 hari bukan jaminan strategi dan bukan rekomendasi investasi.


## 4. Live STT: Teks Bertabrakan/Dobel (6 Nov, siang)
- **Gejala:** transkrip berisi kalimat berulang (mic + audio layar menangkap ucapan yang sama; interim onend/stop ikut menambah teks yang sudah final).
- **Perbaikan** (powmr_service.py): satu pintu tulis `appendLiveText()` untuk semua sumber (mic final, interim, chunk audio layar); `mergeLiveChunkText` ditulis ulang berbasis jangkar urutan kata (>=3 kata atau >=2 kata panjang) pada 70 kata terakhir, jadi tahan beda ejaan dan membuang seluruh bagian yang sudah ada. Dedup mic hanya aktif saat share audio layar (mic-saja tetap apa adanya).

## 5. Rekaman Panjang: Pecah 30 Menit + Salinan Lokal (6 Nov, siang)
- **Penyimpanan:** rekaman tetap di sisi klien (browser). Server hanya memproses sementara di folder temp lalu menghapusnya; yang tersimpan di server hanya teks transkrip & notulen.
- **Pecah otomatis** (`powmr_service.py`, `startMainRecorder`): tiap 30 menit recorder utama berpindah ke segmen baru (recorder baru dimulai dulu, lama dihentikan -> tanpa gap). Tiap segmen file webm yang valid sendiri.
- **Cadangan crash:** tiap chunk 1 dtk ditulis ke IndexedDB (`meetingRec`). Tombol **?? Pulihkan rekaman terakhir** muncul saat halaman dibuka bila ada sisa rekaman; dibersihkan saat rekaman baru dimulai.
- **? Unduh Rekaman:** unduh semua bagian sebagai `.webm` (`rekaman-rapat-YYYYMMDD-HHMM-bagianN.webm`).
- **Analisa rekaman panjang** (`runSegmentedAnalysis`): tiap bagian ditranskripsi berurutan lewat job server, digabung, lalu notulen disusun dari teks gabungan (tidak ada satu panggilan AI raksasa). Rekaman satu bagian memakai alur lama.
- Uji: logika rollover di-mock di Node (bagian terpecah & bagian terakhir tertutup benar); belum diuji rapat >30 menit sungguhan.
