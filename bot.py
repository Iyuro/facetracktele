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
import time
import logging
import tempfile
from datetime import datetime
from collections import defaultdict, deque

import numpy as np
import requests
from PIL import Image, ImageChops, ImageEnhance
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
]

WELCOME_TEXT = (
    "Halo! Bot ini bantu beberapa hal, semuanya berbasis persetujuan (consent) — "
    "bukan buat nyari/ngintip orang lain diam-diam:\n\n"
    "1️⃣ Reverse Image Search — kasih link ke Google Lens / Yandex / TinEye\n"
    "2️⃣ Brand/Logo Monitoring — sama seperti di atas, buat mantau logo/produk kamu sendiri\n"
    "3️⃣ Verifikasi Foto (KYC) — kirim foto referensi + selfie sendiri, dibandingkan otomatis\n"
    "4️⃣ Deteksi Indikasi Editan/Deepfake — analisa kasar (ELA), bukan detektor akurat\n\n"
    "Kamu bisa kirim FOTO langsung atau LINK gambar (http/https).\n"
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
def upload_to_catbox(file_path: str) -> str | None:
    """Upload gambar ke catbox.moe secara anonim buat dapet URL publik."""
    url = "https://catbox.moe/user/api.php"
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                url,
                data={"reqtype": "fileupload"},
                files={"fileToUpload": f},
                timeout=30,
            )
        if resp.status_code == 200 and resp.text.startswith("http"):
            return resp.text.strip()
        logger.warning(f"Upload catbox gagal, response: {resp.text}")
    except Exception as e:
        logger.error(f"Upload catbox error: {e}")
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
        image_url = upload_to_catbox(local_path)
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    logger.info("Bot jalan, polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
