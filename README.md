# Project EDC Telegram Auto Upload

Bot ini menerima foto struk EDC dari Telegram, membaca data struk dengan Gemini, lalu mengisi dan submit Google Form menggunakan Playwright lewat Microsoft Edge.

## Alur kerja

1. Petugas daftar lewat `/register` di Telegram.
2. Petugas mengirim satu foto struk.
3. Bot mengunduh foto dan meminta Gemini mengekstrak TID, nama merchant, tanggal, dan waktu.
4. Bot membuka Google Form di Microsoft Edge.
5. Bot mengisi data petugas, data merchant, kondisi EDC, lalu mengupload foto struk yang sama ke dua field foto sesuai alur saat ini.
6. Bot submit form dan mengirim status sukses/gagal ke Telegram.
7. Setiap hasil proses dicatat ke `submissions.jsonl` di folder project.

## File penting

- `edc_agent.py`: bot Telegram, registrasi user, antrean proses, Gemini OCR, dan log submit.
- `bot_edc.py`: otomasi Google Form dengan Playwright dan Microsoft Edge.
- `.env`: token Telegram dan API key Gemini. File ini tidak boleh dipush ke GitHub.
- `users_db.json`: data petugas terdaftar. File ini lokal dan tidak dipush ke GitHub.
- `submissions.jsonl`: log lokal hasil submit. File ini lokal dan tidak dipush ke GitHub.

## Setup awal

Gunakan Python yang sudah terpasang di Windows. Project ini sudah dites dengan Python 3.14.

```powershell
cd C:\Users\ahmad\Desktop\project-edc
python -m pip install -r requirements.txt
```

Buat file `.env` dari `.env.example`, lalu isi:

```env
TELEGRAM_BOT_TOKEN=isi_token_bot_telegram
GEMINI_API_KEYS=key_gemini_1,key_gemini_2,key_gemini_3
GOOGLE_FORM_URL=isi_link_google_form_clone_atau_test
```

Jangan upload `.env` ke GitHub.

## Testing dengan form clone

Untuk testing upload, jangan arahkan bot ke form kerja asli. Buat salinan Google Form atau pakai form test, lalu isi `GOOGLE_FORM_URL` di `.env` dengan link form clone tersebut.

Kalau `GOOGLE_FORM_URL` belum diisi, bot otomatis memakai test form bawaan dari kode lama:

```env
GOOGLE_FORM_URL=https://docs.google.com/forms/d/e/1FAIpQLSeyQd0QFilraMWZSQQkqoKreMuvHhV5FSrRdOE6ol70v5W82A/viewform
```

Untuk production, baru ganti `GOOGLE_FORM_URL` ke link form kerja asli setelah testing selesai.

## Menjalankan bot

Cara singkat:

```powershell
cd C:\Users\ahmad\Desktop\project-edc
.\run.ps1
```

Atau langsung:

```powershell
python .\edc_agent.py
```

## Microsoft Edge otomatis

Bot sekarang akan mencoba membuka Microsoft Edge otomatis di port `9222` jika port itu belum aktif. Jadi normalnya kamu tidak perlu lagi menjalankan Edge manual lewat `Win + R`.

Urutan profile yang dipakai:

1. `EDGE_USER_DATA_DIR` dari `.env`, kalau diisi.
2. Folder `darul/` di project, kalau ada.
3. Folder `bot_profile/` di project.

Kalau Google Form meminta login, login sekali di jendela Edge yang dibuka bot. Setelah itu session akan tersimpan di profile tersebut.

Kalau lokasi Edge berbeda, isi di `.env`:

```env
EDGE_EXECUTABLE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe
```

## Log proses

Setiap proses sukses atau gagal akan masuk ke `submissions.jsonl`.

Contoh isi log:

```json
{"timestamp":"2026-06-23T15:00:00+00:00","status":"success","chat_id":"123","message_id":10,"user":{"nama":"Nama Petugas","pn":"001","branch_office":"BO JAKARTA"},"extracted":{"tid":"12345678","nama_merchant":"TOKO CONTOH","tanggal":"23/06/2026","waktu":"14:30"},"error":null}
```

Status yang mungkin muncul:

- `success`: form berhasil diproses sampai submit.
- `failed_json`: Gemini tidak mengembalikan JSON valid.
- `failed_upload_timeout`: upload foto timeout dan form dibersihkan.
- `failed`: error umum selain dua kategori di atas.

## Catatan keamanan

File ini sengaja tidak dipush:

- `.env`
- `users_db.json`
- `keys_db.json`
- `submissions.jsonl`
- `bot_profile/`
- `darul/`
- file cache Python dan browser

Alasannya: file tersebut bisa berisi token, data petugas, session login, atau data operasional lokal.
