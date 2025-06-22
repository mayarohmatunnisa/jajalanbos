from flask import Flask, request, render_template, jsonify, redirect, url_for, session
from flask_socketio import SocketIO
import shlex
import subprocess
import os
import logging
from functools import wraps
import re
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import json
from flask_cors import CORS
from filelock import FileLock
from pytz import timezone # Pastikan pytz terinstal: pip install pytz
from threading import Lock
import shutil
from flask import send_from_directory
from apscheduler.jobstores.base import JobLookupError # Tambahkan import ini

# Konfigurasi logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [%(levelname)s] %(message)s')

# Path Konfigurasi
SESSION_FILE = '/root/StreamHibV2/sessions.json'
LOCK_FILE = SESSION_FILE + '.lock'
VIDEO_DIR = "videos"
SERVICE_DIR = "/etc/systemd/system"
USERS_FILE = '/root/StreamHibV2/users.json'
os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
os.makedirs(VIDEO_DIR, exist_ok=True)

# ---- TAMBAHKAN KONFIGURASI MODE TRIAL DI SINI ----
TRIAL_MODE_ENABLED = False  # Ganti menjadi False/true untuk mengubah
TRIAL_RESET_HOURS = 2    # Atur interval reset (dalam jam)

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "http://localhost:5000", "supports_credentials": True}})
app.secret_key = "emuhib"
socketio = SocketIO(app, async_mode='eventlet')
socketio_lock = Lock()
app.permanent_session_lifetime = timedelta(hours=12)
jakarta_tz = timezone('Asia/Jakarta')


# Fungsi Helper
# Tambahkan fungsi ini di bagian fungsi helper di app.py
# ... (fungsi helper lain seperti sanitize_for_service_name, read_sessions, dll.) ...
def trial_reset():
    if not TRIAL_MODE_ENABLED:
        logging.info("Mode trial tidak aktif, proses reset dilewati.")
        return

    logging.info("MODE TRIAL: Memulai proses reset aplikasi...")
    try:
        s_data = read_sessions()
        active_sessions_copy = list(s_data.get('active_sessions', []))
        
        logging.info(f"MODE TRIAL: Menghentikan dan menghapus {len(active_sessions_copy)} sesi aktif...")
        for item in active_sessions_copy:
            # Gunakan sanitized_service_id yang sudah ada jika ada, jika tidak, buat dari ID (nama sesi asli)
            sanitized_id_service = item.get('sanitized_service_id')
            if not sanitized_id_service: # Fallback jika tidak ada, seharusnya jarang terjadi
                sanitized_id_service = sanitize_for_service_name(item.get('id', f'unknown_id_{datetime.now().timestamp()}'))
            
            service_name_to_stop = f"stream-{sanitized_id_service}.service"
            try:
                subprocess.run(["systemctl", "stop", service_name_to_stop], check=False, timeout=15)
                service_path_to_stop = os.path.join(SERVICE_DIR, service_name_to_stop)
                if os.path.exists(service_path_to_stop):
                    os.remove(service_path_to_stop)
                logging.info(f"MODE TRIAL: Service {service_name_to_stop} dihentikan dan dihapus.")
                
                # Pindahkan sesi ke inactive
                item['status'] = 'inactive'
                item['stop_time'] = datetime.now(jakarta_tz).isoformat() 
                # Pertahankan durasi_minutes jika ada, atau set default 0
                item['duration_minutes'] = item.get('duration_minutes', 0)
                s_data['inactive_sessions'] = add_or_update_session_in_list(
                    s_data.get('inactive_sessions', []), item
                )
            except Exception as e_stop:
                logging.error(f"MODE TRIAL: Gagal menghentikan/menghapus service {service_name_to_stop}: {e_stop}")
        s_data['active_sessions'] = [] # Kosongkan sesi aktif setelah diproses

        try:
            subprocess.run(["systemctl", "daemon-reload"], check=False, timeout=10)
        except Exception as e_reload:
            logging.error(f"MODE TRIAL: Gagal daemon-reload: {e_reload}")

        logging.info(f"MODE TRIAL: Menghapus semua ({len(s_data.get('scheduled_sessions', []))}) jadwal...")
        scheduled_sessions_copy = list(s_data.get('scheduled_sessions', []))
        for sched_item in scheduled_sessions_copy:
            sanitized_id = sched_item.get('sanitized_service_id')
            schedule_def_id = sched_item.get('id') # Ini adalah ID definisi jadwal seperti 'daily-XYZ' atau 'onetime-XYZ'
            recurrence = sched_item.get('recurrence_type')

            if not sanitized_id or not schedule_def_id:
                logging.warning(f"MODE TRIAL: Melewati item jadwal karena sanitized_id atau schedule_def_id kurang: {sched_item}")
                continue

            if recurrence == 'daily':
                try: scheduler.remove_job(f"daily-start-{sanitized_id}")
                except JobLookupError: logging.info(f"MODE TRIAL: Job daily-start-{sanitized_id} tidak ditemukan untuk dihapus.")
                try: scheduler.remove_job(f"daily-stop-{sanitized_id}")
                except JobLookupError: logging.info(f"MODE TRIAL: Job daily-stop-{sanitized_id} tidak ditemukan untuk dihapus.")
            elif recurrence == 'one_time':
                try: scheduler.remove_job(schedule_def_id) # schedule_def_id adalah ID job start untuk one-time
                except JobLookupError: logging.info(f"MODE TRIAL: Job {schedule_def_id} (one-time start) tidak ditemukan untuk dihapus.")
                if not sched_item.get('is_manual_stop', sched_item.get('duration_minutes', 0) == 0):
                    try: scheduler.remove_job(f"onetime-stop-{sanitized_id}")
                    except JobLookupError: logging.info(f"MODE TRIAL: Job onetime-stop-{sanitized_id} tidak ditemukan untuk dihapus.")
        s_data['scheduled_sessions'] = []

        logging.info(f"MODE TRIAL: Menghapus semua file video...")
        videos_to_delete = get_videos_list_data() # Dapatkan daftar video sebelum menghapus
        for video_file in videos_to_delete:
            try:
                os.remove(os.path.join(VIDEO_DIR, video_file))
                logging.info(f"MODE TRIAL: File video {video_file} dihapus.")
            except Exception as e_vid_del:
                logging.error(f"MODE TRIAL: Gagal menghapus file video {video_file}: {e_vid_del}")
        
        write_sessions(s_data) # Simpan perubahan pada sessions.json
        
        # Kirim pembaruan ke semua klien melalui SocketIO
        with socketio_lock:
            socketio.emit('sessions_update', get_active_sessions_data())
            socketio.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
            socketio.emit('schedules_update', get_schedules_list_data())
            socketio.emit('videos_update', get_videos_list_data()) # Daftar video akan kosong
            socketio.emit('trial_reset_notification', { # Kirim notifikasi reset
                'message': 'Aplikasi telah direset karena mode trial. Semua sesi dan video telah dihapus.'
            })
            # Kirim status trial terbaru (opsional, jika ingin indikator trial selalu update)
            socketio.emit('trial_status_update', {
                'is_trial': TRIAL_MODE_ENABLED,
                'message': 'Mode Trial Aktif - Reset setiap {} jam.'.format(TRIAL_RESET_HOURS) if TRIAL_MODE_ENABLED else ''
            })

        logging.info("MODE TRIAL: Proses reset aplikasi selesai.")

    except Exception as e:
        logging.error(f"MODE TRIAL: Error besar selama proses reset: {e}", exc_info=True)

def add_or_update_session_in_list(session_list, new_session_item):
    session_id = new_session_item.get('id')
    if not session_id:
        logging.warning("Sesi tidak memiliki ID, tidak dapat ditambahkan/diperbarui dalam daftar.")
        # Kembalikan list asli jika tidak ada ID, atau handle error sesuai kebutuhan
        return session_list 

    # Hapus item lama jika ada ID yang sama
    updated_list = [s for s in session_list if s.get('id') != session_id]
    updated_list.append(new_session_item)
    return updated_list

def sanitize_for_service_name(session_name_original):
    # Fungsi ini HANYA untuk membuat nama file service yang aman.
    # Ganti karakter non-alfanumerik (kecuali underscore dan strip) dengan strip.
    # Juga pastikan tidak terlalu panjang dan tidak dimulai/diakhiri dengan strip.
    sanitized = re.sub(r'[^\w-]', '-', str(session_name_original))
    sanitized = re.sub(r'-+', '-', sanitized) # Ganti strip berurutan dengan satu strip
    sanitized = sanitized.strip('-') # Hapus strip di awal/akhir
    return sanitized[:50] # Batasi panjang untuk keamanan nama file

def create_service_file(session_name_original, video_path, platform_url, stream_key):
    # Gunakan session_name_original untuk deskripsi, tapi nama service disanitasi
    sanitized_service_part = sanitize_for_service_name(session_name_original)
    service_name = f"stream-{sanitized_service_part}.service"
    # Pastikan service_name unik jika sanitasi menghasilkan nama yang sama untuk session_name_original yang berbeda
    # Ini bisa diatasi dengan menambahkan hash pendek atau timestamp jika diperlukan, tapi untuk sekarang kita jaga sederhana.
    # Jika ada potensi konflik nama service yang tinggi, pertimbangkan untuk menggunakan UUID atau hash dari session_name_original.

    service_path = os.path.join(SERVICE_DIR, service_name)
    service_content = f"""[Unit]
Description=Streaming service for {session_name_original}
After=network.target

[Service]
ExecStart=/usr/bin/ffmpeg -stream_loop -1 -re -i "{video_path}" -f flv -c:v copy -c:a copy {platform_url}/{stream_key}
Restart=always
User=root
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
"""
    try:
        with open(service_path, 'w') as f: f.write(service_content)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        logging.info(f"Service file created: {service_name} (from original: '{session_name_original}')")
        return service_name, sanitized_service_part # Kembalikan juga bagian yang disanitasi untuk ID
    except Exception as e:
        logging.error(f"Error creating service file {service_name} (from original: '{session_name_original}'): {e}")
        raise

def read_sessions():
    if not os.path.exists(SESSION_FILE):
        write_sessions({"active_sessions": [], "inactive_sessions": [], "scheduled_sessions": []})
        return {"active_sessions": [], "inactive_sessions": [], "scheduled_sessions": []}
    try:
        with FileLock(LOCK_FILE, timeout=10):
            with open(SESSION_FILE, 'r') as f:
                content = json.load(f)
                content.setdefault('active_sessions', [])
                content.setdefault('inactive_sessions', [])
                content.setdefault('scheduled_sessions', [])
                return content
    except json.JSONDecodeError:
        logging.error(f"Error decoding JSON from {SESSION_FILE}. Re-initializing.")
        write_sessions({"active_sessions": [], "inactive_sessions": [], "scheduled_sessions": []})
        return {"active_sessions": [], "inactive_sessions": [], "scheduled_sessions": []}
    except Exception as e:
        logging.error(f"Error reading {SESSION_FILE}: {e}")
        return {"active_sessions": [], "inactive_sessions": [], "scheduled_sessions": []}


