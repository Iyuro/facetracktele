"""
Bot Telegram - fitur:
1. Reverse Image Search (kasih link Google Lens / Yandex / TinEye)
2. Brand / Logo Monitoring (sama seperti di atas, buat cek logo/produk sendiri)
3. Verifikasi Foto / KYC (consent-based, 2 tahap: foto referensi lalu selfie,
   dibandingkan pakai face_recognition)
4. Deteksi indikasi editan/deepfake (heuristik ELA - BUKAN detektor akurat)

Tambahan:
- Bisa kirim gambar via URL, nggak cuma upload foto langsung.
- Rate limiting per user (anti-spam).
- Audit logging ke file (siapa, kapan, ngapain).

CATATAN PENTING:
- Bot ini TIDAK melakukan scraping otomatis ke sosial media.
- Reverse image search hanya memberi LINK yang dibuka manual oleh user.
- Fitur KYC hanya boleh dipakai untuk foto milik pengguna sendiri (consent).
- Fitur deepfake detector adalah heuristik kasar, bukan pengganti tools profesional
  (Hive Moderation, Sensity, Reality Defender, dll) untuk kebutuhan serius/hukum.
"""

import os
import re
import io
import time
import hashlib
import logging
import tempfile
from datetime import datetime
from collections import defaultdict, deque

import numpy as np
import requests
from PIL import Image, ImageChops, ImageEnhance
from PIL.ExifTags import TAGS, GPSTAGS, IFD
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

try:
    import face_recognition
    FACE_RECOGNITION_AVAILABLE = True
except ImportError:
    FACE_RECOGNITION_AVAILABLE = False

# ------------------------------------------------------------------
# KONFIGURASI
# ------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "GANTI_DENGAN_TOKEN_BOT_LU")
BASE_DIR = os.path.dirname(__file__)
KYC_DIR = os.path.join(BASE_DIR, "kyc_data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(KYC_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

RATE_LIMIT_MAX = int(os.environ.get("RATE_LIMIT_MAX", "5"))       # max request
RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))  # detik
FACE_MATCH_TOLERANCE = float(os.environ.get("FACE_MATCH_TOLERANCE", "0.6"))

URL_REGEX = re.compile(r"^https?://\S+$", re.IGNORECASE)

# ------------------------------------------------------------------
# LOGGING (console + audit trail file)
# ------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("bot")

audit_logger = logging.getLogger("audit")
audit_logger.setLevel(logging.INFO)
audit_handler = logging.FileHandler(os.path.join(LOG_DIR, "audit.log"), encoding="utf-8")
audit_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
audit_logger.addHandler(audit_handler)
audit_logger.propagate = False


def audit_log(user_id: int, username: str, action: str, detail: str = ""):
    audit_logger.info(f"user_id={user_id} username={username!r} action={action} detail={detail}")


# ------------------------------------------------------------------
# RATE LIMITING SEDERHANA (in-memory, per user)
# ------------------------------------------------------------------
_request_log = defaultdict(deque)  # user_id -> deque[timestamp]


def is_rate_limited(user_id: int) -> bool:
    now = time.time()
    dq = _request_log[user_id]
    while dq and now - dq[0] > RATE_LIMIT_WINDOW:
        dq.popleft()
    if len(dq) >= RATE_LIMIT_MAX:
        return True
    dq.append(now)
    return False


# ------------------------------------------------------------------
# MENU
# ------------------------------------------------------------------
MENU = [
    [InlineKeyboardButton("🔍 Reverse Image Search", callback_data="mode_reverse")],
    [InlineKeyboardButton("™️ Brand / Logo Monitoring", callback_data="mode_brand")],
    [InlineKeyboardButton("🪪 Verifikasi Foto (KYC)", callback_data="mode_kyc")],
    [InlineKeyboardButton("🕵️ Deteksi Indikasi Editan/Deepfake", callback_data="mode_deepfake")],
    [InlineKeyboardButton("🧾 Detail Foto (EXIF/Metadata Lengkap)", callback_data="mode_metadata")],
    [InlineKeyboardButton("🔤 OCR — Baca Teks di Foto", callback_data="mode_ocr")],
    [InlineKeyboardButton("🕵️‍♂️ Deteksi Steganografi", callback_data="mode_stego")],
    [InlineKeyboardButton("🎨 Analisis Warna & Kualitas Gambar", callback_data="mode_visual")],
    [InlineKeyboardButton("🔁 Cek Foto Pernah Dikirim? (non-permanen)", callback_data="mode_duplicate")],
    [InlineKeyboardButton("🚗 Cek Pajak Kendaraan (link resmi)", callback_data="mode_pajak")],
]

WELCOME_TEXT = (
    "Halo! Bot ini bantu beberapa hal, semuanya berbasis persetujuan (consent) — "
    "bukan buat nyari/ngintip orang lain diam-diam:\n\n"
    "1️⃣ Reverse Image Search — kasih link ke Google Lens / Yandex / TinEye\n"
    "2️⃣ Brand/Logo Monitoring — sama seperti di atas, buat mantau logo/produk kamu sendiri\n"
    "3️⃣ Verifikasi Foto (KYC) — kirim foto referensi + selfie sendiri, dibandingkan otomatis\n"
    "4️⃣ Deteksi Indikasi Editan/Deepfake — analisa kasar (ELA), bukan detektor akurat\n"
    "5️⃣ Detail Foto (EXIF/Metadata) — data kamera/HP, GPS (kalau ada), hash file, "
    "indikasi editan & indikasi AI-generated\n"
    "6️⃣ OCR — baca teks yang ada di dalam foto\n"
    "7️⃣ Deteksi Steganografi — cek ada data/file tersembunyi yang \"ditempel\" di gambar\n"
    "8️⃣ Analisis Warna & Kualitas — dominant color, brightness/contrast, blur score, estimasi kualitas JPEG\n"
    "9️⃣ Cek Foto Pernah Dikirim? — cocokin hash ke foto yang pernah masuk bot ini "
    "(data di-reset tiap bot restart, TIDAK disimpan permanen)\n"
    "🔟 Cek Pajak Kendaraan — OCR plat nomor, terus kasih LINK RESMI (app SIGNAL "
    "& e-Samsat provinsi) buat cek pajak kendaraan MILIK SENDIRI. Bot ini nggak "
    "nampilin data pemilik siapapun, cuma nunjukin ke mana harus cek.\n\n"
    "Kamu bisa kirim FOTO langsung, kirim sebagai FILE/DOKUMEN, atau LINK gambar (http/https).\n"
    "Pilih menu di bawah dulu ya."
)

