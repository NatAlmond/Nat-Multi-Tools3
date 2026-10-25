import asyncio
import json
"""
Nat-Multi Tools v4.7 (secure token) — scrape fix + downloader PERFECT VERSION
All-in-One Toolkit: Downloader · Comic · Anime · Novel · Movie · GIF · AI
By: Nat🥜
"""
import os, re, uuid, time, hashlib, urllib.parse
from pathlib import Path
from typing import List
from collections import defaultdict

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
import httpx

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False

# ---------- Setup ----------
DOWNLOAD_DIR = Path("/tmp/downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Nat-Multi Tools", version="4.7.7-secure")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

def env(k: str, d: str = "") -> str:
    return os.environ.get(k, d).strip()

# ---------- Rate Limiting ----------
usage_db = defaultdict(lambda: {"chat": 0, "image": 0, "download": 0, "reset_at": 0})

def get_usage(client_ip: str, role: str):
    now = time.time()
    u = usage_db[client_ip]
    if now > u["reset_at"]:
        u["chat"] = u["image"] = u["download"] = 0
        u["reset_at"] = now + 86400
    limits = {
        "free": {"chat": 30, "image": 5, "download": 8},
        "premium": {"chat": 200, "image": 40, "download": 50},
        "admin": {"chat": 999999, "image": 999999, "download": 999999},
    }
    return u, limits[role]

# ---------- Config ----------
# Kode HANYA dari Environment Variables Vercel (jangan hardcode di repo)
ADMIN_CODE = env("ADMIN_CODE", "").strip()
ADMIN_HASH = hashlib.sha256(ADMIN_CODE.encode()).hexdigest() if ADMIN_CODE else ""
# Secret untuk menandatangani token sesi (wajib beda dari kode admin idealnya)
AUTH_SECRET = env("AUTH_SECRET", "") or ADMIN_CODE or env("GROQ_API_KEY", "") or "nmt-change-me-in-vercel"
FIXED_PREMIUM_CODES = set()  # kosong — pakai PREMIUM_CODES di env
# Source premium (MangaDex = gratis API, tidak dimasukkan)
PREMIUM_SOURCES = {"voratoon", "komikindo", "bacakomik", "lumoskomik", "luvyaa"}
# Admin-only adult/doujin-style sources
ADMIN_SOURCES = {"wurmz", "komiktap", "mangasusu"}  # hanya Admin (adult/doujin)

def _sign_token(role: str, exp: int) -> str:
    """HMAC token: role.exp.sig — tidak bisa dipalsu tanpa AUTH_SECRET."""
    import hmac as _hmac
    payload = f"{role}.{exp}"
    sig = _hmac.new(AUTH_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:40]
    return f"{payload}.{sig}"

def _verify_token(token: str) -> str:
    """Validasi token; return role atau ''."""
    import hmac as _hmac
    token = (token or "").strip()
    if not token or token.count(".") != 2:
        return ""
    role, exp_s, sig = token.split(".", 2)
    if role not in ("admin", "premium"):
        return ""
    try:
        exp = int(exp_s)
    except ValueError:
        return ""
    if exp < int(time.time()):
        return ""  # kedaluwarsa
    payload = f"{role}.{exp}"
    expected = _hmac.new(AUTH_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:40]
    if not _hmac.compare_digest(sig, expected):
        return ""
    return role

def detect_role(request: Request) -> str:
    """Role dari token HMAC di header Authorization: Bearer <token>.
    TIDAK menerima string mentah 'admin'/'premium' (itu celah lama).
    """
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        auth = auth[7:].strip()
    # Tolak bypass lama
    if auth.lower() in ("admin", "premium", "free", "guest"):
        return "free"
    role = _verify_token(auth)
    return role if role else "free"

# ---------- Models ----------
class DownloadRequest(BaseModel):
    url: str
    type: str = "video"
    quality: str = "best"

class ChatRequest(BaseModel):
    model: str = "groq"
    messages: List[dict]

class ImageRequest(BaseModel):
    model: str = "gemini"
    prompt: str

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8",
}

# ============================================================
# FRONTEND
# ============================================================
@app.get("/", response_class=HTMLResponse)
def home():
    for p in [Path(__file__).parent / "index.html", Path.cwd() / "index.html"]:
        try:
            if p.exists():
                return HTMLResponse(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return HTMLResponse("<h1>index.html tidak ditemukan</h1>", status_code=404)

@app.get("/health")
def health():
    return {"status": "ok", "version": "4.7.7-secure", "ytdlp": HAS_YTDLP, "bs4": HAS_BS4}


@app.get("/proxy-img")
async def proxy_img(url: str = "", ref: str = ""):
    """Proxy gambar (sampul Komiku dll) agar tidak diblokir hotlink."""
    from fastapi.responses import Response
    url = (url or "").strip()
    if not url.startswith("http"):
        raise HTTPException(400, "url invalid")
    # batasi host yang diizinkan
    allow = (
        "komiku.", "cdn.komiku", "img.komiku", "image.komiku", "uploads",
        "wibulep.", "wsrv.nl", "i0.wp.com", "i1.wp.com", "i2.wp.com",
        "imgkomik", "manhwature", "voratoon", "wurmz.", "luvyaa",
    )
    low = url.lower()
    if not any(a in low for a in allow):
        raise HTTPException(403, "host tidak diizinkan")
    ref = (ref or "").strip()
    if not ref.startswith("http"):
        if "komiku" in low:
            ref = KOMIKU_WEB + "/"
        else:
            ref = url
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers={
            **BROWSER_HEADERS,
            "Referer": ref,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }) as client:
            r = await client.get(url)
            if r.status_code != 200 or not r.content:
                raise HTTPException(404, "gambar gagal")
            ctype = r.headers.get("content-type") or "image/jpeg"
            if "image" not in ctype and "octet" not in ctype:
                ctype = "image/jpeg"
            return Response(content=r.content, media_type=ctype, headers={
                "Cache-Control": "public, max-age=86400",
            })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"proxy: {type(e).__name__}")


@app.get("/banner.gif")
@app.get("/banner1.gif")
@app.get("/banner2.gif")
@app.get("/banner3.gif")
def serve_banner(request: Request):
    name = request.url.path.lstrip("/")
    for base in (Path(__file__).parent, Path(__file__).parent / "public"):
        p = base / name
        if p.exists():
            return FileResponse(p, media_type="image/gif")
    raise HTTPException(404, "banner not found")

# ============================================================
# DOWNLOADER
# ============================================================
@app.post("/download")
async def download(req: DownloadRequest, request: Request):
    if not HAS_YTDLP:
        raise HTTPException(500, "yt-dlp tidak terpasang")
    if not req.url.startswith(("http://", "https://")):
        raise HTTPException(400, "URL tidak valid")
    if "spotify.com" in req.url.lower():
        raise HTTPException(400, "Spotify tidak didukung (DRM)")

    client_ip = request.client.host if request.client else "unknown"
    role = detect_role(request)
    usage, limits = get_usage(client_ip, role)
    if usage["download"] >= limits["download"]:
        raise HTTPException(429, f"Limit download harian ({limits['download']}x)")

    # YouTube hanya Premium / Admin
    low_url = req.url.lower()
    if any(x in low_url for x in ("youtube.com", "youtu.be", "music.youtube.com")):
        if role not in ("premium", "admin"):
            raise HTTPException(403, "YouTube = fitur Premium. Aktifkan Secret Code dulu.")
    
    file_id = str(uuid.uuid4())[:8]
    outtmpl = str(DOWNLOAD_DIR / f"{file_id}.%(ext)s")
    
    if req.type == "audio":
        opts = {
            "format": "bestaudio/best", "outtmpl": outtmpl,
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}],
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "socket_timeout": 30,
            "retries": 3,
        }
    else:
        fmt_map = {
            "best": "best[ext=mp4]/best",
            "720": "best[height<=720][ext=mp4]/best",
            "480": "best[height<=480][ext=mp4]/best",
            "360": "best[height<=360][ext=mp4]/best",
        }
        opts = {
            "format": fmt_map.get(req.quality, "best[ext=mp4]/best"),
            "outtmpl": outtmpl, "quiet": True, "no_warnings": True, "noplaylist": True,
            "socket_timeout": 30, "retries": 3,
        }
    
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(req.url, download=True)
            title = info.get("title", "download")
            downloaded = next(DOWNLOAD_DIR.glob(f"{file_id}.*"), None)
            if not downloaded:
                raise Exception("File tidak ditemukan")
            safe = "".join(c for c in title if c.isalnum() or c in " -_")[:60].strip() or "download"
            final = DOWNLOAD_DIR / f"{safe}{downloaded.suffix}"
            n = 1
            while final.exists():
                final = DOWNLOAD_DIR / f"{safe}_{n}{downloaded.suffix}"
                n += 1
            downloaded.rename(final)
            usage["download"] += 1
            return {
                "status": "success", "filename": final.name,
                "download_url": f"/file/{final.name}", "title": title,
                "usage_left": limits["download"] - usage["download"],
            }
    except Exception as e:
        msg = str(e).lower()
        if "sign in" in msg or "not a bot" in msg:
            raise HTTPException(500, "YouTube memblokir server cloud. Coba TikTok/IG/FB.")
        raise HTTPException(500, f"Download gagal: {str(e)[:200]}")

@app.get("/file/{filename}")
def get_file(filename: str):
    if ".." in filename or "/" in filename:
        raise HTTPException(400, "Invalid")
    path = DOWNLOAD_DIR / filename
    if not path.exists():
        raise HTTPException(404, "File tidak ditemukan")
    return FileResponse(path, filename=filename)

# ============================================================
# COMIC — MangaDex (API) + Komiku (api.komiku.org + HTML)
# ============================================================
KOMIKU_API = "https://api.komiku.org"
KOMIKU_WEB = "https://komiku.org"
KOMIKU_MIRRORS = [
    "https://komiku.org",
    "https://komiku.id",
    "https://api.komiku.org",
]

MD_API = "https://api.mangadex.org"
VORATOON_BASE = "https://v10.voratoon.com"
VORATOON_API = "https://api.voratoon.com"
VORATOON_WEB = "https://voratoon.co"  # HTML fallback saat API 403
WURMZ_BASE = "https://wurmz.net"
KOMIKINDO_BASE = "https://komikindo.ch"
KEIKOMIK_BASE = "https://keikomik.com"
KLIKMANGA_BASE = "https://klikmanga.org"
BACAKOMIK_BASE = "https://bacakomik.my"
LUVYAA_BASE = "https://v5.luvyaa.co"
LUVYAA_MIRRORS = ["https://v5.luvyaa.co", "https://v4.luvyaa.co", "https://luvyaa.co", "https://luvyaa.my.id"]
KOMIKTAP_BASE = "https://komiktap.info"
MANGASUSU_BASE = "https://mangasusuku.com"
LEPOYTL_BASE = "https://www.lepoytl.my.id"

async def get_lepoy_series(q: str = "") -> list:
    """Daftar series unik Lepoy TL dari feed Blogger (bukan menu/chapter)."""
    if not HAS_BS4:
        return []
    qn = (q or "").lower().strip()
    seen = {}
    start = 1
    meta_cat = {
        "chapter", "project", "update", "series", "id", "en", "jp", "end", "ongoing",
        "comedy", "romance", "school life", "slice of life", "supernatural", "drama",
        "action", "fantasy", "isekai", "ecchi", "seinen", "shoujo", "shounen", "adult",
        "mature", "crossdress", "yuri", "yaoi", "bl", "gl", "hentai", "smut",
    }
    img_re = re.compile(r'src=["\']([^"\']+)["\']', re.I)
    ch_re = re.compile(r'chapter\s*\d', re.I)
    ch_short = re.compile(r'^ch\.?\s*\d', re.I)
    num_re = re.compile(r'^[\d.]+$')
    try:
        async with httpx.AsyncClient(timeout=28.0, follow_redirects=True, headers=BROWSER_HEADERS) as client:
            while start < 800:
                url = f"{LEPOYTL_BASE}/feeds/posts/default?alt=json&max-results=150&start-index={start}"
                r = await client.get(url)
                if r.status_code != 200:
                    break
                try:
                    data = r.json()
                except Exception:
                    break
                entries = ((data.get("feed") or {}).get("entry")) or []
                if not entries:
                    break
                for e in entries:
                    title = e.get("title")
                    if isinstance(title, dict):
                        title = title.get("$t") or ""
                    title = (title or "").strip()
                    cats = [c.get("term") for c in (e.get("category") or []) if c.get("term")]
                    cats_l = [c.lower() for c in cats]
                    thumb = ""
                    media = e.get("media$thumbnail") or {}
                    if isinstance(media, dict):
                        thumb = media.get("url") or ""
                    content = ""
                    for key in ("content", "summary"):
                        block = e.get(key)
                        if isinstance(block, dict) and block.get("$t"):
                            content = block.get("$t") or content
                    if not thumb and content:
                        m = img_re.search(content)
                        if m:
                            thumb = m.group(1)
                    if thumb.startswith("//"):
                        thumb = "https:" + thumb
                    names = []
                    if "chapter" in cats_l or ch_re.search(title):
                        for c in cats:
                            cl = c.lower()
                            if cl in meta_cat or num_re.match(c):
                                continue
                            if len(c) < 3:
                                continue
                            names.append(c)
                    elif title and "chapter" not in title.lower() and not ch_short.search(title):
                        skip = {"bookmark", "manga list", "manga", "home", "lepoy tl"}
                        if title.lower() not in skip and len(title) > 3:
                            names.append(title)
                    for name in names:
                        key = name.lower().strip()
                        if key in seen:
                            if thumb and not seen[key].get("thumb"):
                                seen[key]["thumb"] = thumb
                            continue
                        if qn and qn not in key:
                            continue
                        seen[key] = {
                            "title": name[:90],
                            "thumb": thumb,
                            "id": name,
                            "source": "lepoytl",
                            "label": "GRATIS",
                        }
                start += 150
                if len(entries) < 150:
                    break
    except Exception:
        pass
    return list(seen.values())


READNOVELFULL_BASE = "https://readnovelfull.com"
LUMOSKOMIK_BASE = "https://03.lumosgg.com"



def _clean_chapter_title(text: str) -> str:
    text = (text or "").strip()
    m = re.search(r"(?:Chapter|Ch\.?|Bab)[\s._-]*(\d+(?:\.\d+)?)", text, flags=re.I)
    if m:
        return f"Chapter {m.group(1)}"
    m = re.search(r"\b(\d+(?:\.\d+)?)\b", text)
    if m and len(text) < 40:
        return f"Chapter {m.group(1)}"
    text = re.sub(r"[\d.,]+[KkMmBb]\b", " ", text)
    text = re.sub(r"\b\d+\s*(jam|hari|bulan|minggu|menit|detik)\s*(lalu)?\b", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" -·|:")
    return text[:80] or "Chapter"



def _voratoon_parse_covers(html_text: str) -> dict:
    """slug -> signed cover URL from voratoon.co HTML."""
    if not html_text:
        return {}
    text = html_text.replace("&amp;", "&").replace(chr(92) + "u0026", "&").replace(chr(92) + "/", "/")
    pairs = {}
    for m in re.finditer(
        r"(https://cvr\.voratoon\.id/prod/series/([a-z0-9\-]+)/cover/[A-Za-z0-9._\-]+(?:\?[^\s\"\'<>\\]+)?)",
        text,
        re.I,
    ):
        full = m.group(1).rstrip("\\").rstrip("&")
        slug = m.group(2)
        if slug not in pairs or len(full) > len(pairs[slug]):
            pairs[slug] = full
    return pairs


def _komiku_thumb_from_img(img, base: str = "https://komiku.org") -> str:
    """Ambil URL sampul terbaik dari tag img (lazy-load friendly)."""
    if img is None:
        return ""
    candidates = []
    for attr in ("data-src", "data-lazy-src", "data-original", "data-url", "data-bg", "src"):
        v = (img.get(attr) or "").strip()
        if v:
            candidates.append(v)
    for attr in ("data-srcset", "srcset"):
        ss = (img.get(attr) or "").strip()
        if not ss:
            continue
        best, best_w = "", 0
        for part in ss.split(","):
            part = part.strip().split()
            if not part:
                continue
            u = part[0]
            w = 0
            if len(part) > 1 and part[1].endswith("w"):
                try:
                    w = int(part[1][:-1])
                except Exception:
                    w = 0
            if w >= best_w:
                best_w = w
                best = u
        if best:
            candidates.append(best)
    style = (img.get("style") or "")
    m = re.search(r"url\((['\"]?)([^'\"\)\s]+)\1\)", style)
    if m:
        candidates.append(m.group(2))
    skip = ("data:image", "placeholder", "blank.gif", "1x1", "spacer", "loading.", "spinner")
    for v in candidates:
        if not v or any(s in v.lower() for s in skip):
            continue
        if v.startswith("//"):
            v = "https:" + v
        elif v.startswith("/"):
            v = base.rstrip("/") + v
        if v.startswith("http"):
            return v
    return ""

def _komiku_title_from_card(card) -> str:
    h3 = card.find("h3")
    if h3:
        t = h3.get_text(strip=True)
        if t and len(t) > 2:
            return t
    img = card.find("img")
    if img and img.get("alt"):
        return img.get("alt")
    a = card.find("a", href=True)
    if a:
        t = a.get_text(strip=True)
        t = re.sub(r"^(Manga|Manhwa|Manhua|Aksi|Fantasi)+", "", t, flags=re.I).strip()
        if t:
            return t
    return "?"


def _is_komiku_page_image(src: str, img_tag=None) -> bool:
    """True untuk panel chapter Komiku; false untuk iklan/logo."""
    if not src or not str(src).startswith("http"):
        return False
    low = str(src).lower().split("?")[0]
    block = (
        "promosi", "promo-", "-promo", "iklan", "/ads/", "advert", "banner",
        "logo", "icon", "avatar", "emoji", "gravatar", "gstatic", "firebase",
        "spinner", "pixel", "button", "badge", "donate", "donasi",
        "facebook", "twitter", "whatsapp", "telegram", ".svg",
    )
    if any(b in low for b in block):
        return False
    fname = low.rsplit("/", 1)[-1]
    if any(x in fname for x in ("promosi", "promo", "iklan", "ads", "banner", "logo")):
        return False
    # panel: uploads path atau CDN komiku
    if any(x in low for x in ("/uploads/", "/uploads2/", "/upload5/", "img.komiku", "image2.komiku", "cdn.komiku", "komiku.org/upload")):
        if img_tag is not None:
            cls = " ".join(img_tag.get("class") or []).lower()
            if "klazy" in cls or "ww" in cls.split():
                return True
        if re.search(r"(part\d|\d{4,}|chapter)", fname):
            return True
        if fname.endswith((".jpg", ".jpeg", ".png", ".webp")):
            return True
    return False


def _slug_from_href(href: str) -> str:
    href = (href or "").strip().rstrip("/")
    if not href:
        return ""
    # /manga/foo/ -> foo ; /foo-chapter-1/ -> foo-chapter-1
    parts = href.split("/")
    return parts[-1] or (parts[-2] if len(parts) > 1 else "")



def _parse_komiku_cards(html: str, source: str = "komiku"):
    if not HAS_BS4:
        return []
    soup = BeautifulSoup(html, "html.parser")
    base = KOMIKU_WEB.rstrip("/")

    def abs_url(u: str) -> str:
        u = (u or "").strip()
        if not u:
            return ""
        if u.startswith("//"):
            return "https:" + u
        if u.startswith("/"):
            return base + u
        return u

    def pick_thumb_from_node(node) -> str:
        if node is None:
            return ""
        # semua img di dalam node
        imgs = node.find_all("img") if hasattr(node, "find_all") else []
        if getattr(node, "name", None) == "img":
            imgs = [node] + imgs
        scored = []
        for img in imgs:
            u = _komiku_thumb_from_img(img, base)
            if not u:
                continue
            low = u.lower()
            if any(x in low for x in ("logo", "icon", "avatar", "emoji", "spinner", "pixel", "banner", "promo")):
                continue
            score = 0
            if any(x in low for x in ("upload", "cdn.komiku", "img.komiku", "image.komiku", "komiku.id", "komiku.org")):
                score += 5
            if any(x in low for x in (".jpg", ".jpeg", ".png", ".webp")):
                score += 2
            if "cover" in low or "thumb" in low:
                score += 2
            scored.append((score, u))
        if scored:
            scored.sort(key=lambda x: -x[0])
            return scored[0][1]
        # background-image di style
        for el in (node.select("[style*='background']") if hasattr(node, "select") else []):
            st = el.get("style") or ""
            m = re.search(r"url\((['\"]?)([^'\"\)\s]+)\1\)", st)
            if m:
                u = abs_url(m.group(2))
                if u.startswith("http") and not any(x in u.lower() for x in ("logo", "icon", "banner")):
                    return u
        # regex di HTML kartu
        chunk = str(node)
        urls = re.findall(
            r"https?://[^\"'\s>]+(?:cdn\.komiku|img\.komiku|image\d*\.komiku|komiku\.(?:id|org)|/uploads\d*/)[^\"'\s>]+\.(?:jpg|jpeg|png|webp)",
            chunk,
            flags=re.I,
        )
        for u in urls:
            low = u.lower()
            if any(x in low for x in ("logo", "icon", "banner", "promo", "avatar")):
                continue
            return u
        # relative /uploads
        rels = re.findall(r"(?:src|data-src)=[\"'](/[^\"']+\.(?:jpg|jpeg|png|webp))[\"']", chunk, flags=re.I)
        for u in rels:
            if "logo" in u.lower():
                continue
            return abs_url(u)
        return ""

    items = []
    cards = soup.find_all("div", class_="bge")
    if not cards:
        cards = soup.select(".bge, .ls4, .perapih, article.box, .kan")

    if cards:
        for card in cards[:48]:
            link = card.find("a", href=re.compile(r"/manga/")) or card.find("a", href=True)
            if not link:
                continue
            href = link.get("href") or ""
            if "/manga/" not in href and "/komik/" not in href:
                # tetap izinkan bila path mirip series
                if not re.search(r"/manga/[\w-]+", href):
                    continue
            slug = _slug_from_href(href)
            if not slug or slug in ("manga", "genre", "komik", "page"):
                continue
            title = _komiku_title_from_card(card)
            if not title or title == "?":
                title = (link.get("title") or link.get_text(" ", strip=True) or slug)[:90]
            title = re.sub(r"\s+", " ", title).strip()[:90]
            label = "GRATIS"
            tip = card.find(class_="tpe1_inf")
            if tip and "manhwa" in tip.get_text(strip=True).lower():
                label = "MANHWA"
            thumb = pick_thumb_from_node(card)
            items.append({
                "title": title,
                "thumb": thumb,
                "id": slug,
                "source": source,
                "label": label,
            })
    else:
        # fallback: semua link /manga/
        seen = set()
        for a in soup.select('a[href*="/manga/"]'):
            href = a.get("href") or ""
            slug = _slug_from_href(href)
            if not slug or slug in seen or slug in ("manga", "genre"):
                continue
            seen.add(slug)
            title = (a.get("title") or a.get_text(" ", strip=True) or slug)[:90]
            title = re.sub(r"\s+", " ", title).strip()
            if len(title) < 2:
                continue
            parent = a
            thumb = ""
            for _ in range(5):
                if parent is None:
                    break
                thumb = pick_thumb_from_node(parent)
                if thumb:
                    break
                parent = getattr(parent, "parent", None)
            items.append({
                "title": title,
                "thumb": thumb,
                "id": slug,
                "source": source,
                "label": "GRATIS",
            })
            if len(items) >= 40:
                break

    # dedupe by id, prefer yang punya thumb
    best = {}
    for it in items:
        sid = it.get("id") or ""
        if not sid:
            continue
        prev = best.get(sid)
        if not prev or (it.get("thumb") and not prev.get("thumb")):
            best[sid] = it
        elif prev and not prev.get("title") and it.get("title"):
            best[sid] = it
    return list(best.values())[:48]



