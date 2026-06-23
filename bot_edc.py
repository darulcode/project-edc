"""
========================================
  Google Form Auto-Filler via Telegram
  Bot: bot_edc.py (Refactored)
========================================
"""
from playwright.sync_api import sync_playwright
import time
import os

# ============================================================
#     CONFIG
# ============================================================
# https://docs.google.com/forms/d/e/1FAIpQLSfpFIDNslDLq6p9D3XYru8AFrRUKso_YMSjKnfmMfaOMHCoXg/viewform -> real form
# https://docs.google.com/forms/d/e/1FAIpQLSeyQd0QFilraMWZSQQkqoKreMuvHhV5FSrRdOE6ol70v5W82A/viewform -> test form
FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLSfpFIDNslDLq6p9D3XYru8AFrRUKso_YMSjKnfmMfaOMHCoXg/viewform"
DELAY           = 0.5
UPLOAD_TIMEOUT  = 45

# ============================================================
#     HELPER FUNCTIONS
# ============================================================
def wait(factor=1.0):
    time.sleep(DELAY * factor)

def fill_form_field(page, question_name, value):
    try:
        # get_by_role akan otomatis membaca aria-labelledby dari Google Form.
        # exact=False dibiarkan default agar tetap bisa mendeteksi pertanyaan yang ada tanda bintang merah (wajib/required).
        field = page.get_by_role("textbox", name=question_name)
        
        # Tambahkan timeout 5 detik agar jika tidak ketemu, bot tidak macet (stuck) 30 detik
        field.wait_for(state="visible", timeout=5000)
        
        field.click(force=True)
        field.fill(value)
        print(f"    [{question_name}] diisi: {value}")
    except Exception as e:
        print(f"     Gagal isi [{question_name}]: {e}")

def fill_text_by_title(page, question_title, value):
    try:
        # 1. Cari div (blok pertanyaan) yang mengandung teks judul pertanyaan (misal: "PN" atau "TID")
        # 2. Cari input type text di dalam blok tersebut
        input_locator = page.locator(f'div[role="listitem"]:has-text("{question_title}")').locator('input[type="text"]')
        
        # Isi form pada elemen yang ditemukan
        input_locator.first.click(force=True)
        input_locator.first.fill(value)
        print(f"    [{question_title}] diisi: {value}")
    except Exception as e:
        print(f"     Gagal isi [{question_title}]: {e}")

def fill_date_field(page, date_str):
    try:
        dd, mm, yyyy = date_str.split("/")
        standard_format = f"{yyyy}-{mm}-{dd}"
        date_input = page.locator('input[type="date"]').first
        date_input.fill(standard_format)
        print(f"    [Tanggal] diisi: {date_str}")
    except Exception as e:
        print(f"     Gagal isi tanggal: {e}")

def fill_time_field(page, time_str):
    try:
        hh, mm = time_str.split(":")
        hour_input = page.locator('[aria-label*="Hour"], [aria-label*="Jam"]').first
        min_input  = page.locator('[aria-label*="Minute"], [aria-label*="Menit"]').first
        hour_input.fill(hh)
        min_input.fill(mm)
        print(f"    [Waktu] diisi: {time_str}")
    except Exception as e:
        print(f"     Gagal isi waktu: {e}")

def select_radio(page, label_text):
    try:
        btn = page.locator(f'[data-value="{label_text}"]').first
        if btn.count() > 0:
            btn.click()
            print(f"    [Radio] dipilih: {label_text}")
            return
    except Exception:
        pass
    try:
        page.locator(f"span:text-is('{label_text}')").first.click()
        print(f"    [Radio] dipilih: {label_text}")
    except Exception as e:
        print(f"     Gagal pilih radio '{label_text}': {e}")

