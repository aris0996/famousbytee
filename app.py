import logging
from logging.handlers import RotatingFileHandler
from flask import Flask, render_template, redirect, url_for, request, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from flask_migrate import Migrate
from models import db, User, Role, ClassRoom, Student, Schedule, SchedulePreset, ScheduleTemplate, ScheduleTemplateItem, Announcement, BatchFund, FundPeriod, ActivityLog, SystemSetting, GalleryAlbum, GalleryPhoto, PhotoComment, AnnouncementRead, Assignment, NotificationHistory
import os
import json
import re
from PIL import Image
import csv
from io import StringIO
from flask import send_from_directory, make_response
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
from urllib import request as urllib_request, error as urllib_error

import firebase_admin
from firebase_admin import credentials, messaging

from config import Config
from flask_jwt_extended import JWTManager
from flask_cors import CORS
from routes.api import api_bp

# ============================================================
# LOGGING CONFIGURATION
# ============================================================
# Create logs directory if it doesn't exist
_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(_log_dir, exist_ok=True)

# Configure root logger for the application
_log_file = os.path.join(_log_dir, 'famousbytee.log')
_error_log_file = os.path.join(_log_dir, 'error.log')

# Main rotating log file (all levels)
_main_handler = RotatingFileHandler(_log_file, maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
_main_handler.setLevel(logging.INFO)
_main_handler.setFormatter(logging.Formatter(
    '[%(asctime)s] %(levelname)s in %(module)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
))

# Error-only rotating log file
_error_handler = RotatingFileHandler(_error_log_file, maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
_error_handler.setLevel(logging.ERROR)
_error_handler.setFormatter(logging.Formatter(
    '[%(asctime)s] %(levelname)s in %(module)s (%(pathname)s:%(lineno)d):\n%(message)s\n', datefmt='%Y-%m-%d %H:%M:%S'
))

# Console handler (for development / Apache error log capture)
_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.WARNING)
_console_handler.setFormatter(logging.Formatter(
    '[%(asctime)s] %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
))

# Apply to Flask app logger
app = Flask(__name__)
app.logger.handlers.clear()
app.logger.addHandler(_main_handler)
app.logger.addHandler(_error_handler)
app.logger.addHandler(_console_handler)
app.logger.setLevel(logging.INFO)

# Also configure root logger so library errors are captured
logging.basicConfig(level=logging.WARNING)

app.logger.info('Famousbytee application starting up...')
app.config.from_object(Config)

_WAHA_RECENT_COMMANDS = {}
_WAHA_COMMAND_DEDUP_WINDOW_SECONDS = 3  # Reduced from 12 to allow faster re-commands

# ============================================================
# REQUEST ACCESS LOGGING (captures access even without Apache logs)
# ============================================================
import time as _time

@app.before_request
def _log_request_start():
    request._start_time = _time.time()

@app.after_request
def _log_request_end(response):
    duration = _time.time() - getattr(request, '_start_time', _time.time())
    # Skip logging for static files to reduce noise
    if not request.path.startswith('/static/'):
        app.logger.info(
            '%s %s %s -> %s (%.3fs) [IP: %s]',
            request.method, request.path,
            'API' if request.path.startswith('/api/') else 'WEB',
            response.status_code, duration,
            request.remote_addr
        )
    return response

# Initialize Firebase Admin
def _initialize_firebase():
    """Helper to initialize Firebase Admin on-demand."""
    if firebase_admin._apps:
        return True
        
    try:
        cred_path = os.path.join(app.root_path, 'serviceAccountKey.json')
        if os.path.exists(cred_path):
            cred = credentials.Certificate(cred_path)
            firebase_admin.initialize_app(cred)
            app.logger.info('Firebase Admin initialized successfully.')
            return True
        else:
            app.logger.warning('serviceAccountKey.json not found. Push notifications disabled.')
            return False
    except Exception as e:
        app.logger.error(f'Firebase Init Error: {e}')
        return False

# Try initial load
_initialize_firebase()

def get_setting_value(key, default=''):
    setting = SystemSetting.query.filter_by(key=key).first()
    if not setting:
        return default
    return setting.value if setting.value is not None else default

def set_setting_value(key, value, description=None):
    setting = SystemSetting.query.filter_by(key=key).first()
    if setting:
        setting.value = value
        if description:
            setting.description = description
    else:
        db.session.add(SystemSetting(key=key, value=value, description=description))

def _normalize_waha_scalar(value):
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        if 'serialized' in value:
            return _normalize_waha_scalar(value.get('serialized'))
        if '_serialized' in value:
            return _normalize_waha_scalar(value.get('_serialized'))
        if 'id' in value and isinstance(value.get('id'), str):
            return value.get('id')
        user = value.get('user') or value.get('number') or value.get('phone')
        server = value.get('server')
        if user and server:
            return f"{user}@{server}"
        if 'pushname' in value:
            return _normalize_waha_scalar(value.get('pushname'))
        if 'name' in value:
            return _normalize_waha_scalar(value.get('name'))
        if 'wid' in value:
            return _normalize_waha_scalar(value.get('wid'))
        return json.dumps(value, ensure_ascii=True)[:120]
    if isinstance(value, list):
        return ', '.join(_normalize_waha_scalar(v) for v in value[:5])
    return str(value)

def _normalize_waha_chat_id(item):
    candidates = []
    if isinstance(item, dict):
        candidates.extend([
            item.get('chatId'),
            item.get('id'),
            item.get('_id'),
            item.get('wid'),
            item.get('contactId')
        ])
        if isinstance(item.get('id'), dict):
            candidates.append(item.get('id'))
        if isinstance(item.get('wid'), dict):
            candidates.append(item.get('wid'))
    for candidate in candidates:
        normalized = _normalize_waha_chat_identifier(_normalize_waha_scalar(candidate).strip())
        if normalized:
            return normalized
    return ''

def _normalize_waha_chat_identifier(value):
    value = str(value or '').strip()
    if not value:
        return ''
    if value.endswith('@s.whatsapp.net'):
        return value.replace('@s.whatsapp.net', '@c.us')
    return value

def get_fund_periods():
    return FundPeriod.query.filter_by(is_active=True).order_by(FundPeriod.start_date.asc(), FundPeriod.id.asc()).all()

def _count_weekdays_between(start_date, end_date):
    total = 0
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:
            total += 1
        current += timedelta(days=1)
    return total

def _waha_headers():
    api_key = get_setting_value('waha_api_key', '').strip()
    headers = {'Content-Type': 'application/json'}
    if api_key:
        headers['X-Api-Key'] = api_key
    return headers

def _waha_request(method, path, payload=None):
    base_url = get_setting_value('waha_base_url', '').strip().rstrip('/')
    if not base_url:
        return {'ok': False, 'error': 'WAHA base URL belum diatur'}

    url = f"{base_url}{path}"
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib_request.Request(url, data=data, headers=_waha_headers(), method=method)

    try:
        with urllib_request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode('utf-8') if resp.length != 0 else ''
            return {'ok': True, 'status': resp.status, 'data': json.loads(raw) if raw else None}
    except urllib_error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='ignore')
        return {'ok': False, 'error': f'HTTP {e.code}: {detail[:180]}'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}

def _apply_whatsapp_admin_header(text, title=None):
    text = (text or '').strip()
    if not text:
        return text

    header_enabled = get_setting_value('waha_admin_header_enabled', 'true').strip().lower() == 'true'
    if not header_enabled:
        return text

    header_template = get_setting_value(
        'waha_admin_header_text',
        '*[PESAN RESMI ADMIN FAMOUSBYTEE]*\n{title_block}Pesan ini dikirim dari sistem admin.\n'
    )
    title_block = f"*Topik:* {title}\n" if (title or '').strip() else ''
    header = _render_template_string(header_template, {
        'title': (title or '').strip(),
        'title_block': title_block
    }).strip()

    if not header:
        return text
    if text.startswith(header):
        return text
    return f"{header}\n\n{text}".strip()

def get_notification_channel_mode():
    mode = get_setting_value('notification_channel_default', 'push').strip().lower()
    if mode not in {'push', 'whatsapp', 'both'}:
        return 'push'
    return mode

def _get_indo_day_name(date_value):
    return {
        'Monday': 'Senin', 'Tuesday': 'Selasa', 'Wednesday': 'Rabu',
        'Thursday': 'Kamis', 'Friday': 'Jumat', 'Saturday': 'Sabtu', 'Sunday': 'Minggu'
    }.get(date_value.strftime('%A'), date_value.strftime('%A'))

def _format_indo_date(date_value):
    month_name = {
        1: 'Januari', 2: 'Februari', 3: 'Maret', 4: 'April',
        5: 'Mei', 6: 'Juni', 7: 'Juli', 8: 'Agustus',
        9: 'September', 10: 'Oktober', 11: 'November', 12: 'Desember'
    }.get(date_value.month, str(date_value.month))
    return f"{date_value.day:02d} {month_name} {date_value.year}"

def _render_template_string(template, values):
    result = template or ''
    for key, value in values.items():
        result = result.replace(f"{{{key}}}", str(value))
    return result

def _normalize_multiline_text(text):
    lines = [line.rstrip() for line in str(text or '').replace('\r\n', '\n').split('\n')]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    normalized = []
    previous_blank = False
    for line in lines:
        is_blank = not line.strip()
        if is_blank and previous_blank:
            continue
        normalized.append(line)
        previous_blank = is_blank
    return '\n'.join(normalized).strip()

def _build_schedule_summary_message(target_date=None):
    target_date = target_date or (datetime.now().date() + timedelta(days=1))
    day_name = _get_indo_day_name(target_date)
    date_long = _format_indo_date(target_date)

    schedules = Schedule.query.filter_by(day=day_name).order_by(Schedule.time_start.asc()).all()
    assignments = Assignment.query.order_by(Assignment.deadline.asc()).all()
    due_items = [a for a in assignments if a.deadline.date() == target_date]

    if not schedules and not due_items:
        return ''

    item_template = get_setting_value(
        'waha_schedule_item_template',
        '{index}. MK {subject} mulai jam {time_range}'
    )
    deadline_item_template = get_setting_value(
        'waha_schedule_deadline_item_template',
        '{index}. Deadline {subject}: {title} jam {deadline_time}'
    )
    full_template = get_setting_value(
        'waha_schedule_template',
        'Assalamualaikum dan selamat malam, tabe saudara dan saudari sekalian di grup ini, Jadwal Mata Kuliah {day_name}, {date_long}\n'
        '{schedule_lines}\n'
        '{deadline_section}'
        '(Sesuai jadwal dari pihak kampus)\n'
        '{extra_info_section}'
        'Sekian dan terimakasih'
    )
    extra_info = _normalize_multiline_text(get_setting_value('waha_schedule_extra_info', ''))

    schedule_lines = []
    for index, schedule in enumerate(schedules, start=1):
        schedule_lines.append(_render_template_string(item_template, {
            'index': index,
            'subject': schedule.subject or '-',
            'time_start': schedule.time_start or '-',
            'time_end': schedule.time_end or '-',
            'time_range': f"{schedule.time_start or '-'}-{schedule.time_end or '-'}",
            'room': schedule.room or '-',
            'lecturer': schedule.lecturer or '-',
            'day_name': day_name,
            'date_long': date_long
        }))

    if not schedule_lines:
        schedule_lines = ['Tidak ada jadwal mata kuliah pada tanggal tersebut.']

    deadline_lines = []
    for index, assignment in enumerate(due_items, start=1):
        deadline_lines.append(_render_template_string(deadline_item_template, {
            'index': index,
            'subject': assignment.subject or 'Tugas',
            'title': assignment.title or '-',
            'deadline_time': assignment.deadline.strftime('%H:%M'),
            'deadline_date': _format_indo_date(assignment.deadline.date())
        }))

    deadline_section = ''
    if deadline_lines:
        deadline_section = "Deadline Tugas:\n" + "\n".join(deadline_lines) + "\n"

    extra_info_section = ''
    if extra_info:
        extra_info_section = extra_info + "\n"

    message = _render_template_string(full_template, {
        'day_name': day_name,
        'date_long': date_long,
        'date_short': target_date.strftime('%d-%m-%Y'),
        'schedule_lines': '\n'.join(schedule_lines),
        'deadline_lines': '\n'.join(deadline_lines),
        'deadline_section': deadline_section,
        'extra_info': extra_info,
        'extra_info_section': extra_info_section
    })
    return _normalize_multiline_text(message)

def _build_tomorrow_summary_message():
    return _build_schedule_summary_message(datetime.now().date() + timedelta(days=1))

def _normalize_phone_number(raw_value):
    digits = re.sub(r'\D', '', str(raw_value or ''))
    if not digits:
        return ''
    if digits.startswith('0'):
        digits = '62' + digits[1:]
    elif digits.startswith('8'):
        digits = '62' + digits
    return digits

def _find_user_by_whatsapp(raw_value):
    candidate = _normalize_phone_number(raw_value)
    if not candidate:
        return None
    for user in User.query.filter(User.whatsapp.isnot(None)).all():
        user_phone = _normalize_phone_number(user.whatsapp)
        if not user_phone:
            continue
        if user_phone == candidate or user_phone.endswith(candidate) or candidate.endswith(user_phone):
            return user
    return None

def _extract_waha_event(payload):
    payload = payload if isinstance(payload, dict) else {}
    candidates = []
    root_payload = payload.get('payload')
    message_payload = payload.get('message')
    data_payload = payload.get('data')
    event_payload = payload.get('eventPayload')

    for candidate in (root_payload, message_payload, data_payload, event_payload, payload):
        if isinstance(candidate, dict):
            candidates.append(candidate)

    for key in ('payload', 'message', 'data', 'eventPayload'):
        candidate = payload.get(key)
        if isinstance(candidate, dict) and candidate not in candidates:
            candidates.append(candidate)

    body = ''
    chat_id = ''
    outgoing_chat_id = ''
    sender_ref = ''
    from_me = False
    me_id = _normalize_waha_chat_identifier(_normalize_waha_scalar((payload.get('me') or {}).get('id')).strip())
    message_id_hint = ''

    for candidate in candidates:
        if not body:
            value = candidate.get('body') or candidate.get('text') or candidate.get('message')
            if isinstance(value, str):
                body = value.strip()
        if not message_id_hint:
            message_id_hint = _normalize_waha_scalar(candidate.get('id')).strip()
        if not chat_id:
            possible_chat_ids = [
                candidate.get('chatId'),
                candidate.get('from'),
                candidate.get('to'),
                candidate.get('chat_id'),
                candidate.get('conversation'),
                candidate.get('remoteJid'),
                candidate.get('remote')
            ]
            for possible_chat_id in possible_chat_ids:
                normalized_chat_id = _normalize_waha_chat_identifier(_normalize_waha_scalar(possible_chat_id).strip())
                if normalized_chat_id and ('@c.us' in normalized_chat_id or '@g.us' in normalized_chat_id or '@newsletter' in normalized_chat_id or '@lid' in normalized_chat_id):
                    chat_id = normalized_chat_id
                    break
            if not chat_id:
                normalized_nested_chat_id = _normalize_waha_chat_id(candidate).strip()
                if normalized_nested_chat_id and ('@c.us' in normalized_nested_chat_id or '@g.us' in normalized_nested_chat_id or '@newsletter' in normalized_nested_chat_id or '@lid' in normalized_nested_chat_id):
                    chat_id = normalized_nested_chat_id
        if not outgoing_chat_id:
            outgoing_candidates = [
                candidate.get('to'),
                candidate.get('from')
            ]
            for outgoing_candidate in outgoing_candidates:
                possible_outgoing = _normalize_waha_chat_identifier(_normalize_waha_scalar(outgoing_candidate).strip())
                if possible_outgoing and possible_outgoing != me_id and ('@c.us' in possible_outgoing or '@g.us' in possible_outgoing or '@newsletter' in possible_outgoing or '@lid' in possible_outgoing):
                    outgoing_chat_id = possible_outgoing
                    break
        if not outgoing_chat_id and message_id_hint.startswith('true_'):
            parts = message_id_hint.split('_')
            if len(parts) >= 2:
                hinted_chat_id = _normalize_waha_chat_identifier(parts[1])
                if hinted_chat_id and hinted_chat_id != me_id and ('@c.us' in hinted_chat_id or '@g.us' in hinted_chat_id or '@newsletter' in hinted_chat_id or '@lid' in hinted_chat_id):
                    outgoing_chat_id = hinted_chat_id
        if not outgoing_chat_id:
            possible_outgoing = _normalize_waha_chat_identifier(_normalize_waha_scalar(candidate.get('to')).strip())
            if possible_outgoing and ('@c.us' in possible_outgoing or '@g.us' in possible_outgoing or '@newsletter' in possible_outgoing or '@lid' in possible_outgoing):
                outgoing_chat_id = possible_outgoing
        if not sender_ref:
            sender_ref = (
                _normalize_waha_chat_identifier(_normalize_waha_scalar(candidate.get('participant')).strip())
                or _normalize_waha_chat_identifier(_normalize_waha_scalar(candidate.get('author')).strip())
                or _normalize_waha_chat_identifier(_normalize_waha_scalar(candidate.get('participant')).strip())
                or _normalize_waha_chat_identifier(_normalize_waha_scalar(candidate.get('sender')).strip())
                or _normalize_waha_chat_identifier(_normalize_waha_scalar(candidate.get('from')).strip())
                or _normalize_waha_chat_identifier(_normalize_waha_scalar(candidate.get('to')).strip())
            )
        if candidate.get('fromMe') is True:
            from_me = True

    if from_me and outgoing_chat_id:
        chat_id = outgoing_chat_id
    elif chat_id == me_id and outgoing_chat_id:
        chat_id = outgoing_chat_id
    elif not chat_id and from_me:
        for candidate in candidates:
            fallback_to = _normalize_waha_chat_identifier(_normalize_waha_scalar(candidate.get('to')).strip())
            if fallback_to and ('@c.us' in fallback_to or '@g.us' in fallback_to or '@newsletter' in fallback_to or '@lid' in fallback_to):
                chat_id = fallback_to
                break

    return {
        'body': body,
        'chat_id': chat_id,
        'sender_ref': sender_ref,
        'from_me': from_me,
        'event': _normalize_waha_scalar(payload.get('event') or payload.get('eventName') or ''),
        'message_id': message_id_hint or _normalize_waha_scalar(payload.get('id')).strip()
    }


def _is_duplicate_waha_command(event_data):
    now = datetime.now()
    expired_keys = [
        key for key, expires_at in _WAHA_RECENT_COMMANDS.items()
        if expires_at <= now
    ]
    for key in expired_keys:
        _WAHA_RECENT_COMMANDS.pop(key, None)

    body = (event_data.get('body') or '').strip().lower()
    chat_id = (event_data.get('chat_id') or '').strip()
    message_id = (event_data.get('message_id') or '').strip()
    event_name = (event_data.get('event') or '').strip().lower()

    if not body or not chat_id or not event_name.startswith('message'):
        return False

    if message_id:
        message_key = f"id:{message_id}"
        if message_key in _WAHA_RECENT_COMMANDS:
            return True
        _WAHA_RECENT_COMMANDS[message_key] = now + timedelta(
            seconds=_WAHA_COMMAND_DEDUP_WINDOW_SECONDS
        )

    command_key = f"cmd:{chat_id}:{body}"
    if command_key in _WAHA_RECENT_COMMANDS:
        return True

    _WAHA_RECENT_COMMANDS[command_key] = now + timedelta(
        seconds=_WAHA_COMMAND_DEDUP_WINDOW_SECONDS
    )
    return False

