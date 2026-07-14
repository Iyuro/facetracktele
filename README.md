# Bot Telegram — Reverse Search / Brand Monitoring / KYC / Deteksi Editan

Bot ini punya 4 mode, dipilih lewat menu tombol di `/start`. Bisa kirim **foto langsung**
atau **link gambar** (http/https).

1. **Reverse Image Search** — kirim foto/link, bot kasih link Google Lens / Yandex / TinEye.
   Kamu yang buka linknya manual; bot nggak nampilin hasil sosmed orang secara otomatis.
2. **Brand/Logo Monitoring** — sama seperti di atas, dipakai buat cek logo/produk kamu
   sendiri dipakai orang lain tanpa izin atau enggak.
3. **Verifikasi Foto (KYC)** — mode consent-based, 2 tahap:
   - Tahap 1: kirim foto referensi (misal foto KTP/ID)
   - Tahap 2: kirim foto selfie
   - Kalau library `face_recognition` terinstall, bot otomatis membandingkan kemiripan
     wajah dan kasih skor jarak (distance) + verdict cocok/tidak. **Secara default,
     library ini TIDAK diinstall** (lihat bagian "Fitur compare wajah (opsional)" di
     bawah) supaya deploy/build nggak gagal karena `dlib` berat di-compile. Tanpa
     library ini, foto tetap tersimpan di `kyc_data/` tapi belum ada perbandingan
     otomatis — bot kasih pesan "library belum terinstall".
4. **Deteksi Indikasi Editan/Deepfake** — pakai Error Level Analysis (ELA) buat ngasih
   skor kasar apakah gambar kemungkinan sudah diedit. Ini **bukan** detektor deepfake
   yang akurat, cuma indikator awal.

## Fitur tambahan

- **Kirim via link**: selain upload foto langsung, user bisa kirim URL gambar sebagai teks.
- **Rate limiting**: default maksimal 5 request per 60 detik per user (atur lewat env var
  `RATE_LIMIT_MAX` dan `RATE_LIMIT_WINDOW`).
- **Audit log**: setiap aksi dicatat ke `logs/audit.log`.
- **KYC face compare**: pakai library `face_recognition` (berbasis dlib). Threshold
  kecocokan default 0.6, bisa diatur lewat env var `FACE_MATCH_TOLERANCE`.

## Cara jalanin lokal

```bash
pip install -r requirements.txt
export BOT_TOKEN="isi_token_dari_botfather"
python bot.py
```

> ⚠️ `face_recognition` butuh `dlib`, dan `dlib` butuh CMake + compiler C++ terinstall
> di sistem SEBELUM di-pip-install. Ubuntu/Debian: `sudo apt install cmake build-essential`.
> macOS: `brew install cmake`. Kalau males ribet, hapus dua baris `face_recognition`/`dlib`
> dari `requirements.txt` — bot tetap jalan, cuma mode compare wajah KYC kasih pesan error.

## Fitur compare wajah (opsional)

Secara default, `requirements.txt` **tidak** menginstall `face_recognition`/`dlib`
supaya proses build/deploy nggak gagal (dlib butuh compile C++ yang berat & sering
bermasalah di platform PaaS kayak Railway). Tanpa ini, 3 mode lain (reverse search,
brand monitoring, deepfake check) tetap jalan normal — cuma compare wajah otomatis
di mode KYC yang belum aktif.

**Kalau mau ngaktifin fitur compare wajah**, pilihan yang paling aman:

- **Jalan di komputer/server sendiri (bukan Railway)**: install dulu build tools
  (`sudo apt install cmake build-essential` di Ubuntu/Debian, atau `brew install cmake`
  di macOS), lalu uncomment 2 baris `face_recognition` & `dlib` di `requirements.txt`,
  lalu `pip install -r requirements.txt` ulang.
- **Tetap mau di Railway**: perlu Dockerfile custom yang install `cmake`, `build-essential`,
  dan `libopenblas-dev` sebelum `pip install`, karena compile dlib butuh waktu %
  resource yang kadang di luar limit default nixpacks build. Kabarin kalau mau gue
  siapin Dockerfile-nya.

## Deploy ke Railway (auto-run, tinggal push GitHub)

Bot ini pakai `run_polling()` jadi **nggak butuh domain/webhook publik** — cocok banget
buat Railway "Worker" service.

