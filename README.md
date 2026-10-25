# Nat-Multi Tools v4.7 (Secure)

All-In-One Toolkit — Downloader · Comic · Novel · GIF · AI · Safelink  
**By: Nat**

---

## Keamanan v4.7 (penting)

### Apa yang diperbaiki
1. **Token HMAC** — setelah Secret Code benar, server mengeluarkan token bertanda tangan. Header `Authorization: admin` / `premium` mentah **ditolak**.
2. **YouTube** — hanya Premium / Admin (dicek di backend).
3. **MangaDex** — tidak lagi di daftar source premium (API gratis).
4. Frontend menyimpan `nmt_token`; tanpa token valid, role dianggap guest.

### Environment Variables (Vercel)

| Key | Wajib? | Keterangan |
|-----|--------|------------|
| `GROQ_API_KEY` | Ya (AI Chat) | console.groq.com |
| `GEMINI_API_KEY` | Ya (AI Image) | aistudio.google.com |
| `ADMIN_CODE` | Ya (Admin) | Kode admin rahasia — **jangan** di README |
| `PREMIUM_CODES` | Opsional | Beberapa kode, dipisah koma: `KODE1,KODE2` |
| `AUTH_SECRET` | **Sangat disarankan** | String acak panjang untuk tanda tangan token. Jika kosong, fallback ke ADMIN_CODE |

Contoh `AUTH_SECRET`: generate string acak 32+ karakter (password manager).

Setelah ubah env → **Redeploy** wajib.

---

## Limit

| Fitur | Free | Premium | Admin |
|-------|------|---------|-------|
| AI Chat / hari | 30 | 200 | ∞ |
| AI Image / hari | 5 | 40 | ∞ |
| Download / hari | 8 | 50 | ∞ |
| YouTube | ❌ | ✅ | ✅ |
| Source comic premium | ❌ | ✅ | ✅ |
| Source admin (adult) | ❌ | ❌ | ✅ |

*Limit di-serverless Vercel masih per-instance (cold start bisa reset). Token role tetap aman.*

---

## File

```
main.py
index.html
requirements.txt
vercel.json
README.md
banner1.gif  banner2.gif  banner3.gif   (opsional)
```

---

## Deploy

1. Upload semua file ke GitHub (replace `main.py` + `index.html`).
2. Vercel → Environment Variables (tabel di atas).
3. Deploy / Redeploy.
4. Buka `/health` → `"version": "4.7.0-secure"`.
5. Tab **Secret** → masukkan kode → harus dapat role + token.
6. Coba DevTools: request dengan `Authorization: admin` **harus tetap free** (limit ketat).

---

## Alur token (ringkas)

```
User input kode → POST /auth/verify
Server cek ADMIN_CODE / PREMIUM_CODES
Jika valid → return { role, token: "role.exp.hmac" }
Frontend simpan nmt_token
Setiap API call → Authorization: Bearer <token>
Server verifikasi HMAC + expiry → role
```

---

## Comic sources

| Source | Akses |
|--------|--------|
| MangaDex, Komiku, KeiKomik | Gratis |
| Voratoon, KomikIndo, Bacakomik, Lumoskomik | Premium |
| Wurmz, Komiktap, Mangasusu | Admin only |

---

## Troubleshooting

| Masalah | Solusi |
|---------|--------|
| Premium aktif di UI tapi API 403 | Redeploy dengan main.py 4.7; login ulang Secret Code |
| Semua jadi free setelah update | Normal — token lama tidak valid; input kode lagi |
| AI 503 | Cek GROQ / GEMINI env + redeploy |
| YouTube 403 | Butuh Premium |

---

**By: Nat**
