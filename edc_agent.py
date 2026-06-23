import os
import json
import telebot
from telebot import apihelper # <-- 1. Tambahkan ini
import google.generativeai as genai
from PIL import Image
import threading
import queue
import re
from dotenv import load_dotenv
from datetime import datetime, timezone

# Memuat konfigurasi dari file .env
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ==========================================
# 1. CONFIGURATION
# ==========================================
# Tambahkan pengaturan timeout ini agar bot tidak mudah RTO (Request Time Out)
apihelper.READ_TIMEOUT = 90
apihelper.CONNECT_TIMEOUT = 90
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEYS = os.getenv("GEMINI_API_KEYS", "").split(",")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

GEMINI_PROMPT = """
Kamu adalah sistem ekstraksi data otomatis. Analisis gambar struk EDC thermal berikut. Ekstrak 4 data utama:
1. TID (Terminal ID, biasanya 8 digit angka)
2. Nama Merchant / Toko (biasanya teks paling besar di atas)
3. Tanggal Transaksi (Ubah ke format: DD/MM/YYYY)
4. Waktu Transaksi (Ubah ke format: HH:MM, format 24 jam)

ATURAN WAJIB:
Kembalikan HANYA format JSON mentah murni tanpa blok kode markdown dan tanpa penjelasan apapun.
Persis seperti format ini:
{
  "tid": "12345678",
  "nama_merchant": "TOKO MAJU JAYA",
  "tanggal": "15/01/2026",
  "waktu": "14:30"
}
"""

# ==========================================
# 2. DATABASE USER & REGISTRASI (Dengan Lock)
# ==========================================
DB_FILE = os.path.join(BASE_DIR, "users_db.json")
LOG_FILE = os.path.join(BASE_DIR, "submissions.jsonl")
db_lock = threading.Lock() # Mencegah race condition saat menulis DB
log_lock = threading.Lock()

if not os.path.exists(DB_FILE):
    with open(DB_FILE, 'w') as f:
        json.dump({}, f)

def load_users():
    with db_lock:
        with open(DB_FILE, 'r') as f:
            return json.load(f)

def save_users(users_data):
    with db_lock:
        with open(DB_FILE, 'w') as f:
            json.dump(users_data, f, indent=4)

def write_submission_log(status, message, user_data, extracted_data=None, error=None):
    extracted_data = extracted_data or {}
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "chat_id": str(message.chat.id),
        "message_id": message.message_id,
        "user": {
            "nama": user_data.get("nama"),
            "pn": user_data.get("pn"),
            "branch_office": user_data.get("branch_office"),
        },
        "extracted": {
            "tid": extracted_data.get("tid"),
            "nama_merchant": extracted_data.get("nama_merchant"),
            "tanggal": extracted_data.get("tanggal"),
            "waktu": extracted_data.get("waktu"),
        },
        "error": str(error)[:2000] if error else None,
    }
    with log_lock:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

temp_registration = {}

# ==========================================
# 3. SISTEM FALLBACK API (ROTATION)
# ==========================================
current_key_index = 0

# ==========================================
# 3. SISTEM FALLBACK API (ROTATION)
# ==========================================
def ekstrak_dengan_gemini_fallback(img):
    global GEMINI_API_KEYS # Mengambil akses langsung ke daftar API Key
    total_keys = len(GEMINI_API_KEYS)
    
    for attempt in range(total_keys):
        try:
            # SELALU gunakan key yang berada di antrean paling depan (index 0)
            active_key = GEMINI_API_KEYS[0]
            genai.configure(api_key=active_key)
            model = genai.GenerativeModel('gemini-2.5-flash')
            
            response = model.generate_content([GEMINI_PROMPT, img])
            return response.text
            
        except Exception as e:
            error_msg = str(e).lower()
            
            # Jika ditolak karena filter keamanan (bukan limit), jangan putar antrean
            if "safety" in error_msg or "blocked" in error_msg:
                raise Exception("Gambar ditolak oleh AI karena terdeteksi melanggar filter keamanan.")
                
            print(f"  [API Key Gagal/Limit]: {str(e)}")
            
            # LOGIKA ROTASI: Cabut key dari urutan pertama (depan), lalu masukkan ke paling belakang
            failed_key = GEMINI_API_KEYS.pop(0)
            GEMINI_API_KEYS.append(failed_key)
            
            print("  ⚠️ Key ditaruh ke paling belakang. Beralih ke Key berikutnya di antrean...")
            
    raise Exception("Semua API Key Gemini telah gagal atau terkena limit!")