def write_sessions(data):
    try:
        with FileLock(LOCK_FILE, timeout=10):
            with open(SESSION_FILE, 'w') as f: json.dump(data, f, indent=4)
    except Exception as e:
        logging.error(f"Error writing to {SESSION_FILE}: {e}")
        raise

def read_users():
    if not os.path.exists(USERS_FILE):
        write_users({}) 
        return {}
    try:
        with open(USERS_FILE, 'r') as f: return json.load(f)
    except Exception as e:
        logging.error(f"Error reading {USERS_FILE}: {e}")
        return {}

def write_users(data):
    try:
        with open(USERS_FILE, 'w') as f: json.dump(data, f, indent=4)
    except Exception as e:
        logging.error(f"Error writing to {USERS_FILE}: {e}")
        raise

def get_videos_list_data():
    try:
        return sorted([f for f in os.listdir(VIDEO_DIR) if f.endswith(('.mp4', '.mkv', '.flv', '.avi', '.mov', '.webm'))])
    except Exception: return []

def get_active_sessions_data():
    try:
        output = subprocess.check_output(["systemctl", "list-units", "--type=service", "--state=running"], text=True)
        all_sessions_data = read_sessions() 
        active_sessions_list = []
        active_services_systemd = {line.split()[0] for line in output.strip().split('\n') if "stream-" in line}
        json_active_sessions = all_sessions_data.get('active_sessions', [])
        needs_json_update = False

        for service_name_systemd in active_services_systemd:
            sanitized_id_from_systemd_service = service_name_systemd.replace("stream-", "").replace(".service", "")
            
            session_json = next((s for s in json_active_sessions if s.get('sanitized_service_id') == sanitized_id_from_systemd_service), None)

            if session_json: # Ketika sesi ditemukan di sessions.json
                actual_schedule_type = session_json.get('scheduleType', 'manual')
                actual_stop_time_iso = session_json.get('stopTime') 
                formatted_display_stop_time = None
                if actual_stop_time_iso: 
                    try:
                        stop_time_dt = datetime.fromisoformat(actual_stop_time_iso)
                        formatted_display_stop_time = stop_time_dt.astimezone(jakarta_tz).strftime('%d-%m-%Y Pukul %H:%M:%S')
                    except ValueError: pass
                
                active_sessions_list.append({
                    'id': session_json.get('id'), 
                    'name': session_json.get('id'), 
                    'startTime': session_json.get('start_time', 'unknown'),
                    'platform': session_json.get('platform', 'unknown'),
                    'video_name': session_json.get('video_name', 'unknown'),
                    'stream_key': session_json.get('stream_key', 'unknown'), # <<< TAMBAHKAN BARIS INI
                    'stopTime': formatted_display_stop_time, 
                    'scheduleType': actual_schedule_type,
                    'sanitized_service_id': session_json.get('sanitized_service_id')
                })
            
            else: # Ketika service aktif di systemd tapi tidak ada di all_sessions_data['active_sessions']
                logging.warning(f"Service {service_name_systemd} (ID sanitasi: {sanitized_id_from_systemd_service}) aktif tapi tidak di JSON active_sessions. Mencoba memulihkan...")
                
                scheduled_definition = next((
                    sched for sched in all_sessions_data.get('scheduled_sessions', []) 
                    if sched.get('sanitized_service_id') == sanitized_id_from_systemd_service
                ), None)

                session_id_original = f"recovered-{sanitized_id_from_systemd_service}" # Fallback
                video_name_to_use = "unknown (recovered)"
                stream_key_to_use = "unknown"
                platform_to_use = "unknown"
                schedule_type_to_use = "manual_recovered" 
                recovered_stop_time_iso = None
                recovered_duration_minutes = 0
                
                current_recovery_time_iso = datetime.now(jakarta_tz).isoformat()
                current_recovery_dt = datetime.fromisoformat(current_recovery_time_iso)
                formatted_display_stop_time_frontend = None

                if scheduled_definition:
                    logging.info(f"Definisi jadwal ditemukan untuk service {service_name_systemd}: {scheduled_definition.get('session_name_original')}")
                    session_id_original = scheduled_definition.get('session_name_original', session_id_original)
                    video_name_to_use = scheduled_definition.get('video_file', video_name_to_use)
                    stream_key_to_use = scheduled_definition.get('stream_key', stream_key_to_use)
                    platform_to_use = scheduled_definition.get('platform', platform_to_use)
                    
                    recurrence = scheduled_definition.get('recurrence_type')
                    if recurrence == 'daily':
                        schedule_type_to_use = "daily_recurring_instance_recovered"
                        daily_start_time_str = scheduled_definition.get('start_time_of_day')
                        daily_stop_time_str = scheduled_definition.get('stop_time_of_day')
                        if daily_start_time_str and daily_stop_time_str:
                            start_h, start_m = map(int, daily_start_time_str.split(':'))
                            stop_h, stop_m = map(int, daily_stop_time_str.split(':'))
                            
                            duration_daily_minutes = (stop_h * 60 + stop_m) - (start_h * 60 + start_m)
                            if duration_daily_minutes <= 0: 
                                duration_daily_minutes += 24 * 60 
                            recovered_duration_minutes = duration_daily_minutes
                            # Waktu berhenti untuk JSON (mungkin tidak secara aktif digunakan untuk stop harian, tapi untuk data)
                            recovered_stop_time_iso = (current_recovery_dt + timedelta(minutes=recovered_duration_minutes)).isoformat()
                            
                            # Waktu berhenti untuk tampilan frontend (berdasarkan jadwal aktual)
                            intended_stop_today_dt = current_recovery_dt.replace(hour=stop_h, minute=stop_m, second=0, microsecond=0)
                            actual_scheduled_stop_dt = intended_stop_today_dt if current_recovery_dt <= intended_stop_today_dt else (intended_stop_today_dt + timedelta(days=1))
                            formatted_display_stop_time_frontend = actual_scheduled_stop_dt.astimezone(jakarta_tz).strftime('%d-%m-%Y Pukul %H:%M:%S')
                        else:
                            schedule_type_to_use = "manual_recovered_daily_data_missing"
                            
                    elif recurrence == 'one_time':
                        schedule_type_to_use = "scheduled_recovered"
                        original_start_iso = scheduled_definition.get('start_time_iso')
                        duration_mins_sched = scheduled_definition.get('duration_minutes', 0)
                        is_manual_stop_sched = scheduled_definition.get('is_manual_stop', duration_mins_sched == 0)

                        if not is_manual_stop_sched and duration_mins_sched > 0 and original_start_iso:
                            original_start_dt = datetime.fromisoformat(original_start_iso)
                            intended_stop_dt = original_start_dt + timedelta(minutes=duration_mins_sched)
                            recovered_stop_time_iso = intended_stop_dt.isoformat() # Ini akan dipakai check_systemd_sessions
                            recovered_duration_minutes = duration_mins_sched
                            if current_recovery_dt >= intended_stop_dt:
                                schedule_type_to_use = "scheduled_recovered_overdue"
                            formatted_display_stop_time_frontend = intended_stop_dt.astimezone(jakarta_tz).strftime('%d-%m-%Y Pukul %H:%M:%S')
                        elif is_manual_stop_sched:
                             recovered_stop_time_iso = None # Akan tampil "Stop Manual"
                             recovered_duration_minutes = 0
                        else:
                             schedule_type_to_use = "manual_recovered_onetime_data_missing"
                # else: (jika tidak ada scheduled_definition, variabel tetap default "unknown")

                recovered_session_entry_for_json = {
                    "id": session_id_original,
                    "sanitized_service_id": sanitized_id_from_systemd_service, 
                    "video_name": video_name_to_use, "stream_key": stream_key_to_use, "platform": platform_to_use,
                    "status": "active", "start_time": current_recovery_time_iso,
                    "scheduleType": schedule_type_to_use,
                    "stopTime": recovered_stop_time_iso, # Ini adalah ISO string atau None
                    "duration_minutes": recovered_duration_minutes
                }
                
                all_sessions_data['active_sessions'] = add_or_update_session_in_list(
                    all_sessions_data.get('active_sessions', []), 
                    recovered_session_entry_for_json
                )
                needs_json_update = True
                
                active_sessions_list.append({
                    'id': recovered_session_entry_for_json['id'], 
                    'name': recovered_session_entry_for_json['id'], 
                    'startTime': recovered_session_entry_for_json['start_time'],
                    'platform': recovered_session_entry_for_json['platform'],
                    'video_name': recovered_session_entry_for_json['video_name'],
                    'stream_key': recovered_session_entry_for_json['stream_key'],
                    'stopTime': formatted_display_stop_time_frontend, # Ini adalah string yang sudah diformat atau None
                    'scheduleType': recovered_session_entry_for_json['scheduleType'],
                    'sanitized_service_id': recovered_session_entry_for_json['sanitized_service_id']
                })
        
        if needs_json_update: write_sessions(all_sessions_data)
        return sorted(active_sessions_list, key=lambda x: x.get('startTime', ''))
    except Exception as e: 
        logging.error(f"Error get_active_sessions_data: {e}", exc_info=True)
        return []

def get_inactive_sessions_data():
    try:
        data_sessions = read_sessions()
        inactive_list = []
        for item in data_sessions.get('inactive_sessions', []):
            item_details = {
                'id': item.get('id'), # Nama sesi asli
                'sanitized_service_id': item.get('sanitized_service_id'), # Untuk referensi jika perlu
                'video_name': item.get('video_name'),
                'stream_key': item.get('stream_key'),
                'platform': item.get('platform'),
                'status': item.get('status'),
                'start_time_original': item.get('start_time'), # Diubah dari 'start_time' menjadi 'start_time_original'
                'stop_time': item.get('stop_time'),
                'duration_minutes_original': item.get('duration_minutes') # Diubah dari 'duration_minutes'
            }
            inactive_list.append(item_details)
        return sorted(inactive_list, key=lambda x: x.get('stop_time', ''), reverse=True)
    except Exception: return []


