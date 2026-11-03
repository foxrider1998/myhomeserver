# Fanless Home Server — ThinkCentre M90n

Bundel deploy untuk menjadikan Lenovo ThinkCentre M90n Nano sebagai home server 24/7
headless yang menjalankan:

| Service | Peran | Sumber |
|---|---|---|
| `solar-dashboard` | Dashboard + ingest data Inverter POWMR & JK BMS | `D:\ht\test` (sudah ada, di-reuse) |
| `mail-collector` | Poll banyak akun IMAP tiap 60 detik, forward email baru | `./mail-collector` |
| `hermes-agent` | Webhook receiver + orchestrator (LLM via Sumopod) + solar watcher (otomasi SOC) | `./hermes-agent` |
| Appium + ADB | Remote control HP Android fisik (USB, bukan emulator) | `./appium` (systemd di host, bukan Docker) |

## Status saat ini

**Belum bisa deploy** — koneksi laptop ↔ ThinkCentre lewat kabel LAN langsung belum
terdeteksi (`ipconfig /all` menunjukkan adapter Ethernet laptop *Media disconnected*).
Cek dulu: PC menyala, kabel LAN nyangkut penuh di kedua ujung, lampu link di port
RJ-45 ThinkCentre nyala. Setelah itu baru bisa SSH masuk dan jalankan `deploy/deploy.ps1`.

Belum diketahui juga varian RAM ThinkCentre-nya (4/8/16 GB) — total kebutuhan RAM
stack ini (~1.2–2 GB, karena Appium hanya passthrough ke HP fisik, bukan emulator)
sudah aman bahkan di varian 8 GB. Kalau ternyata varian 4 GB/Celeron, skip Appium
dulu atau jalankan bergantian.

## Arsitektur

```
                         ┌────────────────────────────────────────┐
ESP32 (westpro-solar) ───┤  ThinkCentre M90n (Ubuntu Server)       │
  UART→POWMR             │                                        │
  BLE →JK BMS            │  docker compose:                       │
        │ HTTPS POST     │    solar-dashboard :3000 ── SQLite      │
        └───────────────►│         │ (SOC low/high)                │
                          │         ▼                               │
                          │    hermes-agent :8000 ──► Sumopod API   │
                          │         ▲                (LLM eksternal)│
                          │         │ webhook                       │
                          │    mail-collector (IMAP, N akun, 60s)   │
                          │                                          │
                          │  systemd (host, bukan container):        │
                          │    appium :4723 ──ADB/USB──► HP Android  │
                          └────────────────────────────────────────┘
```

`hermes-agent` polling `solar-dashboard`'s `/api/state` untuk trigger otomasi
(baterai lemah/surplus), sekaligus jadi tujuan webhook dari `mail-collector`.
Appium sengaja **tidak** di-Docker-kan — passthrough USB/ADB ke HP fisik lebih
gampang & stabil langsung di host lewat systemd (lihat catatan di `appium/`).

## Prasyarat di ThinkCentre

- OS: Ubuntu Server 22.04+ headless (butuh Node.js ≥ 22 untuk `solar-dashboard`,
  disediakan lewat image Docker `node:22-slim`, jadi host tidak perlu install Node
  kecuali untuk Appium).
- Docker + Docker Compose plugin terpasang.
- Akses SSH dari laptop ini sudah jalan (lihat bagian Status di atas).

## Deploy

1. Generate token ingest solar yang baru (jangan pakai default `ganti-token-ini`):
   ```
   openssl rand -hex 24
   ```
   Isikan ke `.env` (`SOLAR_INGEST_TOKEN=...`) DAN ke `D:\ht\test\esphome\secrets.yaml`
   (`ingest_token`) supaya ESP32 dan server cocok.
2. Copy `.env.example` → `.env`, isi `SUMOPOD_API_KEY` dan cek ulang `SUMOPOD_BASE_URL`
   di dashboard Sumopod (nilai default di sini masih tebakan, lihat catatan di
   `hermes-agent/main.py`).
3. Copy `mail-collector/config.example.json` → `mail-collector/config.json`, isi akun
   IMAP (pakai App Password, bukan password utama).