def select_dropdown(page, option_label, dropdown_index=0):
    try:
        # Klik kotak dropdown
        dropdown_btn = page.locator('div[role="listbox"]').nth(dropdown_index)
        dropdown_btn.click(timeout=10000)
        
        # WAJIB ada jeda statis. Jangan gunakan wait_for_state karena 
        # elemen Google Form sering kali memanipulasi DOM saat animasi berjalan.
        time.sleep(1.5) 
        
        # Coba cara 1: Cari text yang persis sama di dalam span
        option = page.locator(f'div[role="option"] span:text-is("{option_label}")').first
        
        # Jika cara 1 tidak ketemu atau tidak terlihat, gunakan cara 2 (has-text)
        if not option.is_visible():
            option = page.locator(f'div[role="option"]:has-text("{option_label}")').first
            
        # Gunakan force=True agar Playwright tetap memaksa klik walaupun 
        # menganggap elemen tersebut masih tertutup bayangan transparan (overlay)
        option.click(force=True, timeout=10000)
        
        print(f"    [Dropdown] berhasil dipilih: {option_label}")
        
        # Jeda sejenak agar dropdown tertutup sebelum lanjut ke isian berikutnya
        time.sleep(1)
        
    except Exception as e:
        print(f"     Gagal pilih dropdown '{option_label}': {e}")

# ============================================================
# DI DALAM bot_edc.py
# ============================================================

def upload_photo(page, file_path, field_label="foto", btn_index=0):
    abs_path = os.path.abspath(file_path)
    file_name = os.path.basename(abs_path)
    if not os.path.exists(abs_path):
        print(f"     File tidak ditemukan: {abs_path}")
        return
        
    try:
        print(f"    Mengupload [{field_label}]: {file_name}")
        add_btn = page.locator('div[role="button"]:has-text("Tambahkan"), div[role="button"]:has-text("Add")').nth(btn_index)
        add_btn.click(timeout=15000)
        
        picker_frame = page.frame_locator('iframe.picker-frame, iframe[src*="docs.google.com/picker"]').last
        file_input = picker_frame.locator('input[type="file"]').first
        file_input.wait_for(state="attached", timeout=15000)
        
        with page.expect_file_chooser(timeout=15000) as fc_info:
            file_input.evaluate("node => node.click()")
        file_chooser = fc_info.value
        file_chooser.set_files(abs_path)
        
        print(f"       Menunggu proses upload file...")
        # Tunggu indikator file yang terupload muncul di form
        page.locator(f'text="{file_name}"').first.wait_for(state="visible", timeout=UPLOAD_TIMEOUT * 1000)
        print(f"    [{field_label}] berhasil diupload\n")
        
        time.sleep(2)
        
    except Exception as e:
        print(f"     Gagal upload [{field_label}]: {e}")
        try:
            page.keyboard.press("Escape") # Tutup dialog upload jika masih terbuka
            time.sleep(1)
        except:
            pass
        # LEMPAR ERROR SECARA EKSPLISIT AGAR DITANGKAP OLEH run_bot
        raise Exception(f"UploadTimeout: Gagal upload {field_label}")

