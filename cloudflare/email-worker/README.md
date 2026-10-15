# Westpro Mail – Cloudflare Email Worker

Menerima email untuk **semua alamat** `*@westpro.my.id` dan meneruskannya ke web Westpro lewat Cloudflare Tunnel.

## Pasang (dashboard, tanpa CLI)
1. **Aktifkan Email Routing**: Cloudflare Dashboard → domain `westpro.my.id` → *Email* → *Email Routing* → Enable.
   Cloudflare menambahkan record MX + SPF otomatis. (Jika domain sudah punya MX lain, hapus/ganti agar tidak bentrok.)
2. **Buat Worker**: *Workers & Pages* → Create → *Create Worker* → beri nama `westpro-mail-inbound` → Deploy →
   *Edit code* → tempel isi `src/index.js` → Deploy.
3. **Variabel Worker** (Settings → Variables and Secrets):
   - `WEBHOOK_URL` = `https://westpro.my.id/api/mail/inbound`
   - `MAIL_INBOUND_SECRET` (tipe **Secret**) = string acak panjang, mis. hasil `openssl rand -hex 32`
   - (opsional) `FALLBACK_FORWARD` = alamat Gmail yang sudah diverifikasi di Email Routing
4. **Server Westpro**: tambahkan di `.env` nilai `MAIL_INBOUND_SECRET` yang **sama**, lalu `systemctl --user restart powmr`.
5. **Catch-all**: Email Routing → *Routing rules* → *Catch-all address* → Action: **Send to a Worker** → pilih `westpro-mail-inbound` → Save (aktifkan).

## Uji
Kirim email dari Gmail ke `apa-saja@westpro.my.id`. Dalam beberapa detik muncul di menu **Mail** (owner melihat semua alamat; user lain hanya alamat yang dibuatnya).

## Catatan
- Batas ukuran 10 MB per email. Lebih besar → diteruskan ke `FALLBACK_FORWARD` bila diisi, kalau tidak ditolak.
- Lampiran hanya dicatat namanya (tidak disimpan) agar database kecil. Pesan disimpan 30 hari / maks 5000.
- Cloudflare Tunnel cukup untuk jalur ini karena Worker memanggil web lewat HTTPS; tidak perlu port 25 terbuka.