@app.get("/comic/genres")
async def comic_genres(source: str = "all"):
    """Daftar genre untuk filter UI."""
    common = [
        "Action", "Adventure", "Comedy", "Drama", "Ecchi", "Fantasy", "Harem",
        "Horror", "Martial Arts", "Mystery", "Romance", "School", "Sci-Fi",
        "Seinen", "Shoujo", "Shounen", "Slice of Life", "Sports", "Supernatural",
        "Tragedy", "Isekai", "Reincarnation", "Murim", "Historical",
    ]
    if source == "wurmz":
        return {"genres": ["18+", "Yaoi", "Hentai", "Doujinshi", "Ecchi"]}
    return {"genres": common}



@app.get("/comic/search")
async def comic_search(request: Request, source: str = "all", q: str = ""):
    items = []
    q = (q or "").strip()
    if source != "wurmz" and q.lower().startswith("genre:"):
        q = q.split(":", 1)[-1].strip()
    role = detect_role(request)
    if source in ADMIN_SOURCES and role != "admin":
        raise HTTPException(403, "Source Admin — khusus Admin.")
    if source in PREMIUM_SOURCES and role not in ("premium", "admin"):
        raise HTTPException(403, "Source Premium, Harap Gunakan Premium Untuk Membuka.")

    # MangaDex search (premium opsional) — hanya konten normal (tanpa BL/18+/doujin)
    # Tag ID resmi MangaDex
    MD_EXCLUDE_TAGS = [
        "5920b825-4181-4f17-9dcb-a2ca8f7ff68e",  # Boys' Love
        "b13b2a48-c720-426f-a9ed-2fadfe4ed1bc",  # Doujinshi
        "97893a4c-12e4-4d1e-9da9-0c5d9cb0ea24",  # Sexual Violence
        "2d1f5d56-4016-49d6-a636-4ee34890cf5f",  # Loli
        "ddefd648-9846-4bd5-8a1f-d4f2f4b9c8c9",  # Shota — may fail if wrong; also filter client
        "5bd0e105-1209-4d39-b23c-0c0b9a465819",  # Incest
        "b29d6a3d-1569-4e7a-8caf-7557bc834b5e",  # Gore
    ]
    if source == "mangadex":
        try:
            async with httpx.AsyncClient(timeout=22.0) as client:
                # Ambil tag ID akurat sekali (cache di fungsi lewat list lokal di atas + refine)
                tag_r = await client.get(f"{MD_API}/manga/tag")
                exclude_ids = []
                block_names = {
                    "boys' love", "yaoi", "yuri", "girls' love", "doujinshi", "smut",
                    "sexual violence", "loli", "shota", "incest", "gore", "hentai",
                    "ecchi", "adult", "mature",
                }
                if tag_r.status_code == 200:
                    for t in (tag_r.json() or {}).get("data") or []:
                        nm = (((t.get("attributes") or {}).get("name") or {}).get("en") or "").lower()
                        if nm in block_names or any(b in nm for b in ("boys' love", "sexual", "doujin", "loli", "shota")):
                            exclude_ids.append(t["id"])
                if not exclude_ids:
                    exclude_ids = MD_EXCLUDE_TAGS
                params = [
                    ("limit", 24),
                    ("order[relevance]", "desc" if q else "rating"),
                    ("includes[]", "cover_art"),
                    ("contentRating[]", "safe"),
                    ("publicationDemographic[]", "shounen"),
                    ("publicationDemographic[]", "seinen"),
                    ("publicationDemographic[]", "shoujo"),
                    ("publicationDemographic[]", "josei"),
                    ("publicationDemographic[]", "none"),
                ]
                for tid in exclude_ids:
                    params.append(("excludedTags[]", tid))
                params.append(("excludedTagsMode", "OR"))
                if q:
                    params.append(("title", q))
                else:
                    params.append(("order[followedCount]", "desc"))
                r = await client.get(f"{MD_API}/manga", params=params)
                if r.status_code == 200:
                    for m in (r.json() or {}).get("data") or []:
                        attrs = m.get("attributes") or {}
                        # skip non-safe
                        if (attrs.get("contentRating") or "").lower() not in ("safe", ""):
                            continue
                        # filter tag nama di sisi klien
                        bad = False
                        for g in attrs.get("tags") or []:
                            nm = (((g.get("attributes") or {}).get("name") or {}).get("en") or "").lower()
                            if nm in block_names or "boys' love" in nm or "doujin" in nm:
                                bad = True
                                break
                        if bad:
                            continue
                        titles = attrs.get("title") or {}
                        title = titles.get("en") or titles.get("id") or next(iter(titles.values()), m.get("id"))
                        cover = ""
                        for rel in m.get("relationships") or []:
                            if rel.get("type") == "cover_art":
                                fn = ((rel.get("attributes") or {}).get("fileName") or "")
                                if fn:
                                    cover = f"https://uploads.mangadex.org/covers/{m['id']}/{fn}.256.jpg"
                        items.append({"title": title, "thumb": cover, "id": m["id"], "source": "mangadex", "label": "GRATIS"})
        except Exception:
            pass

    # Themesia helpers

    async def _themesia_list(base, source_name, label, path_key="/komik/", limit=48):
        """List series dari situs Themesia-like; ambil hanya URL series (bukan chapter)."""
        out = []
        try:
            async with httpx.AsyncClient(timeout=22.0, follow_redirects=True, headers={**BROWSER_HEADERS, "Referer": base + "/"}) as client:
                url = f"{base}/?s={urllib.parse.quote(q)}" if q else base + "/"
                r = await client.get(url)
                if r.status_code != 200 or not HAS_BS4:
                    return out
                soup = BeautifulSoup(r.text, "html.parser")
                seen = set()
                key = path_key.strip("/")  # komik / manga
                for a in soup.find_all("a", href=True):
                    href = (a.get("href") or "").strip()
                    if f"/{key}/" not in href and not href.rstrip("/").endswith(f"/{key}"):
                        continue
                    # potong query
                    path = href.split("?")[0].rstrip("/")
                    parts = path.split("/")
                    try:
                        i = parts.index(key)
                    except ValueError:
                        continue
                    if i + 1 >= len(parts):
                        continue
                    slug = parts[i + 1]
                    # chapter di path → skip
                    if i + 2 < len(parts) and parts[i + 2]:
                        continue
                    if not slug or slug in seen or slug in (key, "page", "genre", "genres", "type"):
                        continue
                    # judul bersih
                    title = (a.get("title") or "").strip()
                    img = a.find("img")
                    if not title and img is not None:
                        title = (img.get("alt") or img.get("title") or "").strip()
                    if not title:
                        title = a.get_text(" ", strip=True)
                    title = re.sub(r"\s+", " ", title or "").strip()
                    # buang label navigasi
                    low = title.lower()
                    if len(title) < 2 or low.startswith("baca ") or low in ("detail", "more", "view", "chapter"):
                        if img is not None:
                            title = (img.get("alt") or img.get("title") or "").strip()
                        if not title or title.lower() in ("detail",):
                            title = slug.replace("-", " ").title()
                    # bersihkan prefix poster trending
                    title = re.sub(r"^(?:Poster\s+)?Trending:\s*", "", title, flags=re.I).strip()
                    title = title[:90]
                    thumb = ""
                    if img is not None:
                        thumb = (img.get("data-src") or img.get("data-lazy-src") or img.get("src") or "").strip()
                    if not thumb:
                        parent = a.parent
                        for _ in range(4):
                            if parent is None:
                                break
                            im = parent.find("img")
                            if im is not None:
                                thumb = (im.get("data-src") or im.get("src") or "").strip()
                                if not title or len(title) < 3:
                                    title = (im.get("alt") or title or slug).strip()[:90]
                                break
                            parent = getattr(parent, "parent", None)
                    if thumb.startswith("//"):
                        thumb = "https:" + thumb
                    seen.add(slug)
                    out.append({
                        "title": title or slug.replace("-", " ").title(),
                        "thumb": thumb,
                        "id": slug,
                        "source": source_name,
                        "label": label,
                    })
                    if len(out) >= limit:
                        break
        except Exception:
            pass
        return out



    # Komiku (gratis) — coba beberapa mirror + parser kartu .bge

    if source in ("all", "komiku") and HAS_BS4:
        try:
            komiku_headers = {
                **BROWSER_HEADERS,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Upgrade-Insecure-Requests": "1",
            }
            got = []
            async with httpx.AsyncClient(timeout=25.0, follow_redirects=True, headers=komiku_headers) as client:
                urls = []
                qq = urllib.parse.quote(q) if q else ""
                if q:
                    # Format search Komiku (beberapa mirror/path)
                    urls = [
                        f"{KOMIKU_WEB}/?post_type=manga&s={qq}",
                        f"{KOMIKU_WEB}/?s={qq}",
                        f"{KOMIKU_WEB}/page/1/?s={qq}",
                        f"{KOMIKU_WEB}/page/2/?s={qq}",
                        f"{KOMIKU_WEB}/manga/?s={qq}",
                        f"{KOMIKU_API}/?s={qq}",
                        f"{KOMIKU_WEB}/cari/?s={qq}",
                        f"https://komiku.id/?s={qq}",
                        f"https://komiku.id/?post_type=manga&s={qq}",
                    ]
                else:
                    urls = [
                        f"{KOMIKU_WEB}/",
                        f"{KOMIKU_WEB}/manga/",
                        f"{KOMIKU_WEB}/other/hot/",
                        f"{KOMIKU_WEB}/other/berwarna/",
                        f"{KOMIKU_API}/",
                        "https://komiku.id/",
                        "https://komiku.id/manga/",
                    ]
                    max_p = 6 if source == "all" else 15
                    for pno in range(1, max_p + 1):
                        urls.append(f"{KOMIKU_WEB}/manga/page/{pno}/")
                        urls.append(f"{KOMIKU_WEB}/page/{pno}/")
                        if pno <= 8:
                            urls.append(f"https://komiku.id/manga/page/{pno}/")

                async def _fetch_komiku(u: str):
                    try:
                        r = await client.get(u, headers={**komiku_headers, "Referer": KOMIKU_WEB + "/"})
                        if r.status_code != 200 or len(r.text) < 1500:
                            return []
                        if "Just a moment" in r.text or "cf-browser-verification" in r.text:
                            return []
                        part = _parse_komiku_cards(r.text, "komiku")
                        # regex fallback bila parser DOM kosong
                        if not part:
                            for m in re.finditer(
                                r'href=["\']([^"\']*?/manga/([a-z0-9\-]+)/?)["\'][^>]*>',
                                r.text, re.I,
                            ):
                                slug = m.group(2)
                                if slug in ("manga", "genre", "page", "komik"):
                                    continue
                                title = slug.replace("-", " ").title()
                                part.append({
                                    "title": title,
                                    "thumb": "",
                                    "id": slug,
                                    "source": "komiku",
                                    "label": "GRATIS",
                                })
                        for it in part:
                            it["label"] = it.get("label") or "GRATIS"
                            t = (it.get("title") or "")
                            t = re.sub(r"(?i)^\s*baca\s+", "", t).strip()
                            it["title"] = t[:90] or it.get("id", "")
                        return part
                    except Exception:
                        return []

                batch = 5
                for i in range(0, len(urls), batch):
                    chunk = urls[i:i + batch]
                    results = await asyncio.gather(*[_fetch_komiku(u) for u in chunk])
                    for part in results:
                        got.extend(part)
                    if source == "all" and len({x.get("id") for x in got if x.get("id")}) >= 50:
                        break
                    # search: lanjut sampai dapat hasil
                    if q and len({x.get("id") for x in got if x.get("id")}) >= 40:
                        break

            # Filter client-side jika search server kosong tapi katalog terisi
            ql = (q or "").lower().strip()
            seen_k = set()
            filtered = []
            for it in got:
                sid = it.get("id") or ""
                if not sid or sid in seen_k:
                    continue
                title = (it.get("title") or "")
                if ql:
                    blob = (title + " " + sid).lower().replace("-", " ")
                    words = [w for w in ql.replace("-", " ").split() if len(w) > 1]
                    if ql not in blob and not (words and all(w in blob for w in words[:3])):
                        continue
                seen_k.add(sid)
                filtered.append(it)

            # Jika search gagal total: ambil katalog lalu filter lokal (lebih banyak halaman)
            if q and len(filtered) < 5:
                extra_urls = []
                for pno in range(1, 12):
                    extra_urls.append(f"{KOMIKU_WEB}/manga/page/{pno}/")
                    extra_urls.append(f"{KOMIKU_WEB}/page/{pno}/")
                async with httpx.AsyncClient(timeout=25.0, follow_redirects=True, headers=komiku_headers) as client:
                    for i in range(0, len(extra_urls), 5):
                        chunk = extra_urls[i:i + 5]
                        results = await asyncio.gather(*[_fetch_komiku(u) for u in chunk])
                        for part in results:
                            for it in part:
                                sid = it.get("id") or ""
                                if not sid or sid in seen_k:
                                    continue
                                title = (it.get("title") or "")
                                blob = (title + " " + sid).lower().replace("-", " ")
                                words = [w for w in ql.replace("-", " ").split() if len(w) > 1]
                                if ql not in blob and not (words and all(w in blob for w in words[:3])):
                                    continue
                                seen_k.add(sid)
                                filtered.append(it)
                        if len(filtered) >= 40:
                            break

            lim = 40 if source == "all" else 200
            for it in (filtered if q else got):
                sid = it.get("id") or ""
                if not sid:
                    continue
                # dedupe for non-search path
                if not q:
                    if sid in seen_k:
                        continue
                    seen_k.add(sid)
                items.append(it)
                if len([x for x in items if x.get("source") == "komiku"]) >= lim:
                    break
        except Exception:
            pass

        # Jika Komiku kosong (sering karena Cloudflare di Vercel), fallback MangaDex agar list tidak sepi
        if source == "komiku":
            n_komiku = sum(1 for x in items if x.get("source") == "komiku")
            if n_komiku < 8:
                try:
                    async with httpx.AsyncClient(timeout=18.0, follow_redirects=True, headers=BROWSER_HEADERS) as client:
                        params = {"limit": 40, "order[followedCount]": "desc", "contentRating[]": ["safe", "suggestive"], "includes[]": "cover_art"}
                        if q:
                            params["title"] = q
                        r = await client.get(f"{MD_API}/manga", params=params)
                        if r.status_code == 200:
                            for row in (r.json().get("data") or []):
                                mid = row.get("id")
                                attrs = row.get("attributes") or {}
                                titles = attrs.get("title") or {}
                                title = titles.get("en") or titles.get("ja") or next(iter(titles.values()), "Manga")
                                thumb = ""
                                for rel in (row.get("relationships") or []):
                                    if rel.get("type") == "cover_art":
                                        fn = (rel.get("attributes") or {}).get("fileName")
                                        if fn:
                                            thumb = f"https://uploads.mangadex.org/covers/{mid}/{fn}.256.jpg"
                                items.append({
                                    "title": str(title)[:90],
                                    "thumb": thumb,
                                    "id": mid,
                                    "source": "mangadex",
                                    "label": "GRATIS",
                                })
                except Exception:
                    pass

        items.extend(await _themesia_list(KEIKOMIK_BASE, "keikomik", "GRATIS", "/komik/", 36 if source == "all" else 48))

    if source in ("all", "klikmanga") and HAS_BS4:
        items.extend(await _themesia_list(KLIKMANGA_BASE, "klikmanga", "GRATIS", "/manga/", 36 if source == "all" else 48))

    if source in ("all", "lepoytl") and HAS_BS4:
        try:
            series = await get_lepoy_series(q)
            for s in series[:48]:
                items.append({
                    "title": s.get("title") or "?",
                    "thumb": s.get("thumb") or "",
                    "id": s.get("id") or s.get("title") or "",
                    "source": "lepoytl",
                    "label": "GRATIS",
                })
        except Exception:
            pass






    if source in ("voratoon", "all"):
        # Voratoon API sering 403 CF di Vercel — utamakan HTML voratoon.co/browse
        try:
            seen = set()
            got_api = []
            # API singkat (1-2 page) lalu HTML
            try:
                api_headers = {
                    "User-Agent": BROWSER_HEADERS.get("User-Agent", "Mozilla/5.0"),
                    "Accept": "application/json, text/plain, */*",
                    "Origin": VORATOON_BASE,
                    "Referer": VORATOON_BASE + "/",
                }
                async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers=api_headers) as client:
                    pages = 2 if source == "all" else 4
                    for page in range(1, pages + 1):
                        if q:
                            urls = [f"{VORATOON_API}/series?search={urllib.parse.quote(q)}&page={page}&limit=50"]
                        else:
                            urls = [f"{VORATOON_API}/series?page={page}&limit=50"]
                        for u in urls:
                            try:
                                r = await client.get(u)
                            except Exception:
                                continue
                            if r.status_code != 200:
                                continue
                            try:
                                body = r.json()
                            except Exception:
                                continue
                            rows = body if isinstance(body, list) else []
                            if isinstance(body, dict):
                                data = body.get("data")
                                if isinstance(data, list):
                                    rows = data
                                elif isinstance(data, dict):
                                    rows = data.get("data") or data.get("items") or data.get("results") or []
                                else:
                                    rows = body.get("results") or body.get("items") or body.get("series") or []
                            if not isinstance(rows, list):
                                continue
                            for row in rows:
                                if not isinstance(row, dict):
                                    continue
                                if isinstance(row.get("data"), dict) and ("title" in row["data"] or "slug" in row["data"]):
                                    row = row["data"]
                                slug = str(row.get("slug") or row.get("id") or row.get("_id") or "").strip()
                                title = (row.get("title") or row.get("name") or "").strip()
                                if not slug and title:
                                    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
                                if not slug:
                                    continue
                                thumb = row.get("coverImage") or row.get("cover") or row.get("thumbnail") or row.get("coverUrl") or ""
                                if isinstance(thumb, dict):
                                    thumb = thumb.get("url") or thumb.get("src") or ""
                                if not title:
                                    title = str(slug).replace("-", " ").title()
                                got_api.append({
                                    "title": str(title)[:90],
                                    "thumb": str(thumb) if thumb else "",
                                    "id": str(slug),
                                    "source": "voratoon",
                                    "label": "PREMIUM",
                                })
                            break
            except Exception:
                pass

            for it in got_api:
                sid = it.get("id") or ""
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                items.append(it)

            # HTML browse — sumber utama (bisa 20+ halaman)
            async with httpx.AsyncClient(timeout=22.0, follow_redirects=True, headers={**BROWSER_HEADERS, "Referer": VORATOON_WEB + "/"}) as client:
                ql = (q or "").lower().strip()
                page_urls = []
                if ql:
                    page_urls = [
                        f"{VORATOON_WEB}/?s={urllib.parse.quote(q)}",
                        f"{VORATOON_WEB}/browse?q={urllib.parse.quote(q)}",
                    ]
                    for p in range(1, 12):
                        page_urls.append(VORATOON_WEB + ("/browse" if p == 1 else f"/browse?page={p}"))
                else:
                    page_urls = [VORATOON_WEB + "/", VORATOON_WEB + "/library"]
                    max_pages = 8 if source == "all" else 30
                    for p in range(1, max_pages + 1):
                        page_urls.append(VORATOON_WEB + ("/browse" if p == 1 else f"/browse?page={p}"))

                async def _fetch_vt(u: str):
                    try:
                        r = await client.get(u)
                        if r.status_code != 200:
                            return []
                        html = r.text
                        try:
                            cm = _voratoon_parse_covers(html)
                        except Exception:
                            cm = {}
                        rows = []
                        # regex cepat (tanpa dependensi struktur DOM)
                        for m in re.finditer(r'href=["\']([^"\']*/series/([a-z0-9\-]+)/?)["\']', html, re.I):
                            slug = m.group(2)
                            if not slug or slug in ("browse", "library"):
                                continue
                            title = slug.replace("-", " ").title()
                            thumb = cm.get(slug) or ""
                            rows.append((slug, title, thumb))
                        if HAS_BS4:
                            try:
                                soup = BeautifulSoup(html, "html.parser")
                                for a in soup.select("a[href*='/series/']"):
                                    href = (a.get("href") or "").strip()
                                    m = re.search(r"/series/([a-z0-9\-]+)", href)
                                    if not m:
                                        continue
                                    slug = m.group(1)
                                    img = a.find("img")
                                    title = ""
                                    thumb = cm.get(slug) or ""
                                    if img is not None:
                                        title = (img.get("alt") or img.get("title") or "").strip()
                                        if not thumb:
                                            thumb = (img.get("src") or img.get("data-src") or "").strip()
                                    if not title:
                                        t = a.get_text(" ", strip=True)
                                        t = re.sub(r"(?i)(?:\d+\s+)?[\d.]+[kmb]?\s+[\d.]+[kmb]?\s*#\d+\s*", "", t)
                                        t = re.sub(r"^[\d.\sKMB#]+", "", t).strip()
                                        title = t
                                    if not title or len(title) < 3:
                                        title = slug.replace("-", " ").title()
                                    title = re.sub(r"\s+", " ", title)[:90]
                                    if thumb.startswith("//"):
                                        thumb = "https:" + thumb
                                    rows.append((slug, title, thumb))
                            except Exception:
                                pass
                        return rows
                    except Exception:
                        return []

                lim = 40 if source == "all" else 400
                for i in range(0, len(page_urls), 6):
                    chunk = page_urls[i:i + 6]
                    results = await asyncio.gather(*[_fetch_vt(u) for u in chunk])
                    for rows in results:
                        for slug, title, thumb in rows:
                            if not slug or slug in seen:
                                continue
                            if ql:
                                blob = (title + " " + slug).lower().replace("-", " ")
                                words = [w for w in ql.replace("-", " ").split() if len(w) > 1]
                                if ql not in blob and not (words and all(w in blob for w in words[:3])):
                                    continue
                            seen.add(slug)
                            items.append({
                                "title": title,
                                "thumb": thumb,
                                "id": slug,
                                "source": "voratoon",
                                "label": "PREMIUM",
                            })
                            if len(seen) >= lim:
                                break
                    if len(seen) >= lim:
                        break
        except Exception:
            pass

    if source in ("komikindo", "all") and HAS_BS4:
        try:
            async with httpx.AsyncClient(timeout=22.0, follow_redirects=True, headers={**BROWSER_HEADERS, "Referer": KOMIKINDO_BASE + "/"}) as client:
                url = f"{KOMIKINDO_BASE}/?s={urllib.parse.quote(q)}" if q else KOMIKINDO_BASE + "/"
                r = await client.get(url)
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, "html.parser")
                    seen = set()
                    boxes = soup.select(".animepost, .bsx, .listupd .bs, article")
                    if not boxes:
                        boxes = soup.select("div")
                    for box in boxes:
                        a = box.select_one("a[href*='/komik/']")
                        if not a:
                            continue
                        href = (a.get("href") or "").strip()
                        # hanya series: /komik/slug/ tanpa chapter
                        m = re.search(r"/komik/([^/]+)/?$", href.split("?")[0].rstrip("/").replace(KOMIKINDO_BASE, ""))
                        if not m:
                            # full URL
                            m = re.search(r"/komik/([^/]+)/?$", href.split("?")[0])
                        if not m:
                            continue
                        slug = m.group(1)
                        if not slug or slug in seen:
                            continue
                        # skip id numerik murni aneh & path menu
                        if slug in ("komik", "manga", "genre", "genres", "project-list"):
                            continue
                        img = box.select_one("img")
                        title = (a.get("title") or "").strip()
                        if not title and img is not None:
                            title = (img.get("alt") or img.get("title") or "").strip()
                        if not title:
                            title = a.get_text(" ", strip=True)
                        title = re.sub(r"\s+", " ", title or "").strip()
                        # buang prefix "Komik "
                        title = re.sub(r"^Komik\s+", "", title, flags=re.I).strip()
                        low = title.lower()
                        # filter kartu situs / logo / sampah
                        if not title or len(title) < 2:
                            continue
                        if low in ("komikindo", "komik indo", "home", "manga", "manhwa", "manhua"):
                            continue
                        if "komikindo" in low and len(title) < 20:
                            continue
                        thumb = ""
                        if img is not None:
                            thumb = (img.get("data-src") or img.get("data-lazy-src") or img.get("src") or "").strip()
                        if thumb.startswith("//"):
                            thumb = "https:" + thumb
                        # logo situs biasanya kecil / mengandung logo di path
                        if thumb and any(x in thumb.lower() for x in ("logo", "favicon", "icon-")):
                            thumb = ""
                        seen.add(slug)
                        items.append({
                            "title": title[:90],
                            "thumb": thumb,
                            "id": slug,
                            "source": "komikindo",
                            "label": "PREMIUM",
                        })
                        if len(seen) >= (24 if source == "all" else 48):
                            break
        except Exception:
            pass


    if source in ("bacakomik",) and HAS_BS4:
        items.extend(await _themesia_list(BACAKOMIK_BASE, "bacakomik", "PREMIUM", "/komik/", 48))

    if source in ("lumoskomik", "all") and HAS_BS4 and (source == "lumoskomik" or role in ("premium", "admin")):
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers={**BROWSER_HEADERS, "Referer": LUMOSKOMIK_BASE + "/"}) as client:
                urls = [f"{LUMOSKOMIK_BASE}/browse?q={urllib.parse.quote(q)}"] if q else [f"{LUMOSKOMIK_BASE}/browse?page={p}" for p in range(1, 8)]
                seen = set()
                for u in urls:
                    r = await client.get(u)
                    if r.status_code != 200:
                        continue
                    soup = BeautifulSoup(r.text, "html.parser")
                    for a in soup.find_all("a", href=True):
                        if "/comic/" not in a["href"]:
                            continue
                        slug = a["href"].rstrip("/").split("/comic/")[-1].split("?")[0]
                        if not slug or "/" in slug or slug in seen:
                            continue
                        title = a.get_text(" ", strip=True)[:90] or slug
                        img = a.find("img")
                        thumb = (img.get("src") or img.get("data-src") or "") if img else ""
                        seen.add(slug)
                        items.append({"title": title, "thumb": thumb, "id": slug, "source": "lumoskomik", "label": "PREMIUM"})
                        if len(seen) >= 48:
                            break
        except Exception:
            pass


    if source in ("wurmz",) and HAS_BS4:
        try:
            async with httpx.AsyncClient(timeout=25.0, follow_redirects=True, headers={**BROWSER_HEADERS, "Referer": WURMZ_BASE + "/"}) as client:
                if q and q.lower().startswith("genre:"):
                    g = q.split(":", 1)[-1].strip()
                    url = f"{WURMZ_BASE}/semua-komik?q=&type=&status=&genres%5B%5D={urllib.parse.quote(g)}&sort=new"
                elif q:
                    url = f"{WURMZ_BASE}/semua-komik?q={urllib.parse.quote(q)}&sort=new"
                else:
                    url = f"{WURMZ_BASE}/semua-komik?sort=new"
                r = await client.get(url)
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, "html.parser")
                    seen = set()
                    for a in soup.select("a[href*='/detail/']"):
                        href = (a.get("href") or "").strip()
                        m = re.search(r"/detail/([^/]+)/([^/?#]+)", href)
                        if not m:
                            continue
                        typ, slug = m.group(1), m.group(2)
                        key = f"{typ}/{slug}"
                        if key in seen:
                            continue
                        img = a.find("img")
                        title = ""
                        thumb = ""
                        if img is not None:
                            title = (img.get("alt") or img.get("title") or "").strip()
                            thumb = (img.get("data-src") or img.get("src") or "").strip()
                        if not title or title.lower() in ("manhwa", "manhua", "manga", "comic"):
                            for sel in ("h2", "h3", ".title", ".name", "p"):
                                el = a.select_one(sel)
                                if el:
                                    t = el.get_text(" ", strip=True)
                                    if t and t.lower() not in ("manhwa", "manhua", "manga") and not re.match(r"^ch\.?\s*\d", t, re.I):
                                        title = t
                                        break
                        if not title or title.lower() in ("manhwa", "manhua", "manga"):
                            parent = a.parent
                            for _ in range(3):
                                if parent is None:
                                    break
                                for el in parent.select("h2, h3, .title, .name"):
                                    t = el.get_text(" ", strip=True)
                                    if t and t.lower() not in ("manhwa", "manhua", "manga") and not re.match(r"^ch\.?\s*\d", t, re.I):
                                        title = t
                                        break
                                if title and title.lower() not in ("manhwa", "manhua", "manga"):
                                    break
                                parent = getattr(parent, "parent", None)
                        if not title or title.lower() in ("manhwa", "manhua", "manga") or re.match(r"^ch\.?\s*\d", title or "", re.I):
                            title = slug.replace("-", " ").title()
                        title = re.sub(r"\s+", " ", title)[:90]
                        if not thumb:
                            parent = a.parent
                            for _ in range(4):
                                if parent is None:
                                    break
                                im = parent.find("img")
                                if im is not None:
                                    thumb = (im.get("data-src") or im.get("src") or "").strip()
                                    break
                                parent = getattr(parent, "parent", None)
                        if thumb.startswith("//"):
                            thumb = "https:" + thumb
                        elif thumb and thumb.startswith("/"):
                            thumb = WURMZ_BASE + thumb
                        seen.add(key)
                        items.append({
                            "title": title,
                            "thumb": thumb,
                            "id": key,
                            "source": "wurmz",
                            "label": "ADMIN",
                        })
                        if len(seen) >= 48:
                            break
        except Exception:
            pass


    if source in ("luvyaa", "all") and HAS_BS4 and (source == "luvyaa" or True):
        # Luvyaa (Themesia root-slug): series di /{slug}/ , chapter di /{slug}-chapter-N/
        if source == "all" and role not in ("premium", "admin"):
            pass  # skip premium di all untuk free
        else:
            try:
                bases = list(LUVYAA_MIRRORS) if source == "luvyaa" else [LUVYAA_BASE]
                got = []
                async with httpx.AsyncClient(timeout=24.0, follow_redirects=True, headers=BROWSER_HEADERS) as client:
                    urls = []
                    for base in bases[:2]:
                        if q:
                            urls.append(f"{base}/?s={urllib.parse.quote(q)}")
                        else:
                            urls.extend([
                                f"{base}/",
                                f"{base}/manga/?order=update",
                                f"{base}/manga/?type=manhwa&order=update",
                                f"{base}/manga/?type=manga&order=update",
                            ])
                            for pg in range(2, 6 if source == "luvyaa" else 3):
                                urls.append(f"{base}/page/{pg}/")
                    seen = set()
                    skip = {"manga", "page", "genre", "genres", "project", "bookmark", "login", "register",
                            "feed", "list-mode", "status", "type", "novel", "manhwa", "manhua", "komik",
                            "wp-content", "wp-admin", "wp-json", "author", "tag", "category"}
                    for u in urls:
                        try:
                            r = await client.get(u, headers={**BROWSER_HEADERS, "Referer": LUVYAA_BASE + "/"})
                        except Exception:
                            continue
                        if r.status_code != 200 or len(r.text) < 2000:
                            continue
                        if "Just a moment" in r.text or "cf-browser-verification" in r.text:
                            continue
                        soup = BeautifulSoup(r.text, "html.parser")
                        for a in soup.select("a[href]"):
                            href = (a.get("href") or "").strip()
                            if not href or "chapter" in href.lower():
                                continue
                            # root series: https://host/slug/ or /slug/
                            m = re.search(r"https?://[^/]+/([a-z0-9\-]+)/?$", href.split("?")[0]) or re.search(r"^/([a-z0-9\-]+)/?$", href.split("?")[0])
                            if not m:
                                # also /manga/slug/
                                m = re.search(r"/manga/([a-z0-9\-]+)/?$", href.split("?")[0])
                            if not m:
                                continue
                            slug = m.group(1).lower()
                            if slug in skip or slug in seen or len(slug) < 3:
                                continue
                            # must look like series card (img nearby or bs/bsx)
                            parent = a
                            has_img = a.find("img") is not None
                            for _ in range(4):
                                if parent is None:
                                    break
                                cls = " ".join(parent.get("class") or []).lower() if hasattr(parent, "get") else ""
                                if a.find("img") or (hasattr(parent, "find") and parent.find("img")):
                                    has_img = True
                                if any(x in cls for x in ("bs", "bsx", "utao", "listupd", "styleall")):
                                    has_img = True
                                parent = getattr(parent, "parent", None)
                            if not has_img and source == "all":
                                continue
                            img = a.find("img")
                            if img is None and hasattr(a, "parent") and a.parent:
                                img = a.parent.find("img") if hasattr(a.parent, "find") else None
                            title = (a.get("title") or "").strip()
                            if not title and img is not None:
                                title = (img.get("alt") or img.get("title") or "").strip()
                            if not title:
                                title = a.get_text(" ", strip=True)
                            title = re.sub(r"\s+", " ", title or "").strip()
                            title = re.sub(r"(?i)^(baca|komik|manga)\s+", "", title).strip()[:90]
                            if not title or len(title) < 2:
                                title = slug.replace("-", " ").title()
                            if q:
                                blob = (title + " " + slug).lower().replace("-", " ")
                                ql = q.lower().strip()
                                words = [w for w in ql.replace("-", " ").split() if len(w) > 1]
                                if ql not in blob and not (words and all(w in blob for w in words[:3])):
                                    continue
                            thumb = ""
                            if img is not None:
                                thumb = (img.get("data-src") or img.get("data-lazy-src") or img.get("src") or "").strip()
                            if thumb.startswith("//"):
                                thumb = "https:" + thumb
                            seen.add(slug)
                            got.append({
                                "title": title,
                                "thumb": thumb,
                                "id": slug,
                                "source": "luvyaa",
                                "label": "PREMIUM",
                            })
                            lim = 36 if source == "all" else 120
                            if len(got) >= lim:
                                break
                        if len(got) >= (36 if source == "all" else 120):
                            break
                items.extend(got)
            except Exception:
                pass


    if source in ("komiktap",) and HAS_BS4:
        items.extend(await _themesia_list(KOMIKTAP_BASE, "komiktap", "ADMIN", "/manga/", 48))

    if source in ("mangasusu",) and HAS_BS4:
        items.extend(await _themesia_list(MANGASUSU_BASE, "mangasusu", "ADMIN", "/komik/", 48))

    # dedupe
    seen2, unique = set(), []
    for it in items:
        key = ((it.get("title") or "").lower()[:40], it.get("source"), it.get("id"))
        if key in seen2:
            continue
        seen2.add(key)
        unique.append(it)
    limit = 80 if source == "all" else 48
    return {"items": unique[:limit]}



