// Westpro Mail - Cloudflare Email Worker
// Meneruskan SETIAP email yang masuk ke *@domain (catch-all) ke web Westpro lewat HTTPS (Cloudflare Tunnel).
// Tidak ada dependensi: email dikirim mentah (RFC822) dan diurai di server Westpro.
//
// Variabel (Worker -> Settings -> Variables and Secrets):
//   WEBHOOK_URL         mis. https://westpro.my.id/api/mail/inbound
//   MAIL_INBOUND_SECRET (Secret) sama persis dengan MAIL_INBOUND_SECRET di .env server
//   FALLBACK_FORWARD    (opsional) alamat terverifikasi di Email Routing, mis. kamu@gmail.com.
//                       Dipakai bila web Westpro sedang mati/menolak, agar email tidak hilang.

const MAX_BYTES = 10 * 1024 * 1024; // harus <= batas server (10 MB)

function toHex(buf) {
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function sign(secret, ts, bodyBytes) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const prefix = new TextEncoder().encode(ts + ".");
  const data = new Uint8Array(prefix.length + bodyBytes.length);
  data.set(prefix, 0);
  data.set(bodyBytes, prefix.length);
  return toHex(await crypto.subtle.sign("HMAC", key, data));
}

export default {
  async email(message, env, ctx) {
    const fallback = async (reason) => {
      if (env.FALLBACK_FORWARD) {
        await message.forward(env.FALLBACK_FORWARD);
      } else {
        message.setReject("Layanan email sedang tidak tersedia (" + reason + "). Coba lagi nanti.");
      }
    };

    if (message.rawSize > MAX_BYTES) {
      return fallback("ukuran terlalu besar");
    }

    const body = new Uint8Array(await new Response(message.raw).arrayBuffer());
    const ts = Math.floor(Date.now() / 1000).toString();
    const signature = await sign(env.MAIL_INBOUND_SECRET, ts, body);

    let res;
    try {
      res = await fetch(env.WEBHOOK_URL, {
        method: "POST",
        headers: {
          "Content-Type": "message/rfc822",
          "X-Mail-From": message.from,
          "X-Mail-To": message.to,
          "X-Timestamp": ts,
          "X-Signature": signature,
        },
        body,
      });
    } catch (e) {
      return fallback("server tidak terjangkau");
    }
    if (!res.ok) {
      return fallback("server menolak: " + res.status);
    }
  },
};