def get_schedules_list_data():
    sessions_data = read_sessions()
    schedule_list = []

    for sched_json in sessions_data.get('scheduled_sessions', []):
        try:
            session_name_original = sched_json.get('session_name_original', 'N/A') # Nama sesi asli
            # ID definisi jadwal sekarang menggunakan sanitized_service_id untuk konsistensi
            item_id = sched_json.get('id') # Ini adalah ID definisi jadwal (misal "daily-NAMALAYANAN" atau "onetime-NAMALAYANAN")
            platform = sched_json.get('platform', 'N/A')
            video_file = sched_json.get('video_file', 'N/A')
            recurrence = sched_json.get('recurrence_type', 'one_time')

            display_entry = {
                'id': item_id, # ID definisi jadwal
                'session_name_original': session_name_original, # Nama asli untuk tampilan
                'video_file': video_file,
                'platform': platform,
                'stream_key': sched_json.get('stream_key', 'N/A'),
                'recurrence_type': recurrence,
                'sanitized_service_id': sched_json.get('sanitized_service_id') # Penting untuk cancel
            }

            if recurrence == 'daily':
                start_time_of_day = sched_json.get('start_time_of_day')
                stop_time_of_day = sched_json.get('stop_time_of_day')
                if not start_time_of_day or not stop_time_of_day:
                    logging.warning(f"Data jadwal harian tidak lengkap untuk {session_name_original}")
                    continue
                
                display_entry['start_time_display'] = f"Setiap hari pukul {start_time_of_day}"
                display_entry['stop_time_display'] = f"Berakhir pukul {stop_time_of_day}"
                display_entry['is_manual_stop'] = False
            
            elif recurrence == 'one_time':
                if not all(k in sched_json for k in ['start_time_iso', 'duration_minutes']):
                    logging.warning(f"Data jadwal one-time tidak lengkap untuk {session_name_original}")
                    continue
                
                start_dt_iso_val = sched_json['start_time_iso']
                start_dt = datetime.fromisoformat(start_dt_iso_val).astimezone(jakarta_tz)
                duration_mins = sched_json['duration_minutes']
                is_manual_stop_val = sched_json.get('is_manual_stop', duration_mins == 0)
                
                display_entry['start_time_iso'] = start_dt.isoformat()
                display_entry['start_time_display'] = start_dt.strftime('%d-%m-%Y %H:%M:%S')
                display_entry['stop_time_display'] = (start_dt + timedelta(minutes=duration_mins)).strftime('%d-%m-%Y %H:%M:%S') if not is_manual_stop_val else "Stop Manual"
                display_entry['is_manual_stop'] = is_manual_stop_val
            else:
                logging.warning(f"Tipe recurrence tidak dikenal: {recurrence} untuk sesi {session_name_original}")
                continue
            
            schedule_list.append(display_entry)

        except Exception as e:
            logging.error(f"Error memproses item jadwal {sched_json.get('session_name_original')}: {e}", exc_info=True)
            
    try:
        return sorted(schedule_list, key=lambda x: (x['recurrence_type'] == 'daily', x.get('start_time_iso', x['session_name_original'])))
    except TypeError:
        return sorted(schedule_list, key=lambda x: x['session_name_original'])


def check_systemd_sessions():
    try:
        active_sysd_services = {ln.split()[0] for ln in subprocess.check_output(["systemctl","list-units","--type=service","--state=running"],text=True).strip().split('\n') if "stream-" in ln}
        s_data = read_sessions()
        now_jakarta_dt = datetime.now(jakarta_tz)
        json_changed = False

        for sched_item in list(s_data.get('scheduled_sessions', [])): 
            if sched_item.get('recurrence_type', 'one_time') == 'daily': 
                continue
            if sched_item.get('is_manual_stop', False): continue
            
            try:
                start_dt = datetime.fromisoformat(sched_item['start_time_iso'])
                dur_mins = sched_item.get('duration_minutes', 0)
                if dur_mins <= 0: continue 
                stop_dt = start_dt + timedelta(minutes=dur_mins)
                # Gunakan sanitized_service_id dari definisi jadwal
                sanitized_service_id_from_schedule = sched_item.get('sanitized_service_id')
                if not sanitized_service_id_from_schedule:
                    logging.warning(f"CHECK_SYSTEMD: sanitized_service_id tidak ada di jadwal one-time {sched_item.get('session_name_original')}. Skip.")
                    continue
                serv_name = f"stream-{sanitized_service_id_from_schedule}.service"

                if now_jakarta_dt > stop_dt and serv_name in active_sysd_services:
                    logging.info(f"CHECK_SYSTEMD: Menghentikan sesi terjadwal (one-time) yang terlewat waktu: {sched_item['session_name_original']}")
                    stop_scheduled_streaming(sched_item['session_name_original']) 
                    json_changed = True 
            except Exception as e_sched_check:
                 logging.error(f"CHECK_SYSTEMD: Error memeriksa jadwal one-time {sched_item.get('session_name_original')}: {e_sched_check}")
        
        logging.debug("CHECK_SYSTEMD: Memeriksa sesi aktif yang mungkin terlewat waktu berhentinya...")
        for active_session_check in list(s_data.get('active_sessions', [])): # Iterasi salinan list
            stop_time_iso = active_session_check.get('stopTime') # 'stopTime' dari active_sessions
            session_id_to_check = active_session_check.get('id')
            sanitized_id_service_check = active_session_check.get('sanitized_service_id')

            if not session_id_to_check or not sanitized_id_service_check:
               logging.warning(f"CHECK_SYSTEMD (Fallback): Melewati sesi aktif {session_id_to_check or 'UNKNOWN'} karena ID atau sanitized_service_id kurang.")
               continue

            service_name_check = f"stream-{sanitized_id_service_check}.service"

         # Hanya proses jika stopTime ada, dan service-nya memang masih terdaftar sebagai aktif di systemd
            if stop_time_iso and service_name_check in active_sysd_services:
             try:
                 # Pastikan stop_time_dt dalam timezone yang sama dengan now_jakarta_dt untuk perbandingan
                 stop_time_dt = datetime.fromisoformat(stop_time_iso)
                 if stop_time_dt.tzinfo is None: # Jika naive, lokalkan ke Jakarta
                     stop_time_dt = jakarta_tz.localize(stop_time_dt)
                 else: # Jika sudah ada timezone, konversikan ke Jakarta
                     stop_time_dt = stop_time_dt.astimezone(jakarta_tz)

                 if now_jakarta_dt > stop_time_dt:
                     logging.info(f"CHECK_SYSTEMD (Fallback): Sesi aktif '{session_id_to_check}' (service: {service_name_check}) telah melewati waktu berhenti yang tercatat ({stop_time_iso}). Menghentikan sekarang...")
                     # Panggil fungsi stop_scheduled_streaming yang sudah ada.
                     # Fungsi ini sudah menangani pemindahan ke inactive_sessions, penghapusan service, dan update JSON.
                     stop_scheduled_streaming(session_id_to_check)
                     # Karena stop_scheduled_streaming sudah melakukan write_sessions dan emit socket,
                     # kita mungkin tidak perlu set json_changed = True di sini secara eksplisit
                     # HANYA untuk aksi stop ini, tapi perhatikan jika ada logika lain di check_systemd_sessions.
                     # Namun, untuk konsistensi bahwa ada perubahan, bisa saja ditambahkan.
                     json_changed = True # Menandakan ada perubahan pada sessions.json
             except ValueError:
                 logging.warning(f"CHECK_SYSTEMD (Fallback): Format stopTime ('{stop_time_iso}') tidak valid untuk sesi aktif '{session_id_to_check}'. Tidak dapat memeriksa fallback stop.")
             except Exception as e_fallback_stop:
                 logging.error(f"CHECK_SYSTEMD (Fallback): Error saat mencoba menghentikan sesi aktif '{session_id_to_check}' yang overdue via fallback: {e_fallback_stop}", exc_info=True)

        for active_json_session in list(s_data.get('active_sessions',[])): 
            # Gunakan sanitized_service_id dari sesi aktif
            san_id_active_service = active_json_session.get('sanitized_service_id')
            if not san_id_active_service : 
                logging.warning(f"CHECK_SYSTEMD: Sesi aktif {active_json_session.get('id')} tidak memiliki sanitized_service_id. Skip.")
                continue 
            serv_name_active = f"stream-{san_id_active_service}.service"

            if serv_name_active not in active_sysd_services:
                is_recently_stopped_by_scheduler = any(
                    s['id'] == active_json_session.get('id') and 
                    s.get('status') == 'inactive' and
                    (datetime.now(jakarta_tz) - datetime.fromisoformat(s.get('stop_time')).astimezone(jakarta_tz) < timedelta(minutes=2))
                    for s in s_data.get('inactive_sessions', [])
                )
                if is_recently_stopped_by_scheduler:
                    logging.info(f"CHECK_SYSTEMD: Sesi {active_json_session.get('id')} sepertinya baru dihentikan oleh scheduler. Skip pemindahan otomatis.")
                    continue

                logging.info(f"CHECK_SYSTEMD: Sesi {active_json_session.get('id','N/A')} (service: {serv_name_active}) tidak aktif di systemd. Memindahkan ke inactive.")
                active_json_session['status']='inactive'
                active_json_session['stop_time']=now_jakarta_dt.isoformat()
                s_data.setdefault('inactive_sessions',[]).append(active_json_session)
                s_data['active_sessions']=[s for s in s_data['active_sessions'] if s.get('id')!=active_json_session.get('id')]
                json_changed = True
        
        if json_changed: 
            write_sessions(s_data) 
            with socketio_lock:
                socketio.emit('sessions_update', get_active_sessions_data())
                socketio.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
    except Exception as e: logging.error(f"CHECK_SYSTEMD: Error: {e}", exc_info=True)


def start_scheduled_streaming(platform, stream_key, video_file, session_name_original, 
                              one_time_duration_minutes=0, recurrence_type='one_time', 
                              daily_start_time_str=None, daily_stop_time_str=None):
    logging.info(f"Mulai stream terjadwal: '{session_name_original}', Tipe: {recurrence_type}, Durasi One-Time: {one_time_duration_minutes} menit, Jadwal Harian: {daily_start_time_str}-{daily_stop_time_str}")
    
    video_path = os.path.abspath(os.path.join(VIDEO_DIR, video_file))
    if not os.path.isfile(video_path):
        logging.error(f"Video {video_file} tidak ada untuk jadwal '{session_name_original}'. Jadwal mungkin perlu dibatalkan.")
        return

    platform_url = "rtmp://a.rtmp.youtube.com/live2" if platform == "YouTube" else "rtmps://live-api-s.facebook.com:443/rtmp"
    
    try:
        # create_service_file menggunakan session_name_original, dan mengembalikan sanitized_service_part
        service_name_systemd, sanitized_service_id_part = create_service_file(session_name_original, video_path, platform_url, stream_key)
        subprocess.run(["systemctl", "start", service_name_systemd], check=True, capture_output=True, text=True)
        logging.info(f"Service {service_name_systemd} untuk jadwal '{session_name_original}' dimulai.")
        
        current_start_time_iso = datetime.now(jakarta_tz).isoformat()
        s_data = read_sessions()

        active_session_stop_time_iso = None
        active_session_duration_minutes = 0
        active_schedule_type = "unknown"
        current_start_dt = datetime.fromisoformat(current_start_time_iso)

        if recurrence_type == 'daily' and daily_start_time_str and daily_stop_time_str:
            active_schedule_type = "daily_recurring_instance"
            start_h, start_m = map(int, daily_start_time_str.split(':'))
            stop_h, stop_m = map(int, daily_stop_time_str.split(':'))
            duration_for_this_instance = (stop_h * 60 + stop_m) - (start_h * 60 + start_m)
            if duration_for_this_instance <= 0: 
                duration_for_this_instance += 24 * 60
            active_session_duration_minutes = duration_for_this_instance
            active_session_stop_time_iso = (current_start_dt + timedelta(minutes=duration_for_this_instance)).isoformat()
        elif recurrence_type == 'one_time':
            active_schedule_type = "scheduled"
            active_session_duration_minutes = one_time_duration_minutes
            if one_time_duration_minutes > 0:
                active_session_stop_time_iso = (current_start_dt + timedelta(minutes=one_time_duration_minutes)).isoformat()
        else:
             active_schedule_type = "manual_from_schedule_error"

        new_active_session_entry = {
            "id": session_name_original, # Nama sesi asli
            "sanitized_service_id": sanitized_service_id_part, # ID untuk service systemd
            "video_name": video_file, "stream_key": stream_key, "platform": platform,
            "status": "active", "start_time": current_start_time_iso,
            "scheduleType": active_schedule_type,
            "stopTime": active_session_stop_time_iso,
            "duration_minutes": active_session_duration_minutes
        }
        s_data['active_sessions'] = add_or_update_session_in_list(
    s_data.get('active_sessions', []), new_active_session_entry
)

        if recurrence_type == 'one_time':
            # Hapus definisi jadwal one-time dari scheduled_sessions berdasarkan session_name_original
            s_data['scheduled_sessions'] = [s for s in s_data.get('scheduled_sessions', []) if not (s.get('session_name_original') == session_name_original and s.get('recurrence_type', 'one_time') == 'one_time')]
        
        write_sessions(s_data)
        
        with socketio_lock:
            socketio.emit('sessions_update', get_active_sessions_data())
            socketio.emit('schedules_update', get_schedules_list_data())
        logging.info(f"Sesi terjadwal '{session_name_original}' (Tipe: {recurrence_type}) dimulai, update dikirim.")

    except Exception as e:
        logging.error(f"Error start_scheduled_streaming untuk '{session_name_original}': {e}", exc_info=True)