def _build_kas_command_response(sender_ref=''):
    total_in = db.session.query(db.func.sum(BatchFund.amount)).filter(BatchFund.type == 'Masuk').scalar() or 0
    total_out = db.session.query(db.func.sum(BatchFund.amount)).filter(BatchFund.type == 'Keluar').scalar() or 0
    balance = total_in - total_out
    target_payment = get_fund_target()
    active_periods = get_fund_periods()

    lines = [
        "Info kas kelas:",
        f"- Saldo kas saat ini: Rp {'{:,.0f}'.format(balance)}",
        f"- Target iuran aktif sampai hari ini: Rp {'{:,.0f}'.format(target_payment)}"
    ]

    if active_periods:
        latest_periods = active_periods[:3]
        period_labels = [
            f"{period.title} ({period.start_date.strftime('%d/%m/%Y')} - {period.end_date.strftime('%d/%m/%Y')})"
            for period in latest_periods
        ]
        lines.append("- Periode aktif: " + "; ".join(period_labels))

    user = _find_user_by_whatsapp(sender_ref)
    if user and user.student:
        total_paid = db.session.query(db.func.sum(BatchFund.amount)).filter(
            BatchFund.student_id == user.student.id,
            BatchFund.type == 'Masuk'
        ).scalar() or 0
        arrears = max(0, target_payment - total_paid)
        lines.extend([
            "",
            f"Data Anda ({user.student.full_name}):",
            f"- Sudah bayar: Rp {'{:,.0f}'.format(total_paid)}",
            f"- Kekurangan: Rp {'{:,.0f}'.format(arrears)}"
        ])
    else:
        lines.extend([
            "",
            "Data personal belum bisa dicocokkan ke akun. Isi nomor WhatsApp user di backend bila ingin balasan personal ikut tampil."
        ])

    return "\n".join(lines)

def _build_tunggakan_command_response(command_text, sender_ref=''):
    """Build response for /tunggakan command - uses same logic as Flutter (cumulative)"""
    # Get cumulative target (same as Flutter)
    target_payment = get_fund_target()
    
    # Get all active students
    from models import Student
    all_students = Student.query.filter(Student.status == 'Aktif').all()
    
    arrears_list = []
    for student in all_students:
        # Calculate TOTAL paid (all time, not per period - same as Flutter)
        total_paid = db.session.query(db.func.sum(BatchFund.amount)).filter(
            BatchFund.student_id == student.id,
            BatchFund.type == 'Masuk'
        ).scalar() or 0
        
        # Calculate arrears (same logic as Flutter)
        arrears = max(0, target_payment - total_paid)
        
        # ONLY include students with actual arrears
        if arrears > 0 and total_paid < target_payment:
            arrears_list.append({
                'name': student.full_name,
                'arrears': arrears,
                'target': target_payment,
                'paid': total_paid
            })
    
    if not arrears_list:
        return "✅ Tidak ada tunggakan. Semua mahasiswa sudah lunas!"
    
    # Sort by arrears (highest first)
    arrears_list.sort(key=lambda x: x['arrears'], reverse=True)
    
    # Build response
    lines = [f"📊 *Daftar Tunggakan Kas:*", ""]
    
    for index, item in enumerate(arrears_list, start=1):
        lines.append(
            f"{index}. *{item['name']}*\n"
            f"   Target: Rp {item['target']:,}\n"
            f"   Terbayar: Rp {item['paid']:,}\n"
            f"   *Tunggakan: Rp {item['arrears']:,}*"
        )
    
    lines.append("")
    lines.append(f"*Total: {len(arrears_list)} mahasiswa* masih memiliki tunggakan.")
    
    return "\n".join(lines)

def _build_lunas_command_response(command_text, sender_ref=''):
    """Build response for /lunas command - shows ONLY fully paid students"""
    # Get cumulative target (same as Flutter)
    target_payment = get_fund_target()
    
    # Get all active students
    from models import Student
    all_students = Student.query.filter(Student.status == 'Aktif').all()
    
    # ONLY collect students who are FULLY PAID
    fully_paid_list = []
    for student in all_students:
        # Calculate TOTAL paid (all time, not per period - same as Flutter)
        total_paid = db.session.query(db.func.sum(BatchFund.amount)).filter(
            BatchFund.student_id == student.id,
            BatchFund.type == 'Masuk'
        ).scalar() or 0
        
        # ONLY include if FULLY PAID (paid >= target)
        if total_paid >= target_payment:
            fully_paid_list.append({
                'name': student.full_name,
                'target': target_payment,
                'paid': total_paid
            })
    
    if not fully_paid_list:
        return "💰 *Daftar Pembayaran Kas:*\n\nBelum ada mahasiswa yang lunas. Gunakan /tunggakan untuk melihat daftar tunggakan."
    
    # Sort by paid amount (highest first)
    fully_paid_list.sort(key=lambda x: -x['paid'])
    
    # Build response - ONLY show fully paid
    lines = ["💰 *Daftar Pembayaran Kas - LUNAS:*", ""]
    
    for index, item in enumerate(fully_paid_list, start=1):
        lines.append(
            f"{index}. *{item['name']}* - Rp {item['paid']:,}"
        )
    
    lines.append("")
    lines.append(f"*Total: {len(fully_paid_list)} mahasiswa* sudah lunas ✅")
    
    return "\n".join(lines)

def _build_assignment_command_response(limit=5):
    assignments = Assignment.query.filter(Assignment.deadline >= datetime.now()).order_by(Assignment.deadline.asc()).limit(limit).all()
    if not assignments:
        return "Tidak ada tugas yang sedang aktif."
    lines = ["Daftar tugas terdekat:"]
    for index, assignment in enumerate(assignments, start=1):
        lines.append(
            f"{index}. {assignment.subject or 'Tugas'} - {assignment.title} "
            f"(deadline {assignment.deadline.strftime('%d/%m/%Y %H:%M')})"
        )
    return "\n".join(lines)

def _build_deadline_command_response(limit=5):
    assignments = Assignment.query.filter(Assignment.deadline >= datetime.now()).order_by(Assignment.deadline.asc()).limit(limit).all()
    if not assignments:
        return "Tidak ada deadline terdekat."
    lines = ["Deadline terdekat:"]
    for index, assignment in enumerate(assignments, start=1):
        deadline_date = assignment.deadline.date()
        lines.append(
            f"{index}. {assignment.title} - {_get_indo_day_name(deadline_date)}, {assignment.deadline.strftime('%d/%m/%Y %H:%M')} ({assignment.subject or 'Tanpa matkul'})"
        )
    return "\n".join(lines)

def _build_help_command_response():
    return (
        "Perintah WA yang tersedia:\n"
        "\n"
        "1. /help\n"
        "   Menampilkan daftar command yang bisa digunakan.\n"
        "\n"
        "2. /jadwal\n"
        "   Menampilkan jadwal hari ini.\n"
        "\n"
        "3. /jadwal besok\n"
        "   Menampilkan jadwal besok.\n"
        "\n"
        "4. /tugas\n"
        "   Menampilkan tugas aktif terdekat.\n"
        "\n"
        "5. /tugas 10\n"
        "   Menampilkan 10 tugas aktif terdekat.\n"
        "\n"
        "6. /deadline\n"
        "   Menampilkan deadline terdekat.\n"
        "\n"
        "7. /deadline 10\n"
        "   Menampilkan 10 deadline terdekat.\n"
        "\n"
        "8. /datakas\n"
        "   Menampilkan ringkasan kas kelas dan data personal jika nomor cocok.\n"
        "\n"
        "9. /tunggakan\n"
        "   Menampilkan daftar mahasiswa yang masih memiliki tunggakan kas (semua periode).\n"
        "\n"
        "10. /tunggakan /periode 1\n"
        "    Menampilkan tunggakan kas untuk periode tertentu.\n"
        "\n"
        "11. /lunas\n"
        "    Menampilkan daftar mahasiswa yang sudah bayar kas (semua periode).\n"
        "\n"
        "12. /lunas /periode 1\n"
        "    Menampilkan pembayaran kas untuk periode tertentu.\n"
        "\n"
        "Contoh:\n"
        "/jadwal besok\n"
        "/tugas 7\n"
        "/deadline 3\n"
        "/datakas\n"
        "/tunggakan\n"
        "/tunggakan /periode 1\n"
        "/lunas\n"
        "/lunas /periode 2"
    )

def _extract_command_limit(command_text, default_limit=5, max_limit=15):
    parts = (command_text or '').split()
    if len(parts) >= 2 and parts[1].isdigit():
        return min(max_limit, max(1, int(parts[1])))
    return default_limit

def _build_waha_command_response(command_text, sender_ref=''):
    command_text = (command_text or '').strip()
    lowered = command_text.lower()

    # Help commands
    if lowered in {'/help', '/menu', '/commands', '/cmd'}:
        return _build_help_command_response()
    
    # Schedule commands
    if lowered.startswith('/jadwal'):
        target_date = datetime.now().date()
        if 'besok' in lowered:
            target_date = target_date + timedelta(days=1)
        elif 'hari ini' in lowered:
            target_date = datetime.now().date()
        response = _build_schedule_summary_message(target_date)
        return response or f"Tidak ada jadwal untuk {_get_indo_day_name(target_date)}, {_format_indo_date(target_date)}."
    
    # Assignment commands
    if lowered.startswith('/tugas'):
        return _build_assignment_command_response(limit=_extract_command_limit(lowered))
    if lowered.startswith('/deadline'):
        return _build_deadline_command_response(limit=_extract_command_limit(lowered))
    
    # Finance commands
    if lowered.startswith('/datakas'):
        return _build_kas_command_response(sender_ref=sender_ref)
    if lowered.startswith('/tunggakan'):
        return _build_tunggakan_command_response(lowered, sender_ref=sender_ref)
    if lowered.startswith('/lunas'):
        return _build_lunas_command_response(lowered, sender_ref=sender_ref)
    
    # Invalid command - block unknown commands
    valid_commands = ['/help', '/menu', '/commands', '/cmd', '/jadwal', '/tugas', '/deadline', '/datakas', '/tunggakan', '/lunas']
    command_base = lowered.split()[0] if lowered.split() else lowered
    
    if command_base in valid_commands:
        return _build_help_command_response()
    
    # Block invalid commands
    return f"❌ Command tidak dikenal: {command_base}\n\nGunakan /help untuk melihat daftar command yang tersedia."

def _resolve_notification_classroom_id(user_id=None, sender_id=None):
    try:
        if user_id:
            user = User.query.get(int(user_id))
            if user:
                return user.classroom_id or (user.student.classroom_id if user.student else None)
        if sender_id:
            sender = User.query.get(int(sender_id))
            if sender:
                return sender.classroom_id or (sender.student.classroom_id if sender.student else None)
    except Exception:
        pass
    default_class = ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()
    return default_class.id if default_class else None


def _log_notification_history(title, body, user_id, sender_id, status, channel='push', classroom_id=None):
    """Helper to log notification history."""
    try:
        title = (title or '').strip()
        body = (body or '').strip()
        if not title and not body:
            return

        safe_status = str(status)[:99]
        
        history = NotificationHistory(
            title=title,
            body=body,
            channel=channel,
            target=str(user_id) if user_id else "All",
            sent_by=sender_id,
            classroom_id=classroom_id or _resolve_notification_classroom_id(user_id=user_id, sender_id=sender_id),
            status=safe_status
        )
        db.session.add(history)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"History Log Error: {e}")

def send_push(title, body, user_id=None, sender_id=None, extra_data=None, classroom_id=None):
    """Sends push notification to a specific user or everyone."""
    title = (title or '').strip()
    body = (body or '').strip()
    if not title and not body:
        _log_notification_history("Notifikasi dibatalkan", "Judul dan isi kosong.", user_id, sender_id, "Skipped (Empty)", channel='push', classroom_id=classroom_id)
        return
    if not title:
        title = "Notifikasi"
    if not body:
        body = title

    if not _initialize_firebase():
        _log_notification_history(title, body, user_id, sender_id, "Failed (Config)", channel='push', classroom_id=classroom_id)
        return

    payload_data = {'title': title, 'body': body}
    if extra_data:
        payload_data.update({str(k): str(v) for k, v in extra_data.items()})

    try:
        if user_id:
            user = User.query.get(user_id)
            if user and user.fcm_token:
                try:
                    message = messaging.Message(
                        notification=messaging.Notification(title=title, body=body),
                        android=messaging.AndroidConfig(
                            priority='high',
                            notification=messaging.AndroidNotification(
                                channel_id='high_importance_channel',
                                sound='default'
                            )
                        ),
                        data=payload_data,
                        token=user.fcm_token
                    )
                    messaging.send(message)
                    status = "Success"
                except Exception as e:
                    status = f"Error: {str(e)[:90]}"
                    if "registration-token-not-registered" in str(e).lower():
                        user.fcm_token = None
                        db.session.commit()
            else:
                status = "No Token"
        else:
            # Broadcast
            users = User.query.filter(User.fcm_token.isnot(None)).all()
            if classroom_id is not None:
                users = [
                    u for u in users
                    if (u.classroom_id == classroom_id) or (u.student and u.student.classroom_id == classroom_id)
                ]
            if not users:
                status = "No Recipients"
            else:
                messages = [
                    messaging.Message(
                        notification=messaging.Notification(title=title, body=body),
                        android=messaging.AndroidConfig(
                            priority='high',
                            notification=messaging.AndroidNotification(
                                channel_id='high_importance_channel',
                                sound='default'
                            )
                        ),
                        data=payload_data,
                        token=u.fcm_token
                    ) for u in users
                ]
                # send_each is better for multiple tokens
                response = messaging.send_each(messages)
                status = f"Success ({response.success_count}/{len(users)})"
                
                # Cleanup invalid tokens if any failed
                if response.failure_count > 0:
                    for idx, resp in enumerate(response.responses):
                        if not resp.success:
                            if "registration-token-not-registered" in str(resp.exception).lower():
                                users[idx].fcm_token = None
                    db.session.commit()
    except Exception as e:
        print(f"Push Notification General Error: {e}")
        status = f"System Error: {str(e)[:80]}"
    
    _log_notification_history(title, body, user_id, sender_id, status, channel='push', classroom_id=classroom_id)

def send_whatsapp(text, sender_id=None, title=None, chat_id=None, force=False, classroom_id=None):
    text = (text or '').strip()
    if not text:
        _log_notification_history(title or "WhatsApp dibatalkan", "Pesan WhatsApp kosong.", None, sender_id, "Skipped (Empty)", channel='whatsapp', classroom_id=classroom_id)
        return {'ok': False, 'error': 'Pesan WhatsApp kosong'}
    text = _apply_whatsapp_admin_header(text, title=title)

    if not force and get_setting_value('waha_enabled', 'false').lower() != 'true':
        _log_notification_history(title or "WhatsApp nonaktif", text, None, sender_id, "Disabled", channel='whatsapp', classroom_id=classroom_id)
        return {'ok': False, 'error': 'WhatsApp nonaktif'}

    session_name = get_setting_value('waha_session', '').strip()
    target_chat = (chat_id or get_setting_value('waha_group_chat_id', '')).strip()
    if not session_name or not target_chat:
        _log_notification_history(title or "WhatsApp gagal", text, None, sender_id, "Missing session/chat", channel='whatsapp', classroom_id=classroom_id)
        return {'ok': False, 'error': 'Session atau group chat WAHA belum diatur'}

    payload = {'session': session_name, 'chatId': target_chat, 'text': text}
    result = _waha_request('POST', '/api/sendText', payload)
    status = 'Success' if result['ok'] else f"Failed: {result['error'][:80]}"
    _log_notification_history(title or "WhatsApp", text, None, sender_id, status, channel='whatsapp', classroom_id=classroom_id)
    return result

def send_multichannel_notification(title, body, user_id=None, sender_id=None, allow_whatsapp=False, whatsapp_text=None, extra_data=None, classroom_id=None):
    mode = get_notification_channel_mode()
    results = {}

    if mode in {'push', 'both'}:
        send_push(title, body, user_id=user_id, sender_id=sender_id, extra_data=extra_data, classroom_id=classroom_id)
        results['push'] = True

    if allow_whatsapp and mode in {'whatsapp', 'both'}:
        wa_text = whatsapp_text or f"{title}\n{body}".strip()
        results['whatsapp'] = send_whatsapp(wa_text, sender_id=sender_id, title=title, classroom_id=classroom_id)

    return results

def cleanup_old_activity_logs(retention_days=None):
    """Delete activity logs older than the configured retention window."""
    with app.app_context():
        try:
            days = int(retention_days or get_setting_value('activity_log_retention_days', '30') or 30)
        except ValueError:
            days = 30

        cutoff = datetime.now() - timedelta(days=max(days, 1))
        deleted = ActivityLog.query.filter(ActivityLog.timestamp < cutoff).delete(synchronize_session=False)
        if deleted:
            db.session.commit()
            app.logger.info('Activity log cleanup removed %s rows older than %s days.', deleted, days)

def run_automated_reminders():
    """Background task to check and send reminders."""
    with app.app_context():
        now = datetime.now()
        current_day_indo = {'Monday': 'Senin', 'Tuesday': 'Selasa', 'Wednesday': 'Rabu', 'Thursday': 'Kamis', 'Friday': 'Jumat', 'Saturday': 'Sabtu', 'Sunday': 'Minggu'}.get(now.strftime('%A'))
        current_time_plus_15 = (now + timedelta(minutes=15)).strftime('%H:%M')
        
        # 1. Class Reminder (H-15 Menit)
        upcoming_class = Schedule.query.filter_by(day=current_day_indo, time_start=current_time_plus_15).all()
        for c in upcoming_class:
            send_push("Pengingat Kuliah", f"Kelas {c.subject} akan dimulai dalam 15 menit di {c.room}.")

        # 2. Assignment Deadline (H-1) - check once an hour at minute 0
        if now.minute == 0:
            tomorrow = (now + timedelta(days=1)).date()
            assignments = Assignment.query.all()
            for a in assignments:
                if a.deadline.date() == tomorrow:
                    send_push("Deadline Tugas Besok!", f"Jangan lupa tugas {a.subject}: {a.title} dikumpulkan besok.")

        # 3. Weekly Fund Reminder (Every Monday at 08:00)
        if now.strftime('%A') == 'Monday' and now.hour == 8 and now.minute == 0:
            send_push("Tagihan Kas Mingguan", "Selamat pagi! Jangan lupa bayar kas minggu ini ya teman-teman.")

        # 4. Daily WhatsApp Summary
        summary_time = get_setting_value('waha_daily_time', '18:00')
        last_sent_date = get_setting_value('waha_last_daily_summary_date', '')
        if now.strftime('%H:%M') == summary_time and last_sent_date != now.strftime('%Y-%m-%d'):
            summary_text = _build_tomorrow_summary_message()
            if summary_text:
                result = send_whatsapp(summary_text, title="Ringkasan Besok")
                if result.get('ok'):
                    set_setting_value('waha_last_daily_summary_date', now.strftime('%Y-%m-%d'))
                    db.session.commit()

# Initialize Scheduler (WSGI-safe: only start once, not on reload)
scheduler = None
try:
    # Prevent duplicate schedulers in multi-process WSGI deployments
    if not getattr(app, '_scheduler_started', False):
        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(func=run_automated_reminders, trigger="interval", minutes=1)
        scheduler.add_job(func=cleanup_old_activity_logs, trigger="cron", hour=3, minute=0)
        scheduler.start()
        app._scheduler_started = True
        app.logger.info('Background scheduler started successfully.')