@app.get("/comic/detail")
async def comic_detail(request: Request, source: str = "komiku", id: str = "", lang: str = "all"):
    if not id:
        raise HTTPException(400, "id kosong")
    role = detect_role(request)
    if source in ADMIN_SOURCES and role != "admin":
        raise HTTPException(403, "Source Admin — khusus Admin.")
    if source in PREMIUM_SOURCES and role not in ("premium", "admin"):
        raise HTTPException(403, "Source Premium, Harap Gunakan Premium Untuk Membuka.")
    chapters, title, synopsis, thumb, genres = [], id, "", "", []

# 1) MangaDex
    if source == "mangadex":
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(f"{MD_API}/manga/{id}", params=[("includes[]", "cover_art"), ("includes[]", "author")])
                if r.status_code == 200:
                    m = (r.json() or {}).get("data") or {}
                    attrs = m.get("attributes") or {}
                    titles = attrs.get("title") or {}
                    if isinstance(titles, dict) and titles:
                        title = titles.get("en") or titles.get("id") or titles.get("ja") or next(iter(titles.values()), id)
                    else:
                        title = str(titles or id)
                    # alt titles
                    if not title or title == id:
                        for at in (attrs.get("altTitles") or []):
                            if isinstance(at, dict) and at:
                                title = next(iter(at.values()))
                                break
                    desc = attrs.get("description") or {}
                    if isinstance(desc, dict):
                        synopsis = (desc.get("en") or desc.get("id") or desc.get("ja") or next(iter(desc.values()), "") or "")[:1200]
                    elif isinstance(desc, str):
                        synopsis = desc[:1200]
                    for rel in m.get("relationships") or []:
                        if rel.get("type") == "cover_art":
                            fn = ((rel.get("attributes") or {}).get("fileName") or "")
                            if fn:
                                thumb = f"https://uploads.mangadex.org/covers/{id}/{fn}.256.jpg"
                    for g in (attrs.get("tags") or [])[:10]:
                        n = ((g.get("attributes") or {}).get("name") or {})
                        if isinstance(n, dict):
                            n = n.get("en") or next(iter(n.values()), None)
                        if n:
                            genres.append(n)
                lang = (lang or "all").lower().strip()
                offset = 0
                chapters = []
                while offset < 500:
                    feed_params = [
                        ("limit", 100),
                        ("offset", offset),
                        ("order[chapter]", "desc"),
                        ("includeEmptyPages", "0"),
                        ("contentRating[]", "safe"),
                        ("contentRating[]", "suggestive"),
                        ("contentRating[]", "erotica"),
                        ("contentRating[]", "pornographic"),
                    ]
                    if lang in ("id", "indonesia", "ind"):
                        feed_params.append(("translatedLanguage[]", "id"))
                    elif lang in ("en", "eng", "english"):
                        feed_params.append(("translatedLanguage[]", "en"))
                    # lang=all → tanpa filter bahasa
                    rf = await client.get(f"{MD_API}/manga/{id}/feed", params=feed_params)
                    batch = (rf.json() or {}).get("data") or []
                    if not batch:
                        break
                    for ch in batch:
                        a = ch.get("attributes") or {}
                        num = a.get("chapter") or "?"
                        lang_ch = (a.get("translatedLanguage") or "").upper()
                        extra = a.get("title") or ""
                        label = f"Chapter {num}"
                        if extra:
                            label += f" — {extra}"
                        if lang_ch:
                            label += f" [{lang_ch}]"
                        chapters.append({"title": label[:100], "id": ch["id"], "lang": a.get("translatedLanguage") or ""})
                    offset += 100
                    if len(batch) < 100:
                        break
                # fallback: jika filter bahasa kosong, ambil semua
                if not chapters and lang not in ("all", "", "any"):
                    offset = 0
                    while offset < 500:
                        rf = await client.get(f"{MD_API}/manga/{id}/feed", params=[
                            ("limit", 100), ("offset", offset), ("order[chapter]", "desc"), ("includeEmptyPages", "0"),
                            ("contentRating[]", "safe"), ("contentRating[]", "suggestive"),
                            ("contentRating[]", "erotica"), ("contentRating[]", "pornographic"),
                        ])
                        batch = (rf.json() or {}).get("data") or []
                        if not batch:
                            break
                        for ch in batch:
                            a = ch.get("attributes") or {}
                            num = a.get("chapter") or "?"
                            lang_ch = (a.get("translatedLanguage") or "").upper()
                            label = f"Chapter {num}" + (f" [{lang_ch}]" if lang_ch else "")
                            chapters.append({"title": label[:100], "id": ch["id"], "lang": a.get("translatedLanguage") or ""})
                        offset += 100
                        if len(batch) < 100:
                            break
        except Exception as e:
            raise HTTPException(502, f"MangaDex detail: {e}")
        return {"title": title, "chapters": chapters[:2500], "synopsis": synopsis, "thumb": thumb, "genres": genres}



    # ---- KeiKomik ----

    if source == "voratoon":
        try:
            sid = id.strip("/")
            async with httpx.AsyncClient(timeout=22.0, follow_redirects=True, headers={
                "User-Agent": BROWSER_HEADERS["User-Agent"],
                "Accept": "application/json",
                "Origin": VORATOON_BASE,
                "Referer": VORATOON_BASE + "/",
            }) as client:
                try:
                    await client.get(VORATOON_WEB + "/")
                except Exception:
                    pass
                rd = await client.get(f"{VORATOON_API}/series/{sid}")
                if rd.status_code == 200:
                    body = rd.json() or {}
                    row = body.get("data") if isinstance(body.get("data"), dict) else body
                    if not isinstance(row, dict):
                        row = {}
                    title = row.get("title") or row.get("name") or sid
                    synopsis = (row.get("synopsis") or row.get("description") or row.get("summary") or "")[:1200]
                    thumb = row.get("coverImage") or row.get("cover") or row.get("thumbnail") or row.get("coverUrl") or ""
                    if isinstance(thumb, dict):
                        thumb = thumb.get("url") or thumb.get("src") or ""
                    genres = []
                    for g in (row.get("genres") or row.get("genre") or [])[:12]:
                        if isinstance(g, dict):
                            genres.append(str(g.get("name") or g.get("title") or ""))
                        elif g:
                            genres.append(str(g))
                    chapters = []
                    rc = await client.get(f"{VORATOON_API}/series/{sid}/chapters")
                    if rc.status_code == 200:
                        cbody = rc.json() or {}
                        crow = cbody.get("data") or cbody.get("chapters") or cbody.get("results") or []
                        if isinstance(crow, dict):
                            crow = crow.get("data") or crow.get("items") or []
                        for ch in crow:
                            if not isinstance(ch, dict):
                                continue
                            cdata = ch.get("data") if isinstance(ch.get("data"), dict) else ch
                            num = cdata.get("index")
                            if num is None:
                                num = cdata.get("number")
                            if num is None:
                                num = cdata.get("chapter")
                            try:
                                if float(num) == 0:
                                    continue
                            except Exception:
                                pass
                            cid = cdata.get("id") or cdata.get("_id") or num
                            ctitle = cdata.get("title") or ""
                            label = f"Chapter {num}" if num is not None else str(cid)
                            if ctitle and str(ctitle) not in label:
                                label = f"{label} — {ctitle}"
                            try:
                                nval = float(num)
                            except Exception:
                                nval = 0
                            chapters.append({
                                "title": _clean_chapter_title(label),
                                "id": f"{sid}/chapter/{num if num is not None else cid}",
                                "_n": nval,
                            })
                    chapters.sort(key=lambda c: c.get("_n", 0), reverse=True)
                    for c in chapters:
                        c.pop("_n", None)
                    if chapters:
                        return {
                            "title": str(title)[:120],
                            "synopsis": synopsis,
                            "thumb": str(thumb) if thumb else "",
                            "chapters": chapters[:2500],
                            "genres": [g for g in genres if g][:12],
                            "source": "voratoon",
                        }
            # HTML fallback bila API 403
            if HAS_BS4:
                async with httpx.AsyncClient(timeout=22.0, follow_redirects=True, headers={**BROWSER_HEADERS, "Referer": VORATOON_WEB + "/"}) as client:
                    r = await client.get(f"{VORATOON_WEB}/series/{sid}")
                    if r.status_code != 200:
                        raise HTTPException(404, "Voratoon tidak ditemukan")
                    soup = BeautifulSoup(r.text, "html.parser")
                    h1 = soup.find("h1")
                    title = h1.get_text(strip=True) if h1 else sid.replace("-", " ").title()
                    synopsis = ""
                    p = soup.select_one("p")
                    if p:
                        synopsis = p.get_text(" ", strip=True)[:800]
                    covers = _voratoon_parse_covers(r.text)
                    thumb = covers.get(sid) or (next(iter(covers.values())) if covers else "")
                    chapters = []
                    seen = set()
                    for a in soup.find_all("a", href=True):
                        mm = re.search(re.escape(sid) + r"/chapter/([\d.]+)", a["href"])
                        if not mm:
                            continue
                        num = mm.group(1)
                        try:
                            if float(num) == 0:
                                continue
                        except Exception:
                            pass
                        if num in seen:
                            continue
                        seen.add(num)
                        try:
                            n = float(num)
                        except Exception:
                            n = 0
                        chapters.append({"id": f"{sid}/chapter/{num}", "title": f"Chapter {num}", "num": n})
                    chapters.sort(key=lambda c: c.get("num", 0), reverse=True)
                    for c in chapters:
                        c.pop("num", None)
                    return {
                        "title": title,
                        "synopsis": synopsis,
                        "thumb": thumb,
                        "chapters": chapters[:2500],
                        "genres": [],
                        "source": "voratoon",
                    }
            raise HTTPException(404, "Voratoon tidak ditemukan")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Voratoon detail: {type(e).__name__}")





    if source == "lepoytl" and HAS_BS4:
        try:
            rid = id.strip("/")
            label = urllib.parse.unquote(rid[6:] if rid.startswith("label/") else rid)
            async with httpx.AsyncClient(timeout=28.0, follow_redirects=True, headers={**BROWSER_HEADERS, "Referer": LEPOYTL_BASE + "/"}) as client:
                title = label
                synopsis = ""
                thumb = ""
                chapters = []
                seen = set()
                # Category feed = daftar chapter lengkap
                feed = f"{LEPOYTL_BASE}/feeds/posts/default/-/{urllib.parse.quote(label)}?alt=json&max-results=150"
                r = await client.get(feed)
                entries = []
                if r.status_code == 200:
                    try:
                        entries = ((r.json() or {}).get("feed") or {}).get("entry") or []
                    except Exception:
                        entries = []
                if len(entries) < 2:
                    r2 = await client.get(
                        f"{LEPOYTL_BASE}/feeds/posts/default?alt=json&max-results=150&q={urllib.parse.quote(label)}"
                    )
                    if r2.status_code == 200:
                        try:
                            entries = ((r2.json() or {}).get("feed") or {}).get("entry") or []
                        except Exception:
                            pass
                for e in entries:
                    t = e.get("title")
                    text = (t.get("$t") if isinstance(t, dict) else t) or ""
                    text = str(text).strip()
                    # skip pure series index post without chapter num if no chapter in title
                    mnum = re.search(r"chapter\s*([\d.]+)", text, re.I)
                    if not mnum:
                        continue
                    num = mnum.group(1)
                    link = ""
                    for l in e.get("link") or []:
                        if l.get("rel") == "alternate":
                            link = l.get("href") or ""
                            break
                    if not link:
                        continue
                    path_part = link.split("lepoytl.my.id/")[-1].replace(".html", "").strip("/")
                    if not path_part or path_part in seen:
                        continue
                    seen.add(path_part)
                    try:
                        n = float(num)
                    except Exception:
                        n = 0.0
                    chapters.append({
                        "id": "ch/" + path_part,
                        "title": f"Chapter {num}",
                        "num": n,
                    })
                    if not thumb:
                        media = e.get("media$thumbnail") or {}
                        if isinstance(media, dict):
                            thumb = media.get("url") or ""
                chapters.sort(key=lambda c: c.get("num", 0), reverse=True)
                for c in chapters:
                    c.pop("num", None)
                return {
                    "title": str(title)[:120],
                    "synopsis": synopsis,
                    "thumb": thumb,
                    "chapters": chapters[:500],
                    "genres": [],
                    "source": "lepoytl",
                }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"LepoyTL detail: {type(e).__name__}")


    if source == "lumoskomik" and HAS_BS4:
        try:
            slug = id.strip("/")
            async with httpx.AsyncClient(timeout=25.0, follow_redirects=True, headers={**BROWSER_HEADERS, "Referer": LUMOSKOMIK_BASE + "/"}) as client:
                r = await client.get(f"{LUMOSKOMIK_BASE}/comic/{slug}")
                if r.status_code != 200:
                    raise HTTPException(404, "LumosKomik tidak ditemukan")
                soup = BeautifulSoup(r.text, "html.parser")
                h1 = soup.find("h1")
                title = h1.get_text(" ", strip=True) if h1 else slug.replace("-", " ").title()
                synopsis = ""
                for p in soup.select("p"):
                    t = p.get_text(" ", strip=True)
                    if len(t) > 60:
                        synopsis = t[:1200]
                        break
                thumb = ""
                img = soup.select_one("img[src*='thumbnail'], img[src*='api/image'], article img, img")
                if img is not None:
                    thumb = (img.get("src") or img.get("data-src") or "").strip()
                    if thumb.startswith("/"):
                        thumb = LUMOSKOMIK_BASE + thumb
                    elif thumb.startswith("//"):
                        thumb = "https:" + thumb
                genres = []
                for a in soup.find_all("a", href=True):
                    if "/genre" in a["href"] or "/tag" in a["href"]:
                        g = a.get_text(strip=True)
                        if g and g not in genres:
                            genres.append(g)
                chapters = []
                seen = set()
                for a in soup.find_all("a", href=True):
                    href = a.get("href") or ""
                    if "/read/" not in href or "chapter" not in href.lower():
                        continue
                    m = re.search(rf"/read/{re.escape(slug)}/chapter-([\d.]+)", href, re.I)
                    if not m:
                        m = re.search(r"/chapter-([\d.]+)", href, re.I)
                        if not m:
                            continue
                    num = m.group(1)
                    ch_id = f"{slug}/chapter-{num}"
                    if ch_id in seen:
                        continue
                    seen.add(ch_id)
                    try:
                        n = float(num)
                    except Exception:
                        n = 0.0
                    chapters.append({"id": ch_id, "title": f"Chapter {num}", "num": n})
                chapters.sort(key=lambda c: c["num"], reverse=True)
                for c in chapters:
                    c.pop("num", None)
                return {
                    "title": title[:120],
                    "synopsis": synopsis,
                    "thumb": thumb,
                    "genres": genres[:12],
                    "chapters": chapters[:2500],
                    "source": "lumoskomik",
                }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"LumosKomik detail: {type(e).__name__}")



    if source == "bacakomik" and HAS_BS4:
        try:
            slug = id.strip("/")
            # id bisa 653900-lookism, chapter URL: lookism-chapter-N
            slug_base = re.sub(r"^\d+-", "", slug)
            async with httpx.AsyncClient(timeout=22.0, follow_redirects=True, headers={**BROWSER_HEADERS, "Referer": BACAKOMIK_BASE + "/"}) as client:
                r = await client.get(f"{BACAKOMIK_BASE}/komik/{slug}/")
                if r.status_code != 200:
                    r = await client.get(f"{BACAKOMIK_BASE}/komik/{slug_base}/")
                if r.status_code != 200:
                    raise HTTPException(404, "BacaKomik tidak ditemukan")
                soup = BeautifulSoup(r.text, "html.parser")
                h1 = soup.select_one("h1, .entry-title")
                title = h1.get_text(" ", strip=True) if h1 else slug_base.replace("-", " ").title()
                title = re.sub(r"^Komik\s+", "", title, flags=re.I).strip()
                syn_el = soup.select_one(".entry-content p, .desc, .sinopsis, [itemprop='description']")
                synopsis = syn_el.get_text(" ", strip=True)[:800] if syn_el else ""
                img = soup.select_one(".thumb img, .series-thumb img, img.wp-post-image")
                thumb = (img.get("data-src") or img.get("src") or "").strip() if img else ""
                genres = [g.get_text(strip=True) for g in soup.select(".genre-info a, .mgen a, a[rel='tag']") if g.get_text(strip=True)][:12]
                chapters = []
                seen = set()
                anchors = list(soup.select("#chapter_list a, #chapterlist a, .eplister a, li .lchx a"))
                for el in soup.select("span.chapternum, li .lchx"):
                    a = el if getattr(el, "name", "") == "a" else el.find_parent("a")
                    if a and a.get("href"):
                        anchors.append(a)
                if len(anchors) < 3:
                    anchors = [a for a in soup.find_all("a", href=True) if "chapter" in (a.get("href") or "").lower()]
                for a in anchors:
                    href = a.get("href") or ""
                    if "chapter" not in href.lower():
                        continue
                    m = re.search(r"([a-z0-9-]+)-chapter-([\d.]+)", href, re.I)
                    if not m:
                        continue
                    series_key, num = m.group(1), m.group(2)
                    ch_id = f"{series_key}-chapter-{num}"
                    if ch_id in seen:
                        continue
                    seen.add(ch_id)
                    try:
                        n = float(num)
                    except Exception:
                        n = 0.0
                    chapters.append({"id": ch_id, "title": f"Chapter {num}", "num": n, "series": series_key})
                if chapters:
                    from collections import Counter
                    main_series = Counter(c["series"] for c in chapters).most_common(1)[0][0]
                    chapters = [c for c in chapters if c["series"] == main_series]
                chapters.sort(key=lambda c: c["num"], reverse=True)
                for c in chapters:
                    c.pop("num", None)
                    c.pop("series", None)
                return {
                    "title": title,
                    "synopsis": synopsis,
                    "genres": genres,
                    "thumb": thumb,
                    "chapters": chapters[:2500],
                    "source": "bacakomik",
                }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"BacaKomik detail: {type(e).__name__}")

    if source == "luvyaa" and HAS_BS4:
        try:
            slug = id.strip("/").split("/")[-1]
            async with httpx.AsyncClient(timeout=24.0, follow_redirects=True, headers={**BROWSER_HEADERS, "Referer": LUVYAA_BASE + "/"}) as client:
                r = None
                for base in LUVYAA_MIRRORS:
                    for path in (f"{base}/manga/{slug}/", f"{base}/{slug}/"):
                        try:
                            rr = await client.get(path)
                            if rr.status_code == 200 and len(rr.text) > 3000 and "Just a moment" not in rr.text:
                                r = rr
                                break
                        except Exception:
                            continue
                    if r is not None:
                        break
                if r is None:
                    raise HTTPException(404, "Luvyaa: komik tidak ditemukan")
                soup = BeautifulSoup(r.text, "html.parser")
                h1 = soup.select_one("h1.entry-title, h1")
                title = h1.get_text(" ", strip=True) if h1 else slug.replace("-", " ").title()
                title = re.sub(r"(?i)^(komik|manga)\s+", "", title).strip()
                syn_el = soup.select_one(".entry-content p, .desc, .sinopsis, [itemprop='description'], .wd-full p")
                synopsis = syn_el.get_text(" ", strip=True)[:900] if syn_el else ""
                img = soup.select_one(".thumb img, .seriestuimg img, img.wp-post-image, .ime img")
                thumb = ""
                if img is not None:
                    thumb = (img.get("data-src") or img.get("src") or "").strip()
                genres = [g.get_text(strip=True) for g in soup.select(".mgen a, .genre-info a, a[rel='tag'], .seriestugenre a") if g.get_text(strip=True)][:12]
                chapters = []
                seen = set()
                for a in soup.find_all("a", href=True):
                    href = (a.get("href") or "").strip()
                    low = href.lower()
                    txt = (a.get_text() or "").lower()
                    if "chapter" not in low and not re.search(r"-chapter-\d", low):
                        if not re.search(r"(?:chapter|ch\.?|bab)\s*\d", txt):
                            continue
                    m = re.search(r"([a-z0-9\-]+)-chapter-([\d.]+)", href, re.I)
                    if m:
                        ch_id = f"{m.group(1)}-chapter-{m.group(2)}"
                        num = m.group(2)
                    else:
                        path = href.split("?")[0].rstrip("/").split("/")[-1]
                        if not path or path in seen:
                            continue
                        ch_id = path
                        nm = re.search(r"([\d.]+)$", path)
                        num = nm.group(1) if nm else "0"
                    if ch_id in seen:
                        continue
                    seen.add(ch_id)
                    try:
                        n = float(num)
                    except Exception:
                        n = 0.0
                    chapters.append({"id": ch_id, "title": f"Chapter {num}", "num": n})
                chapters.sort(key=lambda c: c["num"], reverse=True)
                for c in chapters:
                    c.pop("num", None)
                return {
                    "title": title,
                    "synopsis": synopsis,
                    "genres": genres,
                    "thumb": thumb,
                    "chapters": chapters[:2500],
                    "source": "luvyaa",
                }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Luvyaa detail: {type(e).__name__}")





    if source == "mangasusu" and HAS_BS4:
        try:
            slug = id.strip("/")
            async with httpx.AsyncClient(timeout=28.0, follow_redirects=True, headers={**BROWSER_HEADERS, "Referer": MANGASUSU_BASE + "/"}) as client:
                r = await client.get(f"{MANGASUSU_BASE}/komik/{slug}/")
                if r.status_code != 200:
                    raise HTTPException(404, "MangaSusu tidak ditemukan")
                soup = BeautifulSoup(r.text, "html.parser")
                h1 = soup.select_one("h1, .entry-title")
                title = h1.get_text(" ", strip=True) if h1 else slug.replace("-", " ").title()
                syn = soup.select_one(".entry-content[itemprop='description'], .wd-full .entry-content, .summary__content, .desc")
                synopsis = syn.get_text(" ", strip=True)[:1200] if syn else ""
                img = soup.select_one(".thumb img, .seriestuimg img, img.wp-post-image")
                thumb = (img.get("data-src") or img.get("src") or "").strip() if img else ""
                genres = [g.get_text(strip=True) for g in soup.select(".mgen a, a[rel='tag']") if g.get_text(strip=True)][:12]
                chapters = []
                seen = set()
                anchors = list(soup.select("#chapterlist a, .eplister a, li .eph-num a"))
                for el in soup.select("span.chapternum"):
                    a = el.find_parent("a")
                    if a and a.get("href"):
                        anchors.append(a)
                if len(anchors) < 2:
                    anchors = [a for a in soup.find_all("a", href=True) if "chapter" in (a.get("href") or "").lower()]
                for a in anchors:
                    href = (a.get("href") or "").strip()
                    if not href or "chapter" not in href.lower():
                        continue
                    full = href if href.startswith("http") else (MANGASUSU_BASE + href if href.startswith("/") else "")
                    if not full:
                        continue
                    ch_id = full.rstrip("/").split("/")[-1]
                    if not ch_id or ch_id in seen:
                        continue
                    seen.add(ch_id)
                    text = a.get_text(" ", strip=True)
                    text = re.sub(r"\s*\d+\s*(bulan|minggu|hari|jam|menit).*$", "", text, flags=re.I).strip()
                    m = re.search(r"(\d+(?:\.\d+)?)", ch_id)
                    if not m:
                        m = re.search(r"(?:chapter|ch\.?)\s*(\d+(?:\.\d+)?)", text, re.I)
                    num = float(m.group(1)) if m else 0.0
                    label = text if text and len(text) > 2 else (f"Chapter {m.group(1)}" if m else ch_id)
                    chapters.append({"id": ch_id, "title": label[:80], "num": num})
                chapters.sort(key=lambda c: c.get("num", 0), reverse=True)
                for c in chapters:
                    c.pop("num", None)
                return {
                    "title": title[:120],
                    "synopsis": synopsis,
                    "thumb": thumb,
                    "genres": genres,
                    "chapters": chapters[:2500],
                    "source": "mangasusu",
                }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"MangaSusu detail: {type(e).__name__}")


    if source == "komiktap" and HAS_BS4:
        try:
            slug = id.strip("/")
            async with httpx.AsyncClient(timeout=28.0, follow_redirects=True, headers={**BROWSER_HEADERS, "Referer": KOMIKTAP_BASE + "/"}) as client:
                r = await client.get(f"{KOMIKTAP_BASE}/manga/{slug}/")
                if r.status_code != 200:
                    raise HTTPException(404, "KomikTap tidak ditemukan")
                soup = BeautifulSoup(r.text, "html.parser")
                h1 = soup.select_one("h1, .entry-title")
                title = h1.get_text(" ", strip=True) if h1 else slug.replace("-", " ").title()
                syn = soup.select_one(".entry-content[itemprop='description'], .wd-full .entry-content, .summary__content, .desc")
                synopsis = syn.get_text(" ", strip=True)[:1200] if syn else ""
                img = soup.select_one(".thumb img, .seriestuimg img, img.wp-post-image")
                thumb = (img.get("data-src") or img.get("src") or "").strip() if img else ""
                genres = [g.get_text(strip=True) for g in soup.select(".mgen a, a[rel='tag']") if g.get_text(strip=True)][:12]
                chapters = []
                seen = set()
                anchors = list(soup.select("#chapterlist a, .eplister a, li .eph-num a"))
                for el in soup.select("span.chapternum"):
                    a = el.find_parent("a")
                    if a and a.get("href"):
                        anchors.append(a)
                if len(anchors) < 2:
                    anchors = [a for a in soup.find_all("a", href=True) if "chapter" in (a.get("href") or "").lower()]
                for a in anchors:
                    href = (a.get("href") or "").strip()
                    if not href or "chapter" not in href.lower():
                        continue
                    full = href if href.startswith("http") else (KOMIKTAP_BASE + href if href.startswith("/") else "")
                    if not full:
                        continue
                    ch_id = full.rstrip("/").split("/")[-1]
                    if not ch_id or ch_id in seen:
                        continue
                    seen.add(ch_id)
                    text = a.get_text(" ", strip=True)
                    text = re.sub(r"\s*\d+\s*(bulan|minggu|hari|jam|menit).*$", "", text, flags=re.I).strip()
                    m = re.search(r"(\d+(?:\.\d+)?)", ch_id)
                    if not m:
                        m = re.search(r"(?:chapter|ch\.?)\s*(\d+(?:\.\d+)?)", text, re.I)
                    num = float(m.group(1)) if m else 0.0
                    label = text if text and len(text) > 2 else (f"Chapter {m.group(1)}" if m else ch_id)
                    chapters.append({"id": ch_id, "title": label[:80], "num": num})
                chapters.sort(key=lambda c: c.get("num", 0), reverse=True)
                for c in chapters:
                    c.pop("num", None)
                return {
                    "title": title[:120],
                    "synopsis": synopsis,
                    "thumb": thumb,
                    "genres": genres,
                    "chapters": chapters[:2500],
                    "source": "komiktap",
                }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"KomikTap detail: {type(e).__name__}")


    if source == "klikmanga" and HAS_BS4:
        try:
            slug = id.strip("/")
            async with httpx.AsyncClient(timeout=28.0, follow_redirects=True, headers={**BROWSER_HEADERS, "Referer": KLIKMANGA_BASE + "/"}) as client:
                r = await client.get(f"{KLIKMANGA_BASE}/manga/{slug}/")
                if r.status_code != 200:
                    raise HTTPException(404, "Komik tidak ditemukan di KlikManga")
                soup = BeautifulSoup(r.text, "html.parser")
                h1 = soup.select_one("h1, .entry-title, .post-title")
                title = h1.get_text(strip=True) if h1 else slug.replace("-", " ").title()
                syn_el = soup.select_one(".description-summary, .summary__content, .entry-content, .dsct")
                synopsis = syn_el.get_text(" ", strip=True)[:800] if syn_el else ""
                genres = [g.get_text(strip=True) for g in soup.select(".genres-content a, .mgen a, a[rel='tag']") if g.get_text(strip=True)][:12]
                thumb = ""
                img = soup.select_one(".summary_image img, .thumb img, .manga-poster img")
                if img is not None:
                    thumb = (img.get("data-src") or img.get("src") or "").strip()
                chapters = []
                seen_ch = set()
                def _add_km_ch(href, text=""):
                    if not href:
                        return
                    if f"/manga/{slug}/" not in href and slug not in href:
                        return
                    if "chapter" not in href.lower():
                        return
                    ch_slug = href.rstrip("/").split("/")[-1]
                    if not ch_slug or ch_slug in seen_ch:
                        return
                    seen_ch.add(ch_slug)
                    m = re.search(r"(\d+(?:\.\d+)?)", ch_slug)
                    num = float(m.group(1)) if m else 0.0
                    label = text.strip() if text and len(text.strip()) > 1 else ch_slug.replace("-", " ").title()
                    # bersihkan "3 bulan lalu" dll
                    label = re.sub(r"\s*\d+\s*(bulan|minggu|hari|jam|menit).*$", "", label, flags=re.I).strip() or f"Chapter {m.group(1) if m else ch_slug}"
                    chapters.append({"id": f"{slug}/{ch_slug}", "title": label[:80], "num": num})

                # Madara ajax full list
                try:
                    ra = await client.post(
                        f"{KLIKMANGA_BASE}/manga/{slug}/ajax/chapters/",
                        headers={**BROWSER_HEADERS, "Referer": f"{KLIKMANGA_BASE}/manga/{slug}/", "X-Requested-With": "XMLHttpRequest"},
                        data={},
                    )
                    if ra.status_code == 200 and len(ra.text) > 100:
                        soup_ch = BeautifulSoup(ra.text, "html.parser")
                        for a in soup_ch.select("a[href]"):
                            _add_km_ch(a.get("href") or "", a.get_text(" ", strip=True))
                except Exception:
                    pass
                # static fallback
                if len(chapters) < 2:
                    for a in soup.select("a[href*='chapter']"):
                        _add_km_ch(a.get("href") or "", a.get_text(" ", strip=True))
                chapters.sort(key=lambda c: c.get("num", 0), reverse=True)
                for c in chapters:
                    c.pop("num", None)
                return {
                    "title": title,
                    "synopsis": synopsis,
                    "genres": genres,
                    "thumb": thumb,
                    "chapters": chapters[:2500],
                    "source": "klikmanga",
                }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"KlikManga detail gagal: {type(e).__name__}")


    # ---- HTML sources: komikindo, voratoon, wurmz ----

    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True, headers=BROWSER_HEADERS) as client:
            if source == "komikindo":
                url = f"{KOMIKINDO_BASE}/komik/{id}/"
            elif source == "voratoon":
                try:
                    async with httpx.AsyncClient(timeout=22.0, headers={
                        "User-Agent": BROWSER_HEADERS["User-Agent"],
                        "Accept": "application/json",
                        "Origin": VORATOON_BASE,
                        "Referer": VORATOON_BASE + "/",
                    }) as apiclient:
                        rd = await apiclient.get(f"{VORATOON_API}/series/{id}")
                        if rd.status_code == 200:
                            row = (rd.json() or {}).get("data") or {}
                            inner = row.get("data") if isinstance(row, dict) else {}
                            if isinstance(inner, dict):
                                title = inner.get("title") or title
                                synopsis = inner.get("synopsis") or ""
                                thumb = inner.get("coverImage") or ""
                        rc = await apiclient.get(f"{VORATOON_API}/series/{id}/chapters")
                        if rc.status_code == 200:
                            rows = (rc.json() or {}).get("data") or []
                            for ch in rows:
                                cdata = ch.get("data") if isinstance(ch.get("data"), dict) else {}
                                num = cdata.get("index") or cdata.get("number") or cdata.get("chapter") or ch.get("id")
                                ctitle = (cdata.get("title") or "") if isinstance(cdata, dict) else ""
                                ch_id = f"{id}/chapter/{num}"
                                chapters.append({
                                    "title": _clean_chapter_title(f"Chapter {num} {ctitle}"),
                                    "id": ch_id,
                                })
                    if chapters:
                        return {"title": title, "chapters": chapters[:2500], "synopsis": synopsis, "thumb": thumb, "genres": genres}
                except Exception:
                    chapters = []
                url = f"{VORATOON_BASE}/series/{id}"
            elif source == "wurmz":
                url = f"{WURMZ_BASE}/detail/{id}"
            elif source == "klikmanga":
                # id format: "series-slug/chapter-xxx"
                parts = id.strip("/").split("/")
                if len(parts) >= 2:
                    url = f"{KLIKMANGA_BASE}/manga/{parts[0]}/{parts[-1]}/"
                else:
                    url = f"{KLIKMANGA_BASE}/manga/{id.strip('/')}/"
            elif source == "bacakomik":
                url = f"{BACAKOMIK_BASE}/komik/{id.strip('/')}/"
            elif source == "luvyaa":
                url = f"{LUVYAA_BASE}/{id.strip('/')}/"
            else:
                url = f"{KOMIKU_WEB}/manga/{id}/"

            r = await client.get(url)
            if r.status_code != 200:
                raise HTTPException(404, f"Komik tidak ditemukan ({r.status_code})")
            if not HAS_BS4:
                raise HTTPException(500, "BeautifulSoup tidak tersedia")
            soup = BeautifulSoup(r.text, "html.parser")
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(" ", strip=True).replace("Komik ", "", 1)
            img = soup.select_one(".ims img, .thumb img, .info-image img, img[itemprop='image']")
            if img:
                thumb = img.get("data-src") or img.get("src") or ""
            desc = soup.select_one("#Sinopsis, .desc, .entry-content, .summary__content, [itemprop='description']")
            if desc:
                synopsis = desc.get_text(" ", strip=True)[:1200]
            seen_ch = set()
            series_key = (id or "").split("/")[-1].lower()
            for a in soup.find_all("a", href=True):
                href = (a.get("href") or "").strip()
                text = a.get_text(" ", strip=True)
                if not href:
                    continue
                low = href.lower()
                is_ch = (
                    "/chapter/" in low
                    or bool(re.search(r"chapter[-_/]?\d", low))
                    or bool(re.search(r"-chapter-\d", low))
                    or bool(re.search(r"(?:chapter|ch\.?|bab)\s*\d", (text or "").lower()))
                )
                if not is_ch:
                    continue
                if source == "voratoon" and "/chapter/" in low:
                    if series_key and series_key not in low:
                        continue
                    parts = href.split("/series/")[-1].strip("/")
                    if "/chapter/" not in parts:
                        continue
                    ch_id = parts
                elif source == "wurmz" and "/detail/" in low and "/chapter/" in low:
                    if series_key and series_key not in low:
                        continue
                    ch_id = href.split("/detail/")[-1].strip("/")
                elif source == "komiku":
                    ch_id = _slug_from_href(href)
                    if "chapter" not in ch_id.lower() and not re.search(r"ch-?\d", ch_id.lower()):
                        continue
                else:
                    ch_id = _slug_from_href(href)
                title_ch = _clean_chapter_title(text) if text else (ch_id[:40] if ch_id else "Chapter")
                if not ch_id or ch_id == id or ch_id in seen_ch or len(ch_id) < 3:
                    continue
                seen_ch.add(ch_id)
                chapters.append({"title": title_ch, "id": ch_id})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Detail error: {e}")
    return {"title": title, "chapters": chapters[:2500], "synopsis": synopsis, "thumb": thumb, "genres": genres}