MODE_PROMPTS = {
    "mode_reverse": (
        "Mode *Reverse Image Search* aktif.\n"
        "Kirim foto atau link gambar yang mau dicari sumbernya."
    ),
    "mode_brand": (
        "Mode *Brand/Logo Monitoring* aktif.\n"
        "Kirim foto atau link logo/produk kamu."
    ),
    "mode_kyc": (
        "Mode *Verifikasi Foto (KYC)* aktif.\n"
        "⚠️ Dengan mengirim foto, kamu menyetujui foto ini disimpan sementara "
        "di server untuk keperluan verifikasi identitas kamu sendiri. "
        "Jangan kirim foto orang lain tanpa izin mereka.\n\n"
        "Langkah 1: kirim foto *referensi* (misal foto KTP/ID) dulu."
    ),
    "mode_deepfake": (
        "Mode *Deteksi Indikasi Editan/Deepfake* aktif.\n"
        "Kirim foto atau link gambar yang mau dianalisa."
    ),
    "mode_metadata": (
        "Mode *Detail Foto (EXIF/Metadata Lengkap)* aktif.\n"
        "Bakal ditampilin: info file (nama, ukuran, dimensi, hash MD5/SHA256), "
        "EXIF kamera/HP (merek, model, software), GPS (kalau ada + nama lokasinya), "
        "indikasi editan, dan indikasi AI-generated.\n\n"
        "⚠️ Penting: kalau kirim lewat menu *Photo* biasa, Telegram otomatis "
        "kompres & MENGHAPUS EXIF-nya duluan. Buat metadata original yang utuh, "
        "kirim gambarnya sebagai *File/Dokumen* (klik 📎 → File, bukan galeri foto "
        "biasa), atau kirim link langsung ke gambarnya."
    ),
    "mode_ocr": (
        "Mode *OCR - Baca Teks di Foto* aktif.\n"
        "Kirim foto atau link gambar yang ada tulisannya (dokumen, plat nomor, "
        "papan nama, screenshot, dll), nanti teksnya diekstrak otomatis."
    ),
    "mode_stego": (
        "Mode *Deteksi Steganografi* aktif.\n"
        "Kirim foto (lebih akurat kalau dikirim sebagai *File/Dokumen*, bukan Photo "
        "terkompresi) atau link gambar. Bot bakal cek apakah ada data/file yang "
        "\"ditempel\" tersembunyi di file gambar itu."
    ),
    "mode_visual": (
        "Mode *Analisis Warna & Kualitas Gambar* aktif.\n"
        "Kirim foto atau link gambar. Bot bakal kasih dominant color, brightness/"
        "contrast, skor blur/ketajaman, dan estimasi kualitas kompresi JPEG."
    ),
    "mode_duplicate": (
        "Mode *Cek Foto Pernah Dikirim?* aktif.\n"
        "Kirim foto atau link gambar. Bot bakal cek hash-nya ke daftar foto yang "
        "pernah masuk ke bot ini.\n\n"
        "⚠️ Catatan: daftar ini CUMA disimpan di memory selama bot nyala — "
        "otomatis kosong lagi tiap kali bot di-restart/redeploy. Nggak ada "
        "penyimpanan permanen ke disk/database."
    ),
    "mode_pajak": (
        "Mode *Cek Pajak Kendaraan* aktif.\n"
        "Kirim foto plat nomor kendaraan kamu sendiri (atau link gambar). Bot "
        "bakal OCR platnya, tebak provinsi asal registrasinya dari kode wilayah, "
        "terus kasih LINK RESMI (app SIGNAL & website e-Samsat provinsi terkait) "
        "buat kamu cek/bayar pajaknya sendiri.\n\n"
        "⚠️ Bot ini TIDAK mengakses database Samsat/Polri dan TIDAK menampilkan "
        "data pemilik, alamat, atau data pribadi apapun — cuma nunjukin ke mana "
        "kamu harus cek, sesuai channel resmi."
    ),
}


async def start(update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(WELCOME_TEXT, reply_markup=InlineKeyboardMarkup(MENU))


async def menu_callback(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode = query.data
    context.user_data.clear()
    context.user_data["mode"] = mode
    if mode == "mode_kyc":
        context.user_data["kyc_step"] = "reference"
    text = MODE_PROMPTS.get(mode, "Mode nggak dikenal, ketik /start ulang.")
    user = query.from_user
    audit_log(user.id, user.username, "select_mode", mode)
    await query.edit_message_text(text, parse_mode="Markdown")


# ------------------------------------------------------------------
# UTIL: UPLOAD & DOWNLOAD GAMBAR
# ------------------------------------------------------------------
def _upload_to_0x0(file_path: str) -> str | None:
    """Upload ke 0x0.st (anonim, tanpa API key)."""
    try:
        headers = {"User-Agent": "TelegramFaceBot/1.0"}
        with open(file_path, "rb") as f:
            resp = requests.post(
                "https://0x0.st",
                files={"file": f},
                headers=headers,
                timeout=30,
            )
        if resp.status_code == 200 and resp.text.strip().startswith("http"):
            return resp.text.strip()
        logger.warning(f"Upload 0x0.st gagal, response: {resp.text!r}")
    except Exception as e:
        logger.error(f"Upload 0x0.st error: {e}")
    return None


def _upload_to_catbox(file_path: str) -> str | None:
    """Upload ke catbox.moe (anonim, tanpa API key)."""
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload"},
                files={"fileToUpload": f},
                timeout=30,
            )
        if resp.status_code == 200 and resp.text.strip().startswith("http"):
            return resp.text.strip()
        logger.warning(f"Upload catbox gagal, response: {resp.text!r}")
    except Exception as e:
        logger.error(f"Upload catbox error: {e}")
    return None


def _upload_to_uguu(file_path: str) -> str | None:
    """Upload ke uguu.se (anonim, tanpa API key, file disimpan sementara ~48 jam)."""
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                "https://uguu.se/upload.php",
                files={"files[]": f},
                timeout=30,
            )
        data = resp.json()
        if data.get("success") and data.get("files"):
            return data["files"][0]["url"]
        logger.warning(f"Upload uguu.se gagal, response: {resp.text!r}")
    except Exception as e:
        logger.error(f"Upload uguu.se error: {e}")
    return None


def _upload_to_tmpfiles(file_path: str) -> str | None:
    """Upload ke tmpfiles.org (anonim, tanpa API key, file disimpan sementara ~1 jam)."""
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                "https://tmpfiles.org/api/v1/upload",
                files={"file": f},
                timeout=30,
            )
        data = resp.json()
        url = data.get("data", {}).get("url")
        if url:
            # Endpoint biasa nampilin halaman preview, butuh "/dl/" biar jadi direct link
            return url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)
        logger.warning(f"Upload tmpfiles.org gagal, response: {resp.text!r}")
    except Exception as e:
        logger.error(f"Upload tmpfiles.org error: {e}")
    return None


def upload_to_public_host(file_path: str) -> str | None:
    """
    Coba beberapa image host publik secara berurutan (fallback chain),
    supaya kalau satu host lagi down/bermasalah, bot tetap bisa jalan
    pakai host lain tanpa perlu ubah kode.

    Urutan: 0x0.st -> catbox.moe -> uguu.se -> tmpfiles.org
    (dua yang terakhir ditambahin karena 0x0.st & catbox.moe sempat
    nolak semua upload gara-gara masalah di sisi mereka sendiri, bukan
    error di bot ini).
    """
    providers = [_upload_to_0x0, _upload_to_catbox, _upload_to_uguu, _upload_to_tmpfiles]
    for provider in providers:
        result = provider(file_path)
        if result:
            return result
    return None