def run_bot(data_form):
    print("=" * 55)
    print("    Playwright Eksekusi: Mulai Mengisi Form")
    print("=" * 55)
    
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp("http://localhost:9222")
            context = browser.contexts[0]
            page = context.new_page()
        except Exception:
            raise Exception("Gagal terhubung ke Microsoft Edge (Port 9222 belum terbuka)")
            
        page.goto(FORM_URL, timeout=60000)
        page.wait_for_load_state("networkidle")
        
        try:
            # --- 1. Email (Checkbox) ---
            try:
                email_checkbox = page.locator('div[role="checkbox"]').first
                if email_checkbox.is_visible() and email_checkbox.get_attribute("aria-checked") != "true":
                    email_checkbox.click()
            except Exception:
                pass
                
            # --- 2. Tanggal & Waktu ---
            fill_date_field(page, data_form["tanggal"])
            fill_time_field(page, data_form["waktu"])
            
            # --- 3. Branch Office ---
            select_radio(page, data_form["branch_office"])
            
            # --- 4 s/d 7. Data Petugas & Merchant ---
            fill_form_field(page, "PN", data_form['pn'])
            select_dropdown(page, data_form["nama_petugas_it"], dropdown_index=0)
            fill_form_field(page, "TID", data_form['tid'])
            fill_form_field(page, "Nama Merchant", data_form['nama_merchant'])
            
            # --- 8. Kondisi EDC ---
            select_radio(page, data_form["kondisi_edc"])
            
            # --- 9 & 10. Upload Foto (Bisa memicu Exception jika gagal) ---
            upload_photo(page, data_form["foto_struk"], "Foto Struk Transaksi", btn_index=0)
            upload_photo(page, data_form["foto_toko"], "Foto Toko / Merchant", btn_index=1)
            
            # --- SUBMIT FORM ---
            submit_selectors = [
                'div[role="button"]:has-text("Kirim")',
                'div[role="button"]:has-text("Submit")',
                '[aria-label="Submit"]',
                '[aria-label="Kirim"]'
            ]
            submitted = False
            for sel in submit_selectors:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible():
                        btn.click()
                        submitted = True
                        break
                except Exception:
                    continue
            
            if submitted:
                try:
                    page.wait_for_url(r".*/formResponse.*", timeout=10000)
                    print("    Form berhasil disubmit!\n")
                except:
                    print("    Tombol diklik, tetapi konfirmasi respons tidak terdeteksi.\n")
            else:
                print("     Tombol submit tidak ditemukan.\n")
                
        except Exception as e:
            # TANGKAP ERROR DARI UPLOAD ATAU PENGISIAN
            print(f"    [Error Pengisian/Upload]: {e}")
            
            if "UploadTimeout" in str(e):
                print("    Mencoba membersihkan form sebelum menutup browser...")
                try:
                    # Daftar kemungkinan teks tombol pembersih di Google Form (Support Bahasa & English)
                    clear_selectors = [
                        'div[role="button"]:has-text("Bersihkan form")',
                        'div[role="button"]:has-text("Kosongkan formulir")',
                        'div[role="button"]:has-text("Clear form")',
                        'span:has-text("Bersihkan form")',
                        'span:has-text("Kosongkan formulir")'
                    ]
                    
                    # 1. Cari dan klik tombol "Bersihkan form" di halaman utama
                    btn_clear_found = False
                    for sel in clear_selectors:
                        try:
                            btn = page.locator(sel).first
                            if btn.is_visible(timeout=1500):
                                btn.click()
                                btn_clear_found = True
                                print(f"    Tombol bersihkan form ({sel}) ditemukan dan diklik.")
                                break
                        except:
                            continue
                    
                    # 2. Jika tombol awal berhasil diklik, cari tombol konfirmasi di pop-up
                    if btn_clear_found:
                        time.sleep(1) # Tunggu animasi pop-up konfirmasi muncul
                        btn_confirm_found = False
                        for sel in clear_selectors:
                            try:
                                # Tombol konfirmasi biasanya ada di urutan terakhir (di dalam dialog pop-up)
                                btn_confirm = page.locator(sel).last
                                if btn_confirm.is_visible(timeout=1500):
                                    btn_confirm.click()
                                    btn_confirm_found = True
                                    break
                            except:
                                continue
                        
                        if btn_confirm_found:
                            time.sleep(2) # Tunggu proses pembersihan selesai
                            print("    ✅ Form berhasil dibersihkan dengan sempurna.")
                        else:
                            print("    ⚠️ Klik awal berhasil, tapi tombol konfirmasi di pop-up tidak ditemukan.")
                    else:
                        print("    ⚠️ Tombol 'Bersihkan form' tidak ditemukan di halaman.")
                        
                except Exception as clear_err:
                    print(f"    ⚠️ Gagal saat mengeksekusi pembersihan form: {clear_err}")
                
                # Lempar ulang pesan error ke edc_agent.py
                raise Exception("koneksi lambat saat upload foto")
            else:
                # Jika error selain upload, lempar apa adanya
                raise e
                
        finally:
            # BLOK INI SELALU DIEKSEKUSI TERAKHIR
            # Memastikan browser/page ditutup HANYA SETELAH form dibersihkan
            print("    Menutup halaman browser...")
            try:
                page.close()
            except:
                pass
            
    print("\n" + "=" * 55)
    print("    Bot Playwright selesai berjalan.")
    print("=" * 55)

if __name__ == "__main__":
    print("Jalankan bot melalui file: python edc_agent.py")