def stop_scheduled_streaming(session_name_original_or_active_id):
    logging.info(f"Menghentikan stream (terjadwal/aktif): '{session_name_original_or_active_id}'")
    s_data = read_sessions()
    # Cari sesi aktif berdasarkan ID (nama sesi asli)
    session_to_stop = next((s for s in s_data.get('active_sessions', []) if s['id'] == session_name_original_or_active_id), None)
    
    if not session_to_stop:
        logging.warning(f"Sesi '{session_name_original_or_active_id}' tidak ditemukan dalam daftar sesi aktif untuk dihentikan.")
        return

    # Gunakan sanitized_service_id dari sesi aktif untuk menghentikan service yang benar
    sanitized_id_service_to_stop = session_to_stop.get('sanitized_service_id')
    if not sanitized_id_service_to_stop:
        logging.error(f"Tidak dapat menghentikan service untuk sesi '{session_name_original_or_active_id}' karena sanitized_service_id tidak ditemukan.")
        return
        
    service_name_to_stop = f"stream-{sanitized_id_service_to_stop}.service"
    
    try:
        subprocess.run(["systemctl", "stop", service_name_to_stop], check=False, timeout=15)
        service_path_to_stop = os.path.join(SERVICE_DIR, service_name_to_stop)
        if os.path.exists(service_path_to_stop):
            os.remove(service_path_to_stop)
            subprocess.run(["systemctl", "daemon-reload"], check=False, timeout=10)

        stop_time_iso = datetime.now(jakarta_tz).isoformat()
        session_to_stop['status'] = 'inactive'
        session_to_stop['stop_time'] = stop_time_iso
        
        s_data['inactive_sessions'] = add_or_update_session_in_list(
    s_data.get('inactive_sessions', []), session_to_stop
)
        s_data['active_sessions'] = [s for s in s_data['active_sessions'] if s['id'] != session_name_original_or_active_id]
        
        write_sessions(s_data)
        
        with socketio_lock:
            socketio.emit('sessions_update', get_active_sessions_data())
            socketio.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
            socketio.emit('schedules_update', get_schedules_list_data())
        logging.info(f"Sesi '{session_name_original_or_active_id}' dihentikan dan dipindah ke inactive.")

    except Exception as e:
        logging.error(f"Error stop_scheduled_streaming untuk '{session_name_original_or_active_id}': {e}", exc_info=True)


def recover_schedules():
    s_data = read_sessions()
    now_jkt = datetime.now(jakarta_tz)
    valid_schedules_in_json = [] 

    logging.info("Memulai pemulihan jadwal...")
    for sched_def in s_data.get('scheduled_sessions', []):
        try:
            session_name_original = sched_def.get('session_name_original')
            # ID definisi jadwal (misal "daily-XYZ" atau "onetime-XYZ")
            schedule_definition_id = sched_def.get('id') 
            # sanitized_service_id digunakan untuk membuat ID job APScheduler yang unik
            sanitized_service_id = sched_def.get('sanitized_service_id') 

            platform = sched_def.get('platform')
            stream_key = sched_def.get('stream_key')
            video_file = sched_def.get('video_file')
            recurrence = sched_def.get('recurrence_type', 'one_time')

            if not all([session_name_original, sanitized_service_id, platform, stream_key, video_file, schedule_definition_id]):
                logging.warning(f"Recover: Skip jadwal '{session_name_original}' karena field dasar (termasuk ID definisi atau sanitized_service_id) kurang.")
                continue

            if recurrence == 'daily':
                start_time_str = sched_def.get('start_time_of_day')
                stop_time_str = sched_def.get('stop_time_of_day')
                if not start_time_str or not stop_time_str:
                    logging.warning(f"Recover: Skip jadwal harian '{session_name_original}' karena field waktu harian kurang.")
                    continue
                
                start_h, start_m = map(int, start_time_str.split(':'))
                stop_h, stop_m = map(int, stop_time_str.split(':'))

                # ID job APScheduler harus unik, gunakan sanitized_service_id
                aps_start_job_id = f"daily-start-{sanitized_service_id}" 
                aps_stop_job_id = f"daily-stop-{sanitized_service_id}"   

                scheduler.add_job(start_scheduled_streaming, 'cron', hour=start_h, minute=start_m,
                                  args=[platform, stream_key, video_file, session_name_original, 0, 'daily', start_time_str, stop_time_str],
                                  id=aps_start_job_id, replace_existing=True, misfire_grace_time=3600)
                logging.info(f"Recovered daily start job '{aps_start_job_id}' for '{session_name_original}' at {start_time_str}")

                scheduler.add_job(stop_scheduled_streaming, 'cron', hour=stop_h, minute=stop_m,
                                  args=[session_name_original],
                                  id=aps_stop_job_id, replace_existing=True, misfire_grace_time=3600)
                logging.info(f"Recovered daily stop job '{aps_stop_job_id}' for '{session_name_original}' at {stop_time_str}")
                valid_schedules_in_json.append(sched_def)

            elif recurrence == 'one_time':
                start_time_iso = sched_def.get('start_time_iso')
                duration_minutes = sched_def.get('duration_minutes')
                is_manual = sched_def.get('is_manual_stop', duration_minutes == 0)
                # ID job start APScheduler = ID definisi jadwal untuk one-time
                aps_start_job_id = schedule_definition_id 

                if not start_time_iso or duration_minutes is None:
                    logging.warning(f"Recover: Skip jadwal one-time '{session_name_original}' karena field waktu/durasi kurang.")
                    continue

                start_dt = datetime.fromisoformat(start_time_iso).astimezone(now_jkt.tzinfo)

                if start_dt > now_jkt:
                    scheduler.add_job(start_scheduled_streaming, 'date', run_date=start_dt,
                                      args=[platform, stream_key, video_file, session_name_original, duration_minutes, 'one_time', None, None],
                                      id=aps_start_job_id, replace_existing=True)
                    logging.info(f"Recovered one-time start job '{aps_start_job_id}' for '{session_name_original}' at {start_dt}")

                    if not is_manual:
                        stop_dt = start_dt + timedelta(minutes=duration_minutes)
                        if stop_dt > now_jkt:
                            # ID job stop APScheduler untuk one-time
                            aps_stop_job_id = f"onetime-stop-{sanitized_service_id}" 
                            scheduler.add_job(stop_scheduled_streaming, 'date', run_date=stop_dt,
                                              args=[session_name_original],
                                              id=aps_stop_job_id, replace_existing=True)
                            logging.info(f"Recovered one-time stop job '{aps_stop_job_id}' for '{session_name_original}' at {stop_dt}")
                    valid_schedules_in_json.append(sched_def)
                else:
                    logging.info(f"Recover: Skip jadwal one-time '{session_name_original}' karena waktu sudah lewat.")
            else:
                 logging.warning(f"Recover: Tipe recurrence '{recurrence}' tidak dikenal untuk '{session_name_original}'.")

        except Exception as e:
            logging.error(f"Gagal memulihkan jadwal '{sched_def.get('session_name_original', 'UNKNOWN')}': {e}", exc_info=True)
    
    if len(s_data.get('scheduled_sessions', [])) != len(valid_schedules_in_json):
        s_data['scheduled_sessions'] = valid_schedules_in_json
        write_sessions(s_data)
        logging.info("File sessions.json diupdate dengan jadwal yang valid setelah pemulihan.")
    logging.info("Pemulihan jadwal selesai.")

scheduler = BackgroundScheduler(timezone=jakarta_tz)
if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
    recover_schedules() 
    scheduler.add_job(check_systemd_sessions, 'interval', minutes=1, id="check_systemd_job", replace_existing=True)
    
    # ---- TAMBAHKAN JOB UNTUK TRIAL RESET DI SINI ----
    if TRIAL_MODE_ENABLED:
        scheduler.add_job(trial_reset, 'interval', hours=TRIAL_RESET_HOURS, id="trial_reset_job", replace_existing=True)
        logging.info(f"Mode Trial Aktif. Reset dijadwalkan setiap {TRIAL_RESET_HOURS} jam.")
    # -------------------------------------------------
    
    try:
        scheduler.start()
        logging.info("Scheduler dimulai. Jobs: %s", scheduler.get_jobs())
    except Exception as e:
        logging.error(f"Gagal start scheduler: {e}")
        