# ==========================================
# 4. SISTEM ANTRIAN (QUEUE) & WORKER
# ==========================================
form_queue = queue.Queue()

def queue_worker():
    import bot_edc
    
    while True:
        task = form_queue.get()
        message, user_data = task
        
        chat_id = str(message.chat.id)
        # KUNCI PERBAIKAN: Penamaan file unik agar tidak bentrok antar user
        timestamp_file = os.path.join(BASE_DIR, f"temp_struk_{chat_id}_{message.message_id}.jpg")
        extracted_data = {}
        
        msg_status = bot.send_message(chat_id, "⚙️ **Mulai memproses antrian:** Mengunduh gambar...", parse_mode="Markdown")
        
        try:
            file_info = bot.get_file(message.photo[-1].file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            
            with open(timestamp_file, 'wb') as new_file:
                new_file.write(downloaded_file)
                
            bot.edit_message_text("🔍 Gambar diunduh. Membaca data menggunakan Gemini AI...", chat_id, msg_status.message_id)
            
            with Image.open(timestamp_file) as img:
                raw_text = ekstrak_dengan_gemini_fallback(img)
            
            # KUNCI PERBAIKAN: Regex untuk memastikan hanya blok JSON yang diambil
            match = re.search(r'\{.*?\}', raw_text, re.DOTALL)
            if not match:
                raise ValueError(f"AI tidak mengembalikan format JSON yang valid. Output AI:\n{raw_text}")
                
            json_str = match.group(0)
            extracted_data = json.loads(json_str)
            
            form_data_payload = {
                "tanggal": extracted_data.get('tanggal', ''),
                "waktu": extracted_data.get('waktu', ''),
                "branch_office": user_data['branch_office'],
                "pn": user_data['pn'],
                "nama_petugas_it": user_data['nama'],
                "tid": extracted_data.get('tid', ''),
                "nama_merchant": extracted_data.get('nama_merchant', ''),
                "kondisi_edc": "EDC digunakan (Normal)",
                "foto_struk": timestamp_file,
                "foto_toko": timestamp_file  
            }
            
            bot.edit_message_text(
                f"✅ **Data Ekstrak:** TID `{extracted_data.get('tid')}` | Toko `{extracted_data.get('nama_merchant')}`\n"
                f"👤 **Petugas:** {user_data['nama']} ({user_data['branch_office']})\n"
                f"🚀 Mengeksekusi browser untuk submit form...", 
                chat_id, msg_status.message_id, parse_mode="Markdown"
            )
            
            bot_edc.run_bot(form_data_payload)
            write_submission_log("success", message, user_data, extracted_data)
            bot.send_message(chat_id, f"  **SUKSES!** Laporan EDC untuk TID `{extracted_data.get('tid')}` telah selesai disubmit.", parse_mode="Markdown")
            
        except json.JSONDecodeError:
            write_submission_log("failed_json", message, user_data, extracted_data, "Hasil bacaan AI rusak atau tidak sesuai format.")
            bot.send_message(chat_id, "  **GAGAL:** Hasil bacaan AI rusak atau tidak sesuai format.", parse_mode="Markdown")
            
        except Exception as e:
            error_msg = str(e)
            
            # --- CEK APAKAH ERROR KARENA UPLOAD ---
            if "koneksi lambat saat upload foto" in error_msg:
                pesan_gagal = (
                    "❌ **PROSES GAGAL (KONEKSI LAMBAT)**\n\n"
                    "Gagal mengupload foto ke Google Form karena *timeout*.\n"
                    "Formulir telah dikosongkan secara otomatis. Silakan coba kirim ulang gambar saat koneksi server lebih stabil."
                )
                write_submission_log("failed_upload_timeout", message, user_data, extracted_data, error_msg)
                bot.send_message(chat_id, pesan_gagal, parse_mode="Markdown")
            else:
                # Error umum lainnya
                write_submission_log("failed", message, user_data, extracted_data, error_msg)
                bot.send_message(chat_id, f"  **GAGAL MEMPROSES:**\n`{error_msg}`", parse_mode="Markdown")
                
        finally:
            # Penghapusan file difokuskan hanya di sini
            if os.path.exists(timestamp_file):
                try:
                    os.remove(timestamp_file)
                except:
                    pass
            form_queue.task_done()

threading.Thread(target=queue_worker, daemon=True).start()

# ==========================================
# 5. TELEGRAM HANDLERS
# ==========================================
@bot.message_handler(commands=['start', 'register', 'help'])
def cmd_register(message):
    chat_id = str(message.chat.id)
    users = load_users()
    
    if chat_id in users and message.text == '/start':
        bot.reply_to(message, f"👋 Halo {users[chat_id]['nama']}! Akun kamu sudah terdaftar. Langsung saja kirim foto struknya.\n\n*(Ketik /register jika ingin mengubah data)*", parse_mode="Markdown")
        return
        
    msg = bot.reply_to(message, "📝 **REGISTRASI PETUGAS IT**\n\nSiapa nama lengkap kamu? *(Pastikan huruf besar/kecilnya persis seperti di pilihan dropdown form)*", parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_nama_step)

def process_nama_step(message):
    chat_id = str(message.chat.id)
    temp_registration[chat_id] = {'nama': message.text}
    msg = bot.reply_to(message, "🔢 Berapa PN/NIK kamu? *(Contoh: OTS2502109)*", parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_pn_step)

def process_pn_step(message):
    chat_id = str(message.chat.id)
    temp_registration[chat_id]['pn'] = message.text
    msg = bot.reply_to(message, "🏢 Apa Branch Office kamu? *(Penting: Ketik huruf kapital semua, contoh: BO JAKARTA CEMPAKA MAS)*", parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_bo_step)

def process_bo_step(message):
    chat_id = str(message.chat.id)
    temp_registration[chat_id]['branch_office'] = message.text
    
    users = load_users()
    users[chat_id] = {
        'nama': temp_registration[chat_id]['nama'],
        'pn': temp_registration[chat_id]['pn'],
        'branch_office': temp_registration[chat_id]['branch_office']
    }
    save_users(users)
    
    if chat_id in temp_registration:
        del temp_registration[chat_id]
        
    bot.reply_to(message, "✅ **Registrasi Berhasil!** Profil kamu telah disimpan. Sekarang kamu bisa langsung mengirim banyak foto struk sekaligus.", parse_mode="Markdown")

@bot.message_handler(content_types=['photo'])
def handle_receipt_photo(message):
    chat_id = str(message.chat.id)
    users = load_users()
    
    if chat_id not in users:
        bot.reply_to(message, "⛔ **Akses Ditolak!**\nKamu belum terdaftar. Silakan ketik perintah /register terlebih dahulu.", parse_mode="Markdown")
        return
        
    user_data = users[chat_id]
    form_queue.put((message, user_data))
    urutan = form_queue.qsize()
    
    bot.reply_to(message, f"📥 **Foto Diterima!** Masuk ke antrian urutan ke-{urutan} (Profile: {user_data['nama']}).")

# ==========================================
# 6. JALANKAN BOT
# ==========================================
if __name__ == '__main__':
    print("🤖 Agen Telegram (Mode Enterprise) AKTIF.")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