@app.get("/comic/chapter")
async def comic_chapter(request: Request, source: str = "komiku", id: str = ""):
    if not id:
        raise HTTPException(400, "id kosong")
    role = detect_role(request)
    if source in ADMIN_SOURCES and role != "admin":
        raise HTTPException(403, "Source Admin — khusus Admin.")
    if source in PREMIUM_SOURCES and role not in ("premium", "admin"):
        raise HTTPException(403, "Source Premium, Harap Gunakan Premium Untuk Membuka.")
    images = []

    if source == "mangadex":
        try:
            async with httpx.AsyncClient(timeout=35.0, follow_redirects=True, headers={
                "User-Agent": BROWSER_HEADERS["User-Agent"],
                "Referer": "https://mangadex.org/",
            }) as client:
                r = await client.get(f"{MD_API}/at-home/server/{id}")
                if r.status_code != 200:
                    r = await client.get(f"{MD_API}/at-home/server/{id}")
                if r.status_code != 200:
                    raise HTTPException(502, f"MangaDex at-home {r.status_code}")
                data = r.json() or {}
                base = (data.get("baseUrl") or "").rstrip("/")
                ch = data.get("chapter") or {}
                hash_ = ch.get("hash") or ""
                full = ch.get("data") or []
                saver = ch.get("dataSaver") or []
                if full and hash_ and base:
                    images = [f"{base}/data/{hash_}/{p}" for p in full]
                elif saver and hash_ and base:
                    images = [f"{base}/data-saver/{hash_}/{p}" for p in saver]
                images = [u for u in images if u.startswith("http")]
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"MangaDex chapter: {e}")
        if not images:
            raise HTTPException(404, "Halaman MangaDex kosong. Coba chapter/bahasa lain.")
        return {"images": images, "count": len(images)}

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers={**BROWSER_HEADERS, "Referer": KEIKOMIK_BASE + "/"}) as client:
            if source == "keikomik":
                if id.startswith("http"):
                    url = id
                elif id.startswith("komik/"):
                    url = f"{KEIKOMIK_BASE}/{id.strip('/')}/"
                else:
                    url = f"{KEIKOMIK_BASE}/{id.strip('/')}/"
            elif source == "komikindo":
                if id.startswith("http"):
                    url = id
                elif "chapter" in id:
                    url = id if id.startswith("http") else f"{KOMIKINDO_BASE}/{id.strip('/')}/"
                else:
                    url = f"{KOMIKINDO_BASE}/{id.strip('/')}/"
            elif source == "voratoon":
                if id.startswith("http"):
                    url = id
                elif id.startswith("series/"):
                    url = f"{VORATOON_WEB}/{id.strip('/')}"
                elif "/chapter/" in id:
                    url = f"{VORATOON_WEB}/series/{id.strip('/')}"
                else:
                    url = f"{VORATOON_WEB}/series/{id.strip('/')}"
            elif source == "wurmz":
                if id.startswith("http"):
                    url = id
                elif id.startswith("detail/"):
                    url = f"{WURMZ_BASE}/{id}"
                else:
                    url = f"{WURMZ_BASE}/detail/{id}"
            elif source == "komiku":
                url = id if id.startswith("http") else f"{KOMIKU_WEB}/{id}/"
            elif source == "klikmanga":
                # id: series-slug/chapter-xxx
                if id.startswith("http"):
                    url = id
                else:
                    parts = id.strip("/").split("/")
                    if len(parts) >= 2:
                        url = f"{KLIKMANGA_BASE}/manga/{parts[0]}/{parts[-1]}/"
                    else:
                        url = f"{KLIKMANGA_BASE}/manga/{id.strip('/')}/"
            elif source == "lumoskomik":
                if id.startswith("http"):
                    url = id
                elif "/chapter-" in id or id.startswith("read/"):
                    path = id if id.startswith("read/") else f"read/{id.strip('/')}"
                    url = f"{LUMOSKOMIK_BASE}/{path.strip('/')}"
                else:
                    url = f"{LUMOSKOMIK_BASE}/read/{id.strip('/')}"

            elif source == "lepoytl":
                if id.startswith("http"):
                    url = id
                elif id.startswith("ch/"):
                    rest = id[3:].strip("/")
                    if re.match(r"20\d{2}/\d{2}/", rest):
                        url = f"{LEPOYTL_BASE}/{rest}.html"
                    else:
                        url = f"{LEPOYTL_BASE}/search?q={urllib.parse.quote(rest.replace('-', ' '))}"
                else:
                    url = f"{LEPOYTL_BASE}/search?q={urllib.parse.quote(id)}"
            elif source == "bacakomik":
                # id: slug-chapter-N
                if id.startswith("http"):
                    url = id
                else:
                    url = f"{BACAKOMIK_BASE}/{id.strip('/')}/"
            elif source == "luvyaa":
                if id.startswith("http"):
                    url = id
                else:
                    url = f"{LUVYAA_BASE}/{id.strip('/')}/"

            elif source == "mangasusu":
                if id.startswith("http"):
                    url = id
                else:
                    url = f"{MANGASUSU_BASE}/{id.strip('/')}/"
            elif source == "komiktap":
                if id.startswith("http"):
                    url = id
                elif "-chapter-" in id:
                    url = f"{KOMIKTAP_BASE}/{id.strip('/')}/"
                elif "/chapter-" in id:
                    # slug/chapter-N -> slug-chapter-N style or manga path
                    parts = id.strip("/").split("/")
                    if len(parts) >= 2:
                        url = f"{KOMIKTAP_BASE}/{parts[0]}-{parts[-1]}/"
                    else:
                        url = f"{KOMIKTAP_BASE}/{id.strip('/')}/"
                else:
                    url = f"{KOMIKTAP_BASE}/{id.strip('/')}/"
            else:
                url = id if id.startswith("http") else f"{KOMIKU_WEB}/{id}/"

            # Referer sesuai source
            if source == "lumoskomik":
                ref = LUMOSKOMIK_BASE + "/"
            elif source == "lepoytl":
                ref = LEPOYTL_BASE + "/"
            elif source == "bacakomik":
                ref = BACAKOMIK_BASE + "/"
            elif source == "luvyaa":
                ref = LUVYAA_BASE + "/"
            elif source == "mangasusu":
                ref = MANGASUSU_BASE + "/"
            elif source == "komiktap":
                ref = KOMIKTAP_BASE + "/"
            elif source == "komikindo":
                ref = KOMIKINDO_BASE + "/"
            elif source == "keikomik":
                ref = KEIKOMIK_BASE + "/"
            elif source == "klikmanga":
                ref = KLIKMANGA_BASE + "/"
            elif source == "voratoon":
                ref = VORATOON_WEB + "/"
            elif source == "wurmz":
                ref = WURMZ_BASE + "/"
            else:
                ref = KOMIKU_WEB + "/"

            r = await client.get(url, headers={**BROWSER_HEADERS, "Referer": ref})
            if r.status_code != 200:
                raise HTTPException(404, f"Chapter tidak ditemukan ({r.status_code})")
            if not HAS_BS4:
                raise HTTPException(500, "BeautifulSoup tidak tersedia")
            soup = BeautifulSoup(r.text, "html.parser")

            for img in soup.find_all("img"):
                if source == "voratoon":
                    break
                src_img = (img.get("data-src") or img.get("src") or img.get("data-lazy-src") or "").strip()
                if src_img.startswith("//"):
                    src_img = "https:" + src_img
                if not src_img.startswith("http"):
                    continue
                low = src_img.lower()
                # Jangan pakai "ads" mentah — ikut nyangkut di path "uploads/"
                if any(x in low for x in ["logo", "avatar", "icon", "emoji", "favicon", "promosi", "promo.webp", "gravatar", "gstatic", "firebase", "pixel", "advert", "banner", "sponsor", "doubleclick", "click.", "popup", "readerarea.svg", "noimage", "histats", "slot", "judi", "/ads/", "adservice", "adserver"]):
                    continue
                if re.search(r"(?:728x|300x250|160x600|adservice)", low):
                    continue
                if source == "komikindo" and ("blogger.googleusercontent" in low or "blogspot." in low or "wp-content" in low):
                    continue
                if source == "komiku":
                    if not _is_komiku_page_image(src_img, img):
                        continue
                else:
                    if not any(x in low for x in ["upload", "cdn", "imgkomik", "manhwature", "wp-content", "storage", "blogspot", "covers/", "bakacdn", "amatipol", "googleusercontent", ".jpg", ".jpeg", ".png", ".webp", "/img/"]):
                        continue
                if src_img not in images:
                    images.append(src_img)


            # KomikIndo — hanya halaman chapter (/media/...), buang iklan blogger/banner
            if source == "komikindo":
                found = []
                seen_key = set()
                # prioritaskan container reader
                nodes = []
                box = soup.select_one("#chimg-auh, #chimg, .chapter-content, #readerarea, .reader-area")
                if box is not None:
                    nodes = box.find_all("img")
                if len(nodes) < 2:
                    nodes = soup.find_all("img")
                for img in nodes:
                    src_img = (img.get("data-src") or img.get("data-lazy-src") or img.get("src") or "").strip()
                    if src_img.startswith("//"):
                        src_img = "https:" + src_img
                    if not src_img.startswith("http"):
                        continue
                    low = src_img.lower()
                    # blok domain/path iklan
                    if any(b in low for b in (
                        "blogger.googleusercontent.com", "blogspot.com",
                        "wp-content/uploads", "i0.wp.com", "i1.wp.com", "i2.wp.com",
                        "logo", "favicon", "avatar", "emoji", "histats", "banner",
                        "/ads/", "advert", "promo", "sponsor", "slot", "judi", "gacor",
                        "728x", "300x250", "160x600", "pixel",
                    )):
                        continue
                    # halaman asli komikindo biasanya /media/.../chapter-
                    if "/media/" not in low and "komikcdn" not in low and "cdn." not in low:
                        continue
                    # dedupe mirror (nama file sama di host beda)
                    key = src_img.rstrip("/").split("/")[-1].split("?")[0]
                    if not key or key in seen_key:
                        continue
                    seen_key.add(key)
                    if src_img not in found:
                        found.append(src_img)
                if found:
                    images = found


            # KlikManga / Madara preloaded
            if source == "klikmanga" and len(images) < 3:
                idxp = r.text.find("chapter_preloaded_images")
                if idxp >= 0:
                    start = r.text.find("[", idxp)
                    if start > 0:
                        depth = 0
                        end = None
                        for i, c in enumerate(r.text[start:start + 250000]):
                            if c == "[":
                                depth += 1
                            elif c == "]":
                                depth -= 1
                                if depth == 0:
                                    end = start + i + 1
                                    break
                        if end:
                            raw = r.text[start:end].replace("\\/", "/")
                            try:
                                arr = json.loads(raw)
                            except Exception:
                                arr = re.findall(r"https?://[^\"\'\\\s]+", raw)
                            for u in arr:
                                if isinstance(u, str) and u.startswith("http") and not any(b in u.lower() for b in ["logo", "avatar", "banner", "728x"]):
                                    images.append(u)

            



            # LumosKomik CDN
            if source == "lumoskomik":
                found = []
                for img in soup.find_all("img"):
                    src_img = (img.get("data-src") or img.get("src") or "").strip()
                    if src_img.startswith("//"):
                        src_img = "https:" + src_img
                    if src_img.startswith("/"):
                        src_img = LUMOSKOMIK_BASE + src_img
                    if not src_img.startswith("http"):
                        continue
                    low = src_img.lower()
                    if any(b in low for b in ("logo", "icon", "avatar", "emoji", "banner", "/ads/", "thumbnail")):
                        continue
                    if any(x in low for x in ("lms.imgsvr", "imgsvr", "/file/comic/", ".jpg", ".jpeg", ".png", ".webp")):
                        if src_img not in found:
                            found.append(src_img)
                for u in re.findall(r"https?://lms\.imgsvr\S+", r.text):
                    if u not in found and "thumbnail" not in u.lower():
                        found.append(u)
                if found:
                    images = found

            # LepoyTL Blogger images + filter iklan
            if source == "lepoytl":
                if "/search" in str(r.url):
                    soup_s = BeautifulSoup(r.text, "html.parser")
                    needle = id.replace("ch/", "").lower().split("/")[-1]
                    target = None
                    for a in soup_s.find_all("a", href=True):
                        href = a["href"]
                        if needle and needle in href.lower() and href.endswith(".html"):
                            target = href if href.startswith("http") else (LEPOYTL_BASE + href)
                            break
                    if target:
                        r = await client.get(target)
                        soup = BeautifulSoup(r.text, "html.parser")
                found = []
                for img in soup.find_all("img"):
                    src_img = (img.get("data-src") or img.get("src") or img.get("data-lazy-src") or "").strip()
                    if src_img.startswith("//"):
                        src_img = "https:" + src_img
                    if not src_img.startswith("http"):
                        continue
                    low = src_img.lower()
                    if any(b in low for b in (
                        "logo", "icon", "avatar", "emoji", "favicon", "banner", "promo",
                        "adsense", "doubleclick", "statcounter", "histats", "pixel",
                        "button", "badge", "widget", "facebook", "twitter", "paypal",
                        "donate", "sponsor", "1x1", "blank.gif", "loading.gif",
                    )):
                        continue
                    if re.search(r"/s(3[2-9]|[4-9]\d|1[0-5]\d)[-/]", low):
                        continue
                    if any(x in low for x in (
                        "blogger.googleusercontent.com", "bp.blogspot.com",
                        ".jpg", ".jpeg", ".png", ".webp",
                    )):
                        if src_img not in found:
                            found.append(src_img)
                for u in re.findall(r"https?://(?:blogger\.googleusercontent\.com|bp\.blogspot\.com)\S+", r.text):
                    low = u.lower()
                    if any(b in low for b in ("logo", "icon", "banner", "emoji", "1x1", "/s72", "/s160")):
                        continue
                    if u not in found:
                        found.append(u)
                if found:
                    images = found

            # Voratoon — HANYA cdn.voratoon.com (tanpa logo/placeholder/iklan)
            if source == "voratoon":
                found = []
                text_v = r.text.replace(chr(92) + "/", "/")
                for u in re.findall(r"https?://cdn\.voratoon\.com\S+", text_v):
                    u = u.strip().rstrip('",]});')
                    low = u.lower()
                    if any(b in low for b in ("/ads/", "logo", "icon", "banner", "placeholder", "avatar", "histats", "emoji", "promo", "sponsor", ".svg", ".gif", "1x1")):
                        continue
                    if not any(x in low for x in (".jpg", ".jpeg", ".png", ".webp", "/pages/", "/img/", "/chapter/")):
                        continue
                    if u not in found:
                        found.append(u)
                images = found

            # MangaSusu / Themesia: prioritaskan #readerarea
            if source == "mangasusu" and HAS_BS4:
                ra = []
                for img in soup.select("#readerarea img, .reader-area img, #chaptercontent img, .chapter-content img"):
                    src_img = (img.get("data-src") or img.get("src") or img.get("data-lazy-src") or "").strip()
                    if src_img.startswith("//"):
                        src_img = "https:" + src_img
                    if not src_img.startswith("http"):
                        continue
                    low = src_img.lower()
                    if any(x in low for x in ("logo", "avatar", "icon", "banner", "readerarea.svg", "noimage", "gif")):
                        continue
                    if src_img not in ra:
                        ra.append(src_img)
                if len(ra) >= 2:
                    images = ra

            # Themesia ts_reader (KomikTap dll)
            if source in ("komiktap", "klikmanga", "bacakomik", "mangasusu") and len(images) < 3:
                idx = r.text.find("ts_reader.run")
                if idx >= 0:
                    chunk = r.text[idx:idx + 100000]
                    chunk = chunk.replace(chr(92) + "/", "/")
                    for u in re.findall(r'https?://[^"\'\s]+', chunk):
                        low = u.lower()
                        if not any(x in low for x in (".jpg", ".jpeg", ".png", ".webp")):
                            continue
                        if any(b in low for b in ("logo", "avatar", "icon", "banner", "readerarea.svg", "noimage")):
                            continue
                        if u not in images:
                            images.append(u)

            if source == "komiku" and len(images) < 2:
                for img in soup.find_all("img"):
                    src_img = (img.get("data-src") or img.get("src") or "").strip()
                    if "uploads" in src_img and src_img.startswith("http") and src_img not in images:
                        if "promosi" not in src_img.lower():
                            images.append(src_img)
            # fallback regex for keikomik CDN
            if source == "keikomik" and len(images) < 3:
                for m in re.findall(r'https?://[^"\'\s]+(?:imgkomik|manhwature|cdn\.ikiru)[^"\'\s]+\.(?:jpg|jpeg|png|webp)', r.text, re.I):
                    if m not in images:
                        images.append(m)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Chapter error: {e}")
    if not images:
        raise HTTPException(404, "Gambar chapter tidak ditemukan")
    return {"images": images, "count": len(images)}