except Exception as _sched_err:
    app.logger.error(f'Failed to start background scheduler: {_sched_err}')

# Enable CORS for all routes (important for mobile/cross-origin requests)
CORS(app)

# Initialize JWT
app.config['JWT_SECRET_KEY'] = app.config.get('SECRET_KEY', 'super-secret-dev-key')
jwt = JWTManager(app)

# Register API Blueprint
app.register_blueprint(api_bp)

UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

db.init_app(app)
migrate = Migrate(app, db)
login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)

# Ensure database tables are created for new features
with app.app_context():
    # Auto-run migrations on startup to ensure schema is always up to date
    if os.path.exists('migrations'):
        try:
            from flask_migrate import upgrade
            upgrade()
            app.logger.info('Database schema is up to date.')
        except Exception as e:
            app.logger.error(f'Migration auto-run skipped or failed: {e}')
    else:
        app.logger.warning("'migrations' folder not found. Auto-upgrade skipped.")
    
    # Auto-Patch for MySQL Production (Missing columns check)
    try:
        from sqlalchemy import text
        db.session.execute(text("SELECT points FROM user LIMIT 1"))
    except Exception:
        db.session.rollback()
        try:
            print("Database Patch: Adding missing columns to user table...")
            db.session.execute(text("ALTER TABLE user ADD COLUMN points INTEGER DEFAULT 0"))
            db.session.commit()
            print("Database Patch: Success.")
        except Exception as e:
            print(f"Database Patch Error: {e}")

    try:
        from sqlalchemy import text
        db.session.execute(text("SELECT classroom_id FROM user LIMIT 1"))
    except Exception:
        db.session.rollback()
        try:
            print("Database Patch: Adding classroom_id column to user table...")
            db.session.execute(text("ALTER TABLE user ADD COLUMN classroom_id INTEGER NULL"))
            db.session.commit()
            print("Database Patch: Success.")
        except Exception as e:
            print(f"Database Patch Error: {e}")

    try:
        from sqlalchemy import text
        db.session.execute(text("SELECT can_manage_assignments FROM role LIMIT 1"))
    except Exception:
        db.session.rollback()
        try:
            print("Database Patch: Adding missing columns to role table...")
            db.session.execute(text("ALTER TABLE role ADD COLUMN can_manage_assignments BOOLEAN DEFAULT FALSE"))
            db.session.commit()
            print("Database Patch: Success.")
        except Exception as e:
            print(f"Database Patch Error: {e}")

    try:
        from sqlalchemy import text
        db.session.execute(text("SELECT can_manage_whatsapp FROM role LIMIT 1"))
    except Exception:
        db.session.rollback()
        try:
            print("Database Patch: Adding missing WhatsApp permission to role table...")
            db.session.execute(text("ALTER TABLE role ADD COLUMN can_manage_whatsapp BOOLEAN DEFAULT FALSE"))
            db.session.commit()
            print("Database Patch: Success.")
        except Exception as e:
            print(f"Database Patch Error: {e}")

    # Multi-class permission columns for older production databases
    multi_class_role_cols = [
        'can_access_multi_classroom',
        'can_switch_classroom_context',
        'can_manage_classrooms',
        'can_assign_users_to_classroom',
        'can_move_users_between_classrooms',
        'can_view_all_classrooms',
        'can_manage_students_multi_class',
        'can_manage_schedule_multi_class',
        'can_manage_announcements_multi_class',
        'can_manage_assignments_multi_class',
        'can_manage_gallery_multi_class',
        'can_manage_notifications_multi_class',
        'can_view_classroom_reports',
        'can_export_classroom_data',
    ]
    for col in multi_class_role_cols:
        try:
            db.session.execute(text(f"SELECT {col} FROM role LIMIT 1"))
        except Exception:
            db.session.rollback()
            try:
                print(f"Database Patch: Adding missing role permission {col}...")
                db.session.execute(text(f"ALTER TABLE role ADD COLUMN {col} BOOLEAN DEFAULT FALSE"))
                db.session.commit()
            except Exception as e:
                print(f"Database Patch Error ({col}): {e}")

    try:
        from sqlalchemy import text
        db.session.execute(text("SELECT channel FROM notification_history LIMIT 1"))
    except Exception:
        db.session.rollback()
        try:
            print("Database Patch: Adding channel column to notification_history table...")
            db.session.execute(text("ALTER TABLE notification_history ADD COLUMN channel VARCHAR(20) DEFAULT 'push'"))
            db.session.commit()
            print("Database Patch: Success.")
        except Exception as e:
            print(f"Database Patch Error: {e}")

    try:
        FundPeriod.__table__.create(bind=db.engine, checkfirst=True)
    except Exception as e:
        print(f"Database Patch Error (fund_period): {e}")

    try:
        SchedulePreset.__table__.create(bind=db.engine, checkfirst=True)
        ScheduleTemplate.__table__.create(bind=db.engine, checkfirst=True)
        ScheduleTemplateItem.__table__.create(bind=db.engine, checkfirst=True)
    except Exception as e:
        print(f"Database Patch Error (schedule preset/template): {e}")

    try:
        default_class = ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()
        if default_class:
            users = User.query.all()
            changed = False
            for user in users:
                if not user.classroom_id:
                    if user.student and user.student.classroom_id:
                        user.classroom_id = user.student.classroom_id
                    else:
                        user.classroom_id = default_class.id
                    changed = True
            if changed:
                db.session.commit()
    except Exception as e:
        print(f"Database Patch Error (user classroom backfill): {e}")


    
    # Sync and Harden RBAC
    def sync_roles():
        """Ensures all roles exist and have correct granular permissions."""
        role_data = {
            'Admin': {
                'description': 'Administrator dengan akses penuh.',
                'perms': {
                    'can_manage_students': True, 'can_manage_schedule': True,
                    'can_manage_fund': True, 'can_manage_announcements': True,
                    'can_manage_roles': True, 'can_view_logs': True,
                    'can_export_data': True, 'can_edit_settings': True,
                    'can_manage_gallery': True, 'can_manage_notifications': True, 'can_manage_whatsapp': True,
                    'can_manage_assignments': True, 'can_use_api': True,
                    'can_access_multi_classroom': True, 'can_switch_classroom_context': True,
                    'can_manage_classrooms': True, 'can_assign_users_to_classroom': True,
                    'can_move_users_between_classrooms': True, 'can_view_all_classrooms': True,
                    'can_manage_students_multi_class': True, 'can_manage_schedule_multi_class': True,
                    'can_manage_announcements_multi_class': True, 'can_manage_assignments_multi_class': True,
                    'can_manage_gallery_multi_class': True, 'can_manage_notifications_multi_class': True,
                    'can_view_classroom_reports': True, 'can_export_classroom_data': True
                }
            },
            'Pengurus': {
                'description': 'Pengurus dengan akses manajemen operasional.',
                'perms': {
                    'can_manage_students': True, 'can_manage_schedule': True,
                    'can_manage_fund': True, 'can_manage_announcements': True,
                    'can_manage_roles': False, 'can_view_logs': False,
                    'can_export_data': True, 'can_edit_settings': False,
                    'can_manage_gallery': True, 'can_manage_notifications': True, 'can_manage_whatsapp': True,
                    'can_manage_assignments': True, 'can_use_api': True,
                    'can_access_multi_classroom': True, 'can_switch_classroom_context': True,
                    'can_manage_classrooms': False, 'can_assign_users_to_classroom': True,
                    'can_move_users_between_classrooms': True, 'can_view_all_classrooms': True,
                    'can_manage_students_multi_class': True, 'can_manage_schedule_multi_class': True,
                    'can_manage_announcements_multi_class': True, 'can_manage_assignments_multi_class': True,
                    'can_manage_gallery_multi_class': True, 'can_manage_notifications_multi_class': True,
                    'can_view_classroom_reports': True, 'can_export_classroom_data': True
                }
            },
            'Member': {
                'description': 'Anggota biasa dengan akses portal dasar.',
                'perms': {
                    'can_manage_students': False, 'can_manage_schedule': False,
                    'can_manage_fund': False, 'can_manage_announcements': False,
                    'can_manage_roles': False, 'can_view_logs': False,
                    'can_export_data': False, 'can_edit_settings': False,
                    'can_manage_gallery': False, 'can_manage_notifications': False, 'can_manage_whatsapp': False,
                    'can_manage_assignments': False, 'can_use_api': True,
                    'can_access_multi_classroom': False, 'can_switch_classroom_context': False,
                    'can_manage_classrooms': False, 'can_assign_users_to_classroom': False,
                    'can_move_users_between_classrooms': False, 'can_view_all_classrooms': False,
                    'can_manage_students_multi_class': False, 'can_manage_schedule_multi_class': False,
                    'can_manage_announcements_multi_class': False, 'can_manage_assignments_multi_class': False,
                    'can_manage_gallery_multi_class': False, 'can_manage_notifications_multi_class': False,
                    'can_view_classroom_reports': False, 'can_export_classroom_data': False
                }
            }

        }

        try:
            for name, data in role_data.items():
                role = Role.query.filter_by(name=name).first()
                if not role:
                    role = Role(name=name)
                    db.session.add(role)
                
                role.description = data['description']
                for perm, value in data['perms'].items():
                    setattr(role, perm, value)
            
            db.session.commit()
            print("RBAC Synchronization: Success.")
        except Exception as e:
            db.session.rollback()
            print(f"RBAC Synchronization Error: {e}")

    sync_roles()

    def calculate_user_points_breakdown(user, target_payment=None):
        target_payment = get_fund_target() if target_payment is None else target_payment
        breakdown = {
            'fund_points': 0,
            'gallery_points': 0,
            'arrears_penalty': 0,
            'total_paid': 0,
            'target_payment': target_payment,
            'arrears': 0,
            'published_photos': 0,
        }

        total_points = 0
        if user.student_id:
            total_paid = db.session.query(db.func.sum(BatchFund.amount)).filter(
                BatchFund.student_id == user.student_id,
                BatchFund.type == 'Masuk'
            ).scalar() or 0
            breakdown['total_paid'] = total_paid

            if target_payment > 0:
                paid_ratio = min(total_paid / target_payment, 1)
                breakdown['fund_points'] = int(round(paid_ratio * 500))
            else:
                breakdown['fund_points'] = min(int(total_paid / 1000), 500)

            arrears = max(0, target_payment - total_paid)
            breakdown['arrears'] = arrears
            breakdown['arrears_penalty'] = min(int(arrears / 2000), 100)
            total_points += breakdown['fund_points']
            total_points -= breakdown['arrears_penalty']

        photos_count = GalleryPhoto.query.filter_by(uploaded_by=user.id, status='Published').count()
        breakdown['published_photos'] = photos_count
        first_tier = min(photos_count, 5) * 10
        second_tier = max(min(photos_count - 5, 10), 0) * 5
        breakdown['gallery_points'] = min(first_tier + second_tier, 100)
        total_points += breakdown['gallery_points']

        breakdown['total_points'] = total_points
        return breakdown

    def auto_recalculate_points():
        try:
            print("Auto-recalculating points for all users...")
            target_payment = get_fund_target()
            users = User.query.all()
            
            for u in users:
                breakdown = calculate_user_points_breakdown(u, target_payment=target_payment)
                u.points = breakdown['total_points']
            
            db.session.commit()
            print("Point Recalculation: Success (Including Arrears Penalty).")
        except Exception as e:
            db.session.rollback()
            print(f"Point Recalculation Error: {e}")

    try:
        if FundPeriod.query.count() == 0:
            legacy_start = get_setting_value('fund_start_date', '2024-03-30')
            legacy_end = get_setting_value('fund_end_date', '')
            legacy_rate = int(get_setting_value('fund_daily_rate', '1000') or 1000)
            start_date = datetime.strptime(legacy_start, '%Y-%m-%d').date()
            end_date = datetime.strptime(legacy_end, '%Y-%m-%d').date() if legacy_end else datetime.now().date()
            db.session.add(FundPeriod(
                title='Periode Awal',
                start_date=start_date,
                end_date=end_date,
                daily_rate=legacy_rate,
                is_active=True
            ))
            db.session.commit()
            print("Fund Period Seed: Legacy configuration migrated to first period.")
    except Exception as e:
        db.session.rollback()
        print(f"Fund Period Seed Error: {e}")
            

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.context_processor
def inject_settings():
    try:
        all_s = SystemSetting.query.all()
        settings = {s.key: s.value for s in all_s}
    except Exception:
        settings = {}
        
    # Defaults
    if 'web_title' not in settings: settings['web_title'] = 'Famousbytee.b Portal'
    if 'web_logo' not in settings: settings['web_logo'] = 'monitor'
    if 'web_desc' not in settings: settings['web_desc'] = 'Portal Resmi Kelas Famousbytee.b'
    if 'social_ig' not in settings: settings['social_ig'] = '#'
    if 'social_wa' not in settings: settings['social_wa'] = '#'
    
    # Logic for Branding Assets
    settings['logo_display_path'] = settings.get('web_logo_path')
    settings['favicon_display_url'] = settings.get('favicon_path') or settings.get('favicon_url', '/static/favicon.ico')
    
    return dict(site_settings=settings, datetime=datetime)