4. Edit `deploy/deploy.ps1` — isi `$RemoteHost` dan `$RemoteUser` sesuai IP/hostname
   ThinkCentre setelah SSH-nya jalan.
5. Jalankan `deploy/deploy.ps1` — ini rsync folder ini (`D:\fanless`) dan
   `D:\ht\test` ke ThinkCentre dengan struktur relatif yang sama (`~/fanless` dan
   `~/ht/test`), supaya path `context: ../ht/test` di `docker-compose.yml` tetap
   valid di sana.
6. Di ThinkCentre: `cd ~/fanless && docker compose up -d --build`.
7. Setup Appium terpisah di host (bukan lewat compose) — lihat `appium/setup.sh`.
8. Update `ingest_url` di ESP32 (`D:\ht\test\esphome\secrets.yaml`) supaya mengarah
   ke ThinkCentre (`https://<domain-atau-ip-thinkcentre>/api/ingest`), lalu flash ulang.

## Keamanan

- `.env` dan `mail-collector/config.json` di-`.gitignore` — jangan pernah commit.
  Isinya app password email + API key Sumopod.
- API key Sumopod sempat diketik langsung di percakapan setup proyek ini (bukan di
  file manapun di repo) — kalau percakapan itu pernah dibagikan/disimpan di tempat
  tidak privat, sebaiknya di-rotate dari dashboard Sumopod.
- `esphome/westpro-solar.yaml` masih `verify_ssl: false` untuk TLS ke ingest endpoint —
  aman untuk LAN lokal, tapi kalau nanti expose ke internet lewat `westpro.my.id`,
  aktifkan `ca_certificate` (lihat catatan di file YAML-nya).

## 9Router (opsional, hanya untuk speech-to-text)