@socketio.on('connect')
def handle_connect():
    logging.info("Klien terhubung")
    if 'user' not in session: 
        logging.warning("Klien tanpa sesi login aktif ditolak.")
        return False 
    with socketio_lock:
        socketio.emit('videos_update', get_videos_list_data())
        socketio.emit('sessions_update', get_active_sessions_data())
        socketio.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
        socketio.emit('schedules_update', get_schedules_list_data())
        
        # ---- TAMBAHKAN EMIT STATUS TRIAL DI SINI ----
        if TRIAL_MODE_ENABLED:
            socketio.emit('trial_status_update', {
                'is_trial': True,
                # Sesuaikan pesan ini jika perlu, atau buat kunci terjemahan baru di frontend
                'message': f"Reset tiap {TRIAL_RESET_HOURS} jam" 
            })
        else:
            socketio.emit('trial_status_update', {'is_trial': False, 'message': ''})
        # ---------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session: return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method=='POST':
        user,pwd = request.form.get('username'),request.form.get('password')
        users = read_users()
        if user in users and users[user]==pwd:
            session.permanent=True; session['user']=user 
            return redirect(request.args.get('next') or url_for('index'))
        return "Salah Password atau Salah Username Kak", 401
    if not read_users(): return redirect(url_for('register')) 
    return '''<!DOCTYPE html><html lang="id"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/bootstrap/5.3.0/css/bootstrap.min.css">
    <title>Login StreamHib</title><link rel="icon" type="image/x-icon" href="/static/favicon.ico"></head><body><div class="container d-flex justify-content-center align-items-center vh-100">
    <div class="card shadow" style="width:100%;max-width:400px;"><div class="card-body"><h3 class="card-title text-center mb-4">Login StreamHib</h3><form method="post">
    <div class="mb-3"><label for="username" class="form-label">Username</label><input type="text" id="username" name="username" class="form-control" placeholder="Masukkan username" required></div>
    <div class="mb-3"><label for="password" class="form-label">Password</label><input type="password" id="password" name="password" class="form-control" placeholder="Masukkan password" required></div>
    <div class="d-grid"><button type="submit" class="btn btn-primary">Login</button></div></form>
    <p class="text-center mt-3">Belum punya akun? <a href="/register" class="text-decoration-none">Daftar di sini</a></p>
    </div></div></div><script src="https://cdnjs.cloudflare.com/ajax/libs/bootstrap/5.3.0/js/bootstrap.bundle.min.js"></script></body></html>'''

@app.route('/register', methods=['GET','POST'])
def register():
    # Pemeriksaan batas pengguna HANYA jika mode trial TIDAK aktif
    if not TRIAL_MODE_ENABLED: # Jika BUKAN mode trial
        if read_users() and len(read_users()) >= 1: 
            # Tampilkan halaman registrasi ditutup jika sudah ada 1 pengguna dan bukan mode trial
            return '''<!DOCTYPE html><html lang="id"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
            <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/bootstrap/5.3.0/css/bootstrap.min.css">
            <title>Registrasi Ditutup</title><link rel="icon" type="image/x-icon" href="/static/favicon.ico"></head><body><div class="container d-flex justify-content-center align-items-center vh-100">
            <div class="card shadow" style="width:100%;max-width:400px;"><div class="card-body"><h3 class="card-title text-center mb-4">Registrasi Ditutup</h3>
            <p class="text-center">Maaf, registrasi sudah ditutup untuk mode ini. Hanya satu pengguna yang diizinkan.</p>
            <div class="d-grid"><a href="/login" class="btn btn-primary">Kembali ke Login</a></div>
            </div></div></div><script src="https://cdnjs.cloudflare.com/ajax/libs/bootstrap/5.3.0/js/bootstrap.bundle.min.js"></script></body></html>'''

    # Jika mode trial AKTIF, atau mode trial TIDAK aktif TAPI belum ada pengguna, lanjutkan ke proses registrasi
    if request.method=='POST':
        user,pwd = request.form.get('username'),request.form.get('password')
        if not user or not pwd: return "Username & Password wajib diisi", 400

        users=read_users() 
        if user in users: return "Username sudah ada", 400

        # Tambahan pengaman untuk POST jika bukan mode trial dan sudah ada user (seharusnya sudah dicegat di GET)
        if not TRIAL_MODE_ENABLED and len(users) >= 1:
            return "Registrasi ditutup (batas pengguna tercapai).", 403 # Forbidden

        users[user]=pwd; write_users(users)
        session['user']=user
        session.permanent = True # Dari app.py utama Anda
        return redirect(url_for('index'))

    # Tampilkan formulir registrasi untuk metode GET (jika lolos pemeriksaan batas di atas atau mode trial aktif)
    return '''<!DOCTYPE html><html lang="id"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/bootstrap/5.3.0/css/bootstrap.min.css">
    <title>Daftar StreamHib</title><link rel="icon" type="image/x-icon" href="/static/favicon.ico"></head><body><div class="container d-flex justify-content-center align-items-center vh-100">
    <div class="card shadow" style="width:100%;max-width:400px;"><div class="card-body"><h3 class="card-title text-center mb-4">Daftar Akun StreamHib</h3><form method="post">
    <div class="mb-3"><label for="username" class="form-label">Username</label><input type="text" id="username" name="username" class="form-control" placeholder="Buat username unik" required></div>
    <div class="mb-3"><label for="password" class="form-label">Password</label><input type="password" id="password" name="password" class="form-control" placeholder="Buat password kuat" required></div>
    <div class="d-grid"><button type="submit" class="btn btn-primary">Daftar</button></div>
    <p class="text-center mt-3">Sudah punya akun? <a href="/login" class="text-decoration-none">Login di sini</a></p>
    </form></div></div></div><script src="https://cdnjs.cloudflare.com/ajax/libs/bootstrap/5.3.0/js/bootstrap.bundle.min.js"></script></body></html>'''

@app.route('/logout')
def logout(): session.pop('user',None); return redirect(url_for('login'))

@app.route('/')
@login_required
def index(): 
    try:
        return render_template('index.html')
    except Exception as e:
        logging.error(f"Error rendering index.html: {e}", exc_info=True)
        return "Internal Server Error: Gagal memuat halaman utama.", 500

def extract_drive_id(val):
    if not val: return None
    if "drive.google.com" in val:
        m = re.search(r'/file/d/([a-zA-Z0-9_-]+)',val) or re.search(r'id=([a-zA-Z0-9_-]+)',val)
        if m: return m.group(1)
        parts = val.split("/")
        for p in reversed(parts): 
            if len(p)>20 and '.' not in p and '=' not in p: return p 
    return val if re.match(r'^[a-zA-Z0-9_-]{20,}$',val) else None 

@app.route('/api/download', methods=['POST'])
@login_required
def download_video_api():
    try:
        data = request.json
        input_val = data.get('file_id')
        if not input_val: return jsonify({'status':'error','message':'ID/URL Video diperlukan'}),400
        vid_id = extract_drive_id(input_val)
        if not vid_id: return jsonify({'status':'error','message':'Format ID/URL GDrive tidak valid atau tidak ditemukan.'}),400
        
        output_dir_param = VIDEO_DIR + os.sep 
        cmd = ["/usr/local/bin/gdown", f"https://drive.google.com/uc?id={vid_id.strip()}&export=download", "-O", output_dir_param, "--no-cookies", "--quiet", "--continue"]
        
        logging.debug(f"Download cmd: {shlex.join(cmd)}")
        files_before = set(os.listdir(VIDEO_DIR))
        res = subprocess.run(cmd,capture_output=True,text=True,timeout=1800) 
        files_after = set(os.listdir(VIDEO_DIR))
        new_files = files_after - files_before

        if res.returncode==0:
            downloaded_filename_to_check = None
            if new_files:
                downloaded_filename_to_check = new_files.pop() 
                name_part, ext_part = os.path.splitext(downloaded_filename_to_check)
                if not ext_part and name_part == vid_id: 
                    new_filename_with_ext = f"{downloaded_filename_to_check}.mp4" 
                    try:
                        os.rename(os.path.join(VIDEO_DIR, downloaded_filename_to_check), os.path.join(VIDEO_DIR, new_filename_with_ext))
                        logging.info(f"File download {downloaded_filename_to_check} di-rename menjadi {new_filename_with_ext}")
                    except Exception as e_rename_gdown:
                        logging.error(f"Gagal me-rename file download {downloaded_filename_to_check} setelah gdown: {e_rename_gdown}")
            elif "already exists" in res.stderr.lower() or "already exists" in res.stdout.lower():
                 logging.info(f"File untuk ID {vid_id} kemungkinan sudah ada. Tidak ada file baru terdeteksi.")
            else:
                logging.warning(f"gdown berhasil (code 0) tapi tidak ada file baru terdeteksi di {VIDEO_DIR}. Output: {res.stdout} Err: {res.stderr}")

            with socketio_lock: socketio.emit('videos_update',get_videos_list_data())
            return jsonify({'status':'success','message':'Download video berhasil. Cek daftar video.'})
        else:
            logging.error(f"Gdown error (code {res.returncode}): {res.stderr} | stdout: {res.stdout}")
            err_msg = f'Download Gagal: {res.stderr[:250]}' 
            if "Permission denied" in res.stderr or "Zugriff verweigert" in res.stderr: err_msg="Download Gagal: Pastikan file publik atau Anda punya izin."
            elif "File not found" in res.stderr or "No such file" in res.stderr or "Cannot retrieve BFC cookies" in res.stderr: err_msg="Download Gagal: File tidak ditemukan atau tidak dapat diakses."
            elif "ERROR:" in res.stderr: err_msg = f"Download Gagal: {res.stderr.split('ERROR:')[1].strip()[:200]}"
            return jsonify({'status':'error','message':err_msg}),500
    except subprocess.TimeoutExpired: 
        logging.error("Proses download video timeout.")
        return jsonify({'status':'error','message':'Download timeout (30 menit).'}),500
    except Exception as e: 
        logging.exception("Error tidak terduga saat download video")
        return jsonify({'status':'error','message':f'Kesalahan Server: {str(e)}'}),500
        
@app.route('/api/videos/delete-all', methods=['POST'])
@login_required
def delete_all_videos_api(): 
    try:
        count=0
        for vid in get_videos_list_data(): 
            try: os.remove(os.path.join(VIDEO_DIR,vid)); count+=1
            except Exception as e: logging.error(f"Error hapus video {vid}: {str(e)}")
        with socketio_lock: socketio.emit('videos_update',get_videos_list_data())
        return jsonify({'status':'success','message':f'Berhasil menghapus {count} video.','deleted_count':count})
    except Exception as e: 
        logging.exception("Error di API delete_all_videos")
        return jsonify({'status':'error','message':f'Kesalahan Server: {str(e)}'}),500

@app.route('/videos/<filename>')
@login_required
def serve_video(filename):
    return send_from_directory(VIDEO_DIR, filename)