@app.errorhandler(404)
def page_not_found(e):
    app.logger.warning(f"404 Not Found: {request.url} (IP: {request.remote_addr})")
    return render_template('errors/404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    import traceback
    error_msg = str(e)
    tb = traceback.format_exc()
    app.logger.error(f"500 Internal Server Error on {request.url} (IP: {request.remote_addr}):\n{error_msg}\n{tb}")
    return render_template('errors/500.html', error=error_msg), 500

@app.errorhandler(Exception)
def handle_exception(e):
    """Catch-all handler for unhandled exceptions."""
    import traceback
    app.logger.error(f"Unhandled Exception: {type(e).__name__}: {e}\n{traceback.format_exc()}")
    # Let Flask's default handler take over for 500 errors
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    return render_template('errors/500.html', error=str(e)), 500

@app.route('/report-error', methods=['POST'])
def report_error():
    err_body = request.form.get('error_details')
    page = request.form.get('page_url')
    # Auto log the error report
    app.logger.error(f"User-reported error on {page}: {err_body[:500] if err_body else 'No details'}")
    log_activity("Error Report", f"User reported error on {page}: {err_body[:200] if err_body else ''}")
    flash('Terima kasih! Laporan galat telah dikirim ke Admin.')
    return redirect(url_for('dashboard'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        id_input = request.form['username'] # Can be username or NIM
        password = request.form.get('password')
        
        # 1. Search in User table (Admin/Staff/Activated Members)
        user = User.query.filter_by(username=id_input).first()
        if user:
            if not password:
                flash('Harap masukkan password untuk akun terdaftar.')
                return redirect(url_for('login'))
            
            from werkzeug.security import check_password_hash
            
            is_valid = False
            # Check if password is hashed (Werkzeug format)
            if user.password.startswith('scrypt:') or user.password.startswith('pbkdf2:'):
                is_valid = check_password_hash(user.password, password)
            else:
                # Fallback for legacy plain-text passwords
                is_valid = (user.password == password)

            if is_valid:
                if user.status != 'Active':
                    flash('Akun Anda dinonaktifkan. Hubungi admin.')
                    return redirect(url_for('login'))
                
                user.last_login = datetime.now()
                db.session.commit()
                login_user(user)
                return redirect(url_for('dashboard'))
            else:
                flash('Password salah.')
                return redirect(url_for('login'))
        
        # 2. Search in Student table for first-time NIM activation
        student = Student.query.filter_by(nim=id_input).first()
        if student:
            # If student found but no User entry, go to activation
            return redirect(url_for('member_activation', nim=id_input))
            
        flash('NIM atau Username tidak terdaftar.')
    return render_template('login.html')

@app.route('/activate/<nim>', methods=['GET', 'POST'])
def member_activation(nim):
    student = Student.query.filter_by(nim=nim).first_or_404()
    # Check if already has user
    existing_user = User.query.filter_by(username=nim).first()
    if existing_user:
        flash('Akun sudah aktif. Silakan login dengan password.')
        return redirect(url_for('login'))
        
    if request.method == 'POST':
        password = request.form['password']
        confirm = request.form['confirm_password']
        
        if password != confirm:
            flash('Password tidak cocok.')
        else:
            # Create user account for member and link it
            member_role = Role.query.filter_by(name='Member').first()
            new_user = User(
                username=nim,
                password=password,
                role_id=member_role.id,
                student_id=student.id,
                full_name=student.full_name,
                status='Active'
            )
            db.session.add(new_user)
            db.session.commit()
            flash('Akun berhasil diaktifasi! Silakan login sekarang.')
            return redirect(url_for('login'))
            
    return render_template('member_activation.html', student=student)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/')
def index():
    announcements = Announcement.query.filter_by(is_public=True).order_by(Announcement.is_pinned.desc(), Announcement.date_posted.desc()).limit(5).all()
    # Fetch top 8 latest public photos for the landing page (ONLY PUBLISHED)
    photos = GalleryPhoto.query.filter_by(is_public=True, status='Published').order_by(GalleryPhoto.created_at.desc()).limit(8).all()
    return render_template('index.html', announcements=announcements, photos=photos)

def log_activity(action, details=None):
    if current_user.is_authenticated:
        enriched_details = f"[{current_user.role.name}] {details}" if details else f"[{current_user.role.name}]"
        log = ActivityLog(user_id=current_user.id, action=action, details=enriched_details)
        db.session.add(log)
        db.session.commit()

def get_fund_target(as_of=None):
    """Calculates cumulative target based on advanced periods, with legacy fallback."""
    today = as_of or datetime.now().date()
    if isinstance(today, datetime):
        today = today.date()

    periods = get_fund_periods()
    if periods:
        total = 0
        for period in periods:
            effective_end = min(today, period.end_date)
            if effective_end < period.start_date:
                continue
            total += _count_weekdays_between(period.start_date, effective_end) * (period.daily_rate or 0)
        return total

    try:
        start_setting = SystemSetting.query.filter_by(key='fund_start_date').first()
        end_setting = SystemSetting.query.filter_by(key='fund_end_date').first()
        rate_setting = SystemSetting.query.filter_by(key='fund_daily_rate').first()

        start_date = datetime.strptime(start_setting.value, '%Y-%m-%d').date() if start_setting else datetime(2024, 3, 30).date()
        end_date = datetime.strptime(end_setting.value, '%Y-%m-%d').date() if end_setting and end_setting.value else None
        daily_rate = int(rate_setting.value) if rate_setting else 1000
    except Exception:
        start_date = datetime(2024, 3, 30).date()
        end_date = None
        daily_rate = 1000

    target_until = min(today, end_date) if end_date else today
    if target_until < start_date:
        return 0
    return _count_weekdays_between(start_date, target_until) * daily_rate

with app.app_context():
    auto_recalculate_points()


@app.route('/announcements/manage')
@login_required
def view_announcements():
    if not current_user.role.can_manage_announcements:
        flash('Akses ditolak.')
        return redirect(url_for('dashboard'))
    logs = ActivityLog.query.order_by(ActivityLog.timestamp.desc()).limit(100).all()
    return render_template('logs.html', logs=logs)

@app.route('/logs')
@login_required
def view_logs():
    if not current_user.role.can_manage_roles:
        flash('Akses ditolak.')
        return redirect(url_for('dashboard'))
    logs = ActivityLog.query.order_by(ActivityLog.timestamp.desc()).limit(100).all()
    return render_template('logs.html', logs=logs)

@app.route('/dashboard')
@login_required
def dashboard():
    """
    Dashboard Unified: Menampilkan info personal member (jika terhubung ke data Mahasiswa)
    serta statistik manajemen bagi Admin/Pengurus.
    """
    # 1. Data Dasar: Pengumuman Terbaru
    active_classroom = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    if not active_classroom:
        active_classroom = ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()

    recent_announcements_query = Announcement.query
    if active_classroom:
        recent_announcements_query = recent_announcements_query.filter(
            (Announcement.classroom_id == active_classroom.id) | (Announcement.classroom_id.is_(None))
        )
    recent_announcements = recent_announcements_query.order_by(Announcement.is_pinned.desc(), Announcement.date_posted.desc()).limit(5).all()
    
    # 2. Ambil Info Personal Member (jika User terhubung ke Student)
    member_info = None
    if current_user.student_id:
        student = current_user.student
        target_payment = get_fund_target()

        
        total_paid = db.session.query(db.func.sum(BatchFund.amount)).filter(
            BatchFund.student_id == student.id, 
            BatchFund.type == 'Masuk'
        ).scalar() or 0
        
        member_info = {
            'nim': student.nim,
            'name': student.full_name,
            'paid': total_paid,
            'target': target_payment,
            'arrears': max(0, target_payment - total_paid)
        }

    # 3. GALLERY SNEAK PEEK (Latest 4 Published)
    gallery_preview_query = GalleryPhoto.query.filter_by(status='Published')
    if active_classroom:
        gallery_preview_query = gallery_preview_query.filter(
            (GalleryPhoto.classroom_id == active_classroom.id) | (GalleryPhoto.classroom_id.is_(None))
        )
    gallery_preview = gallery_preview_query.order_by(GalleryPhoto.created_at.desc()).limit(4).all()

    # 4. NEXT CLASS COUNTDOWN
    # Map Indonesian days to weekday numbers
    day_map = {'Senin': 0, 'Selasa': 1, 'Rabu': 2, 'Kamis': 3, 'Jumat': 4, 'Sabtu': 5, 'Minggu': 6}
    now = datetime.now()
    curr_day_num = now.weekday()
    curr_time_str = now.strftime('%H:%M')
    
    schedules = Schedule.query.filter_by(classroom_id=active_classroom.id).all() if active_classroom else []
    next_class = None
    min_diff = float('inf')
    
    for s in schedules:
        sched_day_num = day_map.get(s.day)
        if sched_day_num is None: continue
        
        # Calculate time diff in minutes
        # We simplify: only look at today's remaining classes or tomorrow's first class if today is done
        if sched_day_num == curr_day_num:
            if s.time_start > curr_time_str:
                h_s, m_s = map(int, s.time_start.split(':'))
                h_c, m_c = now.hour, now.minute
                diff = (h_s * 60 + m_s) - (h_c * 60 + m_c)
                if diff < min_diff:
                    min_diff = diff
                    next_class = s

        # For simplicity, we only show "Today's" next class for now as per user requested "Starting in X hours"
    
    # 5. UNREAD ANNOUNCEMENTS
    read_ids = [r.announcement_id for r in AnnouncementRead.query.filter_by(user_id=current_user.id).all()]

    # 6. Data Statistik Admin/Pengurus (jika punya izin)
    admin_stats = None
    recent_students = []
    if current_user.role.can_manage_students or current_user.role.can_manage_fund or current_user.role.can_manage_announcements:
        total_mhs = Student.query.count()
        total_in = db.session.query(db.func.sum(BatchFund.amount)).filter(BatchFund.type == 'Masuk').scalar() or 0
        total_out = db.session.query(db.func.sum(BatchFund.amount)).filter(BatchFund.type == 'Keluar').scalar() or 0
        total_ann = Announcement.query.count()
        
        admin_stats = {
            'total_members': total_mhs,
            'balance': total_in - total_out,
            'total_announcements': total_ann
        }
        recent_students = Student.query.order_by(Student.id.desc()).limit(5).all()

    return render_template('dashboard.html', 
                         recent_announcements=recent_announcements,
                         member_info=member_info,
                         admin_stats=admin_stats,
                         recent_students=recent_students,
                         gallery_preview=gallery_preview,
                         next_class=next_class,
                         time_diff=min_diff if min_diff != float('inf') else None,
                         read_ids=read_ids)

@app.route('/announcements/read/<int:id>')
@login_required
def mark_announcement_read(id):
    try:
        # Check if already read
        exists = AnnouncementRead.query.filter_by(announcement_id=id, user_id=current_user.id).first()
        if not exists:
            read = AnnouncementRead(announcement_id=id, user_id=current_user.id)
            db.session.add(read)
            db.session.commit()
        return {'status': 'success'}
    except:
        return {'status': 'error'}, 500

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        # Limit what can be changed
        current_user.email = request.form.get('email', current_user.email)
        current_user.bio = request.form.get('bio', '')
        current_user.whatsapp = request.form.get('whatsapp', '')
        
        new_pass = request.form.get('new_password')
        if new_pass:
            current_user.password = new_pass
            
        db.session.commit()
        log_activity("Update Profil")
        flash('Profil berhasil diperbarui!')
        return redirect(url_for('profile'))
        
    return render_template('profile.html')

@app.route('/members', methods=['GET', 'POST'])
@login_required
def manage_members():
    if not current_user.role.can_manage_students:
        flash('Akses ditolak.')
        return redirect(url_for('dashboard'))

    all_classrooms = ClassRoom.query.order_by(ClassRoom.name.asc()).all()
    active_classroom = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    if not active_classroom:
        active_classroom = ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()
    
    if request.method == 'POST':
        classroom = active_classroom
        if current_user.role.can_manage_roles:
            classroom_id = request.form.get('classroom_id')
            if classroom_id:
                classroom = ClassRoom.query.get(int(classroom_id)) or classroom
        new_m = Student(
            nim=request.form['nim'], 
            full_name=request.form['full_name'], 
            status=request.form['status'], 
            classroom_id=classroom.id if classroom else None
        )
        db.session.add(new_m)
        db.session.commit()
        log_activity("Tambah Member", f"NIM: {new_m.nim}, Nama: {new_m.full_name}")
        return redirect(url_for('manage_members'))
    
    if current_user.role.can_manage_roles and request.args.get('classroom_id'):
        classroom = ClassRoom.query.get(int(request.args.get('classroom_id')))
        if classroom:
            active_classroom = classroom

    members_query = Student.query
    if active_classroom:
        members_query = members_query.filter_by(classroom_id=active_classroom.id)
    members = members_query.order_by(Student.full_name.asc()).all()
    return render_template('members.html', students=members, classrooms=all_classrooms, active_classroom=active_classroom)

@app.route('/members/bulk', methods=['POST'])
@login_required
def bulk_add_members():
    if not current_user.role.can_manage_students: return redirect(url_for('dashboard'))
    data = request.form.get('bulk_data')
    class_fb = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    if current_user.role.can_manage_roles:
        classroom_id = request.form.get('classroom_id')
        if classroom_id:
            class_fb = ClassRoom.query.get(int(classroom_id)) or class_fb
    if not class_fb:
        class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()
    
    added_count = 0
    lines = data.strip().split('\n')
    for line in lines:
        if ';' in line: parts = line.split(';')
        elif ',' in line: parts = line.split(',')
        else: continue
        
        if len(parts) >= 2:
            nim = parts[0].strip()
            name = parts[1].strip()
            status = parts[2].strip() if len(parts) >= 3 else 'Aktif'
            
            # Check if exists
            if not Student.query.filter_by(nim=nim).first():
                new_s = Student(nim=nim, full_name=name, status=status, classroom_id=class_fb.id)
                db.session.add(new_s)
                added_count += 1
                
    db.session.commit()
    log_activity("Bulk Add Member", f"Berhasil menambah {added_count} mahasiswa.")
    flash(f'Berhasil menambah {added_count} mahasiswa baru.')
    return redirect(url_for('manage_members'))

@app.route('/members/edit/<int:id>', methods=['POST'])
@login_required
def edit_member(id):
    if not current_user.role.can_manage_students: return redirect(url_for('dashboard'))
    m = Student.query.get_or_404(id)
    old_name = m.full_name
    if current_user.role.can_manage_roles:
        classroom_id = request.form.get('classroom_id')
        if classroom_id:
            classroom = ClassRoom.query.get(int(classroom_id))
            if classroom:
                m.classroom_id = classroom.id
    m.nim = request.form['nim']
    m.full_name = request.form['full_name']
    m.status = request.form['status']
    db.session.commit()
    log_activity("Edit Member", f"Mengubah data {old_name} (ID: {id})")
    flash(f'Data {m.full_name} diperbarui.')
    return redirect(url_for('manage_members'))

@app.route('/members/delete/<int:id>')
@login_required
def delete_member(id):
    if not current_user.role.can_manage_students: return redirect(url_for('dashboard'))
    m = Student.query.get_or_404(id)
    log_activity("Hapus Member", f"NIM: {m.nim}, Nama: {m.full_name}")
    db.session.delete(m)
    db.session.commit()
    return redirect(url_for('manage_members'))

@app.route('/schedule', methods=['GET', 'POST'])
@login_required
def manage_schedule():
    all_classrooms = ClassRoom.query.order_by(ClassRoom.name.asc()).all()
    active_classroom = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    if not active_classroom:
        active_classroom = ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()
    if request.method == 'POST' and not current_user.role.can_manage_schedule:
        flash('Akses ditolak.')
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        classroom = active_classroom
        if current_user.role.can_manage_roles:
            classroom_id = request.form.get('classroom_id')
            if classroom_id:
                classroom = ClassRoom.query.get(int(classroom_id)) or classroom
        # Single Add logic (remains as is)
        sched = Schedule(
            classroom_id=classroom.id if classroom else None, 
            day=request.form['day'], 
            time_start=request.form['time_start'], 
            time_end=request.form['time_end'], 
            subject=request.form['subject'], 
            lecturer=request.form['lecturer'], 
            room=request.form['room']
        )
        db.session.add(sched)
        db.session.commit()
        log_activity("Tambah Jadwal", f"Matkul: {sched.subject}")
        return redirect(url_for('manage_schedule'))
    
    # 1. Get All Schedules
    schedules = Schedule.query.filter_by(classroom_id=active_classroom.id).order_by(Schedule.day.asc(), Schedule.time_start.asc()).all() if active_classroom else []
    schedule_presets = SchedulePreset.query.filter(
        (SchedulePreset.classroom_id == active_classroom.id) | (SchedulePreset.classroom_id.is_(None))
    ).order_by(SchedulePreset.name.asc()).all()
    
    # 2. Logic to Find 'Active' and 'Next' Subject
    now = datetime.now()
    current_day_str = now.strftime('%A')
    # Map English day to Indonesian to match DB
    day_map = {'Monday': 'Senin', 'Tuesday': 'Selasa', 'Wednesday': 'Rabu', 'Thursday': 'Kamis', 'Friday': 'Jumat', 'Saturday': 'Sabtu', 'Sunday': 'Minggu'}
    today_indo = day_map.get(current_day_str, 'Minggu')
    current_time_str = now.strftime('%H:%M')
    
    active_subject = None
    next_subject = None
    
    today_schedules = sorted([s for s in schedules if s.day == today_indo], key=lambda x: x.time_start)
    
    for s in today_schedules:
        if s.time_start <= current_time_str <= s.time_end:
            active_subject = s
        elif s.time_start > current_time_str and not next_subject:
            next_subject = s
            
    return render_template('schedule.html', 
                         schedules=schedules, 
                         active_subject=active_subject, 
                         next_subject=next_subject,
                         today_indo=today_indo,
                         schedule_presets=schedule_presets,
                         assignments=Assignment.query.order_by(Assignment.deadline.asc()).all(),
                         classrooms=all_classrooms,
                         active_classroom=active_classroom)

@app.route('/schedule/presets', methods=['POST'])
@login_required
def create_schedule_preset():
    if not current_user.role.can_manage_schedule:
        return redirect(url_for('dashboard'))

    class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first()
    name = (request.form.get('name') or '').strip()
    subject = (request.form.get('subject') or '').strip()
    if not name or not subject:
        flash('Nama preset dan mata kuliah wajib diisi.')
        return redirect(url_for('manage_schedule') + '#presets')

    preset = SchedulePreset(
        classroom_id=class_fb.id if class_fb else None,
        name=name,
        subject=subject,
        lecturer=(request.form.get('lecturer') or '').strip(),
        room=(request.form.get('room') or '').strip(),
        created_by=current_user.id
    )
    db.session.add(preset)
    db.session.commit()
    log_activity("Tambah Preset Jadwal", f"Preset: {preset.name}, Matkul: {preset.subject}")
    flash('Preset jadwal berhasil dibuat.')
    return redirect(url_for('manage_schedule') + '#presets')

@app.route('/schedule/presets/<int:preset_id>', methods=['POST'])
@login_required
def update_schedule_preset(preset_id):
    if not current_user.role.can_manage_schedule:
        return redirect(url_for('dashboard'))

    class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first()
    preset = SchedulePreset.query.get_or_404(preset_id)
    if class_fb and preset.classroom_id not in (class_fb.id, None):
        return redirect(url_for('manage_schedule') + '#presets')
    name = (request.form.get('name') or '').strip()
    subject = (request.form.get('subject') or '').strip()
    if not name or not subject:
        flash('Nama preset dan mata kuliah wajib diisi.')
        return redirect(url_for('manage_schedule') + '#presets')

    preset.name = name
    preset.subject = subject
    preset.lecturer = (request.form.get('lecturer') or '').strip()
    preset.room = (request.form.get('room') or '').strip()
    db.session.commit()
    log_activity("Edit Preset Jadwal", f"Preset: {preset.name}")
    flash('Preset jadwal berhasil diperbarui.')
    return redirect(url_for('manage_schedule') + '#presets')

@app.route('/schedule/presets/<int:preset_id>/delete', methods=['POST'])
@login_required
def delete_schedule_preset(preset_id):
    if not current_user.role.can_manage_schedule:
        return redirect(url_for('dashboard'))

    class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first()
    preset = SchedulePreset.query.get_or_404(preset_id)
    if class_fb and preset.classroom_id not in (class_fb.id, None):
        return redirect(url_for('manage_schedule') + '#presets')
    name = preset.name
    db.session.delete(preset)
    db.session.commit()
    log_activity("Hapus Preset Jadwal", f"Preset: {name}")
    flash('Preset jadwal berhasil dihapus.')
    return redirect(url_for('manage_schedule') + '#presets')

def _create_schedule_from_template_item(classroom_id, item):
    return Schedule(
        classroom_id=classroom_id,
        day=item.day,
        time_start=item.time_start,
        time_end=item.time_end,
        subject=item.subject,
        lecturer=item.lecturer or '-',
        room=item.room or '-'
    )

def _next_template_sort_order(template_id):
    last_item = ScheduleTemplateItem.query.filter_by(template_id=template_id).order_by(ScheduleTemplateItem.sort_order.desc()).first()
    return (last_item.sort_order + 1) if last_item else 1

@app.route('/schedule/templates', methods=['POST'])
@login_required
def create_schedule_template():
    if not current_user.role.can_manage_schedule:
        return redirect(url_for('dashboard'))

    class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first()
    template = ScheduleTemplate(
        classroom_id=class_fb.id,
        name=(request.form.get('name') or 'Template Jadwal Baru').strip(),
        description=(request.form.get('description') or '').strip(),
        created_by=current_user.id
    )
    db.session.add(template)
    db.session.commit()
    log_activity("Tambah Template Jadwal", f"Template: {template.name}")
    flash('Template jadwal berhasil dibuat.')
    return redirect(url_for('manage_schedule') + '#templates')

@app.route('/schedule/templates/from-current', methods=['POST'])
@login_required
def create_schedule_template_from_current():
    if not current_user.role.can_manage_schedule:
        return redirect(url_for('dashboard'))

    class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first()
    schedules = Schedule.query.filter_by(classroom_id=class_fb.id).order_by(Schedule.day.asc(), Schedule.time_start.asc()).all()
    if not schedules:
        flash('Belum ada jadwal aktif untuk dijadikan template.')
        return redirect(url_for('manage_schedule') + '#templates')

    template = ScheduleTemplate(
        classroom_id=class_fb.id,
        name=(request.form.get('name') or f"Template dari Jadwal {datetime.now().strftime('%d %b %Y')}").strip(),
        description=(request.form.get('description') or 'Dibuat dari jadwal aktif.').strip(),
        created_by=current_user.id
    )
    db.session.add(template)
    db.session.flush()

    for index, schedule in enumerate(schedules, start=1):
        db.session.add(ScheduleTemplateItem(
            template_id=template.id,
            day=schedule.day,
            time_start=schedule.time_start,
            time_end=schedule.time_end,
            subject=schedule.subject,
            lecturer=schedule.lecturer,
            room=schedule.room,
            sort_order=index
        ))

    db.session.commit()
    log_activity("Buat Template Dari Jadwal", f"Template: {template.name}, Item: {len(schedules)}")
    flash(f'Template dibuat dari {len(schedules)} jadwal aktif.')
    return redirect(url_for('manage_schedule') + '#templates')

@app.route('/schedule/templates/<int:template_id>/items', methods=['POST'])
@login_required
def add_schedule_template_item(template_id):
    if not current_user.role.can_manage_schedule:
        return redirect(url_for('dashboard'))

    template = ScheduleTemplate.query.get_or_404(template_id)
    item = ScheduleTemplateItem(
        template_id=template.id,
        day=request.form['day'],
        time_start=request.form['time_start'],
        time_end=request.form['time_end'],
        subject=request.form['subject'],
        lecturer=request.form.get('lecturer') or '-',
        room=request.form.get('room') or '-',
        sort_order=_next_template_sort_order(template.id)
    )
    db.session.add(item)
    db.session.commit()
    log_activity("Tambah Item Template Jadwal", f"Template: {template.name}, Matkul: {item.subject}")
    flash('Item template berhasil ditambahkan.')
    return redirect(url_for('manage_schedule') + '#templates')

@app.route('/schedule/templates/items/<int:item_id>/delete', methods=['POST'])
@login_required
def delete_schedule_template_item(item_id):
    if not current_user.role.can_manage_schedule:
        return redirect(url_for('dashboard'))

    item = ScheduleTemplateItem.query.get_or_404(item_id)
    template_name = item.template.name
    db.session.delete(item)
    db.session.commit()
    log_activity("Hapus Item Template Jadwal", f"Template: {template_name}")
    flash('Item template berhasil dihapus.')
    return redirect(url_for('manage_schedule') + '#templates')

@app.route('/schedule/templates/<int:template_id>/duplicate', methods=['POST'])
@login_required
def duplicate_schedule_template(template_id):
    if not current_user.role.can_manage_schedule:
        return redirect(url_for('dashboard'))

    original = ScheduleTemplate.query.get_or_404(template_id)
    duplicate = ScheduleTemplate(
        classroom_id=original.classroom_id,
        name=(request.form.get('name') or f"Salinan {original.name}").strip(),
        description=original.description,
        created_by=current_user.id
    )
    db.session.add(duplicate)
    db.session.flush()
    for item in original.items:
        db.session.add(ScheduleTemplateItem(
            template_id=duplicate.id,
            day=item.day,
            time_start=item.time_start,
            time_end=item.time_end,
            subject=item.subject,
            lecturer=item.lecturer,
            room=item.room,
            sort_order=item.sort_order
        ))
    db.session.commit()
    log_activity("Duplikasi Template Jadwal", f"Dari: {original.name}, Ke: {duplicate.name}")
    flash('Template berhasil diduplikasi.')
    return redirect(url_for('manage_schedule') + '#templates')

@app.route('/schedule/templates/<int:template_id>/apply', methods=['POST'])
@login_required
def apply_schedule_template(template_id):
    if not current_user.role.can_manage_schedule:
        return redirect(url_for('dashboard'))

    class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first()
    template = ScheduleTemplate.query.get_or_404(template_id)
    if not template.items:
        flash('Template belum memiliki item jadwal.')
        return redirect(url_for('manage_schedule') + '#templates')

    if request.form.get('replace_existing') == 'on':
        Schedule.query.filter_by(classroom_id=class_fb.id).delete(synchronize_session=False)

    for item in template.items:
        db.session.add(_create_schedule_from_template_item(class_fb.id, item))

    db.session.commit()
    log_activity("Terapkan Template Jadwal", f"Template: {template.name}, Item: {len(template.items)}")
    flash(f'Template "{template.name}" berhasil diterapkan ke jadwal aktif.')
    return redirect(url_for('manage_schedule'))

@app.route('/schedule/templates/<int:template_id>/delete', methods=['POST'])
@login_required
def delete_schedule_template(template_id):
    if not current_user.role.can_manage_schedule:
        return redirect(url_for('dashboard'))

    template = ScheduleTemplate.query.get_or_404(template_id)
    name = template.name
    db.session.delete(template)
    db.session.commit()
    log_activity("Hapus Template Jadwal", f"Template: {name}")
    flash('Template jadwal berhasil dihapus.')
    return redirect(url_for('manage_schedule') + '#templates')

@app.route('/assignments', methods=['GET', 'POST'])
@login_required
def manage_assignments():
    active_classroom = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    if not active_classroom:
        active_classroom = ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()
    if request.method == 'POST' and not current_user.role.can_manage_assignments:
        flash('Akses ditolak.')
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        classroom = active_classroom
        if current_user.role.can_manage_roles:
            classroom_id = request.form.get('classroom_id')
            if classroom_id:
                classroom = ClassRoom.query.get(int(classroom_id)) or classroom
        a = Assignment(
            title=request.form['title'],
            subject=request.form['subject'],
            deadline=datetime.strptime(request.form['deadline'], '%Y-%m-%dT%H:%M'),
            description=request.form.get('description', ''),
            classroom_id=classroom.id if classroom else None
        )
        db.session.add(a)
        db.session.commit()
        
        send_multichannel_notification(
            "Tugas Baru!",
            f"Tugas {a.subject}: {a.title}. Deadline: {a.deadline.strftime('%d %b %H:%M')}",
            sender_id=current_user.id,
            allow_whatsapp=True,
            whatsapp_text=f"Tugas baru\nMata kuliah: {a.subject}\nJudul: {a.title}\nDeadline: {a.deadline.strftime('%d %b %Y %H:%M')}"
        )
        log_activity("Tambah Tugas", f"Judul: {a.title}")
        flash('Tugas berhasil ditambahkan!')
        return redirect(url_for('manage_assignments'))
    
    assignments_query = Assignment.query
    if active_classroom:
        assignments_query = assignments_query.filter(
            (Assignment.classroom_id == active_classroom.id) | (Assignment.classroom_id.is_(None))
        )
    assignments = assignments_query.order_by(Assignment.deadline.asc()).all()
    classrooms = ClassRoom.query.order_by(ClassRoom.name.asc()).all()
    return render_template('assignments.html', assignments=assignments, classrooms=classrooms, active_classroom=active_classroom)

@app.route('/assignments/delete/<int:id>')
@login_required
def delete_assignment(id):
    if not current_user.role.can_manage_assignments: return redirect(url_for('dashboard'))
    a = Assignment.query.get_or_404(id)
    log_activity("Hapus Tugas", f"Judul: {a.title}")
    db.session.delete(a)
    db.session.commit()
    flash('Tugas berhasil dihapus.')
    return redirect(url_for('manage_assignments'))


@app.route('/schedule/batch', methods=['POST'])
@login_required
def schedule_batch():
    if not current_user.role.can_manage_schedule: return redirect(url_for('dashboard'))
    class_fb = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    if current_user.role.can_manage_roles:
        classroom_id = request.form.get('classroom_id')
        if classroom_id:
            class_fb = ClassRoom.query.get(int(classroom_id)) or class_fb
    if not class_fb:
        class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()
    
    if 'file' not in request.files: return redirect(url_for('manage_schedule'))
    file = request.files['file']
    if file.filename == '': return redirect(url_for('manage_schedule'))
    
    if file and file.filename.endswith('.csv'):
        stream = StringIO(file.stream.read().decode("UTF8"), newline=None)
        csv_input = csv.DictReader(stream)
        count_new = 0
        count_upd = 0
        
        for row in csv_input:
            sid = row.get('id')
            if sid and sid.strip().isdigit():
                existing = Schedule.query.get(int(sid))
                if existing:
                    existing.day = row['day']
                    existing.time_start = row['time_start']
                    existing.time_end = row['time_end']
                    existing.subject = row['subject']
                    existing.lecturer = row.get('lecturer', '-')
                    existing.room = row.get('room', '-')
                    count_upd += 1
                    continue
            
            ns = Schedule(
                classroom_id=class_fb.id,
                day=row['day'],
                time_start=row['time_start'],
                time_end=row['time_end'],
                subject=row['subject'],
                lecturer=row.get('lecturer', '-'),
                room=row.get('room', '-')
            )
            db.session.add(ns)
            count_new += 1
            
        db.session.commit()
        log_activity("Batch Update Jadwal", f"Baru: {count_new}, Update: {count_upd}")
        flash(f'Sukses: {count_new} jadwal baru ditambahkan, {count_upd} jadwal diperbarui.')
        
    return redirect(url_for('manage_schedule'))

@app.route('/schedule/bulk', methods=['POST'])
@login_required
def bulk_add_schedule():
    if not current_user.role.can_manage_schedule: return redirect(url_for('dashboard'))
    data = request.form.get('bulk_data')
    class_fb = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    if current_user.role.can_manage_roles:
        classroom_id = request.form.get('classroom_id')
        if classroom_id:
            class_fb = ClassRoom.query.get(int(classroom_id)) or class_fb
    if not class_fb:
        class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()
    
    added_count = 0
    lines = data.strip().split('\n')
    for line in lines:
        parts = []
        if ';' in line: parts = [p.strip() for p in line.split(';')]
        elif '\t' in line: parts = [p.strip() for p in line.split('\t')]
        else: continue
        
        if len(parts) >= 5: # day, start, end, subject, lecturer, room
            s = Schedule(
                classroom_id=class_fb.id,
                day=parts[0],
                time_start=parts[1],
                time_end=parts[2],
                subject=parts[3],
                lecturer=parts[4],
                room=parts[5] if len(parts) > 5 else '-'
            )
            db.session.add(s)
            added_count += 1
            
    db.session.commit()
    log_activity("Bulk Tambah Jadwal", f"Ditambahkan: {added_count} jadwal")
    flash(f'{added_count} jadwal berhasil ditambahkan secara kolektif.')
    return redirect(url_for('manage_schedule'))

@app.route('/schedule/edit/<int:id>', methods=['POST'])
@login_required
def edit_schedule(id):
    if not current_user.role.can_manage_schedule: return redirect(url_for('dashboard'))
    s = Schedule.query.get_or_404(id)
    if current_user.role.can_manage_roles:
        classroom_id = request.form.get('classroom_id')
        if classroom_id:
            classroom = ClassRoom.query.get(int(classroom_id))
            if classroom:
                s.classroom_id = classroom.id
    s.day = request.form['day']
    s.time_start = request.form['time_start']
    s.time_end = request.form['time_end']
    s.subject = request.form['subject']
    s.lecturer = request.form['lecturer']
    s.room = request.form['room']
    db.session.commit()
    # Check if WhatsApp notification is enabled for edit (default: false to avoid spam)
    should_notify = get_setting_value('schedule_notify_on_edit', 'false') == 'true'
    # Only send push notification, WhatsApp via daily summary
    send_multichannel_notification(
        "Jadwal Diubah!",
        f"Jadwal {s.subject} telah diperbarui oleh pengurus.",
        sender_id=current_user.id,
        allow_whatsapp=False,  # Don't send per-subject WhatsApp
    )
    log_activity("Edit Jadwal", f"Matkul: {s.subject}")
    return redirect(url_for('manage_schedule'))

@app.route('/schedule/delete/<int:id>')
@login_required
def delete_schedule(id):
    if not current_user.role.can_manage_schedule: return redirect(url_for('dashboard'))
    s = Schedule.query.get_or_404(id)
    subject_name = s.subject
    log_activity("Hapus Jadwal", f"Matkul: {s.subject}")
    db.session.delete(s)
    db.session.commit()
    # Only send push notification, WhatsApp via daily summary
    send_multichannel_notification(
        "Jadwal Dihapus",
        f"Jadwal {subject_name} telah dihapus dari sistem.",
        sender_id=current_user.id,
        allow_whatsapp=False,  # Don't send per-subject WhatsApp
    )
    return redirect(url_for('manage_schedule'))

# Suggestion #15: Download Template CSV with Current Data
@app.route('/schedule/template')
@login_required
def download_schedule_template():
    if not current_user.role.can_manage_schedule: return redirect(url_for('dashboard'))
    class_fb = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    if not class_fb:
        class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()
    schedules = Schedule.query.filter_by(classroom_id=class_fb.id).all()
    
    # Headers dengan ID untuk pendeteksian UPDATE
    headers = ['id', 'day', 'time_start', 'time_end', 'subject', 'lecturer', 'room']
    
    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(headers)
    
    if schedules:
        for s in schedules:
            cw.writerow([s.id, s.day, s.time_start, s.time_end, s.subject, s.lecturer, s.room])
    else:
        # Contoh penginputan jika data kosong (ID dikosongkan)
        cw.writerow(['', 'Senin', '08:00', '10:00', 'Pemrograman Web (CONTOH)', 'Dr. Jhon Doe', 'Lab 01'])
        cw.writerow(['', 'Selasa', '13:00', '15:30', 'Basis Data (CONTOH)', 'Alice, M.Kom', 'Lab 02'])
    
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=master_jadwal_famousbytee.csv"
    output.headers["Content-type"] = "text/csv"
    return output

@app.route('/announcements', methods=['GET', 'POST'])
@login_required
def manage_announcements():
    if request.method == 'POST' and not current_user.role.can_manage_announcements:
        flash('Akses ditolak.')
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        active_classroom = current_user.classroom or (current_user.student.classroom if current_user.student else None)
        if not active_classroom:
            active_classroom = ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()
        ann = Announcement(
            classroom_id=active_classroom.id if active_classroom else None,
            title=request.form['title'], 
            content=request.form['content'],
            category=request.form['category'],
            is_pinned='is_pinned' in request.form,
            is_public='is_public' in request.form
        )
        db.session.add(ann)
        db.session.commit()
        
        # Always notify for all announcements
        title_prefix = "Pengumuman Baru!" if ann.category != 'Penting' else "PENTING: Pengumuman!"
        send_push(title_prefix, ann.title, sender_id=current_user.id)

        log_activity("Tambah Pengumuman", f"Judul: {ann.title} (Publik: {ann.is_public})")
        return redirect(url_for('manage_announcements'))
    
    active_classroom = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    if not active_classroom:
        active_classroom = ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()
    announcements_query = Announcement.query
    if active_classroom:
        announcements_query = announcements_query.filter(
            (Announcement.classroom_id == active_classroom.id) | (Announcement.classroom_id.is_(None))
        )
    announcements = announcements_query.order_by(Announcement.is_pinned.desc(), Announcement.date_posted.desc()).all()
    classrooms = ClassRoom.query.order_by(ClassRoom.name.asc()).all()
    return render_template('announcements.html', announcements=announcements, classrooms=classrooms, active_classroom=active_classroom)

@app.route('/announcements/edit/<int:id>', methods=['POST'])
@login_required
def edit_announcement(id):
    if not current_user.role.can_manage_announcements: return redirect(url_for('dashboard'))
    ann = Announcement.query.get_or_404(id)
    active_classroom = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    if active_classroom and ann.classroom_id not in (active_classroom.id, None):
        flash('Akses ditolak.')
        return redirect(url_for('manage_announcements'))
    ann.title = request.form['title']
    ann.content = request.form['content']
    ann.category = request.form['category']
    ann.is_pinned = 'is_pinned' in request.form
    ann.is_public = 'is_public' in request.form
    db.session.commit()
    log_activity("Edit Pengumuman", f"Judul: {ann.title}")
    flash('Pengumuman diperbarui.')
    return redirect(url_for('manage_announcements'))

@app.route('/announcements/delete/<int:id>')
@login_required
def delete_announcement(id):
    if not current_user.role.can_manage_announcements: return redirect(url_for('dashboard'))
    ann = Announcement.query.get_or_404(id)
    active_classroom = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    if active_classroom and ann.classroom_id not in (active_classroom.id, None):
        flash('Akses ditolak.')
        return redirect(url_for('manage_announcements'))
    log_activity("Hapus Pengumuman", f"Judul: {ann.title}")
    db.session.delete(ann)
    db.session.commit()
    return redirect(url_for('manage_announcements'))

@app.route('/fund', methods=['GET', 'POST'])
@login_required
def manage_fund():
    class_fb = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    if not class_fb:
        class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()
    students = Student.query.filter_by(classroom_id=class_fb.id).all()
    
    if request.method == 'POST':
        if not current_user.role.can_manage_fund:
            flash('Akses ditolak.')
            return redirect(url_for('dashboard'))
            
        # Support JSON for API
        if request.is_json:
            data = request.get_json()
            description = data.get('desc')
            amount = float(data.get('amount', 0))
            type_val = data.get('type')
            category = data.get('category')
            date_val = datetime.strptime(data.get('date'), '%Y-%m-%d') if data.get('date') else datetime.now()
            student_id_val = data.get('student_id')
            tags = data.get('tags', '')
            evidence_note = data.get('note', '')
        else:
            description = request.form['desc']
            amount = float(request.form['amount'])
            type_val = request.form['type']
            category = request.form['category']
            date_val = datetime.strptime(request.form['date'], '%Y-%m-%d')
            student_id_val = request.form.get('student_id')
            tags = request.form.get('tags', '').strip()
            evidence_note = request.form.get('note', '')

        if tags and not tags.startswith('#'): tags = '#' + tags
        
        fund = BatchFund(
            description=description, 
            amount=amount, 
            type=type_val, 
            category=category,
            evidence_note=evidence_note,
            recorded_by=current_user.username,
            date=date_val,
            student_id=int(student_id_val) if student_id_val and str(student_id_val).lower() != 'none' else None,
            tags=tags
        )
        db.session.add(fund)
        
        # Notify student and award points if it's a payment
        if fund.type == 'Masuk' and fund.student_id:
            s = Student.query.get(fund.student_id)
            send_push("Pembayaran Diterima!", f"Dana {fund.category} sebesar Rp {fund.amount:,.0f} telah dicatat.", user_id=s.user.id if s and s.user else None)
        
        # Suggestion #1: Auto-Announcement on Keluar
        if fund.type == 'Keluar':
            ann = Announcement(
                title=f"[PENGELUARAN] {fund.description}",
                content=f"Diberitahukan bahwa dana kas sebesar Rp {fund.amount:,.0f} telah digunakan untuk: {fund.description}. Kategori: {fund.category}. Dicatat oleh: {fund.recorded_by}.",
                category='Penting'
            )
            db.session.add(ann)
            
        db.session.commit()
        auto_recalculate_points()
        flash('Data kas berhasil ditambahkan!')
        return redirect(url_for('manage_fund'))
    
    # Suggestion #17: Advanced Filtering
    query = BatchFund.query
    start_filter = request.args.get('start_date')
    end_filter = request.args.get('end_date')
    tag_filter = request.args.get('tag')
    
    if start_filter:
        query = query.filter(BatchFund.date >= datetime.strptime(start_filter, '%Y-%m-%d'))
    if end_filter:
        query = query.filter(BatchFund.date <= datetime.strptime(end_filter, '%Y-%m-%d'))
    if tag_filter:
        query = query.filter(BatchFund.tags.contains(tag_filter))
        
    funds = query.order_by(BatchFund.date.desc()).all()
    total_in = db.session.query(db.func.sum(BatchFund.amount)).filter(BatchFund.type == 'Masuk').scalar() or 0
    total_out = db.session.query(db.func.sum(BatchFund.amount)).filter(BatchFund.type == 'Keluar').scalar() or 0
    balance = total_in - total_out
    
    target_payment = get_fund_target()
    fund_periods = FundPeriod.query.order_by(FundPeriod.start_date.asc(), FundPeriod.id.asc()).all()

    
    member_statuses = []
    your_status = None
    for s in students:
        total_paid = db.session.query(db.func.sum(BatchFund.amount)).filter(BatchFund.student_id == s.id, BatchFund.type == 'Masuk').scalar() or 0
        status_data = {'student': s,'paid': total_paid,'target': target_payment,'arrears': max(0, target_payment - total_paid)}
        member_statuses.append(status_data)
        
        # Identify current user's personal status
        if current_user.student_id == s.id:
            your_status = status_data
            
    return render_template('batch_fund.html', 
                         funds=funds, 
                         balance=balance, 
                         students=students, 
                         member_statuses=member_statuses, 
                         your_status=your_status,
                         fund_periods=fund_periods,
                         target_daily=int(SystemSetting.query.filter_by(key='fund_daily_rate').first().value) if SystemSetting.query.filter_by(key='fund_daily_rate').first() else 1000,
                         today_date=datetime.now().strftime('%Y-%m-%d'))

@app.route('/fund/periods', methods=['POST'])
@login_required
def create_fund_period():
    if not current_user.role.can_manage_fund:
        return redirect(url_for('dashboard'))

    title = (request.form.get('title') or '').strip()
    start_date = datetime.strptime(request.form['start_date'], '%Y-%m-%d').date()
    end_date = datetime.strptime(request.form['end_date'], '%Y-%m-%d').date()
    daily_rate = int(request.form.get('daily_rate') or 1000)
    is_active = 'is_active' in request.form

    if not title:
        title = f"Periode {FundPeriod.query.count() + 1}"
    if end_date < start_date:
        flash('Tanggal akhir periode tidak boleh sebelum tanggal mulai.')
        return redirect(url_for('manage_fund'))

    db.session.add(FundPeriod(
        title=title,
        start_date=start_date,
        end_date=end_date,
        daily_rate=daily_rate,
        is_active=is_active
    ))
    db.session.commit()
    auto_recalculate_points()
    flash('Periode kas berhasil ditambahkan.')
    return redirect(url_for('manage_fund'))

@app.route('/fund/periods/<int:id>', methods=['POST'])
@login_required
def update_fund_period(id):
    if not current_user.role.can_manage_fund:
        return redirect(url_for('dashboard'))

    period = FundPeriod.query.get_or_404(id)
    start_date = datetime.strptime(request.form['start_date'], '%Y-%m-%d').date()
    end_date = datetime.strptime(request.form['end_date'], '%Y-%m-%d').date()
    if end_date < start_date:
        flash('Tanggal akhir periode tidak boleh sebelum tanggal mulai.')
        return redirect(url_for('manage_fund'))

    period.title = (request.form.get('title') or period.title).strip() or period.title
    period.start_date = start_date
    period.end_date = end_date
    period.daily_rate = int(request.form.get('daily_rate') or period.daily_rate or 1000)
    period.is_active = 'is_active' in request.form
    db.session.commit()
    auto_recalculate_points()
    flash('Periode kas berhasil diperbarui.')
    return redirect(url_for('manage_fund'))

@app.route('/fund/periods/delete/<int:id>')
@login_required
def delete_fund_period(id):
    if not current_user.role.can_manage_fund:
        return redirect(url_for('dashboard'))

    period = FundPeriod.query.get_or_404(id)
    if FundPeriod.query.count() <= 1:
        flash('Minimal harus ada satu periode kas aktif/tersimpan.')
        return redirect(url_for('manage_fund'))

    db.session.delete(period)
    db.session.commit()
    auto_recalculate_points()
    flash('Periode kas dihapus.')
    return redirect(url_for('manage_fund'))

@app.route('/fund/edit/<int:id>', methods=['POST'])
@login_required
def edit_fund(id):
    if not current_user.role.can_manage_fund: return redirect(url_for('dashboard'))
    f = BatchFund.query.get_or_404(id)
    reason = request.form.get('reason')
    
    # Suggestion #10: Forensic Audit Detail
    if not f.is_edited:
        f.original_amount = f.amount
        f.original_description = f.description
    
    f.is_edited = True
    f.edit_reason = reason or "Koreksi Data"
    f.last_edited_by = current_user.username
    f.description = request.form['desc']
    f.amount = float(request.form['amount'])
    f.type = request.form['type']
    f.category = request.form['category']
    
    # Handle Tags
    tags = request.form.get('tags', '').strip()
    if tags: f.tags = tags if tags.startswith('#') else '#' + tags
    
    db.session.commit()
    auto_recalculate_points()

    # Suggestion #1: Auto-Announcement for Edit transactions
    new_ann = Announcement(
        title=f"Update Transaksi: {f.description}",
        content=f"ID: {f.id} diperbarui oleh {current_user.username}.\nAlasan: {f.edit_reason}\nNilai Baru: Rp {f.amount:,.0f}",
        category='Penting'
    )
    db.session.add(new_ann)
    db.session.commit()
    
    log_activity("Edit Kas", f"ID: {f.id}, Alasan: {f.edit_reason}")
    flash('Data kas diperbarui dan riwayat perubahan disimpan.')
    return redirect(url_for('manage_fund'))

# Suggestion #16: Duplicate
@app.route('/fund/duplicate/<int:id>')
@login_required
def duplicate_fund(id):
    if not current_user.role.can_manage_fund: return redirect(url_for('dashboard'))
    f = BatchFund.query.get_or_404(id)
    new_f = BatchFund(
        description=f"{f.description} (Copy)",
        amount=f.amount,
        type=f.type,
        category=f.category,
        date=datetime.now(),
        recorded_by=current_user.username,
        student_id=f.student_id,
        tags=f.tags
    )
    db.session.add(new_f)
    db.session.commit()
    auto_recalculate_points()
    flash('Transaksi diduplikasikan.')
    return redirect(url_for('manage_fund'))

# Suggestion #15: Batch Input
@app.route('/fund/batch', methods=['POST'])
@login_required
def batch_add_fund():
    if not current_user.role.can_manage_fund: return redirect(url_for('dashboard'))
    ids = request.form.getlist('student_ids[]')
    amounts = request.form.getlist('amounts[]')
    desc = request.form.get('common_desc', 'Iuran Massal')
    date_str = request.form.get('common_date', datetime.now().strftime('%Y-%m-%d'))
    
    count = 0
    for i in range(len(ids)):
        if amounts[i] and float(amounts[i]) > 0:
            student = Student.query.get(int(ids[i]))
            if student:
                f = BatchFund(
                    description=f"{desc} - {student.full_name}",
                    amount=float(amounts[i]),
                    type='Masuk',
                    category='Iuran Mingguan',
                    date=datetime.strptime(date_str, '%Y-%m-%d'),
                    recorded_by=current_user.username,
                    student_id=student.id
                )
                db.session.add(f)
                count += 1
    db.session.commit()
    auto_recalculate_points()
    log_activity("Batch Input", f"Total: {count} entri.")
    flash(f'Berhasil mencatat {count} transaksi massal.')
    return redirect(url_for('manage_fund'))

@app.route('/fund/delete/<int:id>')
@login_required
def delete_fund(id):
    if not current_user.role.can_manage_fund: return redirect(url_for('dashboard'))
    f = BatchFund.query.get_or_404(id)
    log_activity("Hapus Kas", f"ID: {id}, Ket: {f.description}")
    db.session.delete(f)
    db.session.commit()
    auto_recalculate_points()
    return redirect(url_for('manage_fund'))

@app.route('/fund/export')
@login_required
def export_fund():
    if not current_user.role.can_export_data:
        flash('Akses ditolak.')
        return redirect(url_for('manage_fund'))
        
    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(['ID', 'Tanggal', 'Keterangan', 'Jumlah', 'Tipe', 'Kategori', 'Pelapor'])
    
    funds = BatchFund.query.order_by(BatchFund.date.desc()).all()
    for f in funds:
        cw.writerow([f.id, f.date, f.description, f.amount, f.type, f.category, f.recorded_by])
        
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=laporan_kas.csv"
    output.headers["Content-type"] = "text/csv"
    return output

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def manage_settings():
    if not current_user.role.can_edit_settings:
        flash('Akses ditolak.')
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        # 1. Handle Text Settings
        text_keys = ['web_title', 'web_logo', 'favicon_url', 'fund_start_date', 'fund_end_date', 'fund_daily_rate', 
                     'web_desc', 'social_ig', 'social_wa', 'seo_keywords']
        for key in text_keys:
            if key in request.form:
                val = request.form[key]
                setting = SystemSetting.query.filter_by(key=key).first()
                if setting: setting.value = val
                else: db.session.add(SystemSetting(key=key, value=val))
        
        # 2. Handle active classroom switch from web settings
        classroom_id = request.form.get('active_classroom_id')
        if classroom_id is not None and str(classroom_id).strip():
            try:
                classroom_id = int(classroom_id)
                if current_user.role.can_manage_roles:
                    allowed_classrooms = ClassRoom.query.all()
                else:
                    allowed_classrooms = [current_user.classroom or (current_user.student.classroom if current_user.student else None)]
                    allowed_classrooms = [c for c in allowed_classrooms if c]
                allowed_ids = {c.id for c in allowed_classrooms}
                if classroom_id in allowed_ids:
                    classroom = ClassRoom.query.get(classroom_id)
                    if classroom:
                        current_user.classroom_id = classroom.id
            except Exception:
                pass

        # 3. Handle Branding File Uploads (Logo/Favicon) with Auto-Compression
        branding_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'branding')
        os.makedirs(branding_dir, exist_ok=True)

        for key in ['logo_file', 'favicon_file']:
            if key in request.files:
                file = request.files[key]
                if file and file.filename != '':
                    try:
                        # Process and Compress Branding Images
                        img = Image.open(file.stream)
                        if img.mode in ("RGBA", "P"): img = img.convert("RGB")
                        
                        asset_type = 'logo' if 'logo' in key else 'favicon'
                        filename = f"{asset_type}_{uuid.uuid4().hex[:8]}.webp"
                        filepath = os.path.join(branding_dir, filename)
                        
                        # Branding logic: Auto-resize and high quality compress
                        if asset_type == 'logo':
                             img.thumbnail((512, 512)) # Max logo size
                        else:
                             img.thumbnail((64, 64)) # Square favicon
                             
                        img.save(filepath, 'WEBP', quality=90)
                        
                        # Save path to DB
                        db_key = 'web_logo_path' if asset_type == 'logo' else 'favicon_path'
                        db_val = f"/static/uploads/branding/{filename}"
                        
                        setting = SystemSetting.query.filter_by(key=db_key).first()
                        if setting: setting.value = db_val
                        else: db.session.add(SystemSetting(key=db_key, value=db_val))
                    except Exception as e:
                        flash(f'Gagal memproses gambar branding: {e}')
                    
        db.session.commit()
        flash('Pengaturan sistem dan branding diperbarui!')
        return redirect(url_for('manage_settings'))
    
    settings = {s.key: s.value for s in SystemSetting.query.all()}
    active_classroom = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    if not active_classroom:
        active_classroom = ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()
    classrooms = []
    if current_user.role.can_manage_roles:
        classrooms = ClassRoom.query.order_by(ClassRoom.name.asc()).all()
    elif active_classroom:
        classrooms = [active_classroom]
    return render_template('settings.html', settings=settings, classrooms=classrooms, active_classroom=active_classroom)

@app.route('/classes', methods=['GET', 'POST'])
@login_required
def manage_classes():
    if not current_user.role.can_manage_roles:
        flash('Akses ditolak.')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        class_id = request.form.get('class_id')
        name = (request.form.get('name') or '').strip()
        batch = (request.form.get('batch') or '').strip()
        if not name:
            flash('Nama kelas wajib diisi.')
            return redirect(url_for('manage_classes'))

        if class_id:
            classroom = ClassRoom.query.get(int(class_id))
            if classroom:
                classroom.name = name
                classroom.batch = batch
                db.session.commit()
                flash('Kelas berhasil diperbarui.')
                return redirect(url_for('manage_classes'))

        existing = ClassRoom.query.filter(db.func.lower(ClassRoom.name) == name.lower()).first()
        if existing:
            flash('Kelas dengan nama tersebut sudah ada.')
            return redirect(url_for('manage_classes'))

        classroom = ClassRoom(name=name, batch=batch)
        db.session.add(classroom)
        db.session.commit()
        flash('Kelas baru berhasil ditambahkan.')
        return redirect(url_for('manage_classes'))

    classes = ClassRoom.query.order_by(ClassRoom.name.asc()).all()
    return render_template('classes.html', classes=classes)

@app.route('/roles', methods=['GET', 'POST'])
@login_required
def manage_roles():
    if not current_user.role.can_manage_roles:
        flash('Akses ditolak.')
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        # 1. Buat Role Baru
        if 'role_name' in request.form:
            new_role = Role(
                name=request.form['role_name'], 
                description=request.form['role_desc'], 
                can_manage_students='can_manage_students' in request.form, 
                can_manage_schedule='can_manage_schedule' in request.form, 
                can_manage_fund='can_manage_fund' in request.form,
                can_manage_announcements='can_manage_announcements' in request.form,
                can_manage_roles='can_manage_roles' in request.form,
                can_view_logs='can_view_logs' in request.form,
                can_export_data='can_export_data' in request.form,
                can_edit_settings='can_edit_settings' in request.form,
                can_manage_gallery='can_manage_gallery' in request.form,
                can_manage_notifications='can_manage_notifications' in request.form,
                can_manage_whatsapp='can_manage_whatsapp' in request.form,
                can_manage_assignments='can_manage_assignments' in request.form,
                can_use_api='can_use_api' in request.form,
                can_access_multi_classroom='can_access_multi_classroom' in request.form,
                can_switch_classroom_context='can_switch_classroom_context' in request.form,
                can_manage_classrooms='can_manage_classrooms' in request.form,
                can_assign_users_to_classroom='can_assign_users_to_classroom' in request.form,
                can_move_users_between_classrooms='can_move_users_between_classrooms' in request.form,
                can_view_all_classrooms='can_view_all_classrooms' in request.form,
                can_manage_students_multi_class='can_manage_students_multi_class' in request.form,
                can_manage_schedule_multi_class='can_manage_schedule_multi_class' in request.form,
                can_manage_announcements_multi_class='can_manage_announcements_multi_class' in request.form,
                can_manage_assignments_multi_class='can_manage_assignments_multi_class' in request.form,
                can_manage_gallery_multi_class='can_manage_gallery_multi_class' in request.form,
                can_manage_notifications_multi_class='can_manage_notifications_multi_class' in request.form,
                can_view_classroom_reports='can_view_classroom_reports' in request.form,
                can_export_classroom_data='can_export_classroom_data' in request.form
            )
            db.session.add(new_role)
            db.session.commit()
            log_activity("Tambah Role", f"Nama: {new_role.name}")
            flash(f'Role {new_role.name} berhasil dibuat.')
            
        # 2. Buat User Baru (Manual)
        elif 'username' in request.form and 'password' in request.form:
            s_id = request.form.get('student_id')
            classroom_id = request.form.get('classroom_id')
            classroom = ClassRoom.query.get(int(classroom_id)) if classroom_id and classroom_id != 'none' else None
            new_user = User(
                username=request.form['username'], 
                password=request.form['password'], 
                role_id=request.form['role_id'], 
                full_name=request.form['full_name'], 
                email=request.form['email'],
                student_id=int(s_id) if s_id and s_id != 'none' else None,
                classroom_id=classroom.id if classroom else None,
                status='Active'
            )
            try:
                db.session.add(new_user)
                db.session.commit()
                log_activity("Tambah User", f"Username: {new_user.username}")
                flash(f'User {new_user.username} berhasil didaftarkan.')
            except Exception as e:
                db.session.rollback()
                flash(f'Gagal menambah user: Email atau Username mungkin sudah terdaftar.')
                print(f"User Add Error: {e}")
            
        return redirect(url_for('manage_roles'))

    roles = Role.query.all()
    users = User.query.all()
    students = Student.query.order_by(Student.full_name).all()
    classrooms = ClassRoom.query.order_by(ClassRoom.name.asc()).all()
    return render_template('roles.html', roles=roles, users=users, students=students, classrooms=classrooms)

@app.route('/roles/edit/user/<int:id>', methods=['POST'])
@login_required
def edit_user(id):
    if not current_user.role.can_manage_roles: return redirect(url_for('dashboard'))
    user = User.query.get_or_404(id)
    user.full_name = request.form['full_name']
    user.email = request.form['email']
    user.role_id = request.form['role_id']
    user.status = request.form['status']
    
    s_id = request.form.get('student_id')
    user.student_id = int(s_id) if s_id and s_id != 'none' else None
    classroom_id = request.form.get('classroom_id')
    if classroom_id and classroom_id != 'none':
        classroom = ClassRoom.query.get(int(classroom_id))
        user.classroom_id = classroom.id if classroom else user.classroom_id
    
    if request.form.get('password'):
        user.password = request.form['password']
    
    try:
        db.session.commit()
        log_activity("Edit User", f"Username: {user.username}")
        flash(f'Data user {user.username} telah diperbarui.')
    except Exception as e:
        db.session.rollback()
        flash(f'Gagal memperbarui user: Email mungkin sudah digunakan oleh akun lain.')
        print(f"User Edit Error: {e}")
    return redirect(url_for('manage_roles'))

@app.route('/roles/delete/user/<int:id>')
@login_required
def delete_user(id):
    if not current_user.role.can_manage_roles: return redirect(url_for('dashboard'))
    user = User.query.get_or_404(id)
    if user.id == current_user.id:
        flash('Anda tidak bisa menghapus akun Anda sendiri.')
        return redirect(url_for('manage_roles'))
    log_activity("Hapus User", f"Username: {user.username}")
    db.session.delete(user)
    db.session.commit()
    return redirect(url_for('manage_roles'))

@app.route('/roles/edit/role/<int:id>', methods=['POST'])
@login_required
def edit_role(id):
    if not current_user.role.can_manage_roles: return redirect(url_for('dashboard'))
    role = Role.query.get_or_404(id)
    role.name = request.form['role_name']
    role.description = request.form['role_desc']
    role.can_manage_students = 'can_manage_students' in request.form
    role.can_manage_schedule = 'can_manage_schedule' in request.form
    role.can_manage_fund = 'can_manage_fund' in request.form
    role.can_manage_announcements = 'can_manage_announcements' in request.form
    role.can_manage_roles = 'can_manage_roles' in request.form
    role.can_view_logs = 'can_view_logs' in request.form
    role.can_export_data = 'can_export_data' in request.form
    role.can_edit_settings = 'can_edit_settings' in request.form
    role.can_manage_gallery = 'can_manage_gallery' in request.form
    role.can_manage_notifications = 'can_manage_notifications' in request.form
    role.can_manage_whatsapp = 'can_manage_whatsapp' in request.form
    role.can_manage_assignments = 'can_manage_assignments' in request.form
    role.can_use_api = 'can_use_api' in request.form
    role.can_access_multi_classroom = 'can_access_multi_classroom' in request.form
    role.can_switch_classroom_context = 'can_switch_classroom_context' in request.form
    role.can_manage_classrooms = 'can_manage_classrooms' in request.form
    role.can_assign_users_to_classroom = 'can_assign_users_to_classroom' in request.form
    role.can_move_users_between_classrooms = 'can_move_users_between_classrooms' in request.form
    role.can_view_all_classrooms = 'can_view_all_classrooms' in request.form
    role.can_manage_students_multi_class = 'can_manage_students_multi_class' in request.form
    role.can_manage_schedule_multi_class = 'can_manage_schedule_multi_class' in request.form
    role.can_manage_announcements_multi_class = 'can_manage_announcements_multi_class' in request.form
    role.can_manage_assignments_multi_class = 'can_manage_assignments_multi_class' in request.form
    role.can_manage_gallery_multi_class = 'can_manage_gallery_multi_class' in request.form
    role.can_manage_notifications_multi_class = 'can_manage_notifications_multi_class' in request.form
    role.can_view_classroom_reports = 'can_view_classroom_reports' in request.form
    role.can_export_classroom_data = 'can_export_classroom_data' in request.form
    db.session.commit()
    log_activity("Edit Role", f"Nama: {role.name}")
    return redirect(url_for('manage_roles'))

@app.route('/roles/delete/role/<int:id>')
@login_required
def delete_role(id):
    if not current_user.role.can_manage_roles: return redirect(url_for('dashboard'))
    role = Role.query.get_or_404(id)
    if Role.query.count() <= 1:
        return redirect(url_for('manage_roles'))
    log_activity("Hapus Role", f"Nama: {role.name}")
    db.session.delete(role)
    db.session.commit()
    return redirect(url_for('manage_roles'))

@app.route('/view/<class_name>')
def public_view(class_name):
    classroom = ClassRoom.query.filter_by(name=class_name).first_or_404()
    days_order = ['Senin', 'Selasa', 'Rabu', 'Kamis', 'Jumat', "Jum'at"]
    grouped_schedules = {day: [s for s in Schedule.query.filter_by(classroom_id=classroom.id, day=day).all()] for day in days_order}
    grouped_schedules = {k: v for k, v in grouped_schedules.items() if v}
    return render_template('public_schedule.html', classroom=classroom, grouped_schedules=grouped_schedules)

import uuid
from PIL import Image

def process_image_upload(file):
    if not file: return None
    try:
        gallery_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'gallery')
        thumb_dir = os.path.join(gallery_dir, 'thumbnails')
        os.makedirs(gallery_dir, exist_ok=True)
        os.makedirs(thumb_dir, exist_ok=True)

        filename_base = uuid.uuid4().hex
        filename = f"{filename_base}.webp"
        filepath = os.path.join(gallery_dir, filename)
        thumbpath = os.path.join(thumb_dir, filename)

        img = Image.open(file.stream)
        if img.mode in ("RGBA", "P"): img = img.convert("RGB")
        
        # Save Preview/Standard (Optimized and Resized)
        preview_img = img.copy()
        preview_img.thumbnail((1200, 1200)) # Resized to lightweight HD
        preview_img.save(filepath, 'WEBP', quality=50) # 50% Quality for speed
        
        # Create and Save Thumbnail (Tiny for grids)
        thumb_img = img.copy()
        thumb_img.thumbnail((300, 300))
        thumb_img.save(thumbpath, 'WEBP', quality=45)
        return filename
    except Exception as e:
        print(f"Error processing image: {e}")
        return None

@app.route('/gallery')
@login_required
def manage_gallery():
    active_classroom = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    if not active_classroom:
        active_classroom = ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()
    # Admin/Pengurus can see all (including pending), Members can only see Published
    photos_query = GalleryPhoto.query
    if active_classroom:
        photos_query = photos_query.filter(
            (GalleryPhoto.classroom_id == active_classroom.id) | (GalleryPhoto.classroom_id.is_(None))
        )
    if current_user.role.name in ['Admin', 'Pengurus']:
        photos = photos_query.order_by(GalleryPhoto.created_at.desc()).all()
    else:
        photos = photos_query.filter(
            (GalleryPhoto.status == 'Published') | 
            ((GalleryPhoto.status == 'Pending') & (GalleryPhoto.uploaded_by == current_user.id))
        ).order_by(GalleryPhoto.created_at.desc()).all()
        
    classrooms = ClassRoom.query.order_by(ClassRoom.name.asc()).all()
    return render_template('gallery.html', photos=photos, classrooms=classrooms, active_classroom=active_classroom)

@app.route('/gallery/upload', methods=['POST'])
@login_required
def upload_gallery():
    files = request.files.getlist('photos')
    if not files or files[0].filename == '':
        flash('Tidak ada file yang dipilih.')
        return redirect(url_for('manage_gallery'))
    
    active_classroom = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    if not active_classroom:
        active_classroom = ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()
    tags = request.form.get('tags', '')
    is_public = 'is_public' in request.form
    
    # Force private if normal member without gallery powers
    is_admin_power = (hasattr(current_user.role, 'can_manage_gallery') and current_user.role.can_manage_gallery) or (current_user.role.name in ['Admin', 'Pengurus'])
    
    status = 'Published' if is_admin_power else 'Pending'
    if not is_admin_power:
        is_public = False # Member uploads are internal first
            
    count = 0
    for file in files:
        if file and (file.filename.endswith(('.png', '.jpg', '.jpeg', '.webp'))):
            filename = process_image_upload(file)
            if filename:
                photo = GalleryPhoto(
                    classroom_id=active_classroom.id if active_classroom else None,
                    filename=filename,
                    thumbnail=filename,
                    caption=request.form.get('caption', ''),
                    is_public=is_public,
                    uploaded_by=current_user.id,
                    status=status,
                    tags=tags
                )
                db.session.add(photo)
                count += 1
                
    db.session.commit()
    auto_recalculate_points()
    log_activity("Upload Foto Galeri", f"{count} foto diunggah (Status: {status}).")
    if status == 'Pending':
        flash(f'{count} foto berhasil diunggah. Menunggu persetujuan Admin untuk dipublikasikan.')
    else:
        send_push("Foto Galeri Baru!", f"{current_user.full_name} baru saja mengunggah {count} foto baru.", sender_id=current_user.id)
        flash(f'{count} foto berhasil diunggah dan dipublikasikan.')
    return redirect(url_for('manage_gallery'))

@app.route('/gallery/delete/<int:id>')
@login_required
def delete_gallery(id):
    photo = GalleryPhoto.query.get_or_404(id)
    # Check permission
    can_manage = (hasattr(current_user.role, 'can_manage_gallery') and current_user.role.can_manage_gallery) or (current_user.role.name in ['Admin', 'Pengurus'])
    
    if not can_manage and photo.uploaded_by != current_user.id:
        flash('Akses ditolak.')
        return redirect(url_for('manage_gallery'))
    
    # Delete Actual Files
    try:
        path = os.path.join(app.config['UPLOAD_FOLDER'], 'gallery', photo.filename)
        thumb = os.path.join(app.config['UPLOAD_FOLDER'], 'gallery', 'thumbnails', photo.thumbnail)
        if os.path.exists(path): os.remove(path)
        if os.path.exists(thumb): os.remove(thumb)
    except Exception as e:
        print(f"File delete error: {e}")

    log_activity("Hapus Foto", f"ID: {photo.id}")
    db.session.delete(photo)
    db.session.commit()
    auto_recalculate_points()
    flash('Foto berhasil dihapus.')
    return redirect(url_for('manage_gallery'))

@app.route('/gallery/approve/<int:id>')
@login_required
def approve_gallery(id):
    if not (current_user.role.name in ['Admin', 'Pengurus'] or (hasattr(current_user.role, 'can_manage_gallery') and current_user.role.can_manage_gallery)):
        flash('Akses ditolak.')
        return redirect(url_for('manage_gallery'))
        
    photo = GalleryPhoto.query.get_or_404(id)
    photo.status = 'Published'
    db.session.commit()
    auto_recalculate_points()
    log_activity("Approve Foto", f"ID: {photo.id}")
    flash(f'Foto oleh {photo.user.full_name} disetujui.')
    return redirect(url_for('manage_gallery'))

@app.route('/gallery/reject/<int:id>')
@login_required
def reject_gallery(id):
    if not (current_user.role.name in ['Admin', 'Pengurus'] or (hasattr(current_user.role, 'can_manage_gallery') and current_user.role.can_manage_gallery)):
        flash('Akses ditolak.')
        return redirect(url_for('manage_gallery'))
        
    photo = GalleryPhoto.query.get_or_404(id)
    # Delete files directly on reject as per user request
    try:
        path = os.path.join(app.config['UPLOAD_FOLDER'], 'gallery', photo.filename)
        thumb = os.path.join(app.config['UPLOAD_FOLDER'], 'gallery', 'thumbnails', photo.thumbnail)
        if os.path.exists(path): os.remove(path)
        if os.path.exists(thumb): os.remove(thumb)
    except: pass
    
    log_activity("Reject Foto", f"ID: {photo.id}, Uploader: {photo.user.username}")
    db.session.delete(photo)
    db.session.commit()
    auto_recalculate_points()
    flash('Foto ditolak dan dihapus permanen.')
    return redirect(url_for('manage_gallery'))

@app.route('/gallery/toggle/<int:id>')
@login_required
def toggle_gallery_visibility(id):
    if not hasattr(current_user.role, 'can_manage_gallery') or not current_user.role.can_manage_gallery:
        if current_user.role.name not in ['Admin', 'Pengurus']: 
            return redirect(url_for('manage_gallery'))
        
    p = GalleryPhoto.query.get_or_404(id)
    p.is_public = not p.is_public
    db.session.commit()
    return redirect(url_for('manage_gallery'))

@app.route('/gallery/public')
def public_gallery():
    # ONLY FETCH PUBLISHED AND PUBLIC PHOTOS
    photos = GalleryPhoto.query.filter_by(is_public=True, status='Published').order_by(GalleryPhoto.created_at.desc()).all()
    class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()
    return render_template('gallery_public.html', photos=photos, classroom=class_fb)

@app.route('/gallery/comment/<int:photo_id>', methods=['POST'])
@login_required
def add_photo_comment(photo_id):
    photo = GalleryPhoto.query.get_or_404(photo_id)
    body = request.form.get('body', '').strip()
    if body:
        comment = PhotoComment(photo_id=photo.id, user_id=current_user.id, body=body)
        db.session.add(comment)
        db.session.commit()
        
        # Real-time AJAX support
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            # Identify name for UI
            name = (current_user.student.full_name if current_user.student else current_user.username)
            if len(name) > 15: name = name[:15] + "..."
            
            return {
                'status': 'success',
                'comment': {
                    'id': comment.id,
                    'user': name,
                    'body': comment.body,
                    'time': comment.created_at.strftime('%d %b %H:%M')
                }
            }
        
        flash('Komentar ditambahkan.')
    return redirect(request.referrer or url_for('manage_gallery'))

@app.route('/gallery/comment/delete/<int:id>')
@login_required
def delete_photo_comment(id):
    comment = PhotoComment.query.get_or_404(id)
    # Allow deletion if Admin/Pengurus or if the user owns the comment
    can_delete = False
    if current_user.role.name in ['Admin', 'Pengurus']:
        can_delete = True
    elif hasattr(current_user.role, 'can_manage_gallery') and current_user.role.can_manage_gallery:
        can_delete = True
    elif current_user.id == comment.user_id:
        can_delete = True
        
    if can_delete:
        db.session.delete(comment)
        db.session.commit()
        flash('Komentar dihapus.')
    else:
        flash('Akses ditolak.')
    return redirect(request.referrer or url_for('manage_gallery'))

@app.route('/gallery/download/<int:id>')
@login_required
def download_gallery_photo(id):
    photo = GalleryPhoto.query.get_or_404(id)
    # Ensure only members can download original resolution
    # (Since it's login_required, any logged in user can download)
    directory = os.path.join(app.config['UPLOAD_FOLDER'], 'gallery')
    # Use as_attachment=True with original original if available, 
    # but here we serve the webp one as the "light original" 
    # (The user specifically asked for compression only for previews)
    return send_from_directory(directory, photo.filename, as_attachment=True)


from sqlalchemy import text

def init_db():
    """
    Inisialisasi database dan sinkronisasi skema otomatis (Multi-Engine Support).
    Mendukung SQLite dan MySQL untuk sinkronisasi kolom baru tanpa merusak data.
    """
    with app.app_context():
        # 1. Buat tabel baru jika belum ada
        db.create_all()

        # 2. Sinkronisasi Skema Otomatis (SQLite & MySQL)
        try:
            from sqlalchemy import inspect
            inspector = inspect(db.engine)
            
            with db.engine.connect() as conn:
                # A. Sinkronisasi tabel 'user'
                user_cols = [c['name'] for c in inspector.get_columns('user')]
                if 'student_id' not in user_cols:
                    conn.execute(text("ALTER TABLE user ADD COLUMN student_id INTEGER NULL"))
                if 'classroom_id' not in user_cols:
                    conn.execute(text("ALTER TABLE user ADD COLUMN classroom_id INTEGER NULL"))
                if 'bio' not in user_cols:
                    conn.execute(text("ALTER TABLE user ADD COLUMN bio VARCHAR(255) NULL"))
                if 'whatsapp' not in user_cols:
                    conn.execute(text("ALTER TABLE user ADD COLUMN whatsapp VARCHAR(20) NULL"))
                if 'points' not in user_cols:
                    conn.execute(text("ALTER TABLE user ADD COLUMN points INTEGER DEFAULT 0"))
                
                # B. Sinkronisasi tabel 'role'
                role_cols = [c['name'] for c in inspector.get_columns('role')]
                for col in ['can_view_logs', 'can_export_data', 'can_edit_settings', 'can_manage_gallery',
                            'can_manage_notifications', 'can_manage_whatsapp', 'can_manage_assignments', 'can_use_api']:
                    if col not in role_cols:
                        conn.execute(text(f"ALTER TABLE role ADD COLUMN {col} BOOLEAN DEFAULT 0"))
                
                # C. Sinkronisasi tabel 'batch_fund'
                fund_cols = [c['name'] for c in inspector.get_columns('batch_fund')]
                if 'original_amount' not in fund_cols:
                    conn.execute(text("ALTER TABLE batch_fund ADD COLUMN original_amount FLOAT NULL"))
                if 'original_description' not in fund_cols:
                    conn.execute(text("ALTER TABLE batch_fund ADD COLUMN original_description VARCHAR(200) NULL"))
                if 'tags' not in fund_cols:
                    conn.execute(text("ALTER TABLE batch_fund ADD COLUMN tags VARCHAR(100) NULL"))
                
                # D. Sinkronisasi tabel 'announcement'
                ann_cols = [c['name'] for c in inspector.get_columns('announcement')]
                if 'classroom_id' not in ann_cols:
                    conn.execute(text("ALTER TABLE announcement ADD COLUMN classroom_id INTEGER NULL"))
                if 'is_public' not in ann_cols:
                    conn.execute(text("ALTER TABLE announcement ADD COLUMN is_public BOOLEAN DEFAULT 1"))

                # E. Sinkronisasi tabel 'assignment'
                assignment_cols = [c['name'] for c in inspector.get_columns('assignment')]
                if 'classroom_id' not in assignment_cols:
                    conn.execute(text("ALTER TABLE assignment ADD COLUMN classroom_id INTEGER NULL"))

                # F. Sinkronisasi tabel 'gallery_photo'
                gallery_cols = [c['name'] for c in inspector.get_columns('gallery_photo')]
                if 'classroom_id' not in gallery_cols:
                    conn.execute(text("ALTER TABLE gallery_photo ADD COLUMN classroom_id INTEGER NULL"))
                if 'status' not in gallery_cols:
                    conn.execute(text("ALTER TABLE gallery_photo ADD COLUMN status VARCHAR(20) DEFAULT 'Published'"))

                # G. Sinkronisasi tabel 'class_room'
                classroom_cols = [c['name'] for c in inspector.get_columns('class_room')]
                if 'batch' not in classroom_cols:
                    conn.execute(text("ALTER TABLE class_room ADD COLUMN batch VARCHAR(50) NULL"))

                # H. Sinkronisasi tabel 'announcement_read'
                notification_cols = [c['name'] for c in inspector.get_columns('notification_history')]
                if 'classroom_id' not in notification_cols:
                    conn.execute(text("ALTER TABLE notification_history ADD COLUMN classroom_id INTEGER NULL"))
                try:
                    # Check if table exists
                    inspector.get_columns('announcement_read')
                except:
                    # If it fails, create via create_all which is already called, 
                    # but this is for extra safety in our self-healing engine
                    db.create_all()

                conn.commit()
                print("Status: Sinkronisasi Skema (Multi-Engine) Berhasil.")
        except Exception as e:
            print(f"Peringatan: Gagal sinkronisasi skema otomatis: {e}")

        # 3. Inisialisasi Data Default jika tabel Role masih kosong
        try:
            fb = ClassRoom.query.filter_by(name='Famousbytee.b').first()
            if not fb:
                fb = ClassRoom(name='Famousbytee.b', batch='TI 2024')
                db.session.add(fb)
                db.session.commit()

            if not Role.query.first():
                # Definisikan Role Default dengan izin baru
                admin_r = Role(
                    name='Admin', description='Akses penuh koordinasi sistem.', 
                    can_manage_students=True, can_manage_schedule=True, can_manage_fund=True, 
                    can_manage_announcements=True, can_manage_roles=True,
                    can_view_logs=True, can_export_data=True, can_edit_settings=True, can_manage_gallery=True,
                    can_manage_notifications=True, can_manage_whatsapp=True, can_manage_assignments=True,
                    can_use_api=True
                )
                staff_r = Role(
                    name='Pengurus', description='Manajemen data operasional.', 
                    can_manage_students=True, can_manage_schedule=True, can_manage_fund=True, 
                    can_manage_announcements=True, can_manage_roles=False,
                    can_view_logs=True, can_export_data=True, can_edit_settings=False, can_manage_gallery=True,
                    can_manage_notifications=True, can_manage_whatsapp=True, can_manage_assignments=True,
                    can_use_api=False
                )
                member_r = Role(
                    name='Member', description='Akses dashboard anggota.', 
                    can_manage_students=False, can_manage_schedule=False, can_manage_fund=False, 
                    can_manage_announcements=False, can_manage_roles=False,
                    can_view_logs=False, can_export_data=False, can_edit_settings=False, can_manage_gallery=False,
                    can_manage_notifications=False, can_manage_whatsapp=False, can_manage_assignments=False,
                    can_use_api=False
                )
                db.session.add_all([admin_r, staff_r, member_r])
                db.session.commit()
                
                # Buat Admin Default
                db.session.flush()
                db.session.add(User(
                    username='admin',
                    password='admin',
                    role_id=admin_r.id,
                    classroom_id=fb.id
                ))
                
                # Seeding Awal
                db.session.add(Announcement(
                    title='Selamat Datang di Portal Famousbytee.b', 
                    content='Ini adalah portal resmi khusus untuk anggota kelas Famousbytee.b.', 
                    category='Penting', is_pinned=True
                ))
                db.session.add(BatchFund(
                    description='Saldo Awal Kas Kelas', amount=1000000, 
                    type='Masuk', category='Iuran Mingguan', 
                    date=datetime.now(), recorded_by='System'
                ))
                db.session.commit()
                print("Status: Data awal berhasil disuntikkan.")
            else:
                admin_user = User.query.filter_by(username='admin').first()
                if admin_user and not admin_user.classroom_id:
                    admin_user.classroom_id = fb.id
                    db.session.commit()
        except Exception as e:
            print(f"Peringatan: Gagal seeding data awal (Mungkin tabel belum sinkron): {e}")

        # 4. Inisialisasi Pengaturan Sistem jika belum ada
        try:
            if not SystemSetting.query.filter_by(key='web_title').first():
                db.session.add_all([
                    SystemSetting(key='web_logo', value='monitor', description='Ikon Logo (Lucide Icon)'),
                    SystemSetting(key='web_title', value='Famousbytee.b Portal', description='Judul Utama Portal'),
                    SystemSetting(key='favicon_url', value='/static/favicon.ico', description='URL Favicon'),
                    SystemSetting(key='fund_start_date', value='2024-03-30', description='Tanggal Mulai Kas (YYYY-MM-DD)'),
                    SystemSetting(key='fund_end_date', value='', description='Tanggal Akhir Periode Wajib Kas (YYYY-MM-DD, kosong = sampai hari ini)'),
                    SystemSetting(key='fund_daily_rate', value='1000', description='Iuran Harian Kas (Senin-Jumat)'),
                    SystemSetting(key='web_desc', value='Portal Resmi Manajemen Kelas Famousbytee.b', description='Deskripsi Web (SEO)'),
                    SystemSetting(key='social_ig', value='#', description='Link Instagram Kelas'),
                    SystemSetting(key='social_wa', value='#', description='Link WhatsApp Group'),
                    SystemSetting(key='seo_keywords', value='famousbytee, portal, kelas, manajemen', description='Kata Kunci SEO (Pisahkan dengan koma)'),
                    SystemSetting(key='activity_log_retention_days', value='30', description='Masa simpan log aktivitas dalam hari'),
                    SystemSetting(key='waha_enabled', value='false', description='Aktifkan integrasi WAHA'),
                    SystemSetting(key='waha_base_url', value='', description='Base URL server WAHA'),
                    SystemSetting(key='waha_api_key', value='', description='API key WAHA'),
                    SystemSetting(key='waha_session', value='', description='Nama session WAHA'),
                    SystemSetting(key='waha_group_chat_id', value='', description='Chat ID grup WAHA'),
                    SystemSetting(key='waha_daily_time', value='18:00', description='Jam ringkasan harian WAHA'),
                    SystemSetting(key='waha_last_daily_summary_date', value='', description='Tanggal ringkasan harian terakhir'),
                    SystemSetting(key='notification_channel_default', value='push', description='Channel default notifikasi: push, whatsapp, both'),
                    SystemSetting(key='waha_schedule_template', value='Assalamualaikum dan selamat malam, tabe saudara dan saudari sekalian di grup ini, Jadwal Mata Kuliah {day_name}, {date_long}\n{schedule_lines}\n{deadline_section}(Sesuai jadwal dari pihak kampus)\n{extra_info_section}Sekian dan terimakasih', description='Template ringkasan jadwal WAHA'),
                    SystemSetting(key='waha_schedule_item_template', value='{index}. MK {subject} mulai jam {time_range}', description='Template item jadwal WAHA'),
                    SystemSetting(key='waha_schedule_deadline_item_template', value='{index}. Deadline {subject}: {title} jam {deadline_time}', description='Template item deadline WAHA'),
                    SystemSetting(key='waha_schedule_extra_info', value='', description='Info tambahan tetap di ringkasan WAHA'),
                    SystemSetting(key='waha_admin_header_enabled', value='true', description='Aktifkan header/pengenal admin di pesan WA'),
                    SystemSetting(key='waha_admin_header_text', value='*[PESAN RESMI ADMIN FAMOUSBYTEE]*\n{title_block}Pesan ini dikirim dari sistem admin.\n', description='Template header admin untuk pesan WA'),
                    SystemSetting(key='schedule_notify_on_create', value='true', description='Kirim notifikasi WhatsApp saat jadwal baru dibuat'),
                    SystemSetting(key='schedule_notify_on_edit', value='false', description='Kirim notifikasi WhatsApp saat jadwal diedit (default: false untuk hindari spam)'),
                    SystemSetting(key='schedule_notify_on_delete', value='true', description='Kirim notifikasi WhatsApp saat jadwal dihapus')
                ])
                db.session.commit()
                print("Status: Pengaturan sistem berhasil diinisialisasi.")
        except Exception as e:
            print(f"Peringatan: Gagal inisialisasi pengaturan: {e}")

        try:
            if not SystemSetting.query.filter_by(key='activity_log_retention_days').first():
                db.session.add(SystemSetting(key='activity_log_retention_days', value='30', description='Masa simpan log aktivitas dalam hari'))
                db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"Peringatan: Gagal memastikan retensi log aktivitas: {e}")

