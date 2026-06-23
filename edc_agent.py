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
from datetime import datetime, timezone, timedelta

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
ADMIN_CHAT_IDS = {chat_id.strip() for chat_id in os.getenv("ADMIN_CHAT_IDS", "").split(",") if chat_id.strip()}

try:
    LOG_DETAIL_LIMIT = max(1, int(os.getenv("LOG_DETAIL_LIMIT", "50")))
except ValueError:
    LOG_DETAIL_LIMIT = 50

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
JAKARTA_TZ = timezone(timedelta(hours=7))
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

STATUS_LABELS = {
    "success": "sukses",
    "failed_json": "gagal baca AI",
    "failed_upload_timeout": "gagal upload timeout",
    "failed": "gagal proses",
}

HELP_TEXT = """Perintah bot:
/register - daftar atau ubah profil petugas
/rekap [YYYY-MM|bulan_ini|bulan_lalu] - ringkasan sukses/gagal per bulan
/gagal [YYYY-MM|bulan_ini|bulan_lalu] - daftar log gagal
/gagal_tid [YYYY-MM|bulan_ini|bulan_lalu] - daftar TID yang gagal
/tid <TID> [YYYY-MM|bulan_ini|bulan_lalu] - riwayat upload untuk TID tertentu

Contoh:
/rekap
/rekap 2026-06
/gagal bulan_lalu
/tid 10396918 2026-06

Catatan: user biasa hanya melihat log miliknya sendiri. Admin melihat semua log jika ADMIN_CHAT_IDS di .env diisi."""

def is_admin(chat_id):
    return str(chat_id) in ADMIN_CHAT_IDS

def require_registered_user(message):
    chat_id = str(message.chat.id)
    users = load_users()
    user_data = users.get(chat_id)
    if not user_data:
        bot.reply_to(message, "Akses ditolak. Kamu belum terdaftar. Ketik /register terlebih dahulu.")
        return None
    return user_data

def get_command_args(message):
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) == 1:
        return ""
    return parts[1].strip()

def parse_month_filter(month_arg):
    raw_arg = (month_arg or "").strip().lower()
    now = datetime.now(JAKARTA_TZ)

    if raw_arg in ("", "bulan_ini", "this_month"):
        year, month = now.year, now.month
    elif raw_arg in ("bulan_lalu", "bulan_sebelumnya", "last_month"):
        previous_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        year, month = previous_month.year, previous_month.month
    else:
        match = re.fullmatch(r"(\d{4})-(\d{2})", raw_arg)
        if not match:
            raise ValueError("Format bulan tidak valid. Pakai YYYY-MM, bulan_ini, atau bulan_lalu.")
        year, month = int(match.group(1)), int(match.group(2))
        if month < 1 or month > 12:
            raise ValueError("Bulan harus antara 01 sampai 12.")

    start = datetime(year, month, 1, tzinfo=JAKARTA_TZ)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=JAKARTA_TZ)
    else:
        end = datetime(year, month + 1, 1, tzinfo=JAKARTA_TZ)

    return start, end, f"{year:04d}-{month:02d}"

def read_submission_logs():
    if not os.path.exists(LOG_FILE):
        return []

    records = []
    with log_lock:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            for line_number, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                record["_line_number"] = line_number
                records.append(record)
    return records

def parse_record_datetime(record):
    timestamp = record.get("timestamp")
    if not timestamp:
        return None
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(JAKARTA_TZ)

def logs_for_request(message, start, end):
    chat_id = str(message.chat.id)
    admin_view = is_admin(chat_id)
    filtered = []

    for record in read_submission_logs():
        if not admin_view and str(record.get("chat_id")) != chat_id:
            continue

        record_time = parse_record_datetime(record)
        if not record_time:
            continue
        if start <= record_time < end:
            filtered.append((record_time, record))

    return filtered, admin_view

def normalize_text(value):
    if value is None:
        return "-"
    text = str(value).strip()
    return text if text else "-"

def record_tid(record):
    extracted = record.get("extracted") or {}
    return normalize_text(extracted.get("tid"))

def record_merchant(record):
    extracted = record.get("extracted") or {}
    return normalize_text(extracted.get("nama_merchant"))

def record_user_name(record):
    user = record.get("user") or {}
    return normalize_text(user.get("nama"))