def download_image_from_url(url: str) -> str | None:
    """Download gambar dari link user ke file temporary lokal."""
    try:
        resp = requests.get(url, timeout=20, stream=True)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "image" not in content_type:
            # Tetap coba proses, siapa tahu servernya nggak kasih header lengkap
            logger.warning(f"Content-Type bukan image: {content_type}")
        fd, local_path = tempfile.mkstemp(suffix=".jpg")
        with os.fdopen(fd, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        # Validasi ini benar-benar gambar yang bisa dibuka
        Image.open(local_path).verify()
        return local_path
    except Exception as e:
        logger.error(f"Gagal download gambar dari url: {e}")
        return None


def build_reverse_search_links(image_url: str) -> str:
    google_url = f"https://lens.google.com/uploadbyurl?url={image_url}"
    yandex_url = f"https://yandex.com/images/search?rpt=imageview&url={image_url}"
    tineye_url = f"https://www.tineye.com/search?url={image_url}"
    return (
        "Klik link di bawah buat lihat hasilnya (dibuka manual, bot nggak "
        "menampilkan hasil sosmed orang secara otomatis):\n\n"
        f"🔎 Google Lens:\n{google_url}\n\n"
        f"🔎 Yandex Images:\n{yandex_url}\n\n"
        f"🔎 TinEye:\n{tineye_url}"
    )


# ------------------------------------------------------------------
# DEEPFAKE HEURISTIK (ELA)
# ------------------------------------------------------------------
def ela_analyze(image_path: str, quality: int = 90) -> dict:
    """
    Error Level Analysis sederhana.
    HEURISTIK KASAR — false positive/negative sangat mungkin, terutama
    untuk foto hasil AI generatif modern yang sudah halus.
    """
    original = Image.open(image_path).convert("RGB")
    tmp_path = image_path + "_resaved.jpg"
    original.save(tmp_path, "JPEG", quality=quality)
    resaved = Image.open(tmp_path)

    ela_image = ImageChops.difference(original, resaved)
    extrema = ela_image.getextrema()
    max_diff = max(ex[1] for ex in extrema)
    if max_diff == 0:
        max_diff = 1
    scale = 255.0 / max_diff
    ela_image = ImageEnhance.Brightness(ela_image).enhance(scale)

    arr = np.asarray(ela_image).astype("float32")
    mean_diff = float(arr.mean())
    os.remove(tmp_path)

    if mean_diff < 2:
        verdict = "Skor rendah — belum ada indikasi editan berat."
    elif mean_diff < 6:
        verdict = "Skor sedang — ada indikasi editan ringan, belum tentu deepfake."
    else:
        verdict = "Skor tinggi — indikasi editan/kompresi tidak konsisten, cek lebih lanjut."

    return {"mean_diff": round(mean_diff, 2), "verdict": verdict}


# ------------------------------------------------------------------
# DETAIL FOTO: FILE INFO, HASH, EXIF, GPS, INDIKASI EDITAN & AI
# ------------------------------------------------------------------
SOFTWARE_EDITOR_KEYWORDS = [
    "photoshop", "lightroom", "gimp", "snapseed", "picsart", "facetune",
    "canva", "capture one", "affinity photo", "luminar", "pixlr", "vsco",
    "polarr", "picmonkey", "paint.net", "inpixio", "photoscape",
]

AI_GENERATOR_KEYWORDS = [
    "midjourney", "dall-e", "dalle", "stable diffusion", "stablediffusion",
    "adobe firefly", "firefly", "novelai", "leonardo.ai", "leonardo ai",
    "playground ai", "ideogram", "runway", "bing image creator",
    "designer.microsoft", "meta ai", "grok imagine", "flux.1", "flux1",
    "comfyui", "automatic1111", "invokeai",
]


def human_readable_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def compute_hashes(file_path: str) -> dict:
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            md5.update(chunk)
            sha256.update(chunk)
    return {"md5": md5.hexdigest(), "sha256": sha256.hexdigest()}


def _dms_to_decimal(dms, ref) -> float | None:
    try:
        degrees, minutes, seconds = dms
        value = float(degrees) + float(minutes) / 60 + float(seconds) / 3600
        if ref in ("S", "W"):
            value = -value
        return round(value, 6)
    except Exception:
        return None


def extract_exif(image: Image.Image) -> dict:
    """
    Ambil EXIF utama (IFD0) + sub-IFD Exif (ISO, lensa, dll) + GPS.
    Return dict siap-tampil: {"tags": {...}, "gps": {"lat":..,"lon":..} atau None,
    "software": str|None, "datetime_original": str|None, "datetime_modified": str|None}
    """
    result = {"tags": {}, "gps": None, "software": None,
              "datetime_original": None, "datetime_modified": None}
    try:
        exif = image.getexif()
        if not exif:
            return result

        # IFD0 (tag umum: Make, Model, Software, Orientation, DateTime, Artist)
        for tag_id, value in exif.items():
            name = TAGS.get(tag_id, str(tag_id))
            if isinstance(value, bytes):
                try:
                    value = value.decode(errors="ignore").strip("\x00").strip()
                except Exception:
                    continue
            result["tags"][name] = value

        # Sub-IFD Exif (ISO, FNumber, ExposureTime, LensModel, DateTimeOriginal, dll)
        try:
            exif_ifd = exif.get_ifd(IFD.Exif)
            for tag_id, value in exif_ifd.items():
                name = TAGS.get(tag_id, str(tag_id))
                if isinstance(value, bytes):
                    try:
                        value = value.decode(errors="ignore").strip("\x00").strip()
                    except Exception:
                        continue
                result["tags"][name] = value
        except Exception:
            pass

        # GPS IFD
        try:
            gps_ifd = exif.get_ifd(IFD.GPSInfo)
            if gps_ifd:
                gps_named = {GPSTAGS.get(k, k): v for k, v in gps_ifd.items()}
                lat = gps_named.get("GPSLatitude")
                lat_ref = gps_named.get("GPSLatitudeRef", "N")
                lon = gps_named.get("GPSLongitude")
                lon_ref = gps_named.get("GPSLongitudeRef", "E")
                if lat and lon:
                    lat_dec = _dms_to_decimal(lat, lat_ref)
                    lon_dec = _dms_to_decimal(lon, lon_ref)
                    if lat_dec is not None and lon_dec is not None:
                        result["gps"] = {"lat": lat_dec, "lon": lon_dec}
        except Exception:
            pass

        result["software"] = result["tags"].get("Software")
        result["datetime_original"] = result["tags"].get("DateTimeOriginal") or result["tags"].get("DateTimeDigitized")
        result["datetime_modified"] = result["tags"].get("DateTime")
    except Exception as e:
        logger.warning(f"Gagal baca EXIF: {e}")
    return result


def reverse_geocode(lat: float, lon: float) -> str | None:
    """Ubah koordinat GPS jadi nama lokasi pakai OpenStreetMap Nominatim (gratis, tanpa API key)."""
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 16},
            headers={"User-Agent": "TelegramFotoDetailBot/1.0"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("display_name")
    except Exception as e:
        logger.warning(f"Reverse geocode gagal: {e}")
    return None


def detect_editing_indicators(exif_info: dict, ela_result: dict) -> dict:
    """
    Gabungan beberapa sinyal indikasi editan (bukan bukti mutlak):
    - Software tag cocok dengan editor foto yang dikenal
    - Tanggal capture vs tanggal modifikasi beda jauh
    - Skor ELA (reuse fungsi yang udah ada)
    """
    flags = []

    software = (exif_info.get("software") or "")
    matched_editor = next((kw for kw in SOFTWARE_EDITOR_KEYWORDS if kw in software.lower()), None)
    if matched_editor:
        flags.append(f"Tag 'Software' EXIF nyebut aplikasi edit: {software!r}")

    dt_orig = exif_info.get("datetime_original")
    dt_mod = exif_info.get("datetime_modified")
    if dt_orig and dt_mod and dt_orig != dt_mod:
        try:
            fmt = "%Y:%m:%d %H:%M:%S"
            t1 = datetime.strptime(dt_orig, fmt)
            t2 = datetime.strptime(dt_mod, fmt)
            if abs((t2 - t1).total_seconds()) > 60:
                flags.append(
                    f"Waktu pengambilan ({dt_orig}) beda dengan waktu file terakhir "
                    f"dimodifikasi ({dt_mod}) — file sempat disave ulang setelah dipotret."
                )
        except Exception:
            flags.append(f"Tanggal EXIF ada tapi format nggak konsisten ({dt_orig} vs {dt_mod}).")

    if ela_result["mean_diff"] >= 6:
        flags.append(f"Skor ELA tinggi ({ela_result['mean_diff']}) — kompresi tidak konsisten antar area gambar.")

    if not flags:
        verdict = "Belum ketemu indikasi editan dari metadata & ELA. Bukan jaminan foto orisinil 100%."
    else:
        verdict = "Ada indikasi foto pernah diedit/disave ulang. Lihat detail di bawah."

    return {"flags": flags, "verdict": verdict}


def detect_ai_generated(image: Image.Image, exif_info: dict) -> dict:
    """
    Heuristik deteksi AI-generated. TIDAK definitif — banyak tool AI modern
    sudah nggak nyimpen metadata generator sama sekali, dan foto asli kadang
    kehilangan EXIF gara-gara re-save/screenshot. Bukan pengganti detektor
    khusus (misal Hive Moderation / Illuminarty / SynthID check dari Google).
    """
    flags = []
    strong_signal = False

    # 1. Cek text chunk PNG (Stable Diffusion WebUI simpen di key "parameters",
    #    ComfyUI simpen di "prompt"/"workflow" sebagai JSON)
    info = getattr(image, "info", {}) or {}
    for key in ("parameters", "prompt", "workflow", "Comment", "Description", "Software"):
        val = info.get(key)
        if val:
            val_str = str(val)
            if key in ("parameters", "prompt", "workflow"):
                flags.append(f"Ketemu metadata generator AI di field '{key}' (khas Stable Diffusion/ComfyUI).")
                strong_signal = True
            else:
                low = val_str.lower()
                matched = next((kw for kw in AI_GENERATOR_KEYWORDS if kw in low), None)
                if matched:
                    flags.append(f"Field '{key}' nyebut tool AI: {matched}")
                    strong_signal = True

    # 2. Cek Software/tag EXIF nyebut generator AI
    software = (exif_info.get("software") or "")
    low_sw = software.lower()
    matched = next((kw for kw in AI_GENERATOR_KEYWORDS if kw in low_sw), None)
    if matched:
        flags.append(f"Tag EXIF 'Software' nyebut tool AI: {matched}")
        strong_signal = True

    # 3. Heuristik lemah: PNG, tanpa Make/Model kamera sama sekali, dimensi kelipatan 64
    #    (khas resolusi output model image-gen). Sinyal lemah, gampang salah.
    if not strong_signal:
        has_camera_info = bool(exif_info["tags"].get("Make") or exif_info["tags"].get("Model"))
        w, h = image.size
        dims_typical = (w % 64 == 0 and h % 64 == 0 and 256 <= w <= 2048 and 256 <= h <= 2048)
        if image.format == "PNG" and not has_camera_info and dims_typical:
            flags.append(
                f"Sinyal lemah: format PNG, nggak ada info kamera (Make/Model), "
                f"dan dimensi {w}x{h} kelipatan 64 (khas output model image-gen). "
                "INI CUMA KEMUNGKINAN, banyak screenshot/desain non-AI juga begini."
            )

    if strong_signal:
        verdict = "🔴 Indikasi KUAT gambar ini AI-generated (ada metadata generator eksplisit)."
    elif flags:
        verdict = "🟡 Ada sinyal LEMAH kemungkinan AI-generated, tapi jauh dari pasti."
    else:
        verdict = "🟢 Nggak ketemu indikasi AI-generated dari metadata yang tersedia (bukan jaminan foto ini asli)."

    return {"flags": flags, "verdict": verdict, "strong_signal": strong_signal}


def analyze_photo_metadata(local_path: str, display_filename: str, reported_size: int | None) -> dict:
    """Kumpulin semua hasil analisa jadi satu dict buat ditampilin ke user."""
    file_size = reported_size if reported_size else os.path.getsize(local_path)
    image = Image.open(local_path)
    width, height = image.size

    hashes = compute_hashes(local_path)
    exif_info = extract_exif(image)

    location_name = None
    if exif_info["gps"]:
        location_name = reverse_geocode(exif_info["gps"]["lat"], exif_info["gps"]["lon"])

    ela_result = ela_analyze(local_path)
    editing = detect_editing_indicators(exif_info, ela_result)
    ai_result = detect_ai_generated(image, exif_info)

    return {
        "file_info": {
            "name": display_filename,
            "size": file_size,
            "format": image.format,
            "dimensions": f"{width}x{height}",
            "md5": hashes["md5"],
            "sha256": hashes["sha256"],
        },
        "exif": exif_info,
        "location_name": location_name,
        "editing": editing,
        "ela": ela_result,
        "ai": ai_result,
    }


def format_metadata_report(result: dict) -> list[str]:
    """Susun hasil analisa jadi beberapa pesan (biar nggak kepotong limit Telegram)."""
    fi = result["file_info"]
    exif = result["exif"]

    msg1 = (
        "🧾 *INFO FILE*\n"
        f"Nama: `{fi['name']}`\n"
        f"Ukuran: {human_readable_size(fi['size'])} ({fi['size']} bytes)\n"
        f"Format: {fi['format']}\n"
        f"Dimensi: {fi['dimensions']} px\n"
        f"MD5: `{fi['md5']}`\n"
        f"SHA256: `{fi['sha256']}`"
    )

    interesting_tags = [
        "Make", "Model", "Software", "LensModel", "DateTimeOriginal", "DateTime",
        "FNumber", "ExposureTime", "ISOSpeedRatings", "FocalLength", "Flash",
        "Orientation", "Artist", "ImageDescription", "Copyright",
    ]
    tag_lines = [f"{k}: {exif['tags'][k]}" for k in interesting_tags if k in exif["tags"]]
    if tag_lines:
        msg2 = "📷 *EXIF KAMERA/DEVICE*\n" + "\n".join(tag_lines)
    else:
        msg2 = (
            "📷 *EXIF KAMERA/DEVICE*\n"
            "Nggak ada data EXIF (kemungkinan dikirim sebagai Photo terkompresi "
            "Telegram, atau memang sudah dihapus/di-strip sebelumnya)."
        )

    if exif["gps"]:
        lat, lon = exif["gps"]["lat"], exif["gps"]["lon"]
        msg2 += f"\n\n📍 *GPS*: {lat}, {lon}"
        if result["location_name"]:
            msg2 += f"\nLokasi: {result['location_name']}"
        msg2 += f"\nMaps: https://www.google.com/maps?q={lat},{lon}"
    else:
        msg2 += "\n\n📍 *GPS*: tidak ada data lokasi di metadata."

    editing = result["editing"]
    msg3 = "🕵️ *INDIKASI EDITAN*\n" + editing["verdict"]
    if editing["flags"]:
        msg3 += "\n" + "\n".join(f"• {f}" for f in editing["flags"])
    msg3 += f"\n\nSkor ELA: {result['ela']['mean_diff']}"

    ai = result["ai"]
    msg4 = "🤖 *INDIKASI AI-GENERATED*\n" + ai["verdict"]
    if ai["flags"]:
        msg4 += "\n" + "\n".join(f"• {f}" for f in ai["flags"])
    msg4 += (
        "\n\n⚠️ Semua indikasi di atas heuristik, bukan bukti forensik pasti. "
        "Metadata gampang dihapus/dipalsu, dan makin banyak tool AI/editor yang "
        "nggak nyimpen jejak sama sekali."
    )

    return [msg1, msg2, msg3, msg4]


# ------------------------------------------------------------------
# FITUR TAMBAHAN 1: OCR (BACA TEKS DI FOTO)
# ------------------------------------------------------------------
try:
    import pytesseract
    PYTESSERACT_AVAILABLE = True
except ImportError:
    PYTESSERACT_AVAILABLE = False


def run_ocr(image_path: str) -> dict:
    if not PYTESSERACT_AVAILABLE:
        return {"error": "Library pytesseract belum terinstall di server."}
    try:
        image = Image.open(image_path)
        try:
            text = pytesseract.image_to_string(image, lang="ind+eng")
        except pytesseract.TesseractError:
            # fallback kalau language pack "ind" belum ke-install di server
            text = pytesseract.image_to_string(image, lang="eng")
        return {"text": text.strip()}
    except Exception as e:
        logger.error(f"OCR gagal: {e}")
        return {"error": f"Gagal OCR: {e}"}


# ------------------------------------------------------------------
# FITUR TAMBAHAN 2: DETEKSI STEGANOGRAFI
# ------------------------------------------------------------------
TRAILING_DATA_SIGNATURES = {
    b"PK\x03\x04": "ZIP archive",
    b"Rar!\x1a\x07": "RAR archive",
    b"%PDF": "PDF document",
    b"\x1f\x8b": "GZIP archive",
    b"7z\xbc\xaf\x27\x1c": "7-Zip archive",
    b"GIF89a": "GIF image",
    b"GIF87a": "GIF image",
    b"\x89PNG\r\n\x1a\n": "PNG image",
    b"\xff\xd8\xff": "JPEG image",
}


def detect_trailing_data(file_path: str) -> dict:
    """
    Cek ada data nyelip SETELAH marker akhir file (JPEG EOI / PNG IEND).
    Ini teknik steganografi paling umum buat 'nempelin' file lain (zip,
    dokumen, dll) di belakang byte terakhir gambar — gambar tetep kebuka
    normal di viewer manapun, tapi ada file lain nebeng di baliknya.
    """
    with open(file_path, "rb") as f:
        data = f.read()

    trailing = None
    if data[:2] == b"\xff\xd8":  # JPEG
        eoi = data.rfind(b"\xff\xd9")
        if eoi != -1 and eoi + 2 < len(data):
            trailing = data[eoi + 2:]
    elif data[:8] == b"\x89PNG\r\n\x1a\n":  # PNG
        iend = data.rfind(b"IEND")
        if iend != -1 and iend + 8 < len(data):  # IEND chunk + 4 byte CRC
            trailing = data[iend + 8:]

    if not trailing or len(trailing) < 4:
        return {"found": False}

    matched_type = next(
        (label for sig, label in TRAILING_DATA_SIGNATURES.items() if trailing.startswith(sig)),
        None,
    )
    return {
        "found": True,
        "size": len(trailing),
        "type_guess": matched_type or "tidak dikenali (data mentah/terenkripsi?)",
    }


def lsb_chi_square_heuristic(image_path: str) -> dict:
    """
    Heuristik chi-square sederhana buat curiga LSB steganography (pasangan
    nilai piksel (2i, 2i+1) yang kelewat 'rata' khas hasil LSB replacement).
    HEURISTIK LEMAH — kompresi/editan berat juga bisa kasih hasil serupa.
    """
    try:
        img = Image.open(image_path).convert("L")
        arr = np.asarray(img).flatten()
        hist = np.bincount(arr, minlength=256).astype(float)

        chi_sq = 0.0
        pairs = 0
        for i in range(0, 256, 2):
            observed_even, observed_odd = hist[i], hist[i + 1]
            expected = (observed_even + observed_odd) / 2
            if expected > 0:
                chi_sq += ((observed_even - expected) ** 2) / expected
                chi_sq += ((observed_odd - expected) ** 2) / expected
                pairs += 1

        avg_chi = chi_sq / pairs if pairs else 0
        return {"avg_chi_square": round(avg_chi, 3), "suspicious": avg_chi < 1.5}
    except Exception as e:
        logger.warning(f"LSB heuristic gagal: {e}")
        return {"avg_chi_square": None, "suspicious": False}


def analyze_steganography(image_path: str) -> dict:
    return {
        "trailing": detect_trailing_data(image_path),
        "lsb": lsb_chi_square_heuristic(image_path),
    }


def format_stego_report(result: dict) -> str:
    trailing, lsb = result["trailing"], result["lsb"]
    lines = ["🕵️‍♂️ *DETEKSI STEGANOGRAFI*\n"]

    if trailing["found"]:
        lines.append(
            "🔴 KETEMU data nyelip setelah akhir file gambar!\n"
            f"Ukuran data tambahan: {human_readable_size(trailing['size'])}\n"
            f"Kemungkinan tipe: {trailing['type_guess']}\n"
            "Ini indikasi KUAT ada file/data yang sengaja ditempel di balik gambar ini."
        )
    else:
        lines.append("🟢 Nggak ada data mencurigakan yang nyelip setelah akhir file gambar.")

    if lsb["avg_chi_square"] is not None:
        lines.append("")
        verdict = (
            "🟡 Pola bit terakhir piksel (LSB) kelihatan mencurigakan, ada "
            "kemungkinan LSB steganography." if lsb["suspicious"] else
            "🟢 Pola bit terakhir piksel (LSB) masih wajar seperti foto normal."
        )
        lines.append(f"{verdict}\n(skor chi-square: {lsb['avg_chi_square']})")

    lines.append(
        "\n⚠️ Cek 'data nyelip setelah EOF' cukup reliable, tapi cek LSB gampang "
        "false positive/negative — bukan pengganti tools forensik khusus "
        "(StegExpose, zsteg, binwalk, dll)."
    )
    return "\n".join(lines)


# ------------------------------------------------------------------
# FITUR TAMBAHAN 3: ANALISIS WARNA & KUALITAS GAMBAR
# ------------------------------------------------------------------
def analyze_visual_quality(image_path: str) -> dict:
    image = Image.open(image_path).convert("RGB")
    thumb = image.copy()
    thumb.thumbnail((400, 400))

    # Dominant colors
    quantized = thumb.quantize(colors=5, method=Image.MEDIANCUT)
    palette = quantized.getpalette()
    color_counts = sorted(quantized.getcolors(), reverse=True)
    total_px = sum(c for c, _ in color_counts)
    dominant_colors = []
    for count, idx in color_counts[:5]:
        r, g, b = palette[idx * 3: idx * 3 + 3]
        dominant_colors.append({"hex": f"#{r:02x}{g:02x}{b:02x}", "pct": round(count / total_px * 100, 1)})

    # Brightness & contrast
    gray = np.asarray(thumb.convert("L")).astype("float32")
    brightness, contrast = float(gray.mean()), float(gray.std())

    # Blur/sharpness (variance of Laplacian, konvolusi manual pakai numpy)
    padded = np.pad(gray, 1, mode="edge")
    laplacian = (
        padded[0:-2, 1:-1] + padded[1:-1, 0:-2] + padded[1:-1, 2:] + padded[2:, 1:-1]
        - 4 * padded[1:-1, 1:-1]
    )
    sharpness = float(laplacian.var())
    if sharpness < 50:
        blur_verdict = "Kelihatan blur/kurang fokus."
    elif sharpness < 150:
        blur_verdict = "Ketajaman sedang."
    else:
        blur_verdict = "Gambar tajam/fokus bagus."

    # Estimasi kualitas JPEG dari quantization table (kasar)
    quality_estimate = None
    try:
        orig = Image.open(image_path)
        luma_table = getattr(orig, "quantization", {}).get(0) if hasattr(orig, "quantization") else None
        if luma_table:
            avg_q = sum(luma_table) / len(luma_table)
            quality_estimate = max(1, min(100, round(100 - avg_q)))
    except Exception:
        pass

    return {
        "dominant_colors": dominant_colors,
        "brightness": round(brightness, 1),
        "contrast": round(contrast, 1),
        "sharpness": round(sharpness, 1),
        "blur_verdict": blur_verdict,
        "quality_estimate": quality_estimate,
    }


def format_visual_report(result: dict) -> str:
    lines = ["🎨 *ANALISIS WARNA & KUALITAS GAMBAR*\n", "*Dominant colors:*"]
    for c in result["dominant_colors"]:
        lines.append(f"`{c['hex']}` — {c['pct']}%")
    lines += [
        "",
        f"Brightness rata-rata: {result['brightness']} / 255",
        f"Contrast (std dev): {result['contrast']}",
        f"Skor ketajaman (Laplacian variance): {result['sharpness']}",
        result["blur_verdict"],
    ]
    if result["quality_estimate"] is not None:
        lines.append(f"\nEstimasi kualitas kompresi JPEG: ~{result['quality_estimate']}/100 (kasar)")
    else:
        lines.append("\nEstimasi kualitas JPEG: nggak bisa dihitung (bukan JPEG / quant table nggak ada).")
    return "\n".join(lines)


# ------------------------------------------------------------------
# FITUR TAMBAHAN 4: CEK FOTO PERNAH DIKIRIM? (in-memory, NON-PERMANEN)
# ------------------------------------------------------------------
# Sengaja cuma disimpan di RAM (dict biasa), BUKAN file/database — sesuai
# keputusan: nggak ada penyimpanan permanen. Otomatis kosong lagi tiap
# kali proses bot restart/redeploy, dan cuma nyimpen hash + waktu, bukan
# identitas user pengirim.
_seen_photo_hashes: dict = {}


def check_duplicate(sha256_hash: str) -> dict:
    existing = _seen_photo_hashes.get(sha256_hash)
    if existing:
        existing["count"] += 1
        return {"is_duplicate": True, "first_seen": existing["first_seen"], "count": existing["count"]}
    _seen_photo_hashes[sha256_hash] = {
        "first_seen": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "count": 1,
    }
    return {"is_duplicate": False, "first_seen": None, "count": 1}


def format_duplicate_report(sha256_hash: str, result: dict) -> str:
    lines = ["🔁 *CEK FOTO PERNAH DIKIRIM?*\n", f"SHA256: `{sha256_hash}`\n"]
    if result["is_duplicate"]:
        lines.append(
            "🔴 Foto ini (hash identik) SUDAH PERNAH dikirim ke bot ini sebelumnya.\n"
            f"Pertama kali terlihat: {result['first_seen']}\n"
            f"Total sudah dikirim: {result['count']}x (termasuk yang sekarang)."
        )
    else:
        lines.append(
            "🟢 Belum pernah ada foto dengan hash identik yang masuk ke bot ini "
            "(sejak terakhir restart)."
        )
    lines.append(
        "\n⚠️ Daftar ini cuma di memory (RAM), nggak disimpan ke disk/database, "
        "dan reset otomatis tiap kali bot restart/redeploy. Nggak nyimpen "
        "identitas user pengirim — cuma hash + waktu pertama kali muncul."
    )
    return "\n".join(lines)


# ------------------------------------------------------------------
# FITUR TAMBAHAN 5: CEK PAJAK KENDARAAN (LINK RESMI, BUKAN DATA PRIBADI)
# ------------------------------------------------------------------
# Cuma mapping kode-wilayah -> provinsi (info publik dari Korlantas Polri),
# BUKAN lookup data pemilik. Best-effort & belum tentu 100% lengkap/terbaru
# (ada pemekaran daerah baru) — makanya selalu dikasih disclaimer + fallback
# link pencarian resmi.
PLATE_CODE_TO_PROVINCE = {
    "BL": "Aceh", "BB": "Sumatera Utara", "BK": "Sumatera Utara",
    "BA": "Sumatera Barat", "BM": "Riau", "BP": "Kepulauan Riau",
    "BH": "Jambi", "BG": "Sumatera Selatan", "BN": "Kepulauan Bangka Belitung",
    "BD": "Bengkulu", "BE": "Lampung",
    "A": "Banten", "B": "DKI Jakarta (Jabodetabek)",
    "D": "Jawa Barat (Bandung Raya)", "E": "Jawa Barat (Cirebon Raya)",
    "F": "Jawa Barat (Bogor Raya)", "T": "Jawa Barat (Purwasuka)",
    "Z": "Jawa Barat (Priangan Timur)",
    "G": "Jawa Tengah (Pekalongan)", "H": "Jawa Tengah (Semarang)",
    "K": "Jawa Tengah (Pati)", "R": "Jawa Tengah (Banyumas)",
    "AA": "Jawa Tengah (Kedu/Magelang)", "AD": "Jawa Tengah (Surakarta/Solo)",
    "AB": "DI Yogyakarta",
    "L": "Jawa Timur (Surabaya)", "M": "Jawa Timur (Madura)",
    "N": "Jawa Timur (Malang)", "P": "Jawa Timur (Besuki/Banyuwangi)",
    "S": "Jawa Timur (Bojonegoro/Mojokerto)", "AE": "Jawa Timur (Madiun)",
    "W": "Jawa Timur (Sidoarjo/Gresik)", "AG": "Jawa Timur (Kediri/Blitar)",
    "DK": "Bali", "DR": "Nusa Tenggara Barat", "EA": "Nusa Tenggara Barat",
    "DH": "Nusa Tenggara Timur", "EB": "Nusa Tenggara Timur", "ED": "Nusa Tenggara Timur",
    "KB": "Kalimantan Barat", "KH": "Kalimantan Tengah", "DA": "Kalimantan Selatan",
    "KT": "Kalimantan Timur", "KU": "Kalimantan Utara",
    "DB": "Sulawesi Utara", "DL": "Sulawesi Utara", "DN": "Gorontalo",
    "DT": "Sulawesi Tengah", "DC": "Sulawesi Barat", "DD": "Sulawesi Selatan",
    "DP": "Sulawesi Tenggara", "DE": "Maluku", "DG": "Maluku Utara",
    "PA": "Papua", "PB": "Papua Barat",
}

# Link e-Samsat provinsi yang datanya udah dicek silang dari beberapa sumber.
# Domain instansi daerah ini kadang berubah — kalau link mati, pakai fallback
# pencarian Google atau app SIGNAL.
PROVINCE_ESAMSAT_LINKS = {
    "DKI Jakarta (Jabodetabek)": "https://samsat-pkb.jakarta.go.id",
    "Jawa Barat (Bandung Raya)": "https://bapenda.jabarprov.go.id/infopkb",
    "Jawa Barat (Cirebon Raya)": "https://bapenda.jabarprov.go.id/infopkb",
    "Jawa Barat (Bogor Raya)": "https://bapenda.jabarprov.go.id/infopkb",
    "Jawa Barat (Purwasuka)": "https://bapenda.jabarprov.go.id/infopkb",
    "Jawa Barat (Priangan Timur)": "https://bapenda.jabarprov.go.id/infopkb",
    "Jawa Tengah (Pekalongan)": "https://bppd.jatengprov.go.id/info-pajak-kendaraan/",
    "Jawa Tengah (Semarang)": "https://bppd.jatengprov.go.id/info-pajak-kendaraan/",
    "Jawa Tengah (Pati)": "https://bppd.jatengprov.go.id/info-pajak-kendaraan/",
    "Jawa Tengah (Banyumas)": "https://bppd.jatengprov.go.id/info-pajak-kendaraan/",
    "Jawa Tengah (Kedu/Magelang)": "https://bppd.jatengprov.go.id/info-pajak-kendaraan/",
    "Jawa Tengah (Surakarta/Solo)": "https://bppd.jatengprov.go.id/info-pajak-kendaraan/",
    "Jawa Timur (Surabaya)": "https://www.esamsat.jatimprov.go.id",
    "Jawa Timur (Madura)": "https://www.esamsat.jatimprov.go.id",
    "Jawa Timur (Malang)": "https://www.esamsat.jatimprov.go.id",
    "Jawa Timur (Besuki/Banyuwangi)": "https://www.esamsat.jatimprov.go.id",
    "Jawa Timur (Bojonegoro/Mojokerto)": "https://www.esamsat.jatimprov.go.id",
    "Jawa Timur (Madiun)": "https://www.esamsat.jatimprov.go.id",
    "Jawa Timur (Sidoarjo/Gresik)": "https://www.esamsat.jatimprov.go.id",
    "Jawa Timur (Kediri/Blitar)": "https://www.esamsat.jatimprov.go.id",
    "Banten": "https://dppkd.bantenprov.go.id/read/info-pkb",
    "Aceh": "https://esamsat.acehprov.go.id",
}

SIGNAL_APP_LINKS = (
    "📱 *Aplikasi SIGNAL (Samsat Digital Nasional)* — resmi POLRI, Kemendagri "
    "& Jasa Raharja, berlaku buat banyak provinsi:\n"
    "Android: https://play.google.com/store/apps/details?id=app.signal.id\n"
    "iOS: cari \"SIGNAL Samsat Digital Nasional\" di App Store"
)

PLATE_REGEX = re.compile(r"\b([A-Z]{1,2})\s?(\d{1,4})\s?([A-Z]{0,3})\b")


def detect_plate_and_province(ocr_text: str) -> dict:
    """Cari pola plat nomor Indonesia di teks hasil OCR, terus tebak provinsi
    dari kode wilayahnya. Ini cuma mapping kode publik -> provinsi, BUKAN
    lookup data pemilik kendaraan."""
    text = ocr_text.upper()
    match = PLATE_REGEX.search(text)
    if not match:
        return {"plate_found": False}

    prefix = match.group(1)
    plate_text = " ".join(g for g in match.groups() if g)
    # Coba kode 2 huruf dulu, baru fallback ke 1 huruf
    province = PLATE_CODE_TO_PROVINCE.get(prefix)
    if not province and len(prefix) == 2:
        province = PLATE_CODE_TO_PROVINCE.get(prefix[0])
    return {"plate_found": True, "plate_text": plate_text, "prefix": prefix, "province": province}


def format_pajak_report(plate_info: dict) -> str:
    lines = ["🚗 *CEK PAJAK KENDARAAN*\n"]

    if not plate_info["plate_found"]:
        lines.append(
            "Nggak ketemu pola plat nomor yang kebaca jelas dari foto ini. "
            "Coba foto ulang lebih dekat & jelas ke bagian platnya.\n"
        )
    else:
        lines.append(f"Plat kebaca: `{plate_info['plate_text']}`")
        if plate_info["province"]:
            lines.append(f"Kemungkinan wilayah registrasi: *{plate_info['province']}*")
            link = PROVINCE_ESAMSAT_LINKS.get(plate_info["province"])
            if link:
                lines.append(f"🔗 e-Samsat wilayah ini: {link}")
            else:
                search_url = f"https://www.google.com/search?q=e-samsat+resmi+{plate_info['province'].split(' ')[0]}"
                lines.append(f"🔗 Cari e-Samsat resmi wilayah ini: {search_url}")
        else:
            lines.append("Kode wilayahnya nggak ada di daftar mapping bot ini.")
        lines.append("")

    lines.append(SIGNAL_APP_LINKS)
    lines.append(
        "\n⚠️ Deteksi wilayah dari kode plat itu perkiraan (best-effort), bisa "
        "meleset karena pemekaran daerah/kesalahan baca OCR — selalu konfirmasi "
        "ulang di app SIGNAL. Bot ini TIDAK mengakses data pemilik kendaraan, "
        "cuma nunjukin channel resmi buat kamu cek kendaraan sendiri."
    )
    return "\n".join(lines)


# ------------------------------------------------------------------
# KYC FACE COMPARE
# ------------------------------------------------------------------
def compare_faces(ref_path: str, selfie_path: str, tolerance: float = FACE_MATCH_TOLERANCE) -> dict:
    if not FACE_RECOGNITION_AVAILABLE:
        return {"error": "Library face_recognition belum terinstall di server."}
    try:
        ref_image = face_recognition.load_image_file(ref_path)
        selfie_image = face_recognition.load_image_file(selfie_path)
        ref_encodings = face_recognition.face_encodings(ref_image)
        selfie_encodings = face_recognition.face_encodings(selfie_image)

        if not ref_encodings:
            return {"error": "Nggak ketemu wajah di foto referensi. Coba foto yang lebih jelas."}
        if not selfie_encodings:
            return {"error": "Nggak ketemu wajah di foto selfie. Coba foto yang lebih jelas."}

        distance = face_recognition.face_distance([ref_encodings[0]], selfie_encodings[0])[0]
        match = bool(distance <= tolerance)
        return {"match": match, "distance": round(float(distance), 4), "tolerance": tolerance}
    except Exception as e:
        return {"error": f"Gagal memproses wajah: {e}"}


# ------------------------------------------------------------------
# HANDLER UTAMA: MEMPROSES GAMBAR (dari foto ATAU link) SESUAI MODE
# ------------------------------------------------------------------
async def process_image_for_mode(update, context, local_path: str):
    mode = context.user_data.get("mode")
    user = update.effective_user

    if mode in ("mode_reverse", "mode_brand"):
        await update.message.reply_text("Lagi upload gambar buat dapet link publik...")
        image_url = upload_to_public_host(local_path)
        if not image_url:
            await update.message.reply_text("Gagal upload gambar ke host publik, coba lagi beberapa saat.")
            audit_log(user.id, user.username, mode, "upload_failed")
            return
        await update.message.reply_text(build_reverse_search_links(image_url))
        audit_log(user.id, user.username, mode, f"success url={image_url}")

    elif mode == "mode_kyc":
        step = context.user_data.get("kyc_step", "reference")
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

        if step == "reference":
            ref_save_path = os.path.join(KYC_DIR, f"{user.id}_{ts}_reference.jpg")
            os.replace(local_path, ref_save_path)
            context.user_data["kyc_ref_path"] = ref_save_path
            context.user_data["kyc_step"] = "selfie"
            audit_log(user.id, user.username, "kyc_reference_saved", ref_save_path)
            await update.message.reply_text(
                "✅ Foto referensi tersimpan.\nLangkah 2: sekarang kirim foto *selfie* kamu.",
                parse_mode="Markdown",
            )
            return  # jangan hapus local_path, sudah dipindah

        elif step == "selfie":
            selfie_save_path = os.path.join(KYC_DIR, f"{user.id}_{ts}_selfie.jpg")
            os.replace(local_path, selfie_save_path)
            ref_path = context.user_data.get("kyc_ref_path")
            audit_log(user.id, user.username, "kyc_selfie_saved", selfie_save_path)

            if not ref_path or not os.path.exists(ref_path):
                await update.message.reply_text(
                    "Foto referensi nggak ketemu, ketik /start dan ulangi dari langkah 1."
                )
                return

            await update.message.reply_text("Lagi membandingkan wajah...")
            result = compare_faces(ref_path, selfie_save_path)

            if "error" in result:
                audit_log(user.id, user.username, "kyc_compare_error", result["error"])
                await update.message.reply_text(f"⚠️ {result['error']}")
            else:
                verdict = "✅ COCOK (kemungkinan orang yang sama)" if result["match"] else "❌ TIDAK COCOK"
                audit_log(
                    user.id, user.username, "kyc_compare_result",
                    f"match={result['match']} distance={result['distance']}",
                )
                await update.message.reply_text(
                    f"Hasil verifikasi:\n{verdict}\n"
                    f"Jarak kemiripan: {result['distance']} (semakin kecil semakin mirip, "
                    f"threshold: {result['tolerance']})\n\n"
                    "⚠️ Ini alat bantu, bukan keputusan hukum final. Untuk KYC produksi, "
                    "kombinasikan dengan liveness check & review manusia."
                )

            # reset supaya bisa verifikasi ulang dari awal
            context.user_data["kyc_step"] = "reference"
            context.user_data.pop("kyc_ref_path", None)
            return

    elif mode == "mode_deepfake":
        await update.message.reply_text("Lagi analisa gambar (ELA)...")
        result = ela_analyze(local_path)
        audit_log(user.id, user.username, "deepfake_check", str(result))
        await update.message.reply_text(
            f"Skor rata-rata ELA: {result['mean_diff']}\n"
            f"Indikasi: {result['verdict']}\n\n"
            "⚠️ Ini heuristik kasar berbasis kompresi ulang JPEG, BUKAN detektor "
            "deepfake yang akurat — terutama untuk foto AI-generatif modern. "
            "Untuk kebutuhan serius/hukum, pakai layanan khusus seperti "
            "Hive Moderation, Sensity, atau Reality Defender."
        )

    elif mode == "mode_metadata":
        await update.message.reply_text("Lagi bongkar metadata gambar...")
        display_filename = context.user_data.pop("pending_filename", None) or os.path.basename(local_path)
        reported_size = context.user_data.pop("pending_filesize", None)
        try:
            result = analyze_photo_metadata(local_path, display_filename, reported_size)
            for part in format_metadata_report(result):
                await update.message.reply_text(part, parse_mode="Markdown", disable_web_page_preview=True)
            audit_log(user.id, user.username, "metadata_check", f"file={display_filename}")
        except Exception as e:
            logger.error(f"Gagal analisa metadata: {e}")
            await update.message.reply_text(f"⚠️ Gagal analisa metadata: {e}")
            audit_log(user.id, user.username, "metadata_check_error", str(e))

    elif mode == "mode_ocr":
        await update.message.reply_text("Lagi baca teks di gambar (OCR)...")
        result = run_ocr(local_path)
        if "error" in result:
            await update.message.reply_text(f"⚠️ {result['error']}")
        elif not result["text"]:
            await update.message.reply_text("🔤 Nggak ketemu teks yang kebaca di gambar ini.")
        else:
            text = result["text"]
            if len(text) > 3800:
                text = text[:3800] + "\n...(dipotong, teksnya kepanjangan)"
            await update.message.reply_text("🔤 Teks yang kebaca:")
            await update.message.reply_text(text)
        audit_log(user.id, user.username, "ocr", "done")

    elif mode == "mode_stego":
        await update.message.reply_text("Lagi cek indikasi steganografi...")
        result = analyze_steganography(local_path)
        await update.message.reply_text(format_stego_report(result), parse_mode="Markdown")
        audit_log(user.id, user.username, "stego_check", f"trailing_found={result['trailing']['found']}")

    elif mode == "mode_visual":
        await update.message.reply_text("Lagi analisa warna & kualitas gambar...")
        result = analyze_visual_quality(local_path)
        await update.message.reply_text(format_visual_report(result), parse_mode="Markdown")
        audit_log(user.id, user.username, "visual_analysis", "done")

    elif mode == "mode_duplicate":
        await update.message.reply_text("Lagi cek hash ke daftar foto yang pernah masuk...")
        hashes = compute_hashes(local_path)
        result = check_duplicate(hashes["sha256"])
        await update.message.reply_text(format_duplicate_report(hashes["sha256"], result), parse_mode="Markdown")
        audit_log(user.id, user.username, "duplicate_check", f"is_dup={result['is_duplicate']}")

    elif mode == "mode_pajak":
        await update.message.reply_text("Lagi baca plat nomor dari foto...")
        ocr_result = run_ocr(local_path)
        ocr_text = ocr_result.get("text", "") if "error" not in ocr_result else ""
        plate_info = detect_plate_and_province(ocr_text)
        await update.message.reply_text(
            format_pajak_report(plate_info), parse_mode="Markdown", disable_web_page_preview=True
        )
        audit_log(user.id, user.username, "pajak_check", f"plate_found={plate_info['plate_found']}")

    if os.path.exists(local_path):
        os.remove(local_path)


async def photo_handler(update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_rate_limited(user.id):
        audit_log(user.id, user.username, "rate_limited", "photo")
        await update.message.reply_text(
            f"⏳ Kebanyakan request. Tunggu {RATE_LIMIT_WINDOW} detik dulu ya."
        )
        return

    mode = context.user_data.get("mode")
    if not mode:
        await update.message.reply_text("Pilih dulu mode-nya ya, ketik /start buat lihat menu.")
        return

    photo = update.message.photo[-1]
    tg_file = await photo.get_file()

    fd, local_path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    await tg_file.download_to_drive(local_path)

    # Foto biasa (compressed) nggak punya nama file asli & EXIF-nya udah
    # dihapus Telegram di sisi mereka — tandain jelas biar user nggak bingung.
    context.user_data["pending_filename"] = f"photo_{photo.file_unique_id}.jpg (terkompresi Telegram)"
    context.user_data["pending_filesize"] = photo.file_size

    await process_image_for_mode(update, context, local_path)


async def document_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """Menangani gambar yang dikirim sebagai File/Dokumen (bukan Photo terkompresi),
    supaya metadata original (EXIF, GPS, dll) nggak hilang."""
    user = update.effective_user
    if is_rate_limited(user.id):
        audit_log(user.id, user.username, "rate_limited", "document")
        await update.message.reply_text(
            f"⏳ Kebanyakan request. Tunggu {RATE_LIMIT_WINDOW} detik dulu ya."
        )
        return

    mode = context.user_data.get("mode")
    if not mode:
        await update.message.reply_text("Pilih dulu mode-nya ya, ketik /start buat lihat menu.")
        return

    document = update.message.document
    if not document.mime_type or not document.mime_type.startswith("image/"):
        await update.message.reply_text("File itu bukan gambar, kirim file gambar (jpg/png/dll) ya.")
        return

    tg_file = await document.get_file()
    suffix = os.path.splitext(document.file_name or "")[1] or ".jpg"
    fd, local_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    await tg_file.download_to_drive(local_path)

    context.user_data["pending_filename"] = document.file_name or os.path.basename(local_path)
    context.user_data["pending_filesize"] = document.file_size

    await process_image_for_mode(update, context, local_path)


async def text_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """Menangani link gambar yang dikirim sebagai teks."""
    user = update.effective_user
    text = (update.message.text or "").strip()

    if not URL_REGEX.match(text):
        await update.message.reply_text(
            "Kirim foto langsung, atau kirim link gambar (harus diawali http:// atau https://). "
            "Ketik /start buat lihat menu."
        )
        return

    if is_rate_limited(user.id):
        audit_log(user.id, user.username, "rate_limited", "link")
        await update.message.reply_text(
            f"⏳ Kebanyakan request. Tunggu {RATE_LIMIT_WINDOW} detik dulu ya."
        )
        return

    mode = context.user_data.get("mode")
    if not mode:
        await update.message.reply_text("Pilih dulu mode-nya ya, ketik /start buat lihat menu.")
        return

    await update.message.reply_text("Lagi download gambar dari link...")
    local_path = download_image_from_url(text)
    if not local_path:
        audit_log(user.id, user.username, "link_download_failed", text)
        await update.message.reply_text(
            "Gagal download gambar dari link itu. Pastikan link langsung mengarah ke file gambar."
        )
        return

    url_filename = os.path.basename(text.split("?")[0]) or "gambar_dari_url"
    context.user_data["pending_filename"] = url_filename
    context.user_data["pending_filesize"] = os.path.getsize(local_path)

    await process_image_for_mode(update, context, local_path)


def main():
    if BOT_TOKEN == "GANTI_DENGAN_TOKEN_BOT_LU":
        raise SystemExit("Set environment variable BOT_TOKEN dulu (token dari @BotFather di Telegram).")
    if not FACE_RECOGNITION_AVAILABLE:
        logger.warning(
            "Library face_recognition belum terinstall — fitur compare wajah KYC "
            "akan menampilkan pesan error ke user. Install dulu: pip install face_recognition"
        )

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.Document.IMAGE, document_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    logger.info("Bot jalan, polling...")
    # drop_pending_updates=True: pas Railway redeploy dan sempat ada 2 instance
    # numpuk sebentar (yang lama belum mati pas yang baru udah nyala), instance
    # baru nggak bakal "kebanjiran" update lama yang udah basi.
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