# ----------------API & SITEMAP----------------
from flask import jsonify

@app.route('/api/students')
@login_required
def api_students():
    if not current_user.role.can_use_api: return jsonify({'error': 'Unauthorized'}), 403
    classroom = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    students_query = Student.query
    if classroom:
        students_query = students_query.filter_by(classroom_id=classroom.id)
    students = students_query.all()
    return jsonify([{'id': s.id, 'nim': s.nim, 'name': s.full_name, 'status': s.status} for s in students])

@app.route('/api/announcements')
@login_required
def api_announcements():
    if not current_user.role.can_use_api: return jsonify({'error': 'Unauthorized'}), 403
    classroom = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    anns_query = Announcement.query
    if classroom:
        anns_query = anns_query.filter((Announcement.classroom_id == classroom.id) | (Announcement.classroom_id.is_(None)))
    anns = anns_query.all()
    return jsonify([{'id': a.id, 'title': a.title, 'category': a.category, 'date': a.date_posted} for a in anns])

@app.route('/notifications', methods=['GET', 'POST'])
@login_required
def manage_notifications():
    if not (current_user.role.can_manage_notifications or current_user.role.can_manage_whatsapp):
        flash('Akses ditolak.')
        return redirect(url_for('dashboard'))

    active_classroom = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    if not active_classroom:
        active_classroom = ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()
    
    if request.method == 'POST':
        if request.is_json:
            data = request.get_json()
            title = (data.get('title') or '').strip()
            body = (data.get('body') or '').strip()
            target = data.get('target')
        else:
            title = (request.form.get('title') or '').strip()
            body = (request.form.get('body') or '').strip()
            target = request.form.get('target') # "all" or user_id

        if not title and not body:
            flash('Judul atau isi notifikasi wajib diisi.')
            return redirect(url_for('manage_notifications'))
        if not title:
            title = 'Notifikasi'
        if not body:
            body = title
        
        if not current_user.role.can_manage_notifications:
            flash('Anda tidak punya izin kirim push notification.')
            return redirect(url_for('manage_notifications'))

        if target == 'all':
            send_multichannel_notification(title, body, sender_id=current_user.id, allow_whatsapp=True, classroom_id=active_classroom.id if active_classroom else None)
            flash('Notifikasi siaran berhasil dikirim!')
        else:
            send_push(title, body, user_id=int(target), sender_id=current_user.id, classroom_id=active_classroom.id if active_classroom else None)
            flash('Notifikasi terkirim ke pengguna.')
            
        log_activity("Kirim Notifikasi", f"Judul: {title}, Target: {target}")
        return redirect(url_for('manage_notifications'))

    history_query = NotificationHistory.query
    if active_classroom:
        history_query = history_query.filter(
            (NotificationHistory.classroom_id == active_classroom.id) | (NotificationHistory.classroom_id.is_(None))
        )
    history = history_query.order_by(NotificationHistory.sent_at.desc()).limit(80).all()
    users_query = User.query
    if active_classroom:
        users_query = users_query.filter(
            (User.classroom_id == active_classroom.id) |
            (User.student.has(Student.classroom_id == active_classroom.id))
        )
    users = users_query.order_by(User.full_name.is_(None), User.full_name.asc(), User.username.asc()).all()
    settings = {
        'waha_enabled': get_setting_value('waha_enabled', 'false'),
        'waha_base_url': get_setting_value('waha_base_url', ''),
        'waha_api_key_masked': ('*' * max(0, len(get_setting_value('waha_api_key', '')) - 4)) + get_setting_value('waha_api_key', '')[-4:],
        'waha_session': get_setting_value('waha_session', ''),
        'waha_group_chat_id': get_setting_value('waha_group_chat_id', ''),
        'waha_daily_time': get_setting_value('waha_daily_time', '18:00'),
        'notification_channel_default': get_setting_value('notification_channel_default', 'push'),
        'waha_schedule_template': get_setting_value(
            'waha_schedule_template',
            'Assalamualaikum dan selamat malam, tabe saudara dan saudari sekalian di grup ini, Jadwal Mata Kuliah {day_name}, {date_long}\n{schedule_lines}\n{deadline_section}(Sesuai jadwal dari pihak kampus)\n{extra_info_section}Sekian dan terimakasih'
        ),
        'waha_schedule_item_template': get_setting_value('waha_schedule_item_template', '{index}. MK {subject} mulai jam {time_range}'),
        'waha_schedule_deadline_item_template': get_setting_value('waha_schedule_deadline_item_template', '{index}. Deadline {subject}: {title} jam {deadline_time}'),
        'waha_schedule_extra_info': get_setting_value('waha_schedule_extra_info', ''),
        'waha_admin_header_enabled': get_setting_value('waha_admin_header_enabled', 'true'),
        'waha_admin_header_text': get_setting_value('waha_admin_header_text', '*[PESAN RESMI ADMIN FAMOUSBYTEE]*\n{title_block}Pesan ini dikirim dari sistem admin.\n'),
        'schedule_notify_on_create': get_setting_value('schedule_notify_on_create', 'true'),
        'schedule_notify_on_edit': get_setting_value('schedule_notify_on_edit', 'false'),
        'schedule_notify_on_delete': get_setting_value('schedule_notify_on_delete', 'true')
    }
    classrooms = ClassRoom.query.order_by(ClassRoom.name.asc()).all()
    return render_template('notifications.html', history=history, users=users, settings=settings, classrooms=classrooms, active_classroom=active_classroom)