def record_status_label(record):
    status = normalize_text(record.get("status"))
    return STATUS_LABELS.get(status, status)

def is_failed_record(record):
    return record.get("status") != "success"

def format_log_entry(record_time, record):
    error = normalize_text(record.get("error"))
    if len(error) > 120:
        error = error[:117] + "..."

    return (
        f"- {record_time.strftime('%d/%m/%Y %H:%M')} | "
        f"{record_status_label(record)} | "
        f"TID {record_tid(record)} | "
        f"{record_merchant(record)} | "
        f"{record_user_name(record)} | "
        f"{error}"
    )

def build_scope_label(user_data, admin_view):
    if admin_view:
        return "semua user"
    return normalize_text(user_data.get("nama"))

def send_plain_chunks(message, text):
    max_length = 3800
    lines = text.splitlines()
    chunks = []
    current = ""

    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= max_length:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = line

    if current:
        chunks.append(current)

    for index, chunk in enumerate(chunks):
        bot.send_message(
            message.chat.id,
            chunk,
            reply_to_message_id=message.message_id if index == 0 else None,
        )

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
@bot.message_handler(commands=['help'])
def cmd_help(message):
    bot.reply_to(message, HELP_TEXT)

@bot.message_handler(commands=['start'])
def cmd_start(message):
    chat_id = str(message.chat.id)
    users = load_users()

    if chat_id in users:
        bot.reply_to(message, f"Halo {users[chat_id]['nama']}! Akun kamu sudah terdaftar. Kirim foto struk untuk upload, atau ketik /help untuk melihat command.")
        return

    bot.reply_to(message, "Kamu belum terdaftar. Ketik /register untuk mulai.")

@bot.message_handler(commands=['register'])
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

@bot.message_handler(commands=['rekap'])
def cmd_rekap(message):
    user_data = require_registered_user(message)
    if not user_data:
        return

    try:
        start, end, label = parse_month_filter(get_command_args(message))
    except ValueError as e:
        bot.reply_to(message, str(e))
        return

    records, admin_view = logs_for_request(message, start, end)
    status_counts = {}
    failed_records = []

    for record_time, record in records:
        status = normalize_text(record.get("status"))
        status_counts[status] = status_counts.get(status, 0) + 1
        if is_failed_record(record):
            failed_records.append((record_time, record))

    total = len(records)
    success_count = status_counts.get("success", 0)
    failed_count = total - success_count
    failed_tids = {
        record_tid(record)
        for _, record in failed_records
        if record_tid(record) != "-"
    }

    lines = [
        f"Rekap upload {label}",
        f"Scope: {build_scope_label(user_data, admin_view)}",
        f"Total log: {total}",
        f"Sukses: {success_count}",
        f"Gagal: {failed_count}",
        f"TID gagal unik: {len(failed_tids)}",
    ]

    if status_counts:
        lines.append("")
        lines.append("Rincian status:")
        for status, count in sorted(status_counts.items()):
            label_status = STATUS_LABELS.get(status, status)
            lines.append(f"- {label_status}: {count}")

    if failed_records:
        lines.append("")
        lines.append("5 gagal terbaru:")
        for record_time, record in sorted(failed_records, key=lambda item: item[0], reverse=True)[:5]:
            lines.append(format_log_entry(record_time, record))

    send_plain_chunks(message, "\n".join(lines))

@bot.message_handler(commands=['gagal'])
def cmd_gagal(message):
    user_data = require_registered_user(message)
    if not user_data:
        return

    try:
        start, end, label = parse_month_filter(get_command_args(message))
    except ValueError as e:
        bot.reply_to(message, str(e))
        return

    records, admin_view = logs_for_request(message, start, end)
    failures = sorted(
        [(record_time, record) for record_time, record in records if is_failed_record(record)],
        key=lambda item: item[0],
        reverse=True,
    )

    lines = [
        f"Daftar gagal {label}",
        f"Scope: {build_scope_label(user_data, admin_view)}",
        f"Total gagal: {len(failures)}",
    ]

    if not failures:
        lines.append("Tidak ada log gagal pada periode ini.")
        send_plain_chunks(message, "\n".join(lines))
        return

    shown = failures[:LOG_DETAIL_LIMIT]
    if len(failures) > LOG_DETAIL_LIMIT:
        lines.append(f"Ditampilkan {LOG_DETAIL_LIMIT} terbaru. Naikkan LOG_DETAIL_LIMIT jika perlu audit lebih panjang.")

    lines.append("")
    for record_time, record in shown:
        lines.append(format_log_entry(record_time, record))

    send_plain_chunks(message, "\n".join(lines))