# ============================================================
# ANIME — Anoboy/Kusonime (gratis) · Samehadaku & Otakudesu (premium)
# Stream = iframe embed saja (jangan fallback ke halaman situs)
# ============================================================
ANOBOY_MIRRORS = [
    "https://anoboy.buzz",
    "https://anoboy.ch",
]
SAMEHADAKU_MIRRORS = [
    "https://samehadaku.li",
    "https://samehadaku.care",
    "https://v2.samehadaku.how",
]
OTAKUDESU_MIRRORS = [
    "https://otakudesu.cam",
    "https://otakudesu.cloud",
    "https://otakudesu.media",
]
KUSONIME_MIRRORS = [
    "https://kusonime.com",
]
PREMIUM_ANIME = {"samehadaku", "otakudesu"}

_SKIP_EP_TEXT = (
    "whatsapp", "telegram", "facebook", "twitter", "discord", "share",
    "all episode", "batch", "download batch", "download only", "privacy",
    "contact", "login", "register", "bookmark", "jadwal", "home",
)


def _anime_role_gate(request: Request, source: str):
    role = detect_role(request)
    src = (source or "").lower().strip()
    if src in PREMIUM_ANIME and role not in ("premium", "admin"):
        raise HTTPException(403, "Source ini Premium. Aktifkan Secret Code dulu.")
    return role