@app.route('/notifications/waha/save-config', methods=['POST'])
@login_required
def save_waha_config():
    if not current_user.role.can_manage_whatsapp:
        flash('Akses ditolak.')
        return redirect(url_for('manage_notifications'))

    set_setting_value('waha_enabled', 'true' if request.form.get('waha_enabled') == 'on' else 'false')
    set_setting_value('waha_base_url', (request.form.get('waha_base_url') or '').strip(), 'Base URL server WAHA')
    new_api_key = (request.form.get('waha_api_key') or '').strip()
    if new_api_key:
        set_setting_value('waha_api_key', new_api_key, 'API key WAHA')
    set_setting_value('waha_session', (request.form.get('waha_session') or '').strip(), 'Nama session WAHA')
    set_setting_value('waha_group_chat_id', (request.form.get('waha_group_chat_id') or '').strip(), 'Chat ID grup WAHA')
    set_setting_value('waha_daily_time', (request.form.get('waha_daily_time') or '18:00').strip(), 'Jam ringkasan harian WAHA')
    set_setting_value('notification_channel_default', (request.form.get('notification_channel_default') or 'push').strip(), 'Channel default notifikasi')
    set_setting_value('waha_schedule_template', request.form.get('waha_schedule_template') or '', 'Template ringkasan jadwal WAHA')
    set_setting_value('waha_schedule_item_template', request.form.get('waha_schedule_item_template') or '', 'Template item jadwal WAHA')
    set_setting_value('waha_schedule_deadline_item_template', request.form.get('waha_schedule_deadline_item_template') or '', 'Template item deadline WAHA')
    set_setting_value('waha_schedule_extra_info', request.form.get('waha_schedule_extra_info') or '', 'Info tambahan tetap di ringkasan WAHA')
    set_setting_value('waha_admin_header_enabled', 'true' if request.form.get('waha_admin_header_enabled') == 'on' else 'false', 'Aktifkan header/pengenal admin di pesan WA')
    set_setting_value('waha_admin_header_text', request.form.get('waha_admin_header_text') or '', 'Template header admin untuk pesan WA')
    set_setting_value('schedule_notify_on_create', 'true' if request.form.get('schedule_notify_on_create') == 'on' else 'false', 'Kirim notifikasi WhatsApp saat jadwal baru dibuat')
    set_setting_value('schedule_notify_on_edit', 'true' if request.form.get('schedule_notify_on_edit') == 'on' else 'false', 'Kirim notifikasi WhatsApp saat jadwal diedit')
    set_setting_value('schedule_notify_on_delete', 'true' if request.form.get('schedule_notify_on_delete') == 'on' else 'false', 'Kirim notifikasi WhatsApp saat jadwal dihapus')
    db.session.commit()
    flash('Konfigurasi WAHA berhasil disimpan.')
    return redirect(url_for('manage_notifications'))

