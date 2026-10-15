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