async def _fetch_first_ok(client, urls, min_len=2000):
    for u in urls:
        try:
            r = await client.get(u)
            if r.status_code == 200 and len(r.text) >= min_len:
                if "Just a moment" in r.text or "cf-browser-verification" in r.text:
                    continue
                # skip parking / maintenance shells
                if len(r.text) < 8000 and "mantenimiento" in r.text.lower():
                    continue
                return r, str(r.url)
        except Exception:
            continue
    return None, None


def _clean_anime_title(t: str) -> str:
    t = re.sub(r"\s+", " ", (t or "")).strip()
    t = re.sub(r"(?i)^(nonton|streaming|download)\s+", "", t).strip()
    return t[:90]


def _parse_anime_cards(html: str, source: str, base: str, limit: int = 24):
    if not HAS_BS4:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items, seen = [], set()
    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        low_h = href.lower()
        # prefer /anime/slug series pages; allow root cards with images
        is_series = "/anime/" in low_h
        img = a.find("img")
        if img is None:
            parent = a.parent
            for _ in range(3):
                if parent is None:
                    break
                img = parent.find("img") if hasattr(parent, "find") else None
                if img is not None:
                    break
                parent = getattr(parent, "parent", None)
        if img is None:
            continue
        if not is_series and source in ("samehadaku", "otakudesu"):
            # only accept /anime/ for these
            continue
        title = (a.get("title") or img.get("alt") or img.get("title") or a.get_text(" ", strip=True) or "").strip()
        title = _clean_anime_title(title)
        if len(title) < 3:
            continue
        low_t = title.lower()
        if any(x in low_t for x in _SKIP_EP_TEXT):
            continue
        if "episode" in low_t and re.search(r"episode\s*\d", low_t):
            # list page should be series, not single episode
            if "/anime/" not in low_h:
                continue
        path = href.split("?")[0].rstrip("/")
        if "/anime/" in path:
            slug = path.split("/anime/")[-1].strip("/")
        else:
            slug = path.split("/")[-1]
        if not slug or slug in seen or len(slug) < 2:
            continue
        if slug in ("anime", "episode", "page", "genre", "batch", "jadwal", "ongoing", "search"):
            continue
        if any(x in slug.lower() for x in ("whatsapp", "telegram", "facebook")):
            continue
        thumb = (img.get("data-src") or img.get("data-lazy-src") or img.get("src") or "").strip()
        if thumb.startswith("//"):
            thumb = "https:" + thumb
        elif thumb.startswith("/"):
            thumb = base.rstrip("/") + thumb
        # id: prefer full series path for reliability
        if "/anime/" in path:
            aid = "anime/" + slug
        else:
            aid = slug
        seen.add(slug)
        items.append({
            "title": title,
            "thumb": thumb,
            "id": aid,
            "source": source,
            "label": "PREMIUM" if source in PREMIUM_ANIME else "GRATIS",
        })
        if len(items) >= limit:
            break
    return items


def _extract_episodes(soup, base: str):
    episodes, seen = [], set()
    # priority selectors for WP anime themes
    anchors = []
    for sel in (
        "div.lstepsiode li a", "div.listeps li a", "ul.episode li a",
        "div.eps a", ".eplister a", "#chapter_list a", ".episodelist a",
        "div.venz a", ".listeps a",
    ):
        anchors.extend(soup.select(sel))
    if len(anchors) < 2:
        anchors = list(soup.find_all("a", href=True))

    for a in anchors:
        href = (a.get("href") or "").strip()
        text = a.get_text(" ", strip=True)
        low = (href + " " + text).lower()
        if any(x in low for x in _SKIP_EP_TEXT):
            continue
        # must look like episode
        if not (
            re.search(r"episode[-_/\s]?\d", low)
            or re.search(r"/episode[-_]?\d", href.lower())
            or re.search(r"^\d{1,4}$", text.strip())
            or re.search(r"^episode\s*\d+", text.strip(), re.I)
        ):
            continue
        if href.startswith("/"):
            eid = base.rstrip("/") + href
        elif href.startswith("http"):
            eid = href
        else:
            eid = href
        key = eid.rstrip("/").split("/")[-1].lower()
        if not key or key in seen:
            continue
        if key in ("anime", "batch", "download"):
            continue
        seen.add(key)
        # title: prefer "Episode N"
        m = re.search(r"(?:episode|eps?|ep)[^\d]*(\d+(?:\.\d+)?)", low)
        if m:
            title = f"Episode {m.group(1)}"
            num = float(m.group(1))
        elif re.match(r"^\d{1,4}$", text.strip()):
            title = f"Episode {text.strip()}"
            num = float(text.strip())
        else:
            title = re.sub(r"\s+", " ", text)[:50] or key
            num = 0.0
        episodes.append({"title": title, "id": eid, "num": num})

    episodes.sort(key=lambda e: (e.get("num") or 0))
    out, sk = [], set()
    for e in episodes:
        k = e["id"]
        if k in sk:
            continue
        sk.add(k)
        e.pop("num", None)
        out.append(e)
    return out


def _clean_synopsis(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "")).strip()
    # drop SEO spam patterns
    if re.search(r"(?i)nonton streaming.*download.*subtitle", t):
        # try take after first period long sentence
        parts = re.split(r"(?<=[.!?])\s+", t)
        parts = [p for p in parts if len(p) > 40 and not re.search(r"(?i)nonton streaming|download.*hd subtitle", p)]
        t = " ".join(parts) if parts else ""
    if len(t) < 40:
        return ""
    return t[:900]


def _extract_stream(soup, page_url: str = ""):
    """Hanya embed player, bukan halaman situs penuh."""
    if soup is None:
        return ""
    # known player containers
    for sel in (
        "#pembed iframe", "#player-embed iframe", ".player-embed iframe",
        "#embed_holder iframe", ".responsive-embed iframe", "#player iframe",
        "iframe[src*='embed']", "iframe[src*='player']", "iframe",
    ):
        for iframe in soup.select(sel):
            src = (iframe.get("src") or iframe.get("data-src") or "").strip()
            if not src:
                continue
            if src.startswith("//"):
                src = "https:" + src
            low = src.lower()
            # skip ads / self site chrome
            if any(x in low for x in ("facebook.com", "twitter.com", "ads", "doubleclick")):
                continue
            # reject if it's just the samehadaku homepage-like path without embed markers
            if page_url and src.rstrip("/") == page_url.rstrip("/"):
                continue
            if any(x in low for x in (
                "embed", "player", "stream", "mp4upload", "filemoon", "vidhide",
                "blogger", "googlevideo", "yourupload", "streamtape", "mega.",
                "desustream", "gdplayer", "krakenfiles", "acefile",
            )) or src.startswith("http"):
                # if same domain as page and no embed keyword, skip (shows whole site)
                try:
                    from urllib.parse import urlparse
                    ph = urlparse(page_url).netloc
                    sh = urlparse(src).netloc
                    if ph and sh and ph == sh and not any(x in low for x in ("embed", "player", "streaming")):
                        continue
                except Exception:
                    pass
                return src
    for v in soup.find_all("source"):
        src = (v.get("src") or "").strip()
        if src and (".mp4" in src or ".m3u8" in src):
            return src
    # data-content base64/html sometimes used by themes
    for el in soup.select("[data-content]"):
        dc = (el.get("data-content") or "").strip()
        if not dc:
            continue
        m = re.search(r'src=["\']([^"\']+)["\']', dc)
        if m:
            src = m.group(1)
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http"):
                return src
        if dc.startswith("http") and any(x in dc.lower() for x in ("embed", "player")):
            return dc
    return ""


@app.get("/anime/search")
async def anime_search(request: Request, source: str = "anoboy", q: str = ""):
    source = (source or "anoboy").lower().strip()
    if source == "all":
        source = "anoboy"
    _anime_role_gate(request, source)
    q = (q or "").strip()
    items = []

    mirrors = {
        "anoboy": ANOBOY_MIRRORS,
        "samehadaku": SAMEHADAKU_MIRRORS,
        "otakudesu": OTAKUDESU_MIRRORS,
        "kusonime": KUSONIME_MIRRORS,
    }.get(source, ANOBOY_MIRRORS)

    try:
        async with httpx.AsyncClient(timeout=22.0, follow_redirects=True, headers=BROWSER_HEADERS) as client:
            urls = []
            for base in mirrors:
                if q:
                    urls.append(f"{base}/?s={urllib.parse.quote(q)}")
                    if source == "otakudesu":
                        urls.append(f"{base}/?s={urllib.parse.quote(q)}&post_type=anime")
                    if source == "kusonime":
                        urls.append(f"{base}/?s={urllib.parse.quote(q)}&post_type[]=post&post_type[]=anime")
                else:
                    urls.append(f"{base}/")
                    if source == "otakudesu":
                        urls.extend([f"{base}/ongoing-anime/", f"{base}/complete-anime/"])
                    if source == "samehadaku":
                        urls.extend([f"{base}/anime-terbaru/", f"{base}/daftar-anime-2/"])
                    if source == "kusonime":
                        urls.append(f"{base}/anime-list/")
            for u in urls:
                try:
                    r = await client.get(u, headers={**BROWSER_HEADERS, "Referer": mirrors[0] + "/"})
                except Exception:
                    continue
                if r.status_code != 200 or len(r.text) < 2000:
                    continue
                if "Just a moment" in r.text:
                    continue
                base = f"{r.url.scheme}://{r.url.netloc}"
                part = _parse_anime_cards(r.text, source, base, 28)
                # kusonime often uses different paths
                if source == "kusonime" and len(part) < 3 and HAS_BS4:
                    soup = BeautifulSoup(r.text, "html.parser")
                    for a in soup.select("a[href]"):
                        img = a.find("img")
                        if not img:
                            continue
                        href = (a.get("href") or "").strip()
                        title = _clean_anime_title(a.get("title") or img.get("alt") or a.get_text(" ", strip=True))
                        if len(title) < 3:
                            continue
                        slug = href.rstrip("/").split("/")[-1]
                        if not slug or len(slug) < 3:
                            continue
                        thumb = (img.get("data-src") or img.get("src") or "").strip()
                        part.append({
                            "title": title[:90], "thumb": thumb, "id": href if href.startswith("http") else slug,
                            "source": "kusonime", "label": "GRATIS",
                        })
                        if len(part) >= 24:
                            break
                items.extend(part)
                if len(items) >= 20:
                    break
    except Exception:
        pass

    if len(items) < 4 and q:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get("https://api.jikan.moe/v4/anime", params={"q": q, "limit": 12})
                if r.status_code == 200:
                    for a in (r.json().get("data") or []):
                        items.append({
                            "title": a.get("title") or "?",
                            "thumb": ((a.get("images") or {}).get("jpg") or {}).get("image_url") or "",
                            "id": str(a.get("mal_id") or ""),
                            "source": "jikan",
                            "label": "META",
                        })
        except Exception:
            pass

    seen, unique = set(), []
    for it in items:
        key = ((it.get("title") or "").lower()[:40], it.get("source"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)
    if not unique:
        return {"items": [], "hint": "Source sedang sibuk / domain ganti. Coba Kusonime atau Anoboy, atau keyword lain."}
    return {"items": unique[:24]}


@app.get("/anime/detail")
async def anime_detail(request: Request, source: str = "anoboy", id: str = ""):
    if not id:
        raise HTTPException(400, "id kosong")
    source = (source or "anoboy").lower().strip()
    _anime_role_gate(request, source)

    if source == "jikan" or (str(id).isdigit() and source not in ("anoboy", "samehadaku", "otakudesu", "kusonime")):
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.get(f"https://api.jikan.moe/v4/anime/{id}/full")
                if r.status_code != 200:
                    r = await client.get(f"https://api.jikan.moe/v4/anime/{id}")
                if r.status_code != 200:
                    raise HTTPException(404, "Anime tidak ditemukan")
                a = (r.json() or {}).get("data") or {}
                title = a.get("title") or id
                synopsis = a.get("synopsis") or ""
                thumb = ((a.get("images") or {}).get("jpg") or {}).get("large_image_url") or ""
                genres = [g.get("name") for g in (a.get("genres") or []) if g.get("name")]
                mal = a.get("url") or f"https://myanimelist.net/anime/{id}"
                ep_count = int(a.get("episodes") or 0)
                episodes = [{"title": f"Episode {i}", "id": str(i), "external": mal} for i in range(1, min(ep_count, 60) + 1)]
                if not episodes:
                    episodes = [{"title": "Buka di MyAnimeList", "id": "mal", "external": mal}]
                return {
                    "title": title, "synopsis": synopsis, "thumb": thumb,
                    "episodes": episodes, "genres": genres,
                    "note": "Metadata Jikan. Pilih Anoboy/Kusonime/Samehadaku untuk stream.",
                }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, str(e))

    mirrors = {
        "anoboy": ANOBOY_MIRRORS,
        "samehadaku": SAMEHADAKU_MIRRORS,
        "otakudesu": OTAKUDESU_MIRRORS,
        "kusonime": KUSONIME_MIRRORS,
    }.get(source, ANOBOY_MIRRORS)

    urls = []
    if str(id).startswith("http"):
        urls = [id]
    else:
        slug = id.strip("/")
        for base in mirrors:
            if slug.startswith("anime/"):
                urls.append(f"{base}/{slug}/")
            else:
                urls.append(f"{base}/anime/{slug}/")
                urls.append(f"{base}/{slug}/")

    try:
        async with httpx.AsyncClient(timeout=24.0, follow_redirects=True, headers=BROWSER_HEADERS) as client:
            r, used = await _fetch_first_ok(client, urls, 2500)
            if r is None:
                raise HTTPException(404, "Detail anime tidak ditemukan (domain mungkin ganti)")
            base = f"{r.url.scheme}://{r.url.netloc}"
            soup = BeautifulSoup(r.text, "html.parser")
            h1 = soup.select_one("h1.entry-title, h1, .jdlrx h1")
            title = _clean_anime_title(h1.get_text(" ", strip=True) if h1 else id.replace("-", " ").title())
            syn_el = soup.select_one(".sinopsis p, .entry-content p, [itemprop='description'], .desc, .sinopc, .infozingle p")
            synopsis = _clean_synopsis(syn_el.get_text(" ", strip=True) if syn_el else "")
            if not synopsis:
                for p_el in soup.select(".sinopsis p, .entry-content p"):
                    synopsis = _clean_synopsis(p_el.get_text(" ", strip=True))
                    if synopsis:
                        break
            img = soup.select_one(".fotoanime img, .thumb img, img.wp-post-image, .animeseries img, .series-thumb img")
            thumb = ""
            if img is not None:
                thumb = (img.get("data-src") or img.get("src") or "").strip()
            genres = [g.get_text(strip=True) for g in soup.select(".genre a, .genzed a, a[rel='tag'], .anim-genre a") if g.get_text(strip=True)][:12]
            episodes = _extract_episodes(soup, base)
            return {
                "title": title,
                "synopsis": synopsis,
                "thumb": thumb,
                "episodes": episodes[:120],
                "genres": genres,
                "source": source,
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Detail gagal: {type(e).__name__}")


@app.get("/anime/episode")
async def anime_episode(request: Request, source: str = "anoboy", id: str = ""):
    """Kembalikan embed player saja. Jika tidak ada, error (jangan tampilkan situs penuh)."""
    source = (source or "anoboy").lower().strip()
    if source != "jikan":
        _anime_role_gate(request, source)
    if not id:
        raise HTTPException(400, "id kosong")
    if source == "jikan" or id == "mal":
        return {"stream_url": "", "embed": "", "message": "Metadata saja — pilih source stream."}

    mirrors = {
        "anoboy": ANOBOY_MIRRORS,
        "samehadaku": SAMEHADAKU_MIRRORS,
        "otakudesu": OTAKUDESU_MIRRORS,
        "kusonime": KUSONIME_MIRRORS,
    }.get(source, ANOBOY_MIRRORS)

    if str(id).startswith("http"):
        urls = [id]
    else:
        urls = [f"{b}/{id.strip('/')}/" for b in mirrors]

    try:
        async with httpx.AsyncClient(timeout=28.0, follow_redirects=True, headers=BROWSER_HEADERS) as client:
            r, used = await _fetch_first_ok(client, urls, 1500)
            if r is None:
                raise HTTPException(404, "Halaman episode tidak ditemukan")
            page_url = str(r.url)
            soup = BeautifulSoup(r.text, "html.parser") if HAS_BS4 else None
            stream = _extract_stream(soup, page_url)
            if not stream:
                # last try: open external page for user (not as iframe src)
                return {
                    "stream_url": "",
                    "embed": "",
                    "page": page_url,
                    "message": "Embed player tidak ditemukan. Pakai 'Buka di tab baru'.",
                    "external": page_url,
                }
            return {"stream_url": stream, "embed": stream, "page": page_url}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))



# ============================================================
# DONGHUA + TOKUSATSU (HTML scrape + embed stream)
# ============================================================
DONGHUA_SOURCES = {
    "anichin": ["https://anichin.care", "https://anichin.moe"],
    "donghuastream": ["https://donghuastream.org"],
    "dongsub": ["https://dongsub.org"],
    "luciferdonghua": ["https://luciferdonghua.in"],
}
TOKUSATSU_SOURCES = {
    "tokusatsuindo": ["https://www.tokusatsuindo.com", "https://tokusatsuindo.com"],
    "tvtokusatsuindo": ["https://www.tvtokusatsuindo.com", "https://tvtokusatsuindo.com"],
}
PREMIUM_DONGHUA = set()  # semua gratis dulu
PREMIUM_TOKU = set()


def _media_mirrors(kind: str, source: str):
    if kind == "donghua":
        return DONGHUA_SOURCES.get(source, DONGHUA_SOURCES["donghuastream"])
    return TOKUSATSU_SOURCES.get(source, TOKUSATSU_SOURCES["tokusatsuindo"])


def _parse_media_cards(html: str, source: str, base: str, limit: int = 24):
    if not HAS_BS4:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items, seen = [], set()
    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        img = a.find("img")
        if img is None:
            continue
        title = (a.get("title") or img.get("alt") or img.get("title") or a.get_text(" ", strip=True) or "").strip()
        title = re.sub(r"\s+", " ", title)
        if len(title) < 3:
            continue
        low = title.lower()
        if any(x in low for x in ("home", "login", "banner", "discord", "telegram", "whatsapp", "facebook", "web stats")):
            continue
        path = href.split("?")[0].rstrip("/")
        # prefer series-ish paths
        if any(x in path.lower() for x in ("/category/", "/genre/", "/tag/", "/page/")):
            continue
        slug = path.split("/")[-1] or path
        if not slug or slug in seen or len(slug) < 2:
            continue
        if slug in ("anime", "movie", "special", "home", "blog"):
            continue
        thumb = (img.get("data-src") or img.get("data-lazy-src") or img.get("src") or "").strip()
        if thumb.startswith("//"):
            thumb = "https:" + thumb
        elif thumb.startswith("/"):
            thumb = base.rstrip("/") + thumb
        full = href if href.startswith("http") else (base.rstrip("/") + "/" + href.lstrip("/"))
        seen.add(slug)
        items.append({
            "title": title[:90],
            "thumb": thumb,
            "id": full if "/anime/" in full or full.startswith("http") else slug,
            "source": source,
            "label": "GRATIS",
        })
        if len(items) >= limit:
            break
    return items