@app.route('/notifications/waha/sessions')
@login_required
def get_waha_sessions():
    if not current_user.role.can_manage_whatsapp:
        return jsonify({'error': 'Unauthorized'}), 403
    result = _waha_request('GET', '/api/sessions')
    if not result.get('ok'):
        return jsonify(result), 400

    raw_data = result.get('data') or []
    sessions = raw_data if isinstance(raw_data, list) else raw_data.get('sessions', [])
    normalized = []
    for item in sessions:
        if not isinstance(item, dict):
            continue
        normalized.append({
            'name': _normalize_waha_scalar(item.get('name') or item.get('session') or item.get('id') or '-'),
            'status': _normalize_waha_scalar(item.get('status') or item.get('state') or item.get('connectionStatus') or '-'),
            'me': _normalize_waha_scalar(item.get('me') or item.get('meId') or item.get('phone') or '-'),
            'engine': _normalize_waha_scalar(item.get('engine') or item.get('type') or '-')
        })
    return jsonify({'ok': True, 'items': normalized, 'count': len(normalized)})

@app.route('/notifications/waha/groups')
@login_required
def get_waha_groups():
    if not current_user.role.can_manage_whatsapp:
        return jsonify({'error': 'Unauthorized'}), 403
    session_name = get_setting_value('waha_session', '').strip()
    if not session_name:
        return jsonify({'ok': False, 'error': 'Session WAHA belum diatur'}), 400
    result = _waha_request('GET', f'/api/{session_name}/groups')
    if not result.get('ok'):
        return jsonify(result), 400

    raw_data = result.get('data') or []
    groups = raw_data if isinstance(raw_data, list) else raw_data.get('groups', [])
    normalized = []
    for item in groups:
        if not isinstance(item, dict):
            continue
        normalized.append({
            'name': _normalize_waha_scalar(item.get('name') or item.get('subject') or item.get('formattedTitle') or 'Tanpa Nama'),
            'chat_id': _normalize_waha_chat_id(item),
            'participants': item.get('participantsCount') or item.get('size') or len(item.get('participants', []) if isinstance(item.get('participants'), list) else []),
            'owner': _normalize_waha_scalar(item.get('owner') or item.get('ownerPn') or '-')
        })
    return jsonify({'ok': True, 'items': normalized, 'count': len(normalized), 'session': session_name})