@app.route('/api/start', methods=['POST'])
@login_required
def start_streaming_api(): 
    try:
        data = request.json
        platform = data.get('platform')
        stream_key = data.get('stream_key')
        video_file = data.get('video_file')
        session_name_original = data.get('session_name') # Nama sesi asli dari frontend
        
        if not all([platform, stream_key, video_file, session_name_original, session_name_original.strip()]):
            return jsonify({'status': 'error', 'message': 'Semua field wajib diisi dan nama sesi tidak boleh kosong.'}), 400
        
        video_path = os.path.abspath(os.path.join(VIDEO_DIR, video_file))
        if not os.path.isfile(video_path):
            return jsonify({'status': 'error', 'message': f'File video {video_file} tidak ditemukan'}), 404
        if platform not in ["YouTube", "Facebook"]:
            return jsonify({'status': 'error', 'message': 'Platform tidak valid. Pilih YouTube atau Facebook.'}), 400
        
        platform_url = "rtmp://a.rtmp.youtube.com/live2" if platform == "YouTube" else "rtmps://live-api-s.facebook.com:443/rtmp"
        
        # create_service_file menggunakan session_name_original, mengembalikan sanitized_service_id_part
        service_name_systemd, sanitized_service_id_part = create_service_file(session_name_original, video_path, platform_url, stream_key)
        subprocess.run(["systemctl", "start", service_name_systemd], check=True)
        
        start_time_iso = datetime.now(jakarta_tz).isoformat()
        new_session_entry = {
            "id": session_name_original, # Simpan nama sesi asli
            "sanitized_service_id": sanitized_service_id_part, # ID untuk service systemd
            "video_name": video_file,
            "stream_key": stream_key, "platform": platform, "status": "active",
            "start_time": start_time_iso, "scheduleType": "manual", "stopTime": None, 
            "duration_minutes": 0 
        }
        
        s_data = read_sessions()
        s_data['active_sessions'] = add_or_update_session_in_list(
    s_data.get('active_sessions', []), new_session_entry
)
        s_data['inactive_sessions'] = [s for s in s_data.get('inactive_sessions', []) if s.get('id') != session_name_original]
        write_sessions(s_data)
        
        with socketio_lock:
            socketio.emit('sessions_update', get_active_sessions_data())
            socketio.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
        return jsonify({'status': 'success', 'message': f'Berhasil memulai Live Stream untuk sesi "{session_name_original}"'}), 200
        
    except subprocess.CalledProcessError as e: 
        session_name_req = data.get('session_name', 'N/A') if isinstance(data, dict) else 'N/A'
        logging.error(f"Gagal start service untuk sesi '{session_name_req}': {e.stderr if e.stderr else e.stdout}")
        return jsonify({'status': 'error', 'message': f"Gagal memulai layanan systemd: {e.stderr if e.stderr else e.stdout}"}), 500
    except Exception as e: 
        session_name_req = data.get('session_name', 'N/A') if isinstance(data, dict) else 'N/A'
        logging.exception(f"Error tidak terduga saat start streaming untuk sesi '{session_name_req}'")
        return jsonify({'status': 'error', 'message': f'Kesalahan Server: {str(e)}'}), 500

@app.route('/api/stop', methods=['POST'])
@login_required
def stop_streaming_api(): 
    try:
        data = request.get_json()
        if not data: return jsonify({'status':'error','message':'Request JSON tidak valid.'}),400
        session_id_to_stop = data.get('session_id') # Ini adalah nama sesi asli
        if not session_id_to_stop: return jsonify({'status':'error','message':'ID sesi (nama sesi asli) diperlukan'}),400
        
        s_data = read_sessions()
        active_session_data = next((s for s in s_data.get('active_sessions',[]) if s['id']==session_id_to_stop),None)
        
        sanitized_service_id_for_stop = None
        if active_session_data and 'sanitized_service_id' in active_session_data:
            sanitized_service_id_for_stop = active_session_data['sanitized_service_id']
        else:
            # Jika tidak ada di sesi aktif atau tidak ada sanitized_service_id, coba buat dari session_id_to_stop
            # Ini adalah fallback, idealnya sanitized_service_id selalu ada di sesi aktif
            sanitized_service_id_for_stop = sanitize_for_service_name(session_id_to_stop)
            logging.warning(f"Menggunakan fallback sanitized_service_id '{sanitized_service_id_for_stop}' untuk menghentikan sesi '{session_id_to_stop}'.")

        service_name_systemd = f"stream-{sanitized_service_id_for_stop}.service"
        
        try:
            subprocess.run(["systemctl","stop",service_name_systemd],check=False, timeout=15)
            service_path = os.path.join(SERVICE_DIR,service_name_systemd)
            if os.path.exists(service_path): 
                os.remove(service_path)
                subprocess.run(["systemctl","daemon-reload"],check=True,timeout=10)
        except Exception as e_service_stop:
             logging.warning(f"Peringatan saat menghentikan/menghapus service {service_name_systemd}: {e_service_stop}")
            
        stop_time_iso = datetime.now(jakarta_tz).isoformat()
        session_updated_or_added_to_inactive = False

        if active_session_data: 
            active_session_data['status']='inactive'
            active_session_data['stop_time']=stop_time_iso
            s_data['inactive_sessions'] = add_or_update_session_in_list(
    s_data.get('inactive_sessions', []), active_session_data
)
            s_data['active_sessions']=[s for s in s_data['active_sessions'] if s['id']!=session_id_to_stop]
            session_updated_or_added_to_inactive = True
        elif not any(s['id']==session_id_to_stop for s in s_data.get('inactive_sessions',[])): 
            s_data.setdefault('inactive_sessions',[]).append({
                "id":session_id_to_stop, # Nama sesi asli
                "sanitized_service_id":sanitized_service_id_for_stop, # Hasil sanitasi
                "video_name":"unknown (force stop)", "stream_key":"unknown", "platform":"unknown",
                "status":"inactive","stop_time":stop_time_iso, "duration_minutes": 0,
                "scheduleType": "manual_force_stop"
            })
            session_updated_or_added_to_inactive = True
            
        if session_updated_or_added_to_inactive:
            write_sessions(s_data)
        
        with socketio_lock:
            socketio.emit('sessions_update',get_active_sessions_data())
            socketio.emit('inactive_sessions_update',{"inactive_sessions":get_inactive_sessions_data()})
        return jsonify({'status':'success','message':f'Sesi "{session_id_to_stop}" berhasil dihentikan atau sudah tidak aktif.'})
    except Exception as e: 
        req_data = request.get_json(silent=True) or {}
        session_id_err = req_data.get('session_id','N/A')
        logging.exception(f"Error stop sesi '{session_id_err}'")
        return jsonify({'status':'error','message':f'Kesalahan Server: {str(e)}'}),500

@app.route('/api/videos', methods=['GET'])
@login_required
def list_videos_api():
    try: return jsonify(get_videos_list_data())
    except Exception as e: 
        logging.error(f"Error API /api/videos: {str(e)}",exc_info=True)
        return jsonify({'status':'error','message':'Gagal ambil daftar video.'}),500

@app.route('/api/videos/rename', methods=['POST'])
@login_required
def rename_video_api(): 
    try:
        data = request.get_json(); old,new_base = data.get('old_name'),data.get('new_name')
        if not all([old,new_base]): return jsonify({'status':'error','message':'Nama lama & baru diperlukan'}),400
        # Validasi nama baru bisa lebih permisif jika diinginkan, tapi hati-hati dengan karakter khusus untuk nama file.
        # Untuk saat ini, kita biarkan validasi yang sudah ada.
        if not re.match(r'^[\w\-. ]+$',new_base): return jsonify({'status':'error','message':'Nama baru tidak valid (hanya huruf, angka, spasi, titik, strip, underscore).'}),400
        old_p = os.path.join(VIDEO_DIR,old)
        if not os.path.isfile(old_p): return jsonify({'status':'error','message':f'File "{old}" tidak ada'}),404
        new_p = os.path.join(VIDEO_DIR,new_base.strip()+os.path.splitext(old)[1])
        if old_p==new_p: return jsonify({'status':'success','message':'Nama video tidak berubah.'})
        if os.path.isfile(new_p): return jsonify({'status':'error','message':f'Nama "{os.path.basename(new_p)}" sudah ada.'}),400
        os.rename(old_p,new_p)
        with socketio_lock: socketio.emit('videos_update',get_videos_list_data())
        return jsonify({'status':'success','message':f'Video diubah ke "{os.path.basename(new_p)}"'})
    except Exception as e: 
        logging.exception("Error rename video")
        return jsonify({'status':'error','message':f'Kesalahan Server: {str(e)}'}),500

@app.route('/api/videos/delete', methods=['POST'])
@login_required
def delete_video_api(): 
    try:
        fname = request.json.get('file_name')
        if not fname: return jsonify({'status':'error','message':'Nama file diperlukan'}),400
        fpath = os.path.join(VIDEO_DIR,fname)
        if not os.path.isfile(fpath): return jsonify({'status':'error','message':f'File "{fname}" tidak ada'}),404
        os.remove(fpath)
        with socketio_lock: socketio.emit('videos_update',get_videos_list_data())
        return jsonify({'status':'success','message':f'Video "{fname}" dihapus'})
    except Exception as e: 
        logging.exception(f"Error delete video {request.json.get('file_name','N/A')}")
        return jsonify({'status':'error','message':f'Kesalahan Server: {str(e)}'}),500
        
@app.route('/api/disk-usage', methods=['GET'])
@login_required
def disk_usage_api(): 
    try:
        t,u,f = shutil.disk_usage(VIDEO_DIR); tg,ug,fg=t/(2**30),u/(2**30),f/(2**30)
        pu = (u/t)*100 if t>0 else 0
        stat = 'full' if pu>95 else 'almost_full' if pu>80 else 'normal'
        return jsonify({'status':stat,'total':round(tg,2),'used':round(ug,2),'free':round(fg,2),'percent_used':round(pu,2)})
    except Exception as e: 
        logging.error(f"Error disk usage: {str(e)}",exc_info=True)
        return jsonify({'status':'error','message':f'Kesalahan Server: {str(e)}'}),500

@app.route('/api/sessions', methods=['GET'])
@login_required
def list_sessions_api():
    try: return jsonify(get_active_sessions_data())
    except Exception as e: 
        logging.error(f"Error API /api/sessions: {str(e)}",exc_info=True)
        return jsonify({'status':'error','message':'Gagal ambil sesi aktif.'}),500