async def _media_search(kind: str, source: str, q: str):
    mirrors = _media_mirrors(kind, source)
    items = []
    q = (q or "").strip()
    try:
        async with httpx.AsyncClient(timeout=22.0, follow_redirects=True, headers=BROWSER_HEADERS) as client:
            urls = []
            for base in mirrors:
                if q:
                    urls.append(f"{base}/?s={urllib.parse.quote(q)}")
                else:
                    urls.append(f"{base}/")
                    if kind == "tokusatsu" and "tokusatsuindo" in base:
                        urls.extend([f"{base}/kamen-rider-2/", f"{base}/movie-special/"])
                    if kind == "donghua" and "donghuastream" in base:
                        urls.append(f"{base}/anime/")
            for u in urls:
                try:
                    r = await client.get(u, headers={**BROWSER_HEADERS, "Referer": mirrors[0] + "/"})
                except Exception:
                    continue
                if r.status_code != 200 or len(r.text) < 2000:
                    continue
                if "Just a moment" in r.text:
                    continue
                base = f"{r.url.scheme}://{r.url.netloc}"
                part = _parse_media_cards(r.text, source, base, 28)
                items.extend(part)
                if len(items) >= 18:
                    break
    except Exception:
        pass
    seen, unique = set(), []
    for it in items:
        key = ((it.get("title") or "").lower()[:40], it.get("source"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)
    if not unique:
        return {"items": [], "hint": "Source sibuk / domain mati. Coba source atau keyword lain."}
    return {"items": unique[:24]}


async def _media_detail(kind: str, source: str, id: str):
    if not id:
        raise HTTPException(400, "id kosong")
    mirrors = _media_mirrors(kind, source)
    urls = [id] if str(id).startswith("http") else [f"{b}/{id.strip('/')}/" for b in mirrors] + [f"{b}/anime/{id.strip('/')}/" for b in mirrors]
    try:
        async with httpx.AsyncClient(timeout=24.0, follow_redirects=True, headers=BROWSER_HEADERS) as client:
            r, used = await _fetch_first_ok(client, urls, 2000)
            if r is None:
                raise HTTPException(404, "Detail tidak ditemukan")
            base = f"{r.url.scheme}://{r.url.netloc}"
            soup = BeautifulSoup(r.text, "html.parser")
            h1 = soup.select_one("h1.entry-title, h1, .entry-title")
            title = (h1.get_text(" ", strip=True) if h1 else id.replace("-", " ").title())[:100]
            syn = ""
            for sel in (".entry-content p", ".synp", ".desc", "[itemprop='description']", ".series-synops p"):
                el = soup.select_one(sel)
                if el:
                    t = el.get_text(" ", strip=True)
                    if len(t) > 40:
                        syn = t[:900]
                        break
            img = soup.select_one(".thumb img, img.wp-post-image, .series-thumb img, .poster img")
            thumb = ""
            if img is not None:
                thumb = (img.get("data-src") or img.get("src") or "").strip()
            episodes = _extract_episodes(soup, base)
            # if episode list empty, treat page itself as single playable
            if not episodes and str(r.url):
                episodes = [{"title": "Tonton", "id": str(r.url)}]
            genres = [g.get_text(strip=True) for g in soup.select("a[rel='tag'], .genre a, .genzed a") if g.get_text(strip=True)][:10]
            return {"title": title, "synopsis": syn, "thumb": thumb, "episodes": episodes[:120], "genres": genres, "source": source}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Detail gagal: {type(e).__name__}")


async def _media_episode(kind: str, source: str, id: str):
    if not id:
        raise HTTPException(400, "id kosong")
    mirrors = _media_mirrors(kind, source)
    urls = [id] if str(id).startswith("http") else [f"{b}/{id.strip('/')}/" for b in mirrors]
    try:
        async with httpx.AsyncClient(timeout=28.0, follow_redirects=True, headers=BROWSER_HEADERS) as client:
            r, used = await _fetch_first_ok(client, urls, 1200)
            if r is None:
                raise HTTPException(404, "Episode tidak ditemukan")
            page_url = str(r.url)
            soup = BeautifulSoup(r.text, "html.parser") if HAS_BS4 else None
            stream = _extract_stream(soup, page_url)
            if not stream:
                return {
                    "stream_url": "", "embed": "", "page": page_url,
                    "external": page_url,
                    "message": "Embed tidak ditemukan. Buka di tab baru.",
                }
            return {"stream_url": stream, "embed": stream, "page": page_url}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get("/donghua/search")
async def donghua_search(request: Request, source: str = "donghuastream", q: str = ""):
    source = (source or "donghuastream").lower().strip()
    if source not in DONGHUA_SOURCES:
        source = "donghuastream"
    return await _media_search("donghua", source, q)


@app.get("/donghua/detail")
async def donghua_detail(request: Request, source: str = "donghuastream", id: str = ""):
    source = (source or "donghuastream").lower().strip()
    return await _media_detail("donghua", source, id)


@app.get("/donghua/episode")
async def donghua_episode(request: Request, source: str = "donghuastream", id: str = ""):
    source = (source or "donghuastream").lower().strip()
    return await _media_episode("donghua", source, id)


@app.get("/tokusatsu/search")
async def tokusatsu_search(request: Request, source: str = "tokusatsuindo", q: str = ""):
    source = (source or "tokusatsuindo").lower().strip()
    if source not in TOKUSATSU_SOURCES:
        source = "tokusatsuindo"
    return await _media_search("tokusatsu", source, q)


@app.get("/tokusatsu/detail")
async def tokusatsu_detail(request: Request, source: str = "tokusatsuindo", id: str = ""):
    source = (source or "tokusatsuindo").lower().strip()
    return await _media_detail("tokusatsu", source, id)


@app.get("/tokusatsu/episode")
async def tokusatsu_episode(request: Request, source: str = "tokusatsuindo", id: str = ""):
    source = (source or "tokusatsuindo").lower().strip()
    return await _media_episode("tokusatsu", source, id)


# ============================================================

# NOVEL

# ============================================================

# NOVEL
# ============================================================

# NOVEL
# ============================================================

# NOVEL
# ============================================================
@app.get("/novel/search")
async def novel_search(q: str = "", source: str = "wbnovel"):
    """WB Novel (wbnovel.com) — web novel Indo."""
    items = []
    q = (q or "").strip()
    if not HAS_BS4:
        return {"items": [], "hint": "BeautifulSoup tidak tersedia"}
    try:
        async with httpx.AsyncClient(
            timeout=22.0, follow_redirects=True,
            headers={**BROWSER_HEADERS, "Referer": "https://wbnovel.com/"},
        ) as client:
            if q:
                url = f"https://wbnovel.com/?s={urllib.parse.quote(q)}&post_type=wp-manga"
            else:
                url = "https://wbnovel.com/"
            r = await client.get(url)
            if r.status_code != 200:
                return {"items": [], "hint": f"WB Novel HTTP {r.status_code}"}
            soup = BeautifulSoup(r.text, "html.parser")
            seen = set()

            def add_item(title, href, thumb=""):
                if not href:
                    return
                if "/chapter-" in href or re.search(r"/chapter-\d", href):
                    return
                slug = href.rstrip("/").split("/")[-1]
                if not slug or slug in seen or slug in ("nv", "novel", "manga"):
                    return
                if not title or len(title) < 2:
                    title = slug.replace("-", " ").title()
                seen.add(slug)
                items.append({
                    "title": title[:90],
                    "thumb": thumb or "",
                    "id": slug,
                    "source": "wbnovel",
                })

            cards = soup.select(".c-tabs-item__content, .page-item-detail, .tab-thumb")
            for box in cards[:50]:
                a = box.select_one("a[href*='/nv/'], .post-title a, h3 a, h4 a")
                if not a:
                    continue
                href = a.get("href") or ""
                title = a.get("title") or a.get_text(strip=True) or ""
                img = box.select_one("img")
                thumb = ""
                if img is not None:
                    thumb = (img.get("data-src") or img.get("src") or "").strip()
                add_item(title, href, thumb)

            if not items:
                for a in soup.select("a[href*='/nv/']"):
                    href = a.get("href") or ""
                    if href.rstrip("/").count("/") < 4:
                        continue
                    img = a.find("img")
                    thumb = (img.get("data-src") or img.get("src") or "") if img else ""
                    add_item(a.get("title") or a.get_text(strip=True), href, thumb)
                    if len(items) >= 55:
                        break
    except Exception:
        pass
    
    # ReadNovelFull (cadangan — katalog besar; EN)
    if source in ("all", "readnovelfull", "rnfull") and HAS_BS4:
        try:
            async with httpx.AsyncClient(timeout=22.0, follow_redirects=True, headers={**BROWSER_HEADERS, "Referer": READNOVELFULL_BASE + "/"}) as client:
                if q:
                    urls = [f"{READNOVELFULL_BASE}/search?keyword={urllib.parse.quote(q)}"]
                    # alt
                    urls.append(f"{READNOVELFULL_BASE}/search?keyword={urllib.parse.quote(q)}&type=novel")
                else:
                    urls = [READNOVELFULL_BASE + "/", READNOVELFULL_BASE + "/latest.html"]
                seen = set()
                for u in urls:
                    try:
                        r = await client.get(u)
                    except Exception:
                        continue
                    if r.status_code != 200:
                        continue
                    soup = BeautifulSoup(r.text, "html.parser")
                    for a in soup.select("a[href$='.html'], .novel-title a, h3 a, .list-novel a"):
                        href = (a.get("href") or "").strip()
                        if not href or "/genre" in href or "chapter" in href.lower():
                            continue
                        if not href.endswith(".html"):
                            continue
                        full = href if href.startswith("http") else (READNOVELFULL_BASE + href if href.startswith("/") else "")
                        if not full or "readnovelfull.com" not in full:
                            continue
                        slug = full.rstrip("/").split("/")[-1]
                        if slug in seen or slug in ("latest.html", "completed.html", "hot.html"):
                            continue
                        title = (a.get_text(" ", strip=True) or slug.replace("-", " "))[:90]
                        if len(title) < 3:
                            continue
                        if q and q.lower() not in title.lower() and q.lower() not in slug.lower():
                            if "search" not in str(r.url):
                                continue
                        seen.add(slug)
                        thumb = ""
                        img = a.find("img") or (a.parent.find("img") if a.parent else None)
                        if img is not None:
                            thumb = (img.get("src") or img.get("data-src") or "").strip()
                        items.append({
                            "title": title,
                            "thumb": thumb,
                            "id": "rnfull/" + slug.replace(".html", ""),
                            "source": "readnovelfull",
                        })
                        if len(seen) >= 48:
                            break
                    if len(seen) >= 48:
                        break
        except Exception:
            pass

    return {"items": items[:60]}


@app.get("/novel/detail")
async def novel_detail(id: str = "", source: str = "wbnovel"):
    if not id:
        raise HTTPException(400, "id kosong")


    if id.startswith("rnfull/") or source == "readnovelfull":
        slug = id.replace("rnfull/", "").replace(".html", "").strip("/")
        url = f"{READNOVELFULL_BASE}/{slug}.html"
        try:
            async with httpx.AsyncClient(timeout=25.0, follow_redirects=True, headers={**BROWSER_HEADERS, "Referer": READNOVELFULL_BASE + "/"}) as client:
                r = await client.get(url)
                if r.status_code != 200:
                    raise HTTPException(404, "Novel tidak ditemukan")
                soup = BeautifulSoup(r.text, "html.parser")
                h = soup.select_one("h3.title, h1, .title")
                title = h.get_text(strip=True) if h else slug.replace("-", " ").title()
                syn = soup.select_one(".desc-text, .description, [itemprop='description']")
                synopsis = syn.get_text(" ", strip=True)[:1500] if syn else ""
                img = soup.select_one(".book-img img, .info-holder img, img")
                thumb = (img.get("src") or img.get("data-src") or "").strip() if img else ""
                chapters = []
                seen = set()
                for a in soup.select("a[href*='chapter']"):
                    href = a.get("href") or ""
                    if "chapter" not in href.lower():
                        continue
                    full = href if href.startswith("http") else (READNOVELFULL_BASE + href if href.startswith("/") else "")
                    if not full or full in seen:
                        continue
                    seen.add(full)
                    chapters.append({"title": (a.get_text(strip=True) or "Chapter")[:80], "id": full})
                return {
                    "title": title[:120],
                    "synopsis": synopsis,
                    "thumb": thumb,
                    "chapters": chapters[:3000],
                    "source": "readnovelfull",
                }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"ReadNovelFull detail: {type(e).__name__}")

    slug = id.strip().strip("/")
    if slug.startswith("http"):
        url = slug if slug.endswith("/") else slug + "/"
        # extract slug
        parts = [p for p in slug.replace("https://wbnovel.com/", "").split("/") if p and p != "nv"]
        slug = parts[0] if parts else slug
    else:
        if slug.startswith("nv/"):
            slug = slug[3:]
        url = f"https://wbnovel.com/nv/{slug}/"
    title, synopsis, thumb, chapters = slug, "", "", []
    try:
        async with httpx.AsyncClient(
            timeout=28.0, follow_redirects=True,
            headers={**BROWSER_HEADERS, "Referer": "https://wbnovel.com/"},
        ) as client:
            r = await client.get(url)
            if r.status_code != 200 or not HAS_BS4:
                raise HTTPException(404, "Novel tidak ditemukan")
            soup = BeautifulSoup(r.text, "html.parser")
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(strip=True)
            img = soup.select_one(".summary_image img, .tab-summary img, img.img-responsive, .summary_content img")
            if img:
                thumb = (img.get("data-src") or img.get("src") or "").strip()
            desc = soup.select_one(".description-summary, .summary__content, .manga-excerpt, [itemprop='description']")
            if desc:
                synopsis = desc.get_text(" ", strip=True)[:1500]
            seen = set()

            def add_ch(href, text):
                if not href:
                    return
                if "wbnovel.com" not in href and href.startswith("/"):
                    href = "https://wbnovel.com" + href
                if "/chapter" not in href.lower():
                    return
                ch_slug = href.rstrip("/").split("/")[-1]
                if not ch_slug or ch_slug in seen:
                    return
                seen.add(ch_slug)
                # id SELALU full URL agar /novel/chapter tidak salah bangun path
                chapters.append({
                    "title": (text or ch_slug)[:80],
                    "id": href if href.startswith("http") else f"https://wbnovel.com/nv/{slug}/{ch_slug}/",
                })

            # 1) Ajax full list (Madara)
            try:
                ra = await client.post(
                    f"https://wbnovel.com/nv/{slug}/ajax/chapters/",
                    headers={**BROWSER_HEADERS, "Referer": url, "X-Requested-With": "XMLHttpRequest"},
                    data={},
                )
                if ra.status_code == 200 and len(ra.text) > 50:
                    soup_ch = BeautifulSoup(ra.text, "html.parser")
                    for a in soup_ch.select("a[href*='chapter']"):
                        add_ch(a.get("href") or "", a.get_text(strip=True))
            except Exception:
                pass

            # 2) Static page
            if not chapters:
                for a in soup.select(".wp-manga-chapter a, .listing-chapters_wrap a, li.wp-manga-chapter a, .version-chap a, a[href*='chapter-']"):
                    add_ch(a.get("href") or "", a.get_text(strip=True))
            if not chapters:
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if "/chapter-" in href or re.search(r"chapter-\d", href):
                        add_ch(href, a.get_text(strip=True))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))
    return {"title": title, "chapters": chapters[:2500], "synopsis": synopsis, "thumb": thumb}


@app.get("/novel/chapter")
async def novel_chapter(id: str = "", source: str = "wbnovel", novel: str = ""):
    """id = full chapter URL (preferred) atau chapter-70 + novel=slug."""
    if not id:
        raise HTTPException(400, "id kosong")


    if "readnovelfull.com" in id or id.startswith("http") and "readnovelfull" in id:
        try:
            async with httpx.AsyncClient(timeout=25.0, follow_redirects=True, headers={**BROWSER_HEADERS, "Referer": READNOVELFULL_BASE + "/"}) as client:
                url = id if id.startswith("http") else id
                r = await client.get(url)
                if r.status_code != 200:
                    raise HTTPException(404, "Chapter tidak ditemukan")
                soup = BeautifulSoup(r.text, "html.parser")
                content = soup.select_one("#chr-content, .chapter-content, #chapter-content, .txt")
                text = content.get_text("\n", strip=True) if content else ""
                if len(text) < 40:
                    raise HTTPException(404, "Teks chapter kosong")
                return {"text": text[:120000], "title": (soup.title.get_text(strip=True) if soup.title else "Chapter")[:120]}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"ReadNovelFull chapter: {type(e).__name__}")
    raw_id = id.strip()
    # bangun kandidat URL
    candidates = []
    if raw_id.startswith("http"):
        candidates.append(raw_id if raw_id.endswith("/") else raw_id + "/")
        candidates.append(raw_id.rstrip("/"))
    else:
        rid = raw_id.strip("/")
        if rid.startswith("nv/"):
            candidates.append(f"https://wbnovel.com/{rid}/")
        elif novel:
            nov = novel.strip().strip("/")
            if nov.startswith("nv/"):
                nov = nov[3:]
            ch = rid if "chapter" in rid else f"chapter-{rid}"
            candidates.append(f"https://wbnovel.com/nv/{nov}/{ch}/")
        elif "/" in rid:
            candidates.append(f"https://wbnovel.com/{rid}/")
            candidates.append(f"https://wbnovel.com/nv/{rid}/")
        else:
            # chapter only tanpa novel — gagal jelas
            if novel:
                pass
            else:
                raise HTTPException(400, "Butuh URL chapter lengkap atau parameter novel=slug")

    last_err = "Chapter tidak ditemukan"
    try:
        async with httpx.AsyncClient(
            timeout=35.0, follow_redirects=True,
            headers={**BROWSER_HEADERS, "Referer": "https://wbnovel.com/"},
        ) as client:
            r = None
            used = ""
            for u in candidates:
                try:
                    rr = await client.get(u)
                    if rr.status_code == 200 and len(rr.text) > 5000:
                        r = rr
                        used = u
                        break
                except Exception:
                    continue
            if r is None or not HAS_BS4:
                raise HTTPException(404, f"{last_err} (coba: {candidates[0] if candidates else id})")

            # Deteksi bot-wall / CF
            low_html = (r.text or "")[:2000].lower()
            if "mohon tunggu" in low_html or "one moment" in low_html or "just a moment" in low_html or "cf-browser" in low_html:
                raise HTTPException(503, "WB Novel memblokir server (bot-wall). Coba lagi nanti atau sumber novel lain.")

            soup = BeautifulSoup(r.text, "html.parser")
            text = ""

            # 1) paragraph join dari reading-content (paling akurat)
            for root_sel in [".reading-content", ".text-left", ".entry-content", "article"]:
                root = soup.select_one(root_sel)
                if not root:
                    continue
                ps = root.find_all("p")
                if ps:
                    parts = [p.get_text(" ", strip=True) for p in ps if p.get_text(strip=True)]
                    joined = "\n\n".join(parts)
                    if len(joined) > 100:
                        text = joined
                        break
                plain = root.get_text("\n", strip=True)
                if len(plain) > 100:
                    text = plain
                    break

            if len(text) < 80:
                # 2) fallback regex: strip scripts/styles then text
                for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
                    tag.decompose()
                body = soup.select_one(".reading-content, .c-blog-post, article, main") or soup.body
                if body:
                    text = body.get_text("\n", strip=True)

            # bersihkan navigasi
            lines = []
            for ln in (text or "").split("\n"):
                s = ln.strip()
                if not s:
                    continue
                low = s.lower()
                if low in ("home", "next", "prev", "previous", "next chapter", "prev chapter", "previous chapter"):
                    continue
                if re.match(r"^(chapter\s*)?\d+(\.\d+)?$", low):
                    continue
                if low.startswith("http"):
                    continue
                lines.append(s)
            text = "\n".join(lines)

            if len(text) < 60:
                raise HTTPException(404, f"Teks chapter kosong di {used or id}. Coba chapter lain.")

            h = soup.find("h1") or soup.find("h2") or soup.find("h3")
            title = h.get_text(strip=True) if h else raw_id
            return {"content": text[:100000], "title": title, "url": used}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Novel chapter: {type(e).__name__}: {e}")



@app.get("/movie/search")
async def movie_search(q: str = "", type: str = "movie"):
    items = []
    mirrors = ["https://tv1.nontondrama.click", "https://layarkaca21.net", "https://www.lk21official.co"]
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=BROWSER_HEADERS) as client:
            for mirror in mirrors:
                try:
                    if type == "drama":
                        url = f"{mirror}/?s={q}" if q else f"{mirror}/drama/"
                    else:
                        url = f"{mirror}/?s={q}" if q else f"{mirror}/"
                    r = await client.get(url)
                    if r.status_code != 200 or not HAS_BS4:
                        continue
                    soup = BeautifulSoup(r.text, "html.parser")
                    cards = soup.find_all("div", class_="col-xs-4") or soup.find_all("article")
                    for card in cards[:20]:
                        link = card.find("a")
                        img = card.find("img")
                        title_tag = card.find("h3") or card.find("h2")
                        if link and img:
                            href = link.get("href", "")
                            slug = href.rstrip("/").split("/")[-1] if href else ""
                            title = title_tag.get_text(strip=True) if title_tag else img.get("alt", "?")
                            thumb = img.get("data-src") or img.get("src") or ""
                            year_tag = card.find("span", class_="year")
                            year = year_tag.get_text(strip=True) if year_tag else ""
                            items.append({
                                "title": title, "thumb": thumb, "id": slug,
                                "year": year, "type": type, "source": mirror,
                            })
                    if items:
                        break
                except Exception:
                    continue
    except Exception as e:
        raise HTTPException(502, str(e))
    if not items:
        return {"items": [], "hint": "Mirror LK21 sedang down. Coba lagi nanti."}
    return {"items": items}

@app.get("/movie/detail")
async def movie_detail(id: str = "", source: str = ""):
    title, synopsis, thumb, year, genres, streams = "", "", "", "", [], []
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=BROWSER_HEADERS) as client:
            if not source:
                source = "https://tv1.nontondrama.click"
            url = f"{source}/{id}/"
            r = await client.get(url)
            if r.status_code != 200:
                raise HTTPException(404, "Film tidak ditemukan")
            if HAS_BS4:
                soup = BeautifulSoup(r.text, "html.parser")
                title_tag = soup.find("h1") or soup.find("h2", class_="title")
                if title_tag:
                    title = title_tag.get_text(strip=True)
                img_tag = soup.find("div", class_=re.compile(r"poster|image", re.I))
                if img_tag:
                    img = img_tag.find("img")
                    if img:
                        thumb = img.get("data-src") or img.get("src") or ""
                desc_tag = soup.find("div", class_=re.compile(r"sinopsis|description|overview", re.I))
                if desc_tag:
                    synopsis = desc_tag.get_text(strip=True)
                year_tag = soup.find("span", class_="year")
                if year_tag:
                    year = year_tag.get_text(strip=True)[:4]
                genre_tag = soup.find("div", class_=re.compile(r"genre|categories", re.I))
                if genre_tag:
                    genres = [g.get_text(strip=True) for g in genre_tag.find_all("a")]
                player = soup.find("div", class_=re.compile(r"player|embed", re.I))
                if player:
                    iframe = player.find("iframe")
                    if iframe:
                        streams.append({"quality": "Auto", "url": iframe.get("src", "")})
                download_section = soup.find("div", class_="download")
                if download_section:
                    for link in download_section.find_all("a"):
                        href = link.get("href", "")
                        quality = link.get_text(strip=True)
                        if href and ("google" in href or "mega" in href or "mediafire" in href):
                            streams.append({"quality": quality, "url": href})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Movie detail error: {str(e)}")
    return {"title": title, "synopsis": synopsis, "thumb": thumb, "year": year, "genres": genres, "streams": streams}

# ============================================================
# GIF
# ============================================================
GIFUKAI_ACTIONS = [
    "hug", "kiss", "pat", "cry", "sleep", "pout", "blush", "dance",
    "happy", "angry", "wave", "slap", "punch", "bite", "cuddle", "highfive",
    "bonk", "smile", "wink", "shrug", "facepalm", "nom", "poke", "stare",
]