@bot.message_handler(commands=['gagal_tid'])
def cmd_gagal_tid(message):
    user_data = require_registered_user(message)
    if not user_data:
        return

    try:
        start, end, label = parse_month_filter(get_command_args(message))
    except ValueError as e:
        bot.reply_to(message, str(e))
        return

    records, admin_view = logs_for_request(message, start, end)
    failed_by_tid = {}

    for record_time, record in records:
        if not is_failed_record(record):
            continue

        tid = record_tid(record)
        item = failed_by_tid.setdefault(tid, {
            "count": 0,
            "last_time": record_time,
            "merchant": record_merchant(record),
            "status_counts": {},
            "error": normalize_text(record.get("error")),
        })
        item["count"] += 1
        status = record_status_label(record)
        item["status_counts"][status] = item["status_counts"].get(status, 0) + 1

        if record_time >= item["last_time"]:
            item["last_time"] = record_time
            item["merchant"] = record_merchant(record)
            item["error"] = normalize_text(record.get("error"))

    rows = sorted(failed_by_tid.items(), key=lambda item: item[1]["last_time"], reverse=True)
    lines = [
        f"TID gagal {label}",
        f"Scope: {build_scope_label(user_data, admin_view)}",
        f"Total TID: {len(rows)}",
    ]

    if not rows:
        lines.append("Tidak ada TID gagal pada periode ini.")
        send_plain_chunks(message, "\n".join(lines))
        return

    shown = rows[:LOG_DETAIL_LIMIT]
    if len(rows) > LOG_DETAIL_LIMIT:
        lines.append(f"Ditampilkan {LOG_DETAIL_LIMIT} terbaru. Naikkan LOG_DETAIL_LIMIT jika perlu audit lebih panjang.")

    lines.append("")
    for tid, item in shown:
        status_parts = ", ".join(f"{status} {count}x" for status, count in sorted(item["status_counts"].items()))
        error = item["error"]
        if len(error) > 90:
            error = error[:87] + "..."
        lines.append(
            f"- TID {tid} | {item['count']}x | terakhir {item['last_time'].strftime('%d/%m/%Y %H:%M')} | "
            f"{status_parts} | {item['merchant']} | {error}"
        )

    send_plain_chunks(message, "\n".join(lines))

@bot.message_handler(commands=['tid'])
def cmd_tid(message):
    user_data = require_registered_user(message)
    if not user_data:
        return

    args = get_command_args(message).split()
    if not args:
        bot.reply_to(message, "Format: /tid <TID> [YYYY-MM|bulan_ini|bulan_lalu]")
        return

    tid_query = args[0].strip()
    month_arg = args[1] if len(args) > 1 else ""

    try:
        start, end, label = parse_month_filter(month_arg)
    except ValueError as e:
        bot.reply_to(message, str(e))
        return

    records, admin_view = logs_for_request(message, start, end)
    matches = sorted(
        [
            (record_time, record)
            for record_time, record in records
            if record_tid(record).lower() == tid_query.lower()
        ],
        key=lambda item: item[0],
        reverse=True,
    )

    success_count = sum(1 for _, record in matches if record.get("status") == "success")
    failed_count = len(matches) - success_count
    lines = [
        f"Riwayat TID {tid_query} {label}",
        f"Scope: {build_scope_label(user_data, admin_view)}",
        f"Total log: {len(matches)}",
        f"Sukses: {success_count}",
        f"Gagal: {failed_count}",
    ]

    if not matches:
        lines.append("Tidak ada log untuk TID ini pada periode tersebut.")
        send_plain_chunks(message, "\n".join(lines))
        return

    shown = matches[:LOG_DETAIL_LIMIT]
    if len(matches) > LOG_DETAIL_LIMIT:
        lines.append(f"Ditampilkan {LOG_DETAIL_LIMIT} terbaru. Naikkan LOG_DETAIL_LIMIT jika perlu audit lebih panjang.")

    lines.append("")
    for record_time, record in shown:
        lines.append(format_log_entry(record_time, record))

    send_plain_chunks(message, "\n".join(lines))

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