@app.route('/notifications/waha/chats')
@login_required
def get_waha_chats():
    if not current_user.role.can_manage_whatsapp:
        return jsonify({'error': 'Unauthorized'}), 403
    session_name = get_setting_value('waha_session', '').strip()
    if not session_name:
        return jsonify({'ok': False, 'error': 'Session WAHA belum diatur'}), 400

    result = _waha_request('GET', f'/api/{session_name}/chats')
    if not result.get('ok'):
        return jsonify(result), 400

    raw_data = result.get('data') or []
    chats = raw_data if isinstance(raw_data, list) else raw_data.get('chats', [])
    normalized = []
    for item in chats:
        if not isinstance(item, dict):
            continue
        chat_id = _normalize_waha_chat_id(item)
        if not chat_id:
            continue
        chat_type = 'group' if '@g.us' in chat_id else 'personal'
        if chat_type != 'personal':
            continue
        normalized.append({
            'name': _normalize_waha_scalar(item.get('name') or item.get('pushName') or item.get('shortName') or item.get('formattedTitle') or chat_id),
            'chat_id': chat_id,
            'participants': item.get('participantsCount') or item.get('size') or 0,
            'owner': _normalize_waha_scalar(item.get('owner') or item.get('ownerPn') or '-'),
            'type': chat_type
        })
    return jsonify({'ok': True, 'items': normalized, 'count': len(normalized), 'session': session_name})

@app.route('/notifications/test-push', methods=['POST'])
@login_required
def test_push_notification():
    if not current_user.role.can_manage_notifications:
        return jsonify({'error': 'Unauthorized'}), 403
    send_push('Test Push Famousbytee', 'Ini adalah notifikasi uji dari backend.', user_id=current_user.id, sender_id=current_user.id, extra_data={'source': 'test_push'})
    return jsonify({'ok': True, 'message': 'Permintaan test push diproses'})

@app.route('/notifications/test-whatsapp', methods=['POST'])
@login_required
def test_whatsapp_notification():
    if not current_user.role.can_manage_whatsapp:
        return jsonify({'error': 'Unauthorized'}), 403
    payload = request.get_json(silent=True) or request.form or {}
    custom_chat_id = (payload.get('chat_id') or '').strip()
    custom_message = (payload.get('message') or 'Test pesan WAHA dari panel backend Famousbytee.').strip()
    result = send_whatsapp(custom_message, sender_id=current_user.id, title='Test WhatsApp', chat_id=custom_chat_id or None)
    return jsonify(result), (200 if result.get('ok') else 400)

@app.route('/webhooks/waha', methods=['POST'])
def waha_webhook():
    payload = request.get_json(silent=True)
    if payload is None:
        payload = request.form.to_dict(flat=True) if request.form else {}
    if not payload:
        raw_body = request.get_data(cache=False, as_text=True) or ''
        payload = {'raw_body': raw_body}

    event_data = _extract_waha_event(payload)
    body = (event_data.get('body') or '').strip()
    chat_id = (event_data.get('chat_id') or '').strip()
    print(f"WAHA webhook received: event={event_data.get('event', '')}, chat_id={chat_id or '-'}, body={body[:80] or '-'}")

    # Allow self messages if they are commands (for admin testing)
    if event_data.get('from_me') is True and not body.startswith('/'):
        print("WAHA webhook ignored. Reason: outgoing/self non-command message.")
        return jsonify({
            'ok': True,
            'accepted': True,
            'ignored': True,
            'reason': 'outgoing-message',
            'event': event_data.get('event', '')
        }), 200

    if not body.startswith('/') or not chat_id:
        print(f"WAHA webhook ignored. Payload preview: {json.dumps(payload, ensure_ascii=True)[:300]}")
        return jsonify({
            'ok': True,
            'accepted': True,
            'ignored': True,
            'reason': 'not-command-or-missing-chat',
            'event': event_data.get('event', '')
        }), 200

    if _is_duplicate_waha_command(event_data):
        print(f"WAHA webhook ignored. Reason: duplicate command for {chat_id or '-'} body={body[:40]}")
        return jsonify({
            'ok': True,
            'accepted': True,
            'ignored': True,
            'reason': 'duplicate-command',
            'event': event_data.get('event', ''),
            'command': body,
            'chat_id': chat_id
        }), 200

    response_text = _build_waha_command_response(body, sender_ref=event_data.get('sender_ref') or chat_id)
    if not response_text:
        return jsonify({
            'ok': True,
            'accepted': True,
            'ignored': True,
            'reason': 'empty-response',
            'command': body
        }), 200

    result = send_whatsapp(response_text, title=f"WA Bot {body.split()[0]}", chat_id=chat_id, force=True)
    print(f"WAHA webhook reply: target={chat_id or '-'}, sent={result.get('ok', False)}, error={result.get('error', '-') if not result.get('ok') else '-'}")
    return jsonify({
        'ok': True,
        'accepted': True,
        'command': body,
        'chat_id': chat_id,
        'reply_sent': result.get('ok', False),
        'result': result
    }), 200

@app.route('/notifications/clear')
@login_required
def clear_notification_history():
    if not current_user.role.can_manage_notifications:
        return redirect(url_for('dashboard'))
    
    try:
        NotificationHistory.query.delete()
        db.session.commit()
        log_activity("Hapus Riwayat Notifikasi", "Seluruh riwayat notifikasi dikosongkan")
        flash('Seluruh riwayat notifikasi telah dibersihkan.')
    except Exception as e:
        db.session.rollback()
        flash(f'Gagal membersihkan riwayat: {e}')
        
    return redirect(url_for('manage_notifications'))

@app.route('/leaderboard')
@login_required
def leaderboard():
    top_users = User.query.filter(User.points > 0).order_by(User.points.desc()).limit(20).all()
    return render_template('leaderboard.html', users=top_users)

@app.route('/sitemap.xml')

def sitemap():
    """Generates sitemap.xml dynamically."""
    pages = []
    # Static pages
    for rule in app.url_map.iter_rules():
        if "GET" in rule.methods and len(rule.arguments) == 0:


            pages.append([url_for(rule.endpoint, _external=True), datetime.now().date()])
    
    sitemap_xml = render_template('sitemap.xml', pages=pages)
    response = make_response(sitemap_xml)
    response.headers["Content-Type"] = "application/xml"
    return response

@app.route('/api/leaderboard', methods=['GET'])
def get_leaderboard():
    # Top 20 users by points
    top_users = User.query.filter(User.points > 0).order_by(User.points.desc()).limit(20).all()
    return jsonify([{
        "id": u.id,
        "full_name": (u.student.full_name if u.student and u.student.full_name else u.full_name) or u.username,
        "points": u.points or 0,
        "role": u.role.name,
        "nim": u.student.nim if u.student else "-"
    } for u in top_users])

@app.route('/api/leaderboard/<int:user_id>', methods=['GET'])
def get_leaderboard_detail(user_id):
    user = User.query.get_or_404(user_id)
    breakdown = calculate_user_points_breakdown(user)
    return jsonify({
        "id": user.id,
        "full_name": (user.student.full_name if user.student and user.student.full_name else user.full_name) or user.username,
        "username": user.username,
        "nim": user.student.nim if user.student else "-",
        "role": user.role.name if user.role else "-",
        "points": breakdown['total_points'],
        "breakdown": {
            "fund_points": breakdown['fund_points'],
            "gallery_points": breakdown['gallery_points'],
            "arrears_penalty": breakdown['arrears_penalty'],
            "total_paid": breakdown['total_paid'],
            "target_payment": breakdown['target_payment'],
            "arrears": breakdown['arrears'],
            "published_photos": breakdown['published_photos'],
        }
    })

# Inisialisasi database saat aplikasi dinyalakan (WSGI-safe)
try:
    init_db()
    app.logger.info('Database initialization completed successfully.')
except Exception as _init_err:
    app.logger.critical(f'FATAL: Database initialization failed: {_init_err}\n'
                        f'The application may not function correctly. '
                        f'Check your database configuration and permissions.')

# Mod_wsgi akan mencari objek "application" secara langsung dari file ini.
# Gunakan "python -m flask run" atau jalankan "wsgi.py" untuk pengembangan lokal.
app.logger.info('Famousbytee application loaded and ready to serve requests.')