@app.get("/gif/search")
async def gif_search(q: str = "hug"):
    q = (q or "hug").strip().lower()[:40]
    alias = {
        "anime": "hug", "love": "kiss", "sad": "cry", "happy": "happy",
        "angry": "angry", "mad": "angry", "dance": "dance", "sleep": "sleep",
        "wave": "wave", "hi": "wave", "hello": "wave", "punch": "punch",
        "slap": "slap", "bite": "bite", "cuddle": "cuddle", "blush": "blush",
        "reaction": "shrug", "meme": "facepalm", "cartoon": "dance",
    }
    action = alias.get(q, q if q in GIFUKAI_ACTIONS else "hug")
    items = []
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            for _ in range(12):
                r = await client.get(f"https://api.gifukai.com/v1/{action}")
                if r.status_code != 200:
                    continue
                data = r.json()
                url = data.get("url") or ""
                if url:
                    items.append({
                        "url": url, "preview": url,
                        "title": f"{data.get('action', action)} · {data.get('anime', 'Anime')}",
                    })
            if len(items) < 6:
                for a in GIFUKAI_ACTIONS:
                    if a == action:
                        continue
                    r = await client.get(f"https://api.gifukai.com/v1/{a}")
                    if r.status_code == 200:
                        data = r.json()
                        url = data.get("url") or ""
                        if url:
                            items.append({"url": url, "preview": url, "title": f"{a} · {data.get('anime', '')}"})
                    if len(items) >= 16:
                        break
    except Exception as e:
        raise HTTPException(502, f"GIF error: {str(e)}")
    return {"items": items[:20], "query": action, "provider": "gifukai"}

@app.get("/gif/actions")
async def gif_actions():
    return {"actions": GIFUKAI_ACTIONS}


async def _maybe_sources(messages: list) -> list:
    """Sumber web sederhana untuk pertanyaan faktual; kosong untuk sapaan."""
    last = ""
    for m in reversed(messages or []):
        if m.get("role") == "user":
            last = (m.get("content") or "").strip()
            break
    if not last or len(last) < 8:
        return []
    low = last.lower()
    greet = ("halo", "hai", "hi", "hello", "pagi", "siang", "malam", "thanks", "terima kasih", "apa kabar")
    if any(low == g or low.startswith(g + " ") or low.startswith(g + "!") for g in greet):
        return []
    # skip pure chit-chat short
    if len(last.split()) < 3 and "?" not in last:
        return []
    sources = []
    try:
        q = urllib.parse.quote(last[:120])
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Wikipedia opensearch
            wr = await client.get(
                "https://id.wikipedia.org/w/api.php",
                params={"action": "opensearch", "search": last[:80], "limit": 1, "namespace": 0, "format": "json"},
            )
            if wr.status_code == 200:
                data = wr.json()
                if len(data) >= 4 and data[1] and data[3]:
                    sources.append({
                        "name": "Wikipedia",
                        "url": data[3][0],
                        "info": data[1][0],
                        "blurb": (data[2][0] if len(data) > 2 and data[2] else "Referensi ensiklopedia")[:160],
                        "logo": "https://www.wikipedia.org/static/favicon/wikipedia.ico",
                    })
    except Exception:
        pass
    return sources


# ============================================================
# AI CHAT
# ============================================================
@app.post("/ai/chat")
async def ai_chat(req: ChatRequest, request: Request):
    raw_messages = req.messages or []
    if not raw_messages:
        raise HTTPException(400, "Pesan kosong")

    # sanitasi pesan (role + content string)
    messages = []
    for m in raw_messages:
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "user").strip().lower()
        if role not in ("user", "assistant", "system"):
            role = "user"
        content = m.get("content")
        if content is None:
            continue
        content = str(content).strip()
        if not content:
            continue
        messages.append({"role": role, "content": content})
    if not messages:
        raise HTTPException(400, "Pesan kosong")
    # jangan kirim system ganda; max 20
    messages = messages[-20:]

    client_ip = request.client.host if request.client else "unknown"
    role_user = detect_role(request)
    usage, limits = get_usage(client_ip, role_user)
    if usage["chat"] >= limits["chat"]:
        raise HTTPException(429, f"Limit chat harian ({limits['chat']}x). Coba lagi besok atau aktifkan Premium.")

    system_id = {
        "role": "system",
        "content": (
            "Kamu asisten AI di aplikasi Nat-Multi Tools. "
            "Jawab jelas, ramah, dan dalam bahasa yang sama dengan user (utamakan Bahasa Indonesia). "
            "Jika tidak yakin, katakan tidak yakin. Jangan mengarang URL sumber."
        ),
    }

    if req.model == "groq":
        key = env("GROQ_API_KEY")
        if not key:
            raise HTTPException(503, "GROQ_API_KEY belum diset di Environment Variables Vercel")
        groq_models = [
            "openai/gpt-oss-20b",
            "openai/gpt-oss-120b",
            "qwen/qwen3.8-27b",
            "llama-3.1-8b-instant",
            "llama-3.3-70b-versatile",
        ]
        payload_msgs = [system_id] + messages
        last_err = ""
        async with httpx.AsyncClient(timeout=75.0) as client:
            for model in groq_models:
                try:
                    r = await client.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json={
                            "model": model,
                            "messages": payload_msgs,
                            "temperature": 0.7,
                            "max_tokens": 2048,
                        },
                    )
                    if r.status_code == 200:
                        data = r.json()
                        reply_text = (
                            ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
                            or ""
                        ).strip()
                        if not reply_text:
                            last_err = "Balasan kosong dari model"
                            continue
                        usage["chat"] += 1
                        sources = []
                        try:
                            sources = await _maybe_sources(messages)
                        except Exception:
                            sources = []
                        return {
                            "reply": reply_text,
                            "model_used": model,
                            "provider": "groq",
                            "sources": sources,
                            "usage_left": limits["chat"] - usage["chat"],
                        }
                    last_err = (r.text or str(r.status_code))[:200]
                except Exception as e:
                    last_err = str(e)[:200]
                    continue
        raise HTTPException(502, f"Groq gagal: {last_err}")

    if req.model in ("gemini", "gemini-flash", "gemini-2.5"):
        key = env("GEMINI_API_KEY")
        if not key:
            raise HTTPException(503, "GEMINI_API_KEY belum diset di Environment Variables Vercel")
        gemini_models = ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.6-flash", "gemini-flash-latest"]
        # Gemini: gabungkan system ke user pertama, role harus bergantian user/model
        contents = []
        for idx, m in enumerate(messages):
            role_g = "user" if m["role"] in ("user", "system") else "model"
            text = m["content"]
            if idx == 0 and m["role"] == "user":
                text = system_id["content"] + "\n\nUser: " + text
            # merge consecutive same roles
            if contents and contents[-1]["role"] == role_g:
                contents[-1]["parts"][0]["text"] += "\n" + text
            else:
                contents.append({"role": role_g, "parts": [{"text": text}]})
        if contents and contents[0]["role"] != "user":
            contents.insert(0, {"role": "user", "parts": [{"text": system_id["content"]}]})
        last_err = ""
        async with httpx.AsyncClient(timeout=75.0) as client:
            for model in gemini_models:
                try:
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
                    r = await client.post(url, json={"contents": contents})
                    if r.status_code == 200:
                        data = r.json()
                        cands = data.get("candidates") or []
                        if not cands:
                            last_err = "Tidak ada kandidat jawaban (mungkin diblok filter)"
                            continue
                        parts = ((cands[0].get("content") or {}).get("parts") or [])
                        reply = "".join(p.get("text", "") for p in parts).strip()
                        if not reply:
                            last_err = "Balasan Gemini kosong"
                            continue
                        usage["chat"] += 1
                        sources = []
                        try:
                            sources = await _maybe_sources(messages)
                        except Exception:
                            sources = []
                        return {
                            "reply": reply,
                            "model_used": model,
                            "provider": "gemini",
                            "sources": sources,
                            "usage_left": limits["chat"] - usage["chat"],
                        }
                    last_err = (r.text or str(r.status_code))[:200]
                except Exception as e:
                    last_err = str(e)[:200]
                    continue
        raise HTTPException(502, f"Gemini error: {last_err}")

    raise HTTPException(400, f"Model tidak dikenal: {req.model}. Pilih groq atau gemini.")


# ============================================================
# AI IMAGE
# ============================================================
@app.post("/ai/image")
async def ai_image(req: ImageRequest, request: Request):
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "Prompt kosong")
    client_ip = request.client.host if request.client else "unknown"
    role = detect_role(request)
    usage, limits = get_usage(client_ip, role)
    if usage["image"] >= limits["image"]:
        raise HTTPException(429, f"Limit image harian ({limits['image']}x)")

    choice = (req.model or "pollinations").lower().strip()
    last_err = ""

    async def do_pollinations():
        safe_prompt = urllib.parse.quote(prompt)
        seed = uuid.uuid4().int % 1000000
        image_url = (
            f"https://image.pollinations.ai/prompt/{safe_prompt}"
            f"?width=1024&height=1024&nologo=true&seed={seed}&model=flux"
        )
        # Pollinations generate on GET — langsung kembalikan URL
        usage["image"] += 1
        return {
            "image_url": image_url,
            "b64": image_url,
            "model_used": "pollinations-flux",
            "provider": "pollinations",
            "usage_left": limits["image"] - usage["image"],
        }

    async def do_gemini():
        nonlocal last_err
        key = env("GEMINI_API_KEY")
        if not key:
            raise HTTPException(503, "GEMINI_API_KEY belum diset")
        # model image terbaru (tanpa 2.5 lawas)
        image_models = [
            "gemini-3.5-flash-image",
            "gemini-3.8-flash-image",
            "gemini-3.5-flash-image-preview",
            "gemini-2.0-flash-preview-image-generation",
        ]
        async with httpx.AsyncClient(timeout=90.0) as client:
            for model in image_models:
                try:
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
                    payload = {
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
                    }
                    r = await client.post(url, json=payload)
                    if r.status_code == 200:
                        data = r.json()
                        parts = (((data.get("candidates") or [{}])[0]).get("content") or {}).get("parts") or []
                        for p in parts:
                            if "inlineData" in p:
                                b64 = p["inlineData"]["data"]
                                mime = p["inlineData"].get("mimeType", "image/png")
                                usage["image"] += 1
                                return {
                                    "image_url": f"data:{mime};base64,{b64}",
                                    "b64": f"data:{mime};base64,{b64}",
                                    "model_used": model,
                                    "provider": "gemini",
                                    "usage_left": limits["image"] - usage["image"],
                                }
                    last_err = r.text[:160]
                except Exception as e:
                    last_err = str(e)[:160]
                    continue
        raise HTTPException(502, f"Gemini image gagal: {last_err}")

    if choice in ("pollinations", "pollination", "flux"):
        try:
            return await do_pollinations()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Pollinations gagal: {e}")

    if choice in ("gemini", "gemini-image", "gemini-3.5", "gemini-3.8"):
        try:
            return await do_gemini()
        except HTTPException as e:
            # fallback Pollinations bila Gemini gagal
            try:
                out = await do_pollinations()
                out["message"] = f"Gemini gagal, pakai Pollinations. ({getattr(e, 'detail', '')})"
                return out
            except Exception:
                raise e

    # default: coba Gemini lalu Pollinations
    if env("GEMINI_API_KEY"):
        try:
            return await do_gemini()
        except Exception as e:
            last_err = str(e)[:160]
    try:
        return await do_pollinations()
    except Exception:
        pass
    raise HTTPException(502, f"Image generation gagal: {last_err}")



# ============================================================
# LIHAT KENYATAAN (Groq)
# ============================================================
class RealityRequest(BaseModel):
    claim: str

class RefinePromptRequest(BaseModel):
    prompt: str

@app.post("/ai/reality")
async def ai_reality(req: RealityRequest, request: Request):
    claim = (req.claim or "").strip()
    if not claim:
        raise HTTPException(400, "Isi klaim / topik yang ingin dicek")
    if len(claim) > 2000:
        raise HTTPException(400, "Teks terlalu panjang")
    client_ip = request.client.host if request.client else "unknown"
    role = detect_role(request)
    usage, limits = get_usage(client_ip, role)
    if usage["chat"] >= limits["chat"]:
        raise HTTPException(429, f"Limit chat harian ({limits['chat']}x)")
    key = env("GROQ_API_KEY")
    if not key:
        raise HTTPException(503, "GROQ_API_KEY belum diset")
    system = (
        "Kamu analis fakta berbahasa Indonesia. User memberi klaim/topik. "
        "Balas HANYA JSON valid tanpa markdown, format: "
        '{"opinion":"penjelasan 2-5 kalimat","real_percent":0-100,"unreal_percent":0-100}. '
        "real_percent + unreal_percent harus = 100. "
        "Nilai real_percent = seberapa besar klaim itu masuk akal / didukung bukti umum. "
        "Jujur, netral, tidak mengarang sumber spesifik yang tidak pasti."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": claim},
    ]
    last_err = ""
    async with httpx.AsyncClient(timeout=60.0) as client:
        for model in ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b", "llama-3.1-8b-instant", "llama-3.3-70b-versatile"]:
            try:
                r = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": model, "messages": messages, "temperature": 0.4, "max_tokens": 800},
                )
                if r.status_code != 200:
                    last_err = r.text[:150]
                    continue
                raw = r.json()["choices"][0]["message"]["content"]
                # parse JSON
                m = re.search(r"\{[\s\S]*\}", raw)
                data = {}
                if m:
                    import json as _json
                    try:
                        data = _json.loads(m.group(0))
                    except Exception:
                        data = {}
                opinion = data.get("opinion") or raw.strip()[:800]
                real_p = int(data.get("real_percent") or data.get("real") or 50)
                unreal_p = int(data.get("unreal_percent") or data.get("unreal") or (100 - real_p))
                real_p = max(0, min(100, real_p))
                unreal_p = max(0, min(100, unreal_p))
                if real_p + unreal_p != 100:
                    unreal_p = 100 - real_p
                usage["chat"] += 1
                return {
                    "opinion": opinion,
                    "real_percent": real_p,
                    "unreal_percent": unreal_p,
                    "model_used": model,
                    "usage_left": limits["chat"] - usage["chat"],
                }
            except Exception as e:
                last_err = str(e)[:150]
                continue
    raise HTTPException(502, f"Reality check gagal: {last_err}")


@app.post("/ai/refine-prompt")
async def ai_refine_prompt(req: RefinePromptRequest, request: Request):
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "Prompt kosong")
    instruction = (
        "Perbaiki dan perluas prompt image generation berikut agar lebih jelas, visual, dan efektif. "
        "Balas HANYA teks prompt yang sudah diperbaiki dalam bahasa Inggris, tanpa penjelasan.\n\n"
        f"Prompt asli: {prompt}"
    )
    last_err = ""

    # 1) Gemini terbaru (tanpa model lawas 2.5)
    key = env("GEMINI_API_KEY")
    if key:
        gemini_models = [
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.8-flash",
            "gemini-3.6-flash",
            "gemini-flash-latest",
        ]
        async with httpx.AsyncClient(timeout=45.0) as client:
            for model in gemini_models:
                try:
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
                    r = await client.post(url, json={"contents": [{"parts": [{"text": instruction}]}]})
                    if r.status_code == 200:
                        text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                        text = re.sub(r"^```[a-z]*\n?", "", text)
                        text = re.sub(r"\n?```$", "", text).strip()
                        if text:
                            return {"refined": text, "model_used": model, "provider": "gemini"}
                    last_err = r.text[:160]
                except Exception as e:
                    last_err = str(e)[:160]
                    continue

    # 2) Groq Llama terbaru
    gkey = env("GROQ_API_KEY")
    if gkey:
        groq_models = [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "openai/gpt-oss-20b",
        ]
        async with httpx.AsyncClient(timeout=45.0) as client:
            for model in groq_models:
                try:
                    r = await client.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {gkey}", "Content-Type": "application/json"},
                        json={
                            "model": model,
                            "messages": [
                                {"role": "system", "content": "You refine image-generation prompts. Reply with ONLY the improved English prompt, no explanation."},
                                {"role": "user", "content": prompt},
                            ],
                            "temperature": 0.7,
                            "max_tokens": 400,
                        },
                    )
                    if r.status_code == 200:
                        text = (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
                        text = re.sub(r"^```[a-z]*\n?", "", text)
                        text = re.sub(r"\n?```$", "", text).strip()
                        if text:
                            return {"refined": text, "model_used": model, "provider": "groq"}
                    last_err = r.text[:160]
                except Exception as e:
                    last_err = str(e)[:160]
                    continue

    # 3) Template lokal
    refined = (
        f"Highly detailed, cinematic lighting, sharp focus, masterpiece, {prompt}, "
        "intricate details, professional photography, 8k resolution"
    )
    return {"refined": refined, "provider": "local-template", "note": last_err[:80] if last_err else ""}


@app.get("/covers/random")
async def covers_random(type: str = "comic", n: int = 8):
    """Sampul acak untuk tarik minat saat user pilih source."""
    n = max(1, min(12, n))
    items = []
    try:
        async with httpx.AsyncClient(timeout=18.0) as client:
            if type in ("comic", "all"):
                r = await client.get(
                    "https://api.mangadex.org/manga",
                    params={"limit": n, "order[followedCount]": "desc", "includes[]": ["cover_art"],
                            "contentRating[]": ["safe", "suggestive"]},
                )
                if r.status_code == 200:
                    for m in (r.json().get("data") or [])[:n]:
                        attrs = m.get("attributes") or {}
                        title = (attrs.get("title") or {}).get("en") or next(iter((attrs.get("title") or {}).values()), "?")
                        cover = ""
                        for rel in m.get("relationships") or []:
                            if rel.get("type") == "cover_art":
                                fn = (rel.get("attributes") or {}).get("fileName") or ""
                                if fn:
                                    cover = f"https://uploads.mangadex.org/covers/{m['id']}/{fn}.256.jpg"
                        if cover:
                            items.append({"title": title, "thumb": cover, "type": "comic"})
            if type in ("anime", "all") and len(items) < n:
                r = await client.get("https://api.jikan.moe/v4/top/anime", params={"limit": n})
                if r.status_code == 200:
                    for a in (r.json().get("data") or [])[:n]:
                        items.append({
                            "title": a.get("title") or "?",
                            "thumb": (a.get("images") or {}).get("jpg", {}).get("image_url") or "",
                            "type": "anime",
                        })
            if type in ("novel", "movie", "drama") and len(items) < n:
                # reuse manga covers as visual filler if no dedicated source
                pass
    except Exception:
        pass
    if not items:
        # placeholder gradient cards
        for i in range(n):
            items.append({"title": f"Koleksi #{i+1}", "thumb": "", "type": type})
    
    return {"items": items[:n]}




# ============================================================
# SAFELINK BYPASS
# ============================================================
@app.get("/safelink/bypass")
async def safelink_bypass(url: str = ""):
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "URL tidak valid")
    original_url = url
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=BROWSER_HEADERS) as client:
            r = await client.get(url)
            html = r.text
            current_url = str(r.url)
            js_patterns = [
                r'window\.location\s*=\s*["\']([^"\']+)["\']',
                r'window\.location\.href\s*=\s*["\']([^"\']+)["\']',
                r'var\s+redirect_url\s*=\s*["\']([^"\']+)["\']',
                r'data-url=["\']([^"\']+)["\']',
            ]
            for pattern in js_patterns:
                match = re.search(pattern, html, re.I)
                if match:
                    target = urllib.parse.unquote(match.group(1))
                    if target.startswith("http"):
                        return {"original": original_url, "final": target, "method": "js_parse", "status": "success"}
            meta_match = re.search(r'<meta[^>]+content=["\']\d+;\s*url=([^"\']+)["\']', html, re.I)
            if meta_match:
                return {"original": original_url, "final": urllib.parse.unquote(meta_match.group(1)), "method": "meta_refresh", "status": "success"}
            if HAS_BS4:
                soup = BeautifulSoup(html, "html.parser")
                domain_asli = re.sub(r'https?://', '', current_url).split('/')[0]
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if href.startswith("http") and domain_asli not in href:
                        if not any(skip in href for skip in ["facebook", "twitter", "google.com/ads"]):
                            return {"original": original_url, "final": href, "method": "html_link", "status": "success"}
            return {"original": original_url, "final": current_url, "method": "http_redirect", "status": "partial"}
    except Exception as e:
        raise HTTPException(502, f"Bypass gagal: {str(e)}")

# ============================================================
# PREMIUM & ADMIN
# ============================================================
def _resolve_code_role(code: str) -> str:
    """Kembalikan 'admin' | 'premium' | ''. Kode hanya dari env Vercel."""
    code = (code or "").strip()
    if not code:
        return ""
    code_u = code.upper()
    # Admin dari ADMIN_CODE / ADMIN_CODES (env)
    if ADMIN_CODE and (code == ADMIN_CODE or code_u == ADMIN_CODE.upper()):
        return "admin"
    if ADMIN_HASH:
        try:
            if hashlib.sha256(code.encode()).hexdigest() == ADMIN_HASH:
                return "admin"
        except Exception:
            pass
    extra_admin = env("ADMIN_CODES", "")
    if extra_admin:
        for c in extra_admin.split(","):
            c = c.strip()
            if c and (code == c or code_u == c.upper()):
                return "admin"
    # Premium dari PREMIUM_CODES (env, pisah koma)
    extra = env("PREMIUM_CODES", "")
    if extra:
        for c in extra.split(","):
            c = c.strip().upper()
            if c and code_u == c:
                return "premium"
    return ""


@app.post("/auth/verify")
@app.post("/premium/verify")
async def auth_verify(request: Request):
    body = await request.json()
    code = (body.get("code") or "").strip()
    role = _resolve_code_role(code)
    if role == "admin":
        # Admin token 30 hari (bisa perpanjang dengan login ulang)
        exp_days = 30
        token = _sign_token("admin", int(time.time()) + exp_days * 86400)
        return {"ok": True, "role": "admin", "message": "Admin aktif", "token": token, "days": exp_days}
    if role == "premium":
        exp_days = 365
        token = _sign_token("premium", int(time.time()) + exp_days * 86400)
        return {"ok": True, "role": "premium", "message": "Premium aktif", "token": token, "days": exp_days}
    raise HTTPException(403, "Kode salah atau tidak dikenali")


@app.post("/admin/verify")
async def admin_verify(request: Request):
    body = await request.json()
    code = (body.get("code") or "").strip()
    if _resolve_code_role(code) == "admin":
        token = _sign_token("admin", int(time.time()) + 30 * 86400)
        return {"ok": True, "role": "admin", "message": "Admin aktif", "token": token, "days": 30}
    raise HTTPException(403, "Kode salah")