@app.route('/api/schedule', methods=['POST'])
@login_required
def schedule_streaming_api():
    try:
        data = request.json
        logging.info(f"Menerima data penjadwalan: {data}")

        recurrence_type = data.get('recurrence_type', 'one_time')
        session_name_original = data.get('session_name_original', '').strip() # Nama sesi asli
        platform = data.get('platform', 'YouTube')
        stream_key = data.get('stream_key', '').strip()
        video_file = data.get('video_file')

        if not all([session_name_original, platform, stream_key, video_file]):
            return jsonify({'status': 'error', 'message': 'Nama sesi, platform, stream key, dan video file wajib diisi.'}), 400
        if platform not in ["YouTube", "Facebook"]:
             return jsonify({'status': 'error', 'message': 'Platform tidak valid.'}), 400
        if not os.path.isfile(os.path.join(VIDEO_DIR, video_file)):
            return jsonify({'status': 'error', 'message': f"File video '{video_file}' tidak ditemukan."}), 404

        # Sanitasi nama sesi HANYA untuk ID service dan ID job scheduler
        sanitized_service_id_part = sanitize_for_service_name(session_name_original)
        if not sanitized_service_id_part: # Jika hasil sanitasi kosong (misal nama sesi hanya simbol)
            return jsonify({'status': 'error', 'message': 'Nama sesi tidak valid setelah sanitasi untuk ID layanan.'}), 400


        s_data = read_sessions()
        idx_to_remove = -1
        for i, sched in enumerate(s_data.get('scheduled_sessions', [])):
            # Hapus jadwal lama jika nama sesi ASLI sama
            if sched.get('session_name_original') == session_name_original:
                logging.info(f"Menemukan jadwal yang sudah ada dengan nama sesi asli '{session_name_original}', akan menggantinya.")
                old_sanitized_service_id = sched.get('sanitized_service_id')
                old_schedule_def_id = sched.get('id')
                try:
                    if sched.get('recurrence_type') == 'daily':
                        scheduler.remove_job(f"daily-start-{old_sanitized_service_id}")
                        scheduler.remove_job(f"daily-stop-{old_sanitized_service_id}")
                    else: # one_time
                        scheduler.remove_job(old_schedule_def_id) 
                        if not sched.get('is_manual_stop', sched.get('duration_minutes', 0) == 0):
                            scheduler.remove_job(f"onetime-stop-{old_sanitized_service_id}")
                    logging.info(f"Job scheduler lama untuk '{session_name_original}' berhasil dihapus.")
                except Exception as e_remove_old_job:
                    logging.info(f"Tidak ada job scheduler lama untuk '{session_name_original}' atau error saat menghapus: {e_remove_old_job}")
                idx_to_remove = i
                break
        if idx_to_remove != -1:
            del s_data['scheduled_sessions'][idx_to_remove]
        
        s_data['inactive_sessions'] = [s for s in s_data.get('inactive_sessions', []) if s.get('id') != session_name_original]

        msg = ""
        schedule_definition_id = "" # ID untuk entri di sessions.json
        sched_entry = {
            'session_name_original': session_name_original,
            'sanitized_service_id': sanitized_service_id_part,
            'platform': platform, 'stream_key': stream_key, 'video_file': video_file,
            'recurrence_type': recurrence_type
        }

        if recurrence_type == 'daily':
            start_time_of_day = data.get('start_time_of_day') 
            stop_time_of_day = data.get('stop_time_of_day')   

            if not start_time_of_day or not stop_time_of_day:
                return jsonify({'status': 'error', 'message': "Untuk jadwal harian, 'start_time_of_day' dan 'stop_time_of_day' (format HH:MM) wajib diisi."}), 400
            try:
                start_hour, start_minute = map(int, start_time_of_day.split(':'))
                stop_hour, stop_minute = map(int, stop_time_of_day.split(':'))
                if not (0 <= start_hour <= 23 and 0 <= start_minute <= 59 and 0 <= stop_hour <= 23 and 0 <= stop_minute <= 59):
                    raise ValueError("Jam atau menit di luar rentang valid.")
            except ValueError as ve:
                return jsonify({'status': 'error', 'message': f"Format waktu harian tidak valid: {ve}. Gunakan HH:MM."}), 400

            schedule_definition_id = f"daily-{sanitized_service_id_part}"
            sched_entry.update({
                'id': schedule_definition_id,
                'start_time_of_day': start_time_of_day,
                'stop_time_of_day': stop_time_of_day
            })
            
            aps_start_job_id = f"daily-start-{sanitized_service_id_part}"
            aps_stop_job_id = f"daily-stop-{sanitized_service_id_part}"

            scheduler.add_job(start_scheduled_streaming, 'cron', hour=start_hour, minute=start_minute,
                              args=[platform, stream_key, video_file, session_name_original, 0, 'daily', start_time_of_day, stop_time_of_day],
                              id=aps_start_job_id, replace_existing=True, misfire_grace_time=3600)
            logging.info(f"Jadwal harian START '{aps_start_job_id}' untuk '{session_name_original}' ditambahkan: {start_time_of_day}")

            scheduler.add_job(stop_scheduled_streaming, 'cron', hour=stop_hour, minute=stop_minute,
                              args=[session_name_original],
                              id=aps_stop_job_id, replace_existing=True, misfire_grace_time=3600)
            logging.info(f"Jadwal harian STOP '{aps_stop_job_id}' untuk '{session_name_original}' ditambahkan: {stop_time_of_day}")
            
            msg = f"Sesi harian '{session_name_original}' dijadwalkan setiap hari dari {start_time_of_day} sampai {stop_time_of_day}."

        elif recurrence_type == 'one_time':
            start_time_str = data.get('start_time') 
            duration_input = data.get('duration', 0) 

            if not start_time_str:
                return jsonify({'status': 'error', 'message': "Untuk jadwal sekali jalan, 'start_time' (YYYY-MM-DDTHH:MM) wajib diisi."}), 400
            try:
                naive_start_dt = datetime.strptime(start_time_str, '%Y-%m-%dT%H:%M')
                start_dt = jakarta_tz.localize(naive_start_dt)
                if start_dt <= datetime.now(jakarta_tz):
                    return jsonify({'status': 'error', 'message': "Waktu mulai jadwal sekali jalan harus di masa depan."}), 400
            except ValueError:
                 return jsonify({'status': 'error', 'message': "Format 'start_time' untuk jadwal sekali jalan tidak valid. Gunakan YYYY-MM-DDTHH:MM."}), 400

            duration_minutes = int(float(duration_input) * 60) if float(duration_input) >= 0 else 0
            is_manual_stop = (duration_minutes == 0)
            schedule_definition_id = f"onetime-{sanitized_service_id_part}" 

            sched_entry.update({
                'id': schedule_definition_id,
                'start_time_iso': start_dt.isoformat(), 
                'duration_minutes': duration_minutes,
                'is_manual_stop': is_manual_stop
            })
            
            # ID job start APScheduler = ID definisi jadwal
            aps_start_job_id = schedule_definition_id
            scheduler.add_job(start_scheduled_streaming, 'date', run_date=start_dt,
                              args=[platform, stream_key, video_file, session_name_original, duration_minutes, 'one_time', None, None],
                              id=aps_start_job_id, replace_existing=True)
            logging.info(f"Jadwal sekali jalan START '{aps_start_job_id}' untuk '{session_name_original}' ditambahkan pada {start_dt}")

            if not is_manual_stop:
                stop_dt = start_dt + timedelta(minutes=duration_minutes)
                aps_stop_job_id = f"onetime-stop-{sanitized_service_id_part}"
                scheduler.add_job(stop_scheduled_streaming, 'date', run_date=stop_dt,
                                  args=[session_name_original], id=aps_stop_job_id, replace_existing=True)
                logging.info(f"Jadwal sekali jalan STOP '{aps_stop_job_id}' untuk '{session_name_original}' ditambahkan pada {stop_dt}")
            
            msg = f'Sesi "{session_name_original}" dijadwalkan sekali pada {start_dt.strftime("%d-%m-%Y %H:%M:%S")}'
            msg += f' selama {duration_minutes} menit.' if not is_manual_stop else ' hingga dihentikan manual.'
        
        else:
            return jsonify({'status':'error','message':f"Tipe recurrence '{recurrence_type}' tidak dikenal."}),400

        s_data.setdefault('scheduled_sessions', []).append(sched_entry)
        write_sessions(s_data)
        
        with socketio_lock:
            socketio.emit('schedules_update', get_schedules_list_data())
            socketio.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
        
        return jsonify({'status': 'success', 'message': msg})

    except (KeyError, ValueError) as e:
        logging.error(f"Input tidak valid untuk penjadwalan: {str(e)}", exc_info=True)
        return jsonify({'status': 'error', 'message': f"Input tidak valid: {str(e)}"}), 400
    except Exception as e:
        req_data_sched = request.get_json(silent=True) or {}
        session_name_err_sched = req_data_sched.get('session_name_original', 'N/A')
        logging.exception(f"Error server saat menjadwalkan sesi '{session_name_err_sched}'")
        return jsonify({'status': 'error', 'message': f'Kesalahan Server Internal: {str(e)}'}), 500


@app.route('/api/schedule-list', methods=['GET'])
@login_required
def get_schedules_api():
    try: return jsonify(get_schedules_list_data())
    except Exception as e: 
        logging.error(f"Error API /api/schedule-list: {str(e)}",exc_info=True)
        return jsonify({'status':'error','message':'Gagal ambil daftar jadwal.'}),500


@app.route('/api/cancel-schedule', methods=['POST'])
@login_required
def cancel_schedule_api():
    try:
        data = request.json
        schedule_definition_id_to_cancel = data.get('id') # ID definisi jadwal
        if not schedule_definition_id_to_cancel:
            return jsonify({'status': 'error', 'message': 'ID definisi jadwal diperlukan.'}), 400

        s_data = read_sessions()
        schedule_to_cancel_obj = None
        idx_to_remove_json = -1

        for i, sched in enumerate(s_data.get('scheduled_sessions', [])):
            if sched.get('id') == schedule_definition_id_to_cancel:
                schedule_to_cancel_obj = sched
                idx_to_remove_json = i
                break
        
        if not schedule_to_cancel_obj:
            return jsonify({'status': 'error', 'message': f"Definisi jadwal dengan ID '{schedule_definition_id_to_cancel}' tidak ditemukan."}), 404

        removed_scheduler_jobs_count = 0
        # Gunakan sanitized_service_id dari definisi jadwal untuk membentuk ID job APScheduler
        sanitized_service_id_from_def = schedule_to_cancel_obj.get('sanitized_service_id')
        session_display_name = schedule_to_cancel_obj.get('session_name_original', schedule_definition_id_to_cancel)

        if not sanitized_service_id_from_def:
            logging.error(f"Tidak dapat membatalkan job scheduler untuk def ID '{schedule_definition_id_to_cancel}' karena sanitized_service_id tidak ada.")
            # Tetap lanjutkan untuk menghapus dari JSON
        else:
            if schedule_to_cancel_obj.get('recurrence_type') == 'daily':
                aps_start_job_id = f"daily-start-{sanitized_service_id_from_def}"
                aps_stop_job_id = f"daily-stop-{sanitized_service_id_from_def}"
                try: scheduler.remove_job(aps_start_job_id); removed_scheduler_jobs_count += 1; logging.info(f"Job harian START '{aps_start_job_id}' dihapus.")
                except Exception as e: logging.info(f"Gagal hapus job harian START '{aps_start_job_id}': {e}")
                try: scheduler.remove_job(aps_stop_job_id); removed_scheduler_jobs_count += 1; logging.info(f"Job harian STOP '{aps_stop_job_id}' dihapus.")
                except Exception as e: logging.info(f"Gagal hapus job harian STOP '{aps_stop_job_id}': {e}")
            
            elif schedule_to_cancel_obj.get('recurrence_type', 'one_time') == 'one_time':
                # ID job start APScheduler = ID definisi jadwal
                aps_start_job_id = schedule_definition_id_to_cancel 
                try: scheduler.remove_job(aps_start_job_id); removed_scheduler_jobs_count += 1; logging.info(f"Job sekali jalan START '{aps_start_job_id}' dihapus.")
                except Exception as e: logging.info(f"Gagal hapus job sekali jalan START '{aps_start_job_id}': {e}")

                if not schedule_to_cancel_obj.get('is_manual_stop', schedule_to_cancel_obj.get('duration_minutes', 0) == 0):
                    aps_stop_job_id = f"onetime-stop-{sanitized_service_id_from_def}"
                    try: scheduler.remove_job(aps_stop_job_id); removed_scheduler_jobs_count += 1; logging.info(f"Job sekali jalan STOP '{aps_stop_job_id}' dihapus.")
                    except Exception as e: logging.info(f"Gagal hapus job sekali jalan STOP '{aps_stop_job_id}': {e}")
        
        if idx_to_remove_json != -1:
            del s_data['scheduled_sessions'][idx_to_remove_json]
            write_sessions(s_data)
            logging.info(f"Definisi jadwal '{session_display_name}' (ID: {schedule_definition_id_to_cancel}) dihapus dari sessions.json.")
        
        with socketio_lock:
            socketio.emit('schedules_update', get_schedules_list_data())
        
        return jsonify({
            'status': 'success',
            'message': f"Definisi jadwal '{session_display_name}' dibatalkan. {removed_scheduler_jobs_count} job dari scheduler berhasil dihapus."
        })
    except Exception as e:
        req_data_cancel = request.get_json(silent=True) or {}
        def_id_err = req_data_cancel.get('id', 'N/A')
        logging.exception(f"Error saat membatalkan jadwal, ID definisi dari request: {def_id_err}")
        return jsonify({'status': 'error', 'message': f'Kesalahan Server Internal: {str(e)}'}), 500