Router AI lokal ([decolua/9router](https://github.com/decolua/9router)) untuk menekan biaya transkripsi/analisa audio. LLM teks tetap lewat Sumopod.

1. Di server sebagai user biasa: `bash 9router/setup.sh` (pasang `npm i -g 9router`, buat `~/.9router/env` berisi kata sandi acak & `REQUIRE_API_KEY=true`, aktifkan `systemctl --user` service, terikat ke `127.0.0.1:20128`).
2. Buka dashboard lewat tunnel SSH: `ssh -L 20128:127.0.0.1:20128 ares@<ip-server>` lalu `http://localhost:20128` (login memakai kata sandi dari setup). Sambungkan provider (OAuth/API key) dan buat API key.
3. Isi `.env` Westpro: `STT_BASE_URL=http://127.0.0.1:20128/v1`, `STT_API_KEY=<kunci>`, `STT_CHAT_MODEL=<model audioInput>`; restart `hermes-agent`.
4. Cek: unggah rekaman pendek di Notulen Rapat; label mesin di status unggahan harus `[gemini]` (atau `[ai]`).

Bila 9Router mati/tidak mendukung audio, transkripsi otomatis jatuh ke mesin berikutnya (`STT_ENGINES`).

## Email (SMTP keluar) — lupa kata sandi, notifikasi, briefing harian

Fitur: tautan **Lupa kata sandi** di halaman login (token sekali pakai, 30 menit), email uji & riwayat pengiriman (menu Sistem), dan opsi **Briefing Hermes harian ke email** (opt-in per user di Profil).

**Cloudflare Tunnel & email** — tunnel hanya meneruskan HTTP(S) ke web, jadi:
- **Email keluar** tidak lewat tunnel: server memakai relay SMTP pihak ketiga (port 587/465). Gratis/murah: Brevo (300/hari), Resend, Amazon SES, atau Gmail dengan App Password. Beberapa ISP rumahan memblokir port 25; 587/465 umumnya aman.
- **Email masuk** (menerima surat ke @domain-mu) tidak bisa lewat tunnel. Pakai **Cloudflare Email Routing** (Dashboard → Email → Email Routing) untuk meneruskan `apa-saja@westpro.my.id` ke Gmail; `mail-collector` yang sudah ada tetap membaca Gmail itu lewat IMAP.

Langkah:
1. Buat akun relay SMTP, verifikasi domain `westpro.my.id` di penyedia, lalu tambahkan record **SPF, DKIM, dan DMARC** yang diminta penyedia di DNS Cloudflare (mode *DNS only*, jangan di-proxy). Tanpa ini email masuk spam.
2. Isi blok `SMTP_*` dan `PUBLIC_BASE_URL` di `.env` (lihat `.env.example`), restart `powmr` dan `hermes-agent`.
3. Menu **Sistem → Layanan Email → Kirim email uji**. Pesan galat SMTP asli ditampilkan bila gagal.
4. (Opsional) Tiap user mengaktifkan briefing email di **Profil & SPEctrum → Notifikasi Email**.

## Westpro Mail — banyak alamat @westpro.my.id untuk testing

Menu **Mail**: kotak masuk untuk **semua** alamat `*@westpro.my.id` (catch-all), Terkirim, tulis/balas email, deteksi **kode OTP** dan **tautan verifikasi/reset**, serta API untuk skrip otomasi tes.

Alur (cocok dengan Cloudflare Tunnel; tidak perlu port 25 terbuka):
`Internet → MX Cloudflare (Email Routing, catch-all) → Email Worker → HTTPS POST → /api/mail/inbound (lewat tunnel, tanda tangan HMAC) → database`.

Pasang: ikuti `cloudflare/email-worker/README.md` (5 langkah, tanpa CLI) dan isi `MAIL_INBOUND_SECRET` di `.env`.

- **Alamat uji**: tiap user membuat alamat sendiri (acak `qa-xxxxxx@` atau nama pilihan; maks 50). Hanya pemilik alamat yang melihat emailnya. Alamat yang belum dibuat (mis. `random@westpro.my.id`) hanya terlihat oleh pemilik server.
- **Plus-addressing**: `qa+akun1@westpro.my.id`, `qa+akun2@…` semuanya masuk ke `qa@…` — satu alamat bisa untuk ratusan akun uji.
- **API otomasi**: lihat bagian *API Westpro Mail* di bawah.
- Pesan disimpan 30 hari (maks 5000); lampiran hanya dicatat nama/ukurannya. HTML email dirender di iframe ter-sandbox tanpa skrip/gambar eksternal.
- Email keluar tetap lewat relay SMTP (bagian Email di atas); balasan mengarah ke alamat Westpro lewat `Reply-To`.

### API Westpro Mail (buat alamat + ambil OTP)

**Autentikasi.** Header `X-API-Key: wpm_...`. Buat kunci di menu **Mail → 🔑 → Buat kunci API** (login sebagai akun yang dimaksud; kunci terikat ke akun itu, jadi alamat yang dibuat dengan kunci akun `areskretindo` menjadi milik akun tersebut dan hanya terlihat oleh akun itu). Alternatif tanpa login web (butuh akses shell ke server): `python3 mail_apikey.py areskretindo@gmail.com` (opsi `--status`, `--revoke`, `--yes`) membuat kunci langsung di database; kunci hanya tampil sekali. Satu kunci aktif per akun; kunci hanya berlaku untuk `/api/webmail/*`. Semua respons JSON; `success: false` + `error` bila gagal.

| Method | Path | Fungsi |
|---|---|---|
| POST | `/api/webmail/addresses` | Buat alamat baru |
| POST | `/api/webmail/addresses/delete` | Hapus alamat `{"address": "..."}` |
| GET | `/api/webmail/addresses?sort=az` | Daftar alamat (`sort=az` = urut A–Z), beserta jumlah email & belum dibaca |
| GET | `/api/webmail/otp?address=...` | **Tunggu & ambil OTP terbaru satu alamat** (long-poll) |
| GET | `/api/webmail/otp/all` | OTP terbaru **tiap alamat, urut A–Z** (snapshot, tanpa menunggu) |
| GET | `/api/webmail/messages?address=...` | Daftar email satu alamat |
| GET | `/api/webmail/message?id=...` | Isi lengkap satu email |
| GET | `/api/webmail/wait?address=...` | (lama) tunggu email apa pun yang baru |

**1. Buat alamat — `POST /api/webmail/addresses`**

Body JSON (semua opsional): `local` = bagian sebelum `@` (3–40 karakter `a-z 0-9 . _ -`; kosong = acak `qa-xxxxxx`), `label` = catatan.

```bash
curl -s -X POST -H "X-API-Key: wpm_xxx" -H "Content-Type: application/json" \
  -d '{"local":"","label":"uji signup"}' https://westpro.my.id/api/webmail/addresses
# {"success":true,"address":"qa-4f2a9c@westpro.my.id","domain":"westpro.my.id","otp_url":"/api/webmail/otp?address=qa-4f2a9c@westpro.my.id","error":null}
```

Galat umum (HTTP 400): nama sudah dipakai, nama dicadangkan (`admin`, `root`, `noreply`, ...), format tidak valid, batas 50 alamat/akun (pemilik server tanpa batas). Plus-addressing (`qa-4f2a9c+akun1@westpro.my.id`) masuk ke alamat dasar yang sama.

**2. Ambil OTP satu alamat — `GET /api/webmail/otp`**

Panggil **setelah** aplikasi Anda memicu pengiriman OTP. Server menunggu (long-poll) sampai ada email yang memenuhi syarat, lalu mengembalikan ringkasan.

| Parameter | Default | Arti |
|---|---|---|
| `address` | wajib | Alamat lengkap |
| `timeout` | 30 | Detik menunggu (maks 60; `0` = cek sekali tanpa menunggu) |
| `max_age` | 120 | Hanya email yang masuk ≤ N detik lalu (cegah OTP basi; `0` = tanpa batas) |
| `since_id` | 0 | Hanya email dengan `id` lebih besar (pakai `last_id` dari respons timeout/`messages`) |
| `subject_contains` / `from_contains` | kosong | Filter subjek / pengirim (tidak peka huruf besar) |
| `need` | `otp` | `otp` = harus berisi kode, `link` = harus ada tautan verifikasi, `any` = email apa pun |
| `mark_read` | 1 | `0` = jangan tandai terbaca |

```bash
curl -s -H "X-API-Key: wpm_xxx" \
  "https://westpro.my.id/api/webmail/otp?address=qa-4f2a9c@westpro.my.id&timeout=45&max_age=120&subject_contains=verifikasi"
```
```json
{"success":true,"found":true,"id":812,"address":"qa-4f2a9c@westpro.my.id","to":"qa-4f2a9c@westpro.my.id",
 "from":"Contoh <noreply@contoh.id>","subject":"Kode verifikasi Anda","received_at":"2026-11-03 10:15:22","age_sec":4,
 "otp":"482913","otps":["482913"],"verify_link":null,"links":[],"preview":"Kode verifikasi Anda 482913 berlaku 5 menit"}
```
Timeout → HTTP **408**: `{"success":false,"found":false,"address":"...","last_id":812,"error":"Timeout: ..."}`. `otp` adalah kandidat utama; `otps` memuat sampai 5 kandidat. OTP dideteksi dari subjek + isi (4–8 digit di dekat kata *kode/otp/verifikasi/token/pin*, cadangan: angka 6 digit).

**3. OTP tiap alamat A–Z — `GET /api/webmail/otp/all`**

Parameter: `max_age` (default 3600 detik), `need` (seperti di atas), `only_found=1` (sembunyikan alamat tanpa OTP). Tidak menunggu dan tidak menandai terbaca.
```json
{"success":true,"domain":"westpro.my.id","count":2,"addresses":[
 {"address":"qa-4f2a9c@westpro.my.id","label":"uji signup","total":3,"unread":1,"latest":{"id":812,"otp":"482913","subject":"...","age_sec":40,"verify_link":null}},
 {"address":"qa-b81d02@westpro.my.id","label":"","total":0,"unread":0,"latest":null}]}
```

**Contoh alur otomasi (Python)**
```python
import requests
B, H = "https://westpro.my.id", {"X-API-Key": "wpm_xxx"}
addr = requests.post(B + "/api/webmail/addresses", headers=H, json={"label": "tes signup"}).json()["address"]
# ... picu aplikasi mengirim OTP ke addr ...
r = requests.get(B + "/api/webmail/otp", headers=H, params={"address": addr, "timeout": 45}, timeout=60)
otp = r.json()["otp"] if r.status_code == 200 else None
```

Catatan: email ke alamat yang **belum** dibuat tetap diterima (catch-all) tetapi hanya terlihat oleh pemilik server; buat alamat dulu bila akun lain yang membacanya. Pesan disimpan 30 hari.