### Langkah-langkah

1. **Push project ini ke GitHub repo baru:**
   ```bash
   git init
   git add .
   git commit -m "init telegram bot"
   git branch -M main
   git remote add origin https://github.com/USERNAME/NAMA_REPO.git
   git push -u origin main
   ```
   (Ganti `USERNAME/NAMA_REPO` sesuai punya kamu. Bisa juga upload lewat web GitHub
   kalau nggak mau pakai command line — drag & drop file juga bisa di halaman
   "Add file → Upload files").

2. **Di [railway.app](https://railway.app):**
   - Login pakai akun GitHub kamu.
   - Klik **New Project → Deploy from GitHub repo** → pilih repo yang tadi di-push.
   - Railway otomatis detect ini Python project dan baca `nixpacks.toml` (buat install
     cmake/gcc dulu sebelum `pip install`), lalu `railway.json` buat start command-nya.

3. **Set Environment Variables** di tab **Variables** pada service Railway kamu:
   | Key | Value |
   |---|---|
   | `BOT_TOKEN` | token dari @BotFather |
   | `RATE_LIMIT_MAX` | `5` (opsional) |
   | `RATE_LIMIT_WINDOW` | `60` (opsional) |
   | `FACE_MATCH_TOLERANCE` | `0.6` (opsional) |

4. **Deploy.** Railway otomatis build & jalanin `python bot.py`. Cek tab **Deployments →
   Logs** buat mastiin muncul log `"Bot jalan, polling..."`.

5. **Auto-redeploy**: tiap kali kamu `git push` ke branch `main`, Railway otomatis build
   ulang & restart bot — nggak perlu upload manual lagi.

### Catatan soal data yang tersimpan (`kyc_data/`, `logs/`)

Railway punya **ephemeral filesystem** by default — artinya file yang disimpan di
`kyc_data/` dan `logs/` bakal **hilang setiap kali service di-redeploy/restart**.
Untuk penggunaan production yang serius:
- Pindahin storage foto KYC ke object storage eksternal (S3, Cloudflare R2, dll), atau
- Tambahin **Railway Volume** (fitur persistent storage) dan mount ke folder `kyc_data/`
  serta `logs/` supaya datanya nggak hilang.
- Untuk sekadar testing/demo, ephemeral storage-nya nggak masalah.

## Batasan & hal yang WAJIB diperhatikan

- **Jangan** ubah bot ini jadi alat buat nyari/melacak wajah orang lain di sosmed tanpa
  izin mereka. Reverse image search sengaja dibuat manual (link, bukan hasil otomatis).
- Mode KYC wajib cuma dipakai untuk foto milik pengguna sendiri, dengan persetujuan.
  Kamu wajib punya kebijakan privasi & retensi data yang jelas, sesuai UU PDP atau
  regulasi privasi lain yang berlaku. Hasil `face_recognition` sebaiknya dikombinasikan
  dengan liveness check dan review manusia untuk KYC produksi, bukan keputusan otomatis
  final.
- Deteksi deepfake heuristik sederhana (ELA), gampang salah untuk foto AI generatif
  modern. Untuk kebutuhan serius, pakai layanan khusus (Hive Moderation, Sensity,
  Reality Defender, dll).
- Upload gambar ke `catbox.moe` publik & anonim — jangan pakai untuk foto sensitif.
  Foto KYC sengaja TIDAK diupload ke catbox, cuma disimpan lokal/volume.
- `logs/audit.log` berisi data pribadi (user_id, username, aktivitas) — perlakukan
  sebagai data sensitif.

## Struktur file

```
telegram_face_bot/
├── bot.py              # kode utama bot
├── requirements.txt    # dependency python
├── Procfile            # buat platform ala Heroku/Railway worker
├── railway.json        # konfigurasi start command Railway
├── nixpacks.toml       # install cmake/gcc dulu sebelum pip install (buat dlib)
├── runtime.txt          # versi python
├── .gitignore
├── .env.example         # contoh isi environment variables
├── kyc_data/            # folder tempat foto KYC tersimpan (auto-dibuat)
├── logs/
│   └── audit.log        # audit trail aktivitas (auto-dibuat saat bot jalan)
└── README.md
```