@app.route('/api/inactive-sessions', methods=['GET'])
@login_required
def list_inactive_sessions_api():
    try: return jsonify({"inactive_sessions":get_inactive_sessions_data()})
    except Exception as e: 
        logging.error(f"Error API /api/inactive-sessions: {str(e)}",exc_info=True)
        return jsonify({'status':'error','message':'Gagal ambil sesi tidak aktif.'}),500

@app.route('/api/reactivate', methods=['POST'])
@login_required
def reactivate_session_api(): 
    try:
        data = request.json
        session_id_to_reactivate = data.get('session_id') # Nama sesi asli
        if not session_id_to_reactivate: return jsonify({"status":"error","message":"ID sesi (nama sesi asli) diperlukan"}),400
        
        s_data = read_sessions()
        session_obj_to_reactivate = next((s for s in s_data.get('inactive_sessions',[]) if s['id']==session_id_to_reactivate),None)
        if not session_obj_to_reactivate: return jsonify({"status":"error","message":f"Sesi '{session_id_to_reactivate}' tidak ada di daftar tidak aktif."}),404
        
        video_file = session_obj_to_reactivate.get("video_name")
        stream_key = session_obj_to_reactivate.get("stream_key")
        platform = data.get('platform', session_obj_to_reactivate.get('platform', 'YouTube')) 
        
        if not video_file or not stream_key:
            return jsonify({"status":"error","message":"Detail video atau stream key tidak lengkap untuk reaktivasi."}),400
        
        video_path = os.path.abspath(os.path.join(VIDEO_DIR, video_file))
        if not os.path.isfile(video_path):
            return jsonify({"status":"error","message":f"File video '{video_file}' tidak ditemukan untuk reaktivasi."}),404
        if platform not in ["YouTube", "Facebook"]: platform="YouTube" 
        
        platform_url = "rtmp://a.rtmp.youtube.com/live2" if platform == "YouTube" else "rtmps://live-api-s.facebook.com:443/rtmp"
        
        # Gunakan nama sesi asli untuk service, create_service_file akan sanitasi untuk nama service
        service_name_systemd, new_sanitized_service_id_part = create_service_file(session_id_to_reactivate, video_path, platform_url, stream_key) 
        subprocess.run(["systemctl", "start", service_name_systemd], check=True) 
        
        session_obj_to_reactivate['status'] = 'active'
        session_obj_to_reactivate['start_time'] = datetime.now(jakarta_tz).isoformat()
        session_obj_to_reactivate['platform'] = platform 
        session_obj_to_reactivate['sanitized_service_id'] = new_sanitized_service_id_part # Update jika berbeda
        if 'stop_time' in session_obj_to_reactivate: del session_obj_to_reactivate['stop_time'] 
        session_obj_to_reactivate['scheduleType'] = 'manual_reactivated'
        session_obj_to_reactivate['stopTime'] = None 
        session_obj_to_reactivate['duration_minutes'] = 0 # Reaktivasi manual dianggap durasi tak terbatas

        s_data['inactive_sessions'] = [s for s in s_data['inactive_sessions'] if s['id'] != session_id_to_reactivate] 
        s_data['active_sessions'] = add_or_update_session_in_list(
    s_data.get('active_sessions', []), session_obj_to_reactivate
)
        write_sessions(s_data)
        
        with socketio_lock:
            socketio.emit('sessions_update', get_active_sessions_data())
            socketio.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
        return jsonify({"status":"success","message":f"Sesi '{session_id_to_reactivate}' berhasil diaktifkan kembali (Live Sekarang).","platform":platform})

    except subprocess.CalledProcessError as e: 
        req_data_reactivate = request.get_json(silent=True) or {}
        session_id_err_reactivate = req_data_reactivate.get('session_id','N/A')
        logging.error(f"Gagal start service untuk reaktivasi sesi '{session_id_err_reactivate}': {e.stderr if e.stderr else e.stdout}")
        return jsonify({"status":"error","message":f"Gagal memulai layanan systemd: {e.stderr if e.stderr else e.stdout}"}),500
    except Exception as e: 
        req_data_reactivate_exc = request.get_json(silent=True) or {}
        session_id_err_reactivate_exc = req_data_reactivate_exc.get('session_id','N/A')
        logging.exception(f"Error saat reaktivasi sesi '{session_id_err_reactivate_exc}'")
        return jsonify({"status":"error","message":f'Kesalahan Server Internal: {str(e)}'}),500

@app.route('/api/delete-session', methods=['POST'])
@login_required
def delete_session_api(): 
    try:
        session_id_to_delete = request.json.get('session_id') # Nama sesi asli
        if not session_id_to_delete: return jsonify({'status':'error','message':'ID sesi (nama sesi asli) diperlukan'}),400
        s_data = read_sessions()
        if not any(s['id']==session_id_to_delete for s in s_data.get('inactive_sessions',[])): 
            return jsonify({'status':'error','message':f"Sesi '{session_id_to_delete}' tidak ditemukan di daftar tidak aktif."}),404
        s_data['inactive_sessions']=[s for s in s_data['inactive_sessions'] if s['id']!=session_id_to_delete]
        write_sessions(s_data)
        with socketio_lock: socketio.emit('inactive_sessions_update',{"inactive_sessions":get_inactive_sessions_data()})
        return jsonify({'status':'success','message':f"Sesi '{session_id_to_delete}' berhasil dihapus dari daftar tidak aktif."})
    except Exception as e: 
        req_data_del_sess = request.get_json(silent=True) or {}
        session_id_err_del_sess = req_data_del_sess.get('session_id','N/A')
        logging.exception(f"Error delete sesi '{session_id_err_del_sess}'")
        return jsonify({'status':'error','message':f'Kesalahan Server: {str(e)}'}),500

@app.route('/api/edit-session', methods=['POST']) # Hanya untuk edit detail sesi tidak aktif
@login_required
def edit_inactive_session_api(): 
    try:
        data = request.json
        session_id_to_edit = data.get('session_name_original', data.get('id')) # Terima nama sesi asli
        new_stream_key = data.get('stream_key')
        new_video_name = data.get('video_file') # Sesuai dengan frontend
        new_platform = data.get('platform', 'YouTube')
        
        if not session_id_to_edit: return jsonify({"status":"error","message":"ID sesi (nama sesi asli) diperlukan untuk edit."}),400
        s_data = read_sessions()
        session_found = next((s for s in s_data.get('inactive_sessions',[]) if s['id']==session_id_to_edit),None)
        if not session_found: return jsonify({"status":"error","message":f"Sesi '{session_id_to_edit}' tidak ditemukan di daftar tidak aktif."}),404
        
        if not new_stream_key or not new_video_name:
            return jsonify({"status":"error","message":"Stream key dan nama video baru diperlukan untuk update."}),400
        
        video_path_check = os.path.join(VIDEO_DIR,new_video_name)
        if not os.path.isfile(video_path_check):
            return jsonify({"status":"error","message":f"File video baru '{new_video_name}' tidak ditemukan."}),404
        if new_platform not in ["YouTube", "Facebook"]: new_platform="YouTube" 
        
        session_found['stream_key'] = new_stream_key.strip()
        session_found['video_name'] = new_video_name
        session_found['platform'] = new_platform
        
        write_sessions(s_data)
        with socketio_lock: socketio.emit('inactive_sessions_update',{"inactive_sessions":get_inactive_sessions_data()})
        return jsonify({"status":"success","message":f"Detail sesi tidak aktif '{session_id_to_edit}' berhasil diperbarui."})
    except Exception as e: 
        req_data_edit_sess = request.get_json(silent=True) or {}
        session_id_err_edit_sess = req_data_edit_sess.get('session_name_original', req_data_edit_sess.get('id', 'N/A'))
        logging.exception(f"Error edit sesi tidak aktif '{session_id_err_edit_sess}'")
        return jsonify({'status':'error','message':f'Kesalahan Server Internal: {str(e)}'}),500
        
# Tambahkan ini di dalam app.py, di bagian API endpoint Anda

@app.route('/api/inactive-sessions/delete-all', methods=['POST'])
@login_required
def delete_all_inactive_sessions_api():
    try:
        s_data = read_sessions()
        
        # Hitung jumlah sesi nonaktif yang akan dihapus (opsional, untuk logging atau respons)
        deleted_count = len(s_data.get('inactive_sessions', []))
        
        if deleted_count == 0:
            return jsonify({'status': 'success', 'message': 'Tidak ada sesi nonaktif untuk dihapus.', 'deleted_count': 0}), 200

        # Kosongkan daftar sesi nonaktif
        s_data['inactive_sessions'] = []
        write_sessions(s_data)
        
        with socketio_lock:
            socketio.emit('inactive_sessions_update', {"inactive_sessions": get_inactive_sessions_data()})
            
        logging.info(f"Berhasil menghapus semua ({deleted_count}) sesi tidak aktif.")
        return jsonify({'status': 'success', 'message': f'Berhasil menghapus {deleted_count} sesi tidak aktif.', 'deleted_count': deleted_count}), 200
    except Exception as e:
        logging.exception("Error di API delete_all_inactive_sessions")
        return jsonify({'status': 'error', 'message': f'Kesalahan Server: {str(e)}'}), 500
        
@app.route('/api/check-session', methods=['GET'])
@login_required
def check_session_api(): 
    return jsonify({'logged_in':True,'user':session.get('user')})

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True, use_reloader=True)
