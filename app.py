import logging
from logging.handlers import RotatingFileHandler
from flask import Flask, render_template, redirect, url_for, request, flash, session, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from flask_migrate import Migrate
from models import db, User, Role, ClassRoom, Student, Schedule, SchedulePreset, ScheduleTemplate, ScheduleTemplateItem, Announcement, BatchFund, FundPeriod, ActivityLog, SystemSetting, GalleryAlbum, GalleryPhoto, PhotoComment, AnnouncementRead, Assignment, NotificationHistory, ClassroomNotificationConfig, WhatsAppBot, ClassroomWhatsAppBinding, NewsCategory, NewsArticle, normalize_member_status
import os
import json
import re
from PIL import Image, ImageOps
import csv
from io import StringIO
from flask import send_from_directory, make_response
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash
import secrets
import hmac
from collections import defaultdict, deque
from threading import Lock
from markupsafe import escape
from security_utils import hash_password, is_password_hash, verify_password
from datetime import datetime, timedelta
from urllib import request as urllib_request, error as urllib_error
from sqlalchemy.exc import OperationalError

import firebase_admin
from firebase_admin import credentials, messaging

from config import Config
from flask_jwt_extended import JWTManager, create_access_token
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

_LOGIN_ATTEMPTS = defaultdict(deque)
_LOGIN_ATTEMPTS_LOCK = Lock()
_LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_MAX_ATTEMPTS = 10


def _csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token


def _login_rate_limited(client_ip):
    now = _time.time()
    with _LOGIN_ATTEMPTS_LOCK:
        attempts = _LOGIN_ATTEMPTS[client_ip]
        while attempts and now - attempts[0] > _LOGIN_WINDOW_SECONDS:
            attempts.popleft()
        if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
            return True
        attempts.append(now)
    return False


def _clear_login_attempts(client_ip):
    with _LOGIN_ATTEMPTS_LOCK:
        _LOGIN_ATTEMPTS.pop(client_ip, None)

_SIDOBE_RECENT_COMMANDS = {}
_SIDOBE_COMMAND_DEDUP_WINDOW_SECONDS = 3  # Reduced from 12 to allow faster re-commands
_WAHA_RECENT_COMMANDS = _SIDOBE_RECENT_COMMANDS  # legacy alias retained for compatibility
_WAHA_COMMAND_DEDUP_WINDOW_SECONDS = _SIDOBE_COMMAND_DEDUP_WINDOW_SECONDS  # legacy alias retained for compatibility

# ============================================================
# REQUEST ACCESS LOGGING (captures access even without Apache logs)
# ============================================================
import time as _time

@app.before_request
def _log_request_start():
    request._start_time = _time.time()

    if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        if request.path.startswith(('/api/', '/webhooks/')):
            return None
        expected = session.get('_csrf_token', '')
        supplied = request.form.get('_csrf_token', '') or request.headers.get('X-CSRF-Token', '')
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            abort(400, description='Permintaan keamanan tidak valid. Muat ulang halaman dan coba lagi.')

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
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Cross-Origin-Opener-Policy', 'same-origin')
    response.headers.setdefault(
        'Content-Security-Policy',
        "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; object-src 'none'; "
        "form-action 'self'; img-src 'self' data: https:; font-src 'self' https://fonts.gstatic.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net https://unpkg.com; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com https://cdnjs.cloudflare.com; "
        "connect-src 'self' https://unpkg.com https://cdn.jsdelivr.net https://fonts.googleapis.com https://fonts.gstatic.com"
    )
    if request.is_secure:
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
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
with app.app_context():
    try:
        migrate_legacy_sidobe_settings()
    except Exception as _sidobe_migrate_err:
        app.logger.warning(f'Si Dobe migration check skipped: {_sidobe_migrate_err}')

def get_setting_value(key, default=''):
    setting = SystemSetting.query.filter_by(key=key).first()
    if not setting:
        return default
    return setting.value if setting.value is not None else default


def get_sidobe_setting_value(key, default=''):
    value = get_setting_value(f'sidobe_{key}', None)
    if value not in (None, ''):
        return value
    return default

def migrate_legacy_sidobe_settings():
    mapping = [
        ('enabled', 'false'),
        ('base_url', ''),
        ('api_key', ''),
        ('session', ''),
        ('group_chat_id', ''),
        ('daily_time', '18:00'),
        ('last_daily_summary_date', ''),
        ('schedule_template', 'Assalamualaikum dan selamat malam, tabe saudara dan saudari sekalian di grup ini, Jadwal Mata Kuliah {day_name}, {date_long}\n{schedule_lines}\n{deadline_section}(Sesuai jadwal dari pihak kampus)\n{extra_info_section}Sekian dan terimakasih'),
        ('schedule_item_template', '{index}. MK {subject} mulai jam {time_range}'),
        ('schedule_deadline_item_template', '{index}. Deadline {subject}: {title} jam {deadline_time}'),
        ('schedule_extra_info', ''),
        ('admin_header_enabled', 'true'),
        ('admin_header_text', '*[PESAN RESMI ADMIN FAMOUSBYTEE]*\n{title_block}Pesan ini dikirim dari sistem admin.\n'),
    ]
    migrated = False
    for key, default in mapping:
        sidobe_key = f'sidobe_{key}'
        legacy_key = f'waha_{key}'
        sidobe_setting = SystemSetting.query.filter_by(key=sidobe_key).first()
        legacy_setting = SystemSetting.query.filter_by(key=legacy_key).first()
        if sidobe_setting and sidobe_setting.value not in (None, ''):
            continue
        if legacy_setting and legacy_setting.value not in (None, ''):
            if sidobe_setting:
                sidobe_setting.value = legacy_setting.value
            else:
                db.session.add(SystemSetting(key=sidobe_key, value=legacy_setting.value, description=f'Migrasi {sidobe_key} dari legacy Si Dobe'))
            migrated = True
        elif not sidobe_setting:
            db.session.add(SystemSetting(key=sidobe_key, value=default, description=f'Default {sidobe_key}'))
    if migrated:
        db.session.commit()
        marker = SystemSetting.query.filter_by(key='notifications_legacy_migrated').first()
        if marker:
            marker.value = 'true'
        else:
            db.session.add(SystemSetting(key='notifications_legacy_migrated', value='true', description='Penanda migrasi konfigurasi notifikasi legacy ke model multi-kelas'))
        db.session.commit()

def set_setting_value(key, value, description=None):
    setting = SystemSetting.query.filter_by(key=key).first()
    if setting:
        setting.value = value
        if description:
            setting.description = description
    else:
        db.session.add(SystemSetting(key=key, value=value, description=description))


class _TemplateSettings(dict):
    def __getattr__(self, item):
        return self.get(item)


def _build_template_settings():
    defaults = {
        'web_title': 'Portal Kelas Famousbytee.b',
        'web_desc': 'Portal Resmi Kelas Famousbytee.b',
        'seo_keywords': 'famousbytee, portal, kelas',
        'web_logo': 'monitor',
        'favicon_url': '/static/favicon.ico',
        'web_logo_path': '',
        'favicon_path': '',
        'social_ig': '#',
        'social_wa': '#',
        'fund_daily_rate': '1000',
    }
    try:
        values = {s.key: (s.value or '') for s in SystemSetting.query.all()}
    except Exception:
        values = {}
    merged = {**defaults, **values}
    merged['logo_display_path'] = merged.get('web_logo_path') or ''
    merged['favicon_display_url'] = (
        merged.get('favicon_path')
        or merged.get('favicon_url')
        or '/static/favicon.ico'
    )
    return _TemplateSettings(merged)


def _get_site_settings():
    return _build_template_settings()


@app.context_processor
def inject_global_template_data():
    return {
        'site_settings': _build_template_settings(),
        'datetime': datetime,
        'csrf_token': _csrf_token,
    }

def _normalize_sidobe_scalar(value):
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        if 'serialized' in value:
            return _normalize_sidobe_scalar(value.get('serialized'))
        if '_serialized' in value:
            return _normalize_sidobe_scalar(value.get('_serialized'))
        if 'id' in value and isinstance(value.get('id'), str):
            return value.get('id')
        user = value.get('user') or value.get('number') or value.get('phone')
        server = value.get('server')
        if user and server:
            return f"{user}@{server}"
        if 'pushname' in value:
            return _normalize_sidobe_scalar(value.get('pushname'))
        if 'name' in value:
            return _normalize_sidobe_scalar(value.get('name'))
        if 'wid' in value:
            return _normalize_sidobe_scalar(value.get('wid'))
        return json.dumps(value, ensure_ascii=True)[:120]
    if isinstance(value, list):
        return ', '.join(_normalize_sidobe_scalar(v) for v in value[:5])
    return str(value)

def _normalize_sidobe_chat_id(item):
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
        normalized = _normalize_sidobe_chat_identifier(_normalize_sidobe_scalar(candidate).strip())
        if normalized:
            return normalized
    return ''

def _normalize_sidobe_chat_identifier(value):
    value = str(value or '').strip()
    if not value:
        return ''
    if value.endswith('@s.whatsapp.net'):
        return value.replace('@s.whatsapp.net', '@c.us')
    return value

def get_fund_periods(classroom_id=None):
    query = FundPeriod.query.filter_by(is_active=True)
    try:
        if classroom_id:
            query = query.filter(
                (FundPeriod.classroom_id == classroom_id) |
                (FundPeriod.classroom_id.is_(None))
            )
        return query.order_by(FundPeriod.start_date.asc(), FundPeriod.id.asc()).all()
    except OperationalError as exc:
        message = str(exc).lower()
        if 'fund_period.classroom_id' not in message and 'unknown column' not in message:
            raise
        app.logger.warning(
            "fund_period.classroom_id belum tersedia di database. Menggunakan fallback periode global sementara."
        )
        return FundPeriod.query.filter_by(is_active=True).order_by(
            FundPeriod.start_date.asc(),
            FundPeriod.id.asc()
        ).all()

def _count_weekdays_between(start_date, end_date):
    total = 0
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:
            total += 1
        current += timedelta(days=1)
    return total


def _default_classroom():
    return ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()


def _active_classroom_for_user(user=None):
    user = user or current_user
    classroom = getattr(user, 'classroom', None) or (user.student.classroom if getattr(user, 'student', None) else None)
    return classroom or _default_classroom()


def _has_any_classroom_scope(*flags):
    role = current_user.role
    return bool(
        role.can_manage_roles or
        any(getattr(role, flag, False) for flag in flags)
    )


def _web_allowed_classrooms(*flags):
    active_classroom = _active_classroom_for_user()
    if _has_any_classroom_scope(*flags):
        return ClassRoom.query.order_by(ClassRoom.name.asc()).all()
    return [active_classroom] if active_classroom else []


def _requested_classroom(form_key='classroom_id', fallback=None, *flags):
    fallback = fallback or _active_classroom_for_user()
    requested_id = request.form.get(form_key) or request.args.get(form_key)
    allowed = _web_allowed_classrooms(*flags)
    allowed_ids = {item.id for item in allowed}
    if requested_id not in (None, ''):
        try:
            classroom_id = int(requested_id)
        except Exception:
            return fallback
        if classroom_id in allowed_ids:
            classroom = ClassRoom.query.get(classroom_id)
            if classroom:
                return classroom
    return fallback


def _can_manage_gallery_content(role=None):
    role = role or current_user.role
    return bool(
        getattr(role, 'can_manage_roles', False) or
        getattr(role, 'can_manage_gallery', False) or
        getattr(role, 'name', '') in ['Admin', 'Pengurus']
    )


def _gallery_allowed_classrooms():
    return _web_allowed_classrooms(
        'can_manage_gallery_multi_class',
        'can_view_all_classrooms',
        'can_access_multi_classroom',
    )


def _requested_gallery_classroom():
    return _requested_classroom(
        'classroom_id',
        _active_classroom_for_user(),
        'can_manage_gallery_multi_class',
        'can_view_all_classrooms',
        'can_access_multi_classroom',
    )


def _is_gallery_photo_in_allowed_scope(photo, classroom):
    if not classroom:
        return True
    if photo.classroom_id == classroom.id:
        return True
    return bool(photo.classroom_id is None and _default_classroom() and classroom.id == _default_classroom().id)


def _fund_allowed_classrooms():
    return _web_allowed_classrooms(
        'can_view_all_classrooms',
        'can_access_multi_classroom',
        'can_switch_classroom_context',
        'can_view_classroom_reports',
    )


def _requested_fund_classroom():
    return _requested_classroom(
        'classroom_id',
        _active_classroom_for_user(),
        'can_view_all_classrooms',
        'can_access_multi_classroom',
        'can_switch_classroom_context',
        'can_view_classroom_reports',
    )


def _apply_fund_classroom_filter(query, classroom, include_legacy_default=True):
    if not classroom:
        return query

    if include_legacy_default and _default_classroom() and classroom.id == _default_classroom().id:
        return query.filter(
            (BatchFund.classroom_id == classroom.id) |
            (BatchFund.classroom_id.is_(None))
        )
    return query.filter(BatchFund.classroom_id == classroom.id)


def _apply_fund_classroom_sum_filter(query, classroom, include_legacy_default=True):
    if not classroom:
        return query
    if include_legacy_default and _default_classroom() and classroom.id == _default_classroom().id:
        return query.filter(
            (BatchFund.classroom_id == classroom.id) |
            (BatchFund.classroom_id.is_(None))
        )
    return query.filter(BatchFund.classroom_id == classroom.id)


def _is_fund_in_allowed_scope(fund, classroom):
    if not classroom:
        return True
    if fund.classroom_id == classroom.id:
        return True
    return bool(fund.classroom_id is None and _default_classroom() and classroom.id == _default_classroom().id)

def _sidobe_headers(api_key_override=None, auth_mode='x-api-key'):
    api_key = (api_key_override if api_key_override is not None else get_sidobe_setting_value('api_key', '')).strip()
    headers = {'Content-Type': 'application/json'}
    if api_key:
        if auth_mode == 'bearer':
            headers['Authorization'] = f'Bearer {api_key}'
        else:
            headers['X-Api-Key'] = api_key
    return headers

def _sidobe_request(method, path, payload=None, base_url_override=None, api_key_override=None, auth_mode='x-api-key'):
    base_url = (base_url_override if base_url_override is not None else get_sidobe_setting_value('base_url', '')).strip().rstrip('/')
    if not base_url:
        return {'ok': False, 'error': 'Si Dobe base URL belum diatur'}
    if not base_url.startswith(('http://', 'https://')):
        return {'ok': False, 'error': 'Si Dobe base URL harus diawali http:// atau https://'}

    url = f"{base_url}{path}"
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib_request.Request(url, data=data, headers=_sidobe_headers(api_key_override, auth_mode=auth_mode), method=method)

    try:
        with urllib_request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode('utf-8') if resp.length != 0 else ''
            return {'ok': True, 'status': resp.status, 'data': json.loads(raw) if raw else None}
    except urllib_error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='ignore')
        detail_text = (detail or '').strip()
        if e.code in (401, 403):
            return {'ok': False, 'error': f'Si Dobe menolak akses (HTTP {e.code}). Periksa API key/session login.'}
        if e.code == 404:
            return {'ok': False, 'error': 'Si Dobe endpoint tidak ditemukan. Cek base URL dan versinya.'}
        if e.code >= 500:
            return {'ok': False, 'error': f'Si Dobe error server (HTTP {e.code}). Cek worker/session di sana.'}
        return {'ok': False, 'error': f'HTTP {e.code}: {detail_text[:180]}' if detail_text else f'HTTP {e.code}: Permintaan Si Dobe gagal'}
    except urllib_error.URLError as e:
        reason = getattr(e, 'reason', None)
        return {'ok': False, 'error': f'Gagal konek ke Si Dobe: {reason}' if reason else 'Gagal konek ke Si Dobe'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}

def _sidobe_request_any(method, paths, payload=None, base_url_override=None, api_key_override=None):
    last_error = None
    for path in paths:
        result = _sidobe_request(method, path, payload=payload, base_url_override=base_url_override, api_key_override=api_key_override)
        if result.get('ok'):
            result['path'] = path
            return result
        last_error = result.get('error', 'Permintaan Si Dobe gagal')
    return {'ok': False, 'error': last_error or 'Permintaan Si Dobe gagal'}

def _sidobe_request_with_auth_fallback(method, path, payload=None, base_url_override=None, api_key_override=None):
    first = _sidobe_request(method, path, payload=payload, base_url_override=base_url_override, api_key_override=api_key_override, auth_mode='x-api-key')
    if first.get('ok'):
        return first
    error_text = (first.get('error') or '').lower()
    if 'http 401' not in error_text and 'http 403' not in error_text:
        return first
    second = _sidobe_request(method, path, payload=payload, base_url_override=base_url_override, api_key_override=api_key_override, auth_mode='bearer')
    if second.get('ok'):
        second['auth_mode'] = 'bearer'
        return second
    if second.get('error') and second.get('error') != first.get('error'):
        second['error'] = f"{first.get('error')} | Fallback Bearer juga gagal: {second.get('error')}"
    return second

def _apply_whatsapp_admin_header(text, title=None):
    text = (text or '').strip()
    if not text:
        return text

    header_enabled = get_sidobe_setting_value('admin_header_enabled', 'true').strip().lower() == 'true'
    if not header_enabled:
        return text

    header_template = get_sidobe_setting_value(
        'admin_header_text',
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
    mode = get_sidobe_setting_value('notification_channel_default', get_setting_value('notification_channel_default', 'push')).strip().lower()
    if mode not in {'push', 'whatsapp', 'both'}:
        return 'push'
    return mode

def get_classroom_notification_policy(classroom_id):
    if not classroom_id:
        return None
    return ClassroomNotificationConfig.query.filter_by(classroom_id=classroom_id).first()

def get_classroom_whatsapp_binding(classroom_id):
    if not classroom_id:
        return None
    return ClassroomWhatsAppBinding.query.filter_by(classroom_id=classroom_id).first()


def get_classroom_sidobe_binding(classroom_id):
    return get_classroom_whatsapp_binding(classroom_id)

def resolve_whatsapp_bot_for_classroom(classroom_id):
    binding = get_classroom_sidobe_binding(classroom_id)
    if not binding or not binding.bot or not binding.bot.is_active:
        return None, binding
    return binding.bot, binding


def resolve_sidobe_bot_for_classroom(classroom_id):
    return resolve_whatsapp_bot_for_classroom(classroom_id)

def get_classroom_notification_channel_mode(classroom_id):
    policy = get_classroom_notification_policy(classroom_id)
    mode = (policy.default_channel if policy else 'push') or 'push'
    mode = mode.strip().lower()
    if mode not in {'push', 'whatsapp', 'both'}:
        return 'push'
    return mode

def _is_notification_category_enabled(policy, category):
    if not policy:
        return category != 'whatsapp_binding'
    mapping = {
        'announcement': policy.announcement_enabled,
        'assignment': policy.assignment_enabled,
        'schedule': policy.schedule_enabled,
        'finance': policy.finance_enabled,
        'emergency': policy.emergency_enabled,
    }
    return mapping.get(category, True)

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

def _build_schedule_summary_message(target_date=None, classroom_id=None):
    target_date = target_date or (datetime.now().date() + timedelta(days=1))
    day_name = _get_indo_day_name(target_date)
    date_long = _format_indo_date(target_date)

    schedules_query = Schedule.query.filter_by(day=day_name)
    assignments_query = Assignment.query.order_by(Assignment.deadline.asc())
    if classroom_id:
        schedules_query = schedules_query.filter_by(classroom_id=classroom_id)
        assignments_query = assignments_query.filter(
            (Assignment.classroom_id == classroom_id) | (Assignment.classroom_id.is_(None))
        )
    schedules = schedules_query.order_by(Schedule.time_start.asc()).all()
    assignments = assignments_query.all()
    due_items = [a for a in assignments if a.deadline.date() == target_date]

    if not schedules and not due_items:
        return ''

    item_template = get_sidobe_setting_value(
        'schedule_item_template',
        '{index}. MK {subject} mulai jam {time_range}'
    )
    deadline_item_template = get_sidobe_setting_value(
        'schedule_deadline_item_template',
        '{index}. Deadline {subject}: {title} jam {deadline_time}'
    )
    full_template = get_sidobe_setting_value(
        'schedule_template',
        'Assalamualaikum dan selamat malam, tabe saudara dan saudari sekalian di grup ini, Jadwal Mata Kuliah {day_name}, {date_long}\n'
        '{schedule_lines}\n'
        '{deadline_section}'
        '(Sesuai jadwal dari pihak kampus)\n'
        '{extra_info_section}'
        'Sekian dan terimakasih'
    )
    extra_info = _normalize_multiline_text(get_sidobe_setting_value('schedule_extra_info', ''))

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

def _build_tomorrow_summary_message(classroom_id=None):
    return _build_schedule_summary_message(datetime.now().date() + timedelta(days=1), classroom_id=classroom_id)

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

def _extract_sidobe_event(payload):
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
    me_id = _normalize_sidobe_chat_identifier(_normalize_sidobe_scalar((payload.get('me') or {}).get('id')).strip())
    message_id_hint = ''

    for candidate in candidates:
        if not body:
            value = candidate.get('body') or candidate.get('text') or candidate.get('message')
            if isinstance(value, str):
                body = value.strip()
        if not message_id_hint:
            message_id_hint = _normalize_sidobe_scalar(candidate.get('id')).strip()
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
                normalized_chat_id = _normalize_sidobe_chat_identifier(_normalize_sidobe_scalar(possible_chat_id).strip())
                if normalized_chat_id and ('@c.us' in normalized_chat_id or '@g.us' in normalized_chat_id or '@newsletter' in normalized_chat_id or '@lid' in normalized_chat_id):
                    chat_id = normalized_chat_id
                    break
            if not chat_id:
                normalized_nested_chat_id = _normalize_sidobe_chat_id(candidate).strip()
                if normalized_nested_chat_id and ('@c.us' in normalized_nested_chat_id or '@g.us' in normalized_nested_chat_id or '@newsletter' in normalized_nested_chat_id or '@lid' in normalized_nested_chat_id):
                    chat_id = normalized_nested_chat_id
        if not outgoing_chat_id:
            outgoing_candidates = [
                candidate.get('to'),
                candidate.get('from')
            ]
            for outgoing_candidate in outgoing_candidates:
                possible_outgoing = _normalize_sidobe_chat_identifier(_normalize_sidobe_scalar(outgoing_candidate).strip())
                if possible_outgoing and possible_outgoing != me_id and ('@c.us' in possible_outgoing or '@g.us' in possible_outgoing or '@newsletter' in possible_outgoing or '@lid' in possible_outgoing):
                    outgoing_chat_id = possible_outgoing
                    break
        if not outgoing_chat_id and message_id_hint.startswith('true_'):
            parts = message_id_hint.split('_')
            if len(parts) >= 2:
                hinted_chat_id = _normalize_sidobe_chat_identifier(parts[1])
                if hinted_chat_id and hinted_chat_id != me_id and ('@c.us' in hinted_chat_id or '@g.us' in hinted_chat_id or '@newsletter' in hinted_chat_id or '@lid' in hinted_chat_id):
                    outgoing_chat_id = hinted_chat_id
        if not outgoing_chat_id:
            possible_outgoing = _normalize_sidobe_chat_identifier(_normalize_sidobe_scalar(candidate.get('to')).strip())
            if possible_outgoing and ('@c.us' in possible_outgoing or '@g.us' in possible_outgoing or '@newsletter' in possible_outgoing or '@lid' in possible_outgoing):
                outgoing_chat_id = possible_outgoing
        if not sender_ref:
            sender_ref = (
                _normalize_sidobe_chat_identifier(_normalize_sidobe_scalar(candidate.get('participant')).strip())
                or _normalize_sidobe_chat_identifier(_normalize_sidobe_scalar(candidate.get('author')).strip())
                or _normalize_sidobe_chat_identifier(_normalize_sidobe_scalar(candidate.get('participant')).strip())
                or _normalize_sidobe_chat_identifier(_normalize_sidobe_scalar(candidate.get('sender')).strip())
                or _normalize_sidobe_chat_identifier(_normalize_sidobe_scalar(candidate.get('from')).strip())
                or _normalize_sidobe_chat_identifier(_normalize_sidobe_scalar(candidate.get('to')).strip())
            )
        if candidate.get('fromMe') is True:
            from_me = True

    if from_me and outgoing_chat_id:
        chat_id = outgoing_chat_id
    elif chat_id == me_id and outgoing_chat_id:
        chat_id = outgoing_chat_id
    elif not chat_id and from_me:
        for candidate in candidates:
            fallback_to = _normalize_sidobe_chat_identifier(_normalize_sidobe_scalar(candidate.get('to')).strip())
            if fallback_to and ('@c.us' in fallback_to or '@g.us' in fallback_to or '@newsletter' in fallback_to or '@lid' in fallback_to):
                chat_id = fallback_to
                break

    return {
        'body': body,
        'chat_id': chat_id,
        'sender_ref': sender_ref,
        'from_me': from_me,
        'event': _normalize_sidobe_scalar(payload.get('event') or payload.get('eventName') or ''),
        'message_id': message_id_hint or _normalize_sidobe_scalar(payload.get('id')).strip()
    }


def _is_duplicate_sidobe_command(event_data):
    now = datetime.now()
    expired_keys = [
        key for key, expires_at in _SIDOBE_RECENT_COMMANDS.items()
        if expires_at <= now
    ]
    for key in expired_keys:
        _SIDOBE_RECENT_COMMANDS.pop(key, None)

    body = (event_data.get('body') or '').strip().lower()
    chat_id = (event_data.get('chat_id') or '').strip()
    message_id = (event_data.get('message_id') or '').strip()
    event_name = (event_data.get('event') or '').strip().lower()

    if not body or not chat_id or not event_name.startswith('message'):
        return False

    if message_id:
        message_key = f"id:{message_id}"
        if message_key in _SIDOBE_RECENT_COMMANDS:
            return True
        _SIDOBE_RECENT_COMMANDS[message_key] = now + timedelta(
            seconds=_SIDOBE_COMMAND_DEDUP_WINDOW_SECONDS
        )

    command_key = f"cmd:{chat_id}:{body}"
    if command_key in _SIDOBE_RECENT_COMMANDS:
        return True

    _SIDOBE_RECENT_COMMANDS[command_key] = now + timedelta(
        seconds=_SIDOBE_COMMAND_DEDUP_WINDOW_SECONDS
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
            "Data personal belum bisa dicocokkan ke akun. Isi nomor telepon user di backend bila ingin balasan personal ikut tampil."
        ])

    return "\n".join(lines)

def _build_tunggakan_command_response(command_text, sender_ref=''):
    """Build response for /tunggakan command - uses same logic as Flutter (cumulative)"""
    default_class = ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()
    class_id = default_class.id if default_class else None
    
    target_payment = get_fund_target(classroom_id=class_id)
    
    # Get all active students
    from models import Student
    all_students = Student.query.filter(Student.status == 'Aktif')
    if class_id:
        all_students = all_students.filter_by(classroom_id=class_id)
    all_students = all_students.all()
    
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
    default_class = ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()
    class_id = default_class.id if default_class else None
    
    target_payment = get_fund_target(classroom_id=class_id)
    
    # Get all active students
    from models import Student
    all_students = Student.query.filter(Student.status == 'Aktif')
    if class_id:
        all_students = all_students.filter_by(classroom_id=class_id)
    all_students = all_students.all()
    
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

def _build_sidobe_command_response(command_text, sender_ref=''):
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


def _log_notification_history(title, body, user_id, sender_id, status, channel='push', classroom_id=None, category=None, delivery_mode=None, bot_id=None, chat_id=None):
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
            category=category,
            delivery_mode=delivery_mode,
            bot_id=bot_id,
            chat_id=chat_id,
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

def send_push(title, body, user_id=None, sender_id=None, extra_data=None, classroom_id=None, category=None, delivery_mode='policy_push'):
    """Sends push notification to a specific user or everyone."""
    title = (title or '').strip()
    body = (body or '').strip()
    if not title and not body:
        _log_notification_history("Notifikasi dibatalkan", "Judul dan isi kosong.", user_id, sender_id, "Skipped (Empty)", channel='push', classroom_id=classroom_id, category=category, delivery_mode=delivery_mode)
        return
    if not title:
        title = "Notifikasi"
    if not body:
        body = title

    if not _initialize_firebase():
        _log_notification_history(title, body, user_id, sender_id, "Failed (Config)", channel='push', classroom_id=classroom_id, category=category, delivery_mode=delivery_mode)
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
    
    _log_notification_history(title, body, user_id, sender_id, status, channel='push', classroom_id=classroom_id, category=category, delivery_mode=delivery_mode)

def send_whatsapp(text, sender_id=None, title=None, chat_id=None, force=False, classroom_id=None, category=None, delivery_mode='policy_whatsapp'):
    text = (text or '').strip()
    if not text:
        _log_notification_history(title or "Si Dobe dibatalkan", "Pesan Si Dobe kosong.", None, sender_id, "Skipped (Empty)", channel='whatsapp', classroom_id=classroom_id, category=category, delivery_mode=delivery_mode, chat_id=chat_id)
        return {'ok': False, 'error': 'Pesan Si Dobe kosong'}
    text = _apply_whatsapp_admin_header(text, title=title)

    target_classroom_id = classroom_id or _resolve_notification_classroom_id(sender_id=sender_id)
    policy = get_classroom_notification_policy(target_classroom_id)
    if not force:
        if not policy or not policy.whatsapp_enabled:
            _log_notification_history(title or "Si Dobe nonaktif", text, None, sender_id, "Disabled", channel='whatsapp', classroom_id=target_classroom_id, category=category, delivery_mode=delivery_mode, chat_id=chat_id)
            return {'ok': False, 'error': 'Si Dobe untuk kelas ini belum aktif'}
        if category and not _is_notification_category_enabled(policy, category):
            _log_notification_history(title or "Si Dobe diblokir", text, None, sender_id, "Blocked by policy", channel='whatsapp', classroom_id=target_classroom_id, category=category, delivery_mode=delivery_mode, chat_id=chat_id)
            return {'ok': False, 'error': 'Kategori notifikasi Si Dobe tidak aktif'}

    bot = None
    binding = None
    session_name = ''
    target_chat = (chat_id or '').strip()
    if force and target_chat:
        session_name = get_sidobe_setting_value('session', '').strip()
    else:
        bot, binding = resolve_sidobe_bot_for_classroom(target_classroom_id)
        session_name = bot.session_name.strip() if bot and bot.session_name else ''
        if not target_chat and binding:
            target_chat = (binding.chat_id or '').strip()

    if not session_name or not target_chat:
        _log_notification_history(title or "Si Dobe gagal", text, None, sender_id, "Missing session/chat", channel='whatsapp', classroom_id=target_classroom_id, category=category, delivery_mode=delivery_mode, bot_id=bot.id if bot else None, chat_id=target_chat or chat_id)
        return {'ok': False, 'error': 'Bot atau target chat kelas belum diatur'}

    payload = {'session': session_name, 'chatId': target_chat, 'text': text}
    result = _sidobe_request_with_auth_fallback('POST', '/api/sendText', payload, base_url_override=bot.base_url if bot and bot.base_url else None)
    status = 'Success' if result['ok'] else f"Failed: {result['error'][:80]}"
    _log_notification_history(title or "Si Dobe", text, None, sender_id, status, channel='whatsapp', classroom_id=target_classroom_id, category=category, delivery_mode=delivery_mode, bot_id=bot.id if bot else None, chat_id=target_chat)
    return result


def send_sidobe(text, sender_id=None, title=None, chat_id=None, force=False, classroom_id=None, category=None, delivery_mode='policy_sidobe'):
    return send_whatsapp(
        text,
        sender_id=sender_id,
        title=title,
        chat_id=chat_id,
        force=force,
        classroom_id=classroom_id,
        category=category,
        delivery_mode=delivery_mode,
    )

def send_multichannel_notification(title, body, user_id=None, sender_id=None, allow_whatsapp=False, whatsapp_text=None, extra_data=None, classroom_id=None, category=None):
    mode = get_classroom_notification_channel_mode(classroom_id)
    results = {}
    policy = get_classroom_notification_policy(classroom_id)
    if policy and category and not _is_notification_category_enabled(policy, category):
        return {'blocked': True, 'reason': 'category-disabled'}

    if mode in {'push', 'both'} and (not policy or policy.push_enabled):
        send_push(title, body, user_id=user_id, sender_id=sender_id, extra_data=extra_data, classroom_id=classroom_id, category=category, delivery_mode=f'policy_{mode}')
        results['push'] = True

    if allow_whatsapp and mode in {'whatsapp', 'both'}:
        wa_text = whatsapp_text or f"{title}\n{body}".strip()
        results['whatsapp'] = send_whatsapp(wa_text, sender_id=sender_id, title=title, classroom_id=classroom_id, category=category, delivery_mode=f'policy_{mode}')

    return results


def send_sidobe_multichannel(title, body, user_id=None, sender_id=None, allow_sidobe=False, sidobe_text=None, extra_data=None, classroom_id=None, category=None):
    return send_multichannel_notification(
        title,
        body,
        user_id=user_id,
        sender_id=sender_id,
        allow_whatsapp=allow_sidobe,
        whatsapp_text=sidobe_text,
        extra_data=extra_data,
        classroom_id=classroom_id,
        category=category,
    )


def send_sidobe_notification(title, body, user_id=None, sender_id=None, allow_sidobe=False, sidobe_text=None, extra_data=None, classroom_id=None, category=None):
    return send_sidobe_multichannel(
        title,
        body,
        user_id=user_id,
        sender_id=sender_id,
        allow_sidobe=allow_sidobe,
        sidobe_text=sidobe_text,
        extra_data=extra_data,
        classroom_id=classroom_id,
        category=category,
    )

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
            send_push("Pengingat Kuliah", f"Kelas {c.subject} akan dimulai dalam 15 menit di {c.room}.", classroom_id=c.classroom_id, category='schedule')

        # 2. Assignment Deadline (H-1) - check once an hour at minute 0
        if now.minute == 0:
            tomorrow = (now + timedelta(days=1)).date()
            assignments = Assignment.query.all()
            for a in assignments:
                if a.deadline.date() == tomorrow:
                    send_push("Deadline Tugas Besok!", f"Jangan lupa tugas {a.subject}: {a.title} dikumpulkan besok.", classroom_id=a.classroom_id, category='assignment')

        # 3. Weekly Fund Reminder (Every Monday at 08:00)
        if now.strftime('%A') == 'Monday' and now.hour == 8 and now.minute == 0:
            for classroom in ClassRoom.query.order_by(ClassRoom.id.asc()).all():
                send_push("Tagihan Kas Mingguan", "Selamat pagi! Jangan lupa bayar kas minggu ini ya teman-teman.", classroom_id=classroom.id, category='finance')

        # 4. Daily Si Dobe Summary
        summary_time = get_sidobe_setting_value('daily_time', '18:00')
        last_sent_date = get_sidobe_setting_value('last_daily_summary_date', '')
        if now.strftime('%H:%M') == summary_time and last_sent_date != now.strftime('%Y-%m-%d'):
            sent_any = False
            for policy in ClassroomNotificationConfig.query.filter_by(whatsapp_enabled=True).all():
                summary_text = _build_tomorrow_summary_message(classroom_id=policy.classroom_id)
                if not summary_text:
                    continue
                result = send_sidobe(summary_text, title="Ringkasan Besok", classroom_id=policy.classroom_id, category='schedule')
                if result.get('ok'):
                    sent_any = True
            if sent_any:
                set_setting_value('sidobe_last_daily_summary_date', now.strftime('%Y-%m-%d'))
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

# Browser CORS is limited to explicitly trusted web origins. Native mobile
# clients are unaffected because they do not use browser CORS enforcement.
_cors_origins = [
    origin.strip()
    for origin in os.environ.get(
        'CORS_ALLOWED_ORIGINS',
        'https://famousbytee.arisdev.web.id',
    ).split(',')
    if origin.strip()
]
CORS(
    app,
    resources={r'/api/*': {'origins': _cors_origins}},
    allow_headers=['Authorization', 'Content-Type'],
    methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'],
    supports_credentials=False,
)

# Initialize JWT
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


@login_manager.user_loader
def load_user(user_id):
    try:
        return User.query.get(int(user_id))
    except Exception:
        return None

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
            print("Database Patch: Adding missing Si Dobe permission to role table...")
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
        'can_manage_news',
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
                    'can_manage_gallery': True, 'can_manage_notifications': True, 'sidobe_enabled': True, 'can_manage_whatsapp': True,
                    'can_manage_assignments': True, 'can_use_api': True, 'can_manage_news': True,
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
                    'can_manage_gallery': True, 'can_manage_notifications': True, 'sidobe_enabled': True, 'can_manage_whatsapp': True,
                    'can_manage_assignments': True, 'can_use_api': True, 'can_manage_news': True,
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
                    'can_manage_gallery': False, 'can_manage_notifications': False, 'sidobe_enabled': False, 'can_manage_whatsapp': False,
                    'can_manage_assignments': False, 'can_use_api': True, 'can_manage_news': False,
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

        for role_name, role_config in role_data.items():
            role = Role.query.filter_by(name=role_name).first()
            if not role:
                role = Role(name=role_name)
                db.session.add(role)

            role.description = role_config.get('description', role.description)
            for key, value in role_config.get('perms', {}).items():
                if hasattr(role, key):
                    setattr(role, key, value)

        db.session.commit()

    sync_roles()


def get_fund_target(classroom_id=None):
    today = datetime.now().date()
    periods = get_fund_periods(classroom_id)
    if periods:
        total = 0
        for period in periods:
            effective_end = min(today, period.end_date) if period.end_date else today
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


def calculate_user_points_breakdown(user):
    if not user:
        return {
            'fund_points': 0,
            'gallery_points': 0,
            'arrears_penalty': 0,
            'total_paid': 0,
            'target_payment': 0,
            'arrears': 0,
            'published_photos': 0,
            'total_points': 0,
        }

    classroom_id = user.classroom_id or (user.student.classroom_id if user.student else None)
    target_payment = get_fund_target(classroom_id=classroom_id)

    total_paid = 0
    arrears = target_payment
    if user.student_id:
        total_paid = db.session.query(db.func.sum(BatchFund.amount)).filter(
            BatchFund.student_id == user.student_id,
            BatchFund.type == 'Masuk'
        ).scalar() or 0
        arrears = max(0, target_payment - total_paid)

    published_photos = GalleryPhoto.query.filter_by(
        uploaded_by=user.id,
        status='Published'
    ).count()

    fund_points = int(total_paid // 10000)
    gallery_points = published_photos * 5
    arrears_penalty = int(arrears // 10000)
    # Tunggakan tetap ditampilkan sebagai informasi keuangan, tetapi tidak boleh
    # menghapus poin yang sudah benar-benar diperoleh dari pembayaran kas.
    total_points = fund_points + gallery_points

    return {
        'fund_points': fund_points,
        'gallery_points': gallery_points,
        'arrears_penalty': arrears_penalty,
        'total_paid': total_paid,
        'target_payment': target_payment,
        'arrears': arrears,
        'published_photos': published_photos,
        'total_points': total_points,
    }


def auto_recalculate_points():
    users = User.query.all()
    dirty = False
    for user in users:
        breakdown = calculate_user_points_breakdown(user)
        new_points = breakdown['total_points']
        if (user.points or 0) != new_points:
            user.points = new_points
            dirty = True
    if dirty:
        db.session.commit()


@app.route('/announcements/manage')
@login_required
def view_announcements():
    if not current_user.role.can_manage_announcements:
        flash('Akses ditolak.')
        return redirect(url_for('dashboard'))

    active_classroom = _requested_classroom(
        'classroom_id',
        _active_classroom_for_user(),
        'can_manage_announcements_multi_class',
        'can_view_all_classrooms',
        'can_access_multi_classroom',
    )
    anns_query = Announcement.query
    if active_classroom:
        anns_query = anns_query.filter(
            (Announcement.classroom_id == active_classroom.id) | (Announcement.classroom_id.is_(None))
        )
    announcements = anns_query.order_by(Announcement.is_pinned.desc(), Announcement.date_posted.desc()).all()
    classrooms = _web_allowed_classrooms(
        'can_manage_announcements_multi_class',
        'can_view_all_classrooms',
        'can_access_multi_classroom',
    )
    return render_template('announcements.html', announcements=announcements, classrooms=classrooms, active_classroom=active_classroom)

def log_activity(action, details=None):
    if current_user.is_authenticated:
        class_fb = current_user.classroom or (current_user.student.classroom if current_user.student else None)
        enriched_details = f"[{current_user.role.name}] {details}" if details else f"[{current_user.role.name}]"
        log = ActivityLog(
            user_id=current_user.id, 
            action=action, 
            details=enriched_details,
            classroom_id=class_fb.id if class_fb else None
        )
        db.session.add(log)
        db.session.commit()

@app.route('/logs')
@login_required
def view_logs():
    if not current_user.role.can_manage_roles:
        flash('Akses ditolak.')
        return redirect(url_for('dashboard'))

    active_classroom = _requested_classroom(
        'classroom_id',
        _active_classroom_for_user(),
        'can_view_all_classrooms',
        'can_access_multi_classroom',
        'can_switch_classroom_context',
    )
    query = ActivityLog.query
    if active_classroom:
        query = query.filter((ActivityLog.classroom_id == active_classroom.id) | (ActivityLog.classroom_id.is_(None)))

    logs = query.order_by(ActivityLog.timestamp.desc()).limit(100).all()
    classrooms = _web_allowed_classrooms(
        'can_view_all_classrooms',
        'can_access_multi_classroom',
        'can_switch_classroom_context',
    )
    return render_template('logs.html', logs=logs, classrooms=classrooms, active_classroom=active_classroom)


@app.route('/')
def index():
    
    announcements = Announcement.query.filter_by(is_public=True).order_by(Announcement.is_pinned.desc(), Announcement.date_posted.desc()).limit(3).all()
    photos = GalleryPhoto.query.filter_by(is_public=True).order_by(GalleryPhoto.created_at.desc()).limit(8).all()
    classrooms = ClassRoom.query.order_by(ClassRoom.name.asc()).all()
    recent_news = NewsArticle.query.filter_by(status='Published', is_public=True).order_by(NewsArticle.published_at.desc(), NewsArticle.created_at.desc()).limit(3).all()
    
    return render_template('index.html', announcements=announcements, photos=photos, classrooms=classrooms, recent_news=recent_news)




@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        client_ip = request.remote_addr or 'unknown'
        if _login_rate_limited(client_ip):
            flash('Terlalu banyak percobaan login. Coba kembali beberapa menit lagi.')
            return render_template('login.html'), 429

        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''

        if not username:
            flash('Username atau NIM wajib diisi.')
            return render_template('login.html')

        user = User.query.filter(
            db.or_(
                db.func.lower(User.username) == username.lower(),
                db.func.lower(User.email) == username.lower(),
                User.student.has(Student.nim == username),
            )
        ).first()

        if not user:
            flash('Username/NIM atau password salah.')
            return render_template('login.html')

        is_valid, needs_rehash = verify_password(user.password, password)

        if not is_valid:
            flash('Username/NIM atau password salah.')
            return render_template('login.html')

        if user.status != 'Active':
            flash('Username/NIM atau password salah.')
            return render_template('login.html')

        if needs_rehash:
            user.password = hash_password(password)
            db.session.commit()

        login_user(user, remember=True)
        _clear_login_attempts(client_ip)
        flash(f'Selamat datang, {user.full_name or user.username}.')
        return redirect(url_for('dashboard'))

    return render_template('login.html')


@app.route('/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    flash('Anda telah keluar dari portal.')
    return redirect(url_for('login'))

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
        total_mhs_query = Student.query
        total_in_query = db.session.query(db.func.sum(BatchFund.amount)).filter(BatchFund.type == 'Masuk')
        total_out_query = db.session.query(db.func.sum(BatchFund.amount)).filter(BatchFund.type == 'Keluar')
        total_ann_query = Announcement.query
        if active_classroom:
            total_mhs_query = total_mhs_query.filter_by(classroom_id=active_classroom.id)
            total_in_query = total_in_query.filter((BatchFund.classroom_id == active_classroom.id) | (BatchFund.classroom_id.is_(None)))
            total_out_query = total_out_query.filter((BatchFund.classroom_id == active_classroom.id) | (BatchFund.classroom_id.is_(None)))
            total_ann_query = total_ann_query.filter((Announcement.classroom_id == active_classroom.id) | (Announcement.classroom_id.is_(None)))
        total_mhs = total_mhs_query.count()
        total_in = total_in_query.scalar() or 0
        total_out = total_out_query.scalar() or 0
        total_ann = total_ann_query.count()
        
        admin_stats = {
            'total_members': total_mhs,
            'balance': total_in - total_out,
            'total_announcements': total_ann
        }
        recent_students_query = Student.query
        if active_classroom:
            recent_students_query = recent_students_query.filter_by(classroom_id=active_classroom.id)
        recent_students = recent_students_query.order_by(Student.id.desc()).limit(5).all()

    return render_template('dashboard.html', 
                         recent_announcements=recent_announcements,
                         member_info=member_info,
                         admin_stats=admin_stats,
                         recent_students=recent_students,
                         gallery_preview=gallery_preview,
                         next_class=next_class,
                         time_diff=min_diff if min_diff != float('inf') else None,
                         read_ids=read_ids)

@app.route('/announcements/read/<int:id>', methods=['POST'])
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
            current_user.password = hash_password(new_pass)
            
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

    all_classrooms = _web_allowed_classrooms(
        'can_manage_students_multi_class',
        'can_assign_users_to_classroom',
        'can_move_users_between_classrooms',
        'can_view_all_classrooms',
        'can_access_multi_classroom',
        'can_switch_classroom_context',
    )
    active_classroom = _active_classroom_for_user()
    
    if request.method == 'POST':
        classroom = _requested_classroom(
            'classroom_id',
            active_classroom,
            'can_manage_students_multi_class',
            'can_assign_users_to_classroom',
            'can_move_users_between_classrooms',
            'can_view_all_classrooms',
        )
        new_m = Student(
            nim=request.form['nim'], 
            full_name=request.form['full_name'], 
            status=normalize_member_status(request.form.get('status')),
            classroom_id=classroom.id if classroom else None
        )
        db.session.add(new_m)
        db.session.commit()
        log_activity("Tambah Member", f"NIM: {new_m.nim}, Nama: {new_m.full_name}")
        return redirect(url_for('manage_members'))
    
    active_classroom = _requested_classroom(
        'classroom_id',
        active_classroom,
        'can_manage_students_multi_class',
        'can_assign_users_to_classroom',
        'can_move_users_between_classrooms',
        'can_view_all_classrooms',
        'can_access_multi_classroom',
        'can_switch_classroom_context',
    )

    members_query = Student.query
    if active_classroom and not _has_any_classroom_scope(
        'can_manage_students_multi_class',
        'can_view_all_classrooms',
    ):
        members_query = members_query.filter_by(classroom_id=active_classroom.id)
    elif active_classroom and request.args.get('classroom_id'):
        members_query = members_query.filter_by(classroom_id=active_classroom.id)
    members = members_query.order_by(Student.full_name.asc()).all()
    return render_template('members.html', students=members, classrooms=all_classrooms, active_classroom=active_classroom)

@app.route('/members/bulk', methods=['POST'])
@login_required
def bulk_add_members():
    if not current_user.role.can_manage_students: return redirect(url_for('dashboard'))
    data = request.form.get('bulk_data')
    class_fb = _requested_classroom(
        'classroom_id',
        _active_classroom_for_user(),
        'can_manage_students_multi_class',
        'can_assign_users_to_classroom',
        'can_move_users_between_classrooms',
        'can_view_all_classrooms',
    )
    
    added_count = 0
    lines = data.strip().split('\n')
    for line in lines:
        if ';' in line: parts = line.split(';')
        elif ',' in line: parts = line.split(',')
        else: continue
        
        if len(parts) >= 2:
            nim = parts[0].strip()
            name = parts[1].strip()
            status = normalize_member_status(parts[2] if len(parts) >= 3 else 'Aktif')
            
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
    allowed_ids = {item.id for item in _web_allowed_classrooms(
        'can_manage_students_multi_class',
        'can_assign_users_to_classroom',
        'can_move_users_between_classrooms',
        'can_view_all_classrooms',
    )}
    if allowed_ids and m.classroom_id not in allowed_ids:
        flash('Akses edit member ditolak untuk kelas tersebut.')
        return redirect(url_for('manage_members'))
    old_name = m.full_name
    classroom = _requested_classroom(
        'classroom_id',
        m.classroom or _active_classroom_for_user(),
        'can_manage_students_multi_class',
        'can_assign_users_to_classroom',
        'can_move_users_between_classrooms',
        'can_view_all_classrooms',
    )
    if classroom:
        m.classroom_id = classroom.id
    m.nim = request.form['nim']
    m.full_name = request.form['full_name']
    m.status = normalize_member_status(request.form.get('status'), m.status or 'Aktif')
    if m.user:
        m.user.status = 'Active' if m.status == 'Aktif' else 'Inactive'
    db.session.commit()
    log_activity("Edit Member", f"Mengubah data {old_name} (ID: {id})")
    flash(f'Data {m.full_name} diperbarui.')
    return redirect(url_for('manage_members'))

@app.route('/members/delete/<int:id>', methods=['POST'])
@login_required
def delete_member(id):
    if not current_user.role.can_manage_students: return redirect(url_for('dashboard'))
    m = Student.query.get_or_404(id)
    allowed_ids = {item.id for item in _web_allowed_classrooms(
        'can_manage_students_multi_class',
        'can_assign_users_to_classroom',
        'can_move_users_between_classrooms',
        'can_view_all_classrooms',
    )}
    if allowed_ids and m.classroom_id not in allowed_ids:
        flash('Akses hapus member ditolak untuk kelas tersebut.')
        return redirect(url_for('manage_members'))
    log_activity("Hapus Member", f"NIM: {m.nim}, Nama: {m.full_name}")
    db.session.delete(m)
    db.session.commit()
    return redirect(url_for('manage_members'))

@app.route('/schedule', methods=['GET', 'POST'])
@login_required
def manage_schedule():
    all_classrooms = _web_allowed_classrooms(
        'can_manage_schedule_multi_class',
        'can_view_all_classrooms',
        'can_access_multi_classroom',
        'can_switch_classroom_context',
    )
    active_classroom = _requested_classroom(
        'classroom_id',
        _active_classroom_for_user(),
        'can_manage_schedule_multi_class',
        'can_view_all_classrooms',
        'can_access_multi_classroom',
        'can_switch_classroom_context',
    )
    if request.method == 'POST' and not current_user.role.can_manage_schedule:
        flash('Akses ditolak.')
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        classroom = _requested_classroom(
            'classroom_id',
            active_classroom,
            'can_manage_schedule_multi_class',
            'can_view_all_classrooms',
        )
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

    class_fb = _requested_classroom(
        'classroom_id',
        _active_classroom_for_user(),
        'can_manage_schedule_multi_class',
        'can_view_all_classrooms',
    )
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

    class_fb = _requested_classroom(
        'classroom_id',
        _active_classroom_for_user(),
        'can_manage_schedule_multi_class',
        'can_view_all_classrooms',
    )
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

    class_fb = _requested_classroom(
        'classroom_id',
        _active_classroom_for_user(),
        'can_manage_schedule_multi_class',
        'can_view_all_classrooms',
    )
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

    class_fb = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    if current_user.role.can_manage_roles:
        classroom_id = request.form.get('classroom_id')
        if classroom_id:
            class_fb = ClassRoom.query.get(int(classroom_id)) or class_fb
    if not class_fb:
        class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()
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

    class_fb = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    if current_user.role.can_manage_roles:
        classroom_id = request.form.get('classroom_id')
        if classroom_id:
            class_fb = ClassRoom.query.get(int(classroom_id)) or class_fb
    if not class_fb:
        class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()
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
    active_classroom = _requested_classroom(
        'classroom_id',
        _active_classroom_for_user(),
        'can_manage_assignments_multi_class',
        'can_view_all_classrooms',
        'can_access_multi_classroom',
        'can_switch_classroom_context',
    )
    if request.method == 'POST' and not current_user.role.can_manage_assignments:
        flash('Akses ditolak.')
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        classroom = _requested_classroom(
            'classroom_id',
            active_classroom,
            'can_manage_assignments_multi_class',
            'can_view_all_classrooms',
        )
        a = Assignment(
            title=request.form['title'],
            subject=request.form['subject'],
            deadline=datetime.strptime(request.form['deadline'], '%Y-%m-%dT%H:%M'),
            description=request.form.get('description', ''),
            classroom_id=classroom.id if classroom else None
        )
        db.session.add(a)
        db.session.commit()
        
        send_sidobe_notification(
            "Tugas Baru!",
            f"Tugas {a.subject}: {a.title}. Deadline: {a.deadline.strftime('%d %b %H:%M')}",
            sender_id=current_user.id,
            allow_sidobe=True,
            sidobe_text=f"Tugas baru\nMata kuliah: {a.subject}\nJudul: {a.title}\nDeadline: {a.deadline.strftime('%d %b %Y %H:%M')}",
            classroom_id=a.classroom_id,
            category='assignment',
        )
        log_activity("Tambah Tugas", f"Judul: {a.title}")
        flash('Tugas berhasil ditambahkan!')
        return redirect(url_for('manage_assignments'))
    
    assignments_query = Assignment.query
    if active_classroom and not _has_any_classroom_scope(
        'can_manage_assignments_multi_class',
        'can_view_all_classrooms',
    ):
        assignments_query = assignments_query.filter(
            (Assignment.classroom_id == active_classroom.id) | (Assignment.classroom_id.is_(None))
        )
    elif active_classroom and request.args.get('classroom_id'):
        assignments_query = assignments_query.filter(
            (Assignment.classroom_id == active_classroom.id) | (Assignment.classroom_id.is_(None))
        )
    assignments = assignments_query.order_by(Assignment.deadline.asc()).all()
    classrooms = _web_allowed_classrooms(
        'can_manage_assignments_multi_class',
        'can_view_all_classrooms',
        'can_access_multi_classroom',
        'can_switch_classroom_context',
    )
    return render_template('assignments.html', assignments=assignments, classrooms=classrooms, active_classroom=active_classroom)

@app.route('/assignments/delete/<int:id>', methods=['POST'])
@login_required
def delete_assignment(id):
    if not current_user.role.can_manage_assignments: return redirect(url_for('dashboard'))
    a = Assignment.query.get_or_404(id)
    allowed_ids = {item.id for item in _web_allowed_classrooms(
        'can_manage_assignments_multi_class',
        'can_view_all_classrooms',
    )}
    if a.classroom_id not in allowed_ids and a.classroom_id is not None:
        flash('Akses hapus tugas ditolak untuk kelas tersebut.')
        return redirect(url_for('manage_assignments'))
    log_activity("Hapus Tugas", f"Judul: {a.title}")
    db.session.delete(a)
    db.session.commit()
    flash('Tugas berhasil dihapus.')
    return redirect(url_for('manage_assignments'))


@app.route('/schedule/batch', methods=['POST'])
@login_required
def schedule_batch():
    if not current_user.role.can_manage_schedule: return redirect(url_for('dashboard'))
    class_fb = _requested_classroom(
        'classroom_id',
        _active_classroom_for_user(),
        'can_manage_schedule_multi_class',
        'can_view_all_classrooms',
    )
    
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
    class_fb = _requested_classroom(
        'classroom_id',
        _active_classroom_for_user(),
        'can_manage_schedule_multi_class',
        'can_view_all_classrooms',
    )
    
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
    allowed_ids = {item.id for item in _web_allowed_classrooms(
        'can_manage_schedule_multi_class',
        'can_view_all_classrooms',
    )}
    if s.classroom_id not in allowed_ids and s.classroom_id is not None:
        flash('Akses edit jadwal ditolak untuk kelas tersebut.')
        return redirect(url_for('manage_schedule'))
    classroom = _requested_classroom(
        'classroom_id',
        s.classroom or _active_classroom_for_user(),
        'can_manage_schedule_multi_class',
        'can_view_all_classrooms',
    )
    if classroom:
        s.classroom_id = classroom.id
    s.day = request.form['day']
    s.time_start = request.form['time_start']
    s.time_end = request.form['time_end']
    s.subject = request.form['subject']
    s.lecturer = request.form['lecturer']
    s.room = request.form['room']
    db.session.commit()
    # Check if Si Dobe notification is enabled for edit (default: false to avoid spam)
    should_notify = get_setting_value('schedule_notify_on_edit', 'false') == 'true'
    # Only send push notification, Si Dobe via daily summary
    send_sidobe_multichannel(
        "Jadwal Diubah!",
        f"Jadwal {s.subject} telah diperbarui oleh pengurus.",
        sender_id=current_user.id,
        allow_whatsapp=False,  # Don't send per-subject Si Dobe
        classroom_id=s.classroom_id,
        category='schedule',
    )
    log_activity("Edit Jadwal", f"Matkul: {s.subject}")
    return redirect(url_for('manage_schedule'))

@app.route('/schedule/delete/<int:id>', methods=['POST'])
@login_required
def delete_schedule(id):
    if not current_user.role.can_manage_schedule: return redirect(url_for('dashboard'))
    s = Schedule.query.get_or_404(id)
    allowed_ids = {item.id for item in _web_allowed_classrooms(
        'can_manage_schedule_multi_class',
        'can_view_all_classrooms',
    )}
    if s.classroom_id not in allowed_ids and s.classroom_id is not None:
        flash('Akses hapus jadwal ditolak untuk kelas tersebut.')
        return redirect(url_for('manage_schedule'))
    subject_name = s.subject
    log_activity("Hapus Jadwal", f"Matkul: {s.subject}")
    db.session.delete(s)
    db.session.commit()
    # Only send push notification, Si Dobe via daily summary
    send_sidobe_multichannel(
        "Jadwal Dihapus",
        f"Jadwal {subject_name} telah dihapus dari sistem.",
        sender_id=current_user.id,
        allow_whatsapp=False,  # Don't send per-subject Si Dobe
        classroom_id=s.classroom_id,
        category='schedule',
    )
    return redirect(url_for('manage_schedule'))

# Suggestion #15: Download Template CSV with Current Data
@app.route('/schedule/template')
@login_required
def download_schedule_template():
    if not current_user.role.can_manage_schedule: return redirect(url_for('dashboard'))
    class_fb = _requested_classroom(
        'classroom_id',
        _active_classroom_for_user(),
        'can_manage_schedule_multi_class',
        'can_view_all_classrooms',
    )
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
        active_classroom = _requested_classroom(
            'classroom_id',
            _active_classroom_for_user(),
            'can_manage_announcements_multi_class',
            'can_view_all_classrooms',
        )
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
        send_push(title_prefix, ann.title, sender_id=current_user.id, classroom_id=ann.classroom_id or active_classroom.id if active_classroom else None, category='announcement')

        log_activity("Tambah Pengumuman", f"Judul: {ann.title} (Publik: {ann.is_public})")
        return redirect(url_for('manage_announcements'))
    
    active_classroom = _requested_classroom(
        'classroom_id',
        _active_classroom_for_user(),
        'can_manage_announcements_multi_class',
        'can_view_all_classrooms',
        'can_access_multi_classroom',
        'can_switch_classroom_context',
    )
    announcements_query = Announcement.query
    if active_classroom and not _has_any_classroom_scope(
        'can_manage_announcements_multi_class',
        'can_view_all_classrooms',
    ):
        announcements_query = announcements_query.filter(
            (Announcement.classroom_id == active_classroom.id) | (Announcement.classroom_id.is_(None))
        )
    elif active_classroom and request.args.get('classroom_id'):
        announcements_query = announcements_query.filter(
            (Announcement.classroom_id == active_classroom.id) | (Announcement.classroom_id.is_(None))
        )
    announcements = announcements_query.order_by(Announcement.is_pinned.desc(), Announcement.date_posted.desc()).all()
    classrooms = _web_allowed_classrooms(
        'can_manage_announcements_multi_class',
        'can_view_all_classrooms',
        'can_access_multi_classroom',
        'can_switch_classroom_context',
    )
    return render_template('announcements.html', announcements=announcements, classrooms=classrooms, active_classroom=active_classroom)

@app.route('/announcements/edit/<int:id>', methods=['POST'])
@login_required
def edit_announcement(id):
    if not current_user.role.can_manage_announcements: return redirect(url_for('dashboard'))
    ann = Announcement.query.get_or_404(id)
    allowed_ids = {item.id for item in _web_allowed_classrooms(
        'can_manage_announcements_multi_class',
        'can_view_all_classrooms',
    )}
    if ann.classroom_id not in allowed_ids and ann.classroom_id is not None:
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

@app.route('/announcements/delete/<int:id>', methods=['POST'])
@login_required
def delete_announcement(id):
    if not current_user.role.can_manage_announcements: return redirect(url_for('dashboard'))
    ann = Announcement.query.get_or_404(id)
    allowed_ids = {item.id for item in _web_allowed_classrooms(
        'can_manage_announcements_multi_class',
        'can_view_all_classrooms',
    )}
    if ann.classroom_id not in allowed_ids and ann.classroom_id is not None:
        flash('Akses ditolak.')
        return redirect(url_for('manage_announcements'))
    log_activity("Hapus Pengumuman", f"Judul: {ann.title}")
    db.session.delete(ann)
    db.session.commit()
    return redirect(url_for('manage_announcements'))

@app.route('/fund', methods=['GET', 'POST'])
@login_required
def manage_fund():
    class_fb = _requested_fund_classroom()
    classrooms = _fund_allowed_classrooms()
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
        
        student_obj = Student.query.get(int(student_id_val)) if student_id_val and str(student_id_val).lower() != 'none' else None
        fund_classroom = student_obj.classroom if student_obj and student_obj.classroom else class_fb

        fund = BatchFund(
            classroom_id=fund_classroom.id if fund_classroom else None,
            description=description, 
            amount=amount, 
            type=type_val, 
            category=category,
            evidence_note=evidence_note,
            recorded_by=current_user.username,
            date=date_val,
            student_id=student_obj.id if student_obj else None,
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
                category='Penting',
                classroom_id=fund_classroom.id if fund_classroom else None
            )
            db.session.add(ann)
            
        db.session.commit()
        auto_recalculate_points()
        flash('Data kas berhasil ditambahkan!')
        return redirect(url_for('manage_fund'))
    
    # Suggestion #17: Advanced Filtering
    query = _apply_fund_classroom_filter(BatchFund.query, class_fb)
        
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
    
    total_in_query = _apply_fund_classroom_sum_filter(
        db.session.query(db.func.sum(BatchFund.amount)).filter(BatchFund.type == 'Masuk'),
        class_fb,
    )
    total_out_query = _apply_fund_classroom_sum_filter(
        db.session.query(db.func.sum(BatchFund.amount)).filter(BatchFund.type == 'Keluar'),
        class_fb,
    )
        
    total_in = total_in_query.scalar() or 0
    total_out = total_out_query.scalar() or 0
    balance = total_in - total_out
    
    target_payment = get_fund_target(classroom_id=class_fb.id if class_fb else None)
    fund_periods = get_fund_periods(classroom_id=class_fb.id if class_fb else None)
    
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
                         classrooms=classrooms,
                         active_classroom=class_fb,
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

    class_fb = _requested_fund_classroom()

    db.session.add(FundPeriod(
        classroom_id=class_fb.id if class_fb else None,
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
    if class_fb := _requested_fund_classroom():
        if period.classroom_id not in (class_fb.id, None if class_fb.id == _default_classroom().id else -1):
            flash('Akses ditolak.')
            return redirect(url_for('manage_fund', classroom_id=class_fb.id))
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

@app.route('/fund/periods/delete/<int:id>', methods=['POST'])
@login_required
def delete_fund_period(id):
    if not current_user.role.can_manage_fund:
        return redirect(url_for('dashboard'))

    period = FundPeriod.query.get_or_404(id)
    if class_fb := _requested_fund_classroom():
        if period.classroom_id not in (class_fb.id, None if class_fb.id == _default_classroom().id else -1):
            flash('Akses ditolak.')
            return redirect(url_for('manage_fund', classroom_id=class_fb.id))
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
    class_fb = _requested_fund_classroom()
    if not _is_fund_in_allowed_scope(f, class_fb):
        flash('Akses ditolak.')
        return redirect(url_for('manage_fund', classroom_id=class_fb.id if class_fb else None))
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
    f.type = request.form.get('type', f.type)
    f.category = request.form.get('category', f.category)
    
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
@app.route('/fund/duplicate/<int:id>', methods=['POST'])
@login_required
def duplicate_fund(id):
    if not current_user.role.can_manage_fund: return redirect(url_for('dashboard'))
    f = BatchFund.query.get_or_404(id)
    class_fb = _requested_fund_classroom()
    if not _is_fund_in_allowed_scope(f, class_fb):
        flash('Akses ditolak.')
        return redirect(url_for('manage_fund', classroom_id=class_fb.id if class_fb else None))
    new_f = BatchFund(
        classroom_id=f.classroom_id or (class_fb.id if class_fb else None),
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
    class_fb = _requested_fund_classroom()
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
                    classroom_id=student.classroom_id or (class_fb.id if class_fb else None),
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

@app.route('/fund/delete/<int:id>', methods=['POST'])
@login_required
def delete_fund(id):
    if not current_user.role.can_manage_fund: return redirect(url_for('dashboard'))
    f = BatchFund.query.get_or_404(id)
    class_fb = _requested_fund_classroom()
    if not _is_fund_in_allowed_scope(f, class_fb):
        flash('Akses ditolak.')
        return redirect(url_for('manage_fund', classroom_id=class_fb.id if class_fb else None))
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
    
    class_fb = _requested_fund_classroom()
    funds = _apply_fund_classroom_filter(BatchFund.query, class_fb).order_by(BatchFund.date.desc()).all()
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
                if current_user.role.can_manage_roles or getattr(current_user.role, 'can_access_multi_classroom', False) or getattr(current_user.role, 'can_switch_classroom_context', False):
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
    if current_user.role.can_manage_roles or getattr(current_user.role, 'can_access_multi_classroom', False) or getattr(current_user.role, 'can_switch_classroom_context', False):
        classrooms = ClassRoom.query.order_by(ClassRoom.name.asc()).all()
    elif active_classroom:
        classrooms = [active_classroom]
    return render_template('settings.html', settings=settings, classrooms=classrooms, active_classroom=active_classroom)

@app.route('/classes', methods=['GET', 'POST'])
@login_required
def manage_classes():
    if not (current_user.role.can_manage_roles or getattr(current_user.role, 'can_manage_classrooms', False)):
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
                duplicate = ClassRoom.query.filter(
                    db.func.lower(ClassRoom.name) == name.lower(),
                    ClassRoom.id != classroom.id
                ).first()
                if duplicate:
                    flash('Nama kelas sudah dipakai kelas lain.')
                    return redirect(url_for('manage_classes'))
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
                sidobe_enabled='can_manage_whatsapp' in request.form,
                can_manage_whatsapp='can_manage_whatsapp' in request.form,
                can_manage_assignments='can_manage_assignments' in request.form,
                can_use_api='can_use_api' in request.form,
                can_manage_news='can_manage_news' in request.form,
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
                password=hash_password(request.form['password']),
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
        user.password = hash_password(request.form['password'])
    
    try:
        db.session.commit()
        log_activity("Edit User", f"Username: {user.username}")
        flash(f'Data user {user.username} telah diperbarui.')
    except Exception as e:
        db.session.rollback()
        flash(f'Gagal memperbarui user: Email mungkin sudah digunakan oleh akun lain.')
        print(f"User Edit Error: {e}")
    return redirect(url_for('manage_roles'))

@app.route('/roles/delete/user/<int:id>', methods=['POST'])
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
    role.sidobe_enabled = 'can_manage_whatsapp' in request.form
    role.can_manage_whatsapp = 'can_manage_whatsapp' in request.form
    role.can_manage_assignments = 'can_manage_assignments' in request.form
    role.can_use_api = 'can_use_api' in request.form
    role.can_manage_news = 'can_manage_news' in request.form
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

@app.route('/roles/delete/role/<int:id>', methods=['POST'])
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
    active_classroom = _requested_classroom(
        'classroom_id',
        _active_classroom_for_user(),
        'can_manage_gallery_multi_class',
        'can_view_all_classrooms',
        'can_access_multi_classroom',
    )
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

    classrooms = _gallery_allowed_classrooms()
    return render_template(
        'gallery.html',
        photos=photos,
        classrooms=classrooms,
        active_classroom=active_classroom,
        gallery_can_moderate=_can_manage_gallery_content(),
    )

@app.route('/gallery/edit/<int:id>', methods=['POST'])
@login_required
def edit_gallery(id):
    photo = GalleryPhoto.query.get_or_404(id)
    active_classroom = _requested_gallery_classroom()
    can_manage = _can_manage_gallery_content()

    if not can_manage and photo.uploaded_by != current_user.id:
        flash('Akses ditolak.')
        return redirect(url_for('manage_gallery', classroom_id=active_classroom.id if active_classroom else None))

    if not _is_gallery_photo_in_allowed_scope(photo, active_classroom):
        flash('Foto tidak termasuk kelas aktif.')
        return redirect(url_for('manage_gallery', classroom_id=active_classroom.id if active_classroom else None))

    caption = (request.form.get('caption') or '').strip()
    if not caption:
        flash('Caption foto wajib diisi.')
        return redirect(url_for('manage_gallery', classroom_id=active_classroom.id if active_classroom else None))

    photo.caption = caption
    photo.tags = (request.form.get('tags') or '').strip()
    if can_manage:
        photo.is_public = 'is_public' in request.form

    db.session.commit()
    flash('Foto berhasil diperbarui.')
    return redirect(url_for('manage_gallery', classroom_id=active_classroom.id if active_classroom else None))

@app.route('/gallery/upload', methods=['POST'])
@login_required
def upload_gallery():
    files = request.files.getlist('photos')
    if not files or files[0].filename == '':
        flash('Tidak ada file yang dipilih.')
        return redirect(url_for('manage_gallery'))
    
    active_classroom = _requested_classroom(
        'classroom_id',
        _active_classroom_for_user(),
        'can_manage_gallery_multi_class',
        'can_view_all_classrooms',
        'can_access_multi_classroom',
    )
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

@app.route('/gallery/delete/<int:id>', methods=['POST'])
@login_required
def delete_gallery(id):
    photo = GalleryPhoto.query.get_or_404(id)
    # Check permission
    can_manage = _can_manage_gallery_content()
    
    if not can_manage and photo.uploaded_by != current_user.id:
        flash('Akses ditolak.')
        return redirect(url_for('manage_gallery'))
    if not _is_gallery_photo_in_allowed_scope(photo, _requested_gallery_classroom()):
        flash('Foto tidak termasuk kelas aktif.')
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

@app.route('/gallery/approve/<int:id>', methods=['POST'])
@login_required
def approve_gallery(id):
    if not _can_manage_gallery_content():
        flash('Akses ditolak.')
        return redirect(url_for('manage_gallery'))
        
    photo = GalleryPhoto.query.get_or_404(id)
    if not _is_gallery_photo_in_allowed_scope(photo, _requested_gallery_classroom()):
        flash('Foto tidak termasuk kelas aktif.')
        return redirect(url_for('manage_gallery'))
    photo.status = 'Published'
    db.session.commit()
    auto_recalculate_points()
    log_activity("Approve Foto", f"ID: {photo.id}")
    flash(f'Foto oleh {photo.user.full_name} disetujui.')
    return redirect(url_for('manage_gallery'))

@app.route('/gallery/reject/<int:id>', methods=['POST'])
@login_required
def reject_gallery(id):
    if not _can_manage_gallery_content():
        flash('Akses ditolak.')
        return redirect(url_for('manage_gallery'))
        
    photo = GalleryPhoto.query.get_or_404(id)
    if not _is_gallery_photo_in_allowed_scope(photo, _requested_gallery_classroom()):
        flash('Foto tidak termasuk kelas aktif.')
        return redirect(url_for('manage_gallery'))
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

@app.route('/gallery/toggle/<int:id>', methods=['POST'])
@login_required
def toggle_gallery_visibility(id):
    if not _can_manage_gallery_content():
        return redirect(url_for('manage_gallery'))
        
    p = GalleryPhoto.query.get_or_404(id)
    if not _is_gallery_photo_in_allowed_scope(p, _requested_gallery_classroom()):
        flash('Foto tidak termasuk kelas aktif.')
        return redirect(url_for('manage_gallery'))
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
    active_classroom = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    if active_classroom and photo.classroom_id not in (active_classroom.id, None):
        flash('Akses ditolak.')
        return redirect(request.referrer or url_for('manage_gallery'))
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
                    'user': escape(name),
                    'body': escape(comment.body),
                    'time': comment.created_at.strftime('%d %b %H:%M')
                }
            }
        
        flash('Komentar ditambahkan.')
    return redirect(request.referrer or url_for('manage_gallery'))

@app.route('/gallery/comment/delete/<int:id>', methods=['POST'])
@login_required
def delete_photo_comment(id):
    comment = PhotoComment.query.get_or_404(id)
    active_classroom = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    if active_classroom and comment.photo.classroom_id not in (active_classroom.id, None):
        flash('Akses ditolak.')
        return redirect(url_for('manage_gallery'))
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
            
            with db.engine.begin() as conn:
                fund_cols = [c['name'] for c in inspector.get_columns('batch_fund')]
                if 'classroom_id' not in fund_cols:
                    conn.execute(text("ALTER TABLE batch_fund ADD COLUMN classroom_id INTEGER NULL"))
                if 'original_amount' not in fund_cols:
                    conn.execute(text("ALTER TABLE batch_fund ADD COLUMN original_amount FLOAT NULL"))
                if 'original_description' not in fund_cols:
                    conn.execute(text("ALTER TABLE batch_fund ADD COLUMN original_description VARCHAR(200) NULL"))
                if 'tags' not in fund_cols:
                    conn.execute(text("ALTER TABLE batch_fund ADD COLUMN tags VARCHAR(100) NULL"))

                # Password hashes (scrypt/pbkdf2) exceed the original 120 chars.
                user_cols = {c['name']: c for c in inspector.get_columns('user')}
                password_length = getattr(user_cols.get('password', {}).get('type'), 'length', 0) or 0
                if password_length < 255:
                    conn.execute(text("ALTER TABLE user MODIFY COLUMN password VARCHAR(255) NOT NULL"))
                
                # C2. Sinkronisasi tabel 'fund_period'
                if 'fund_period' in inspector.get_table_names():
                    fp_cols = [c['name'] for c in inspector.get_columns('fund_period')]
                    if 'classroom_id' not in fp_cols:
                        conn.execute(text("ALTER TABLE fund_period ADD COLUMN classroom_id INTEGER NULL"))

                # C3. Sinkronisasi tabel 'activity_log'
                if 'activity_log' in inspector.get_table_names():
                    log_cols = [c['name'] for c in inspector.get_columns('activity_log')]
                    if 'classroom_id' not in log_cols:
                        conn.execute(text("ALTER TABLE activity_log ADD COLUMN classroom_id INTEGER NULL"))
                
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
                if 'bot_id' not in notification_cols:
                    conn.execute(text("ALTER TABLE notification_history ADD COLUMN bot_id INTEGER NULL"))
                if 'chat_id' not in notification_cols:
                    conn.execute(text("ALTER TABLE notification_history ADD COLUMN chat_id VARCHAR(255) NULL"))
                if 'category' not in notification_cols:
                    conn.execute(text("ALTER TABLE notification_history ADD COLUMN category VARCHAR(50) NULL"))
                if 'delivery_mode' not in notification_cols:
                    conn.execute(text("ALTER TABLE notification_history ADD COLUMN delivery_mode VARCHAR(30) NULL"))

                # I. Sinkronisasi tabel notifikasi multi-kelas
                try:
                    inspector.get_columns('classroom_notification_config')
                except Exception:
                    conn.execute(text("""
                        CREATE TABLE classroom_notification_config (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            classroom_id INTEGER NOT NULL UNIQUE,
                            push_enabled BOOLEAN DEFAULT 1,
                            whatsapp_enabled BOOLEAN DEFAULT 0,
                            default_channel VARCHAR(20) DEFAULT 'push',
                            announcement_enabled BOOLEAN DEFAULT 1,
                            assignment_enabled BOOLEAN DEFAULT 1,
                            schedule_enabled BOOLEAN DEFAULT 1,
                            finance_enabled BOOLEAN DEFAULT 1,
                            emergency_enabled BOOLEAN DEFAULT 1,
                            updated_by INTEGER NULL,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )
                    """))
                try:
                    inspector.get_columns('whats_app_bot')
                except Exception:
                    conn.execute(text("""
                        CREATE TABLE whats_app_bot (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            name VARCHAR(120) NOT NULL UNIQUE,
                            provider VARCHAR(30) DEFAULT 'sidobe',
                            session_name VARCHAR(120) NOT NULL,
                            base_url VARCHAR(255) NULL,
                            status VARCHAR(30) DEFAULT 'unknown',
                            is_active BOOLEAN DEFAULT 1,
                            last_seen_at DATETIME NULL,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )
                    """))
                try:
                    inspector.get_columns('classroom_whats_app_binding')
                except Exception:
                    conn.execute(text("""
                        CREATE TABLE classroom_whats_app_binding (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            classroom_id INTEGER NOT NULL UNIQUE,
                            bot_id INTEGER NOT NULL,
                            chat_id VARCHAR(255) NOT NULL,
                            chat_label VARCHAR(120) NULL,
                            is_default BOOLEAN DEFAULT 1,
                            updated_by INTEGER NULL,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )
                    """))
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
                    can_manage_notifications=True, sidobe_enabled=True, can_manage_whatsapp=True, can_manage_assignments=True,
                    can_use_api=True
                )
                staff_r = Role(
                    name='Pengurus', description='Manajemen data operasional.', 
                    can_manage_students=True, can_manage_schedule=True, can_manage_fund=True, 
                    can_manage_announcements=True, can_manage_roles=False,
                    can_view_logs=True, can_export_data=True, can_edit_settings=False, can_manage_gallery=True,
                    can_manage_notifications=True, sidobe_enabled=True, can_manage_whatsapp=True, can_manage_assignments=True,
                    can_use_api=False
                )
                member_r = Role(
                    name='Member', description='Akses dashboard anggota.', 
                    can_manage_students=False, can_manage_schedule=False, can_manage_fund=False, 
                    can_manage_announcements=False, can_manage_roles=False,
                    can_view_logs=False, can_export_data=False, can_edit_settings=False, can_manage_gallery=False,
                    can_manage_notifications=False, sidobe_enabled=False, can_manage_whatsapp=False, can_manage_assignments=False,
                    can_use_api=False
                )
                db.session.add_all([admin_r, staff_r, member_r])
                db.session.commit()
                
                # Buat Admin Default
                db.session.flush()
                initial_admin_password = os.environ.get('INITIAL_ADMIN_PASSWORD')
                if not initial_admin_password:
                    raise RuntimeError(
                        'INITIAL_ADMIN_PASSWORD wajib disetel saat membuat instalasi baru.'
                    )
                db.session.add(User(
                    username='admin',
                    password=hash_password(initial_admin_password),
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

            # One-way migration for legacy plaintext passwords. The existing
            # plaintext value becomes the user's password input to scrypt.
            migrated_passwords = False
            for existing_user in User.query.all():
                if existing_user.password and not is_password_hash(existing_user.password):
                    existing_user.password = hash_password(existing_user.password)
                    migrated_passwords = True
            if migrated_passwords:
                db.session.commit()
                app.logger.warning('Legacy plaintext passwords migrated to scrypt hashes.')

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
                    SystemSetting(key='social_wa', value='#', description='Link Si Dobe Group'),
                    SystemSetting(key='seo_keywords', value='famousbytee, portal, kelas, manajemen', description='Kata Kunci SEO (Pisahkan dengan koma)'),
                    SystemSetting(key='activity_log_retention_days', value='30', description='Masa simpan log aktivitas dalam hari'),
                    SystemSetting(key='sidobe_enabled', value='false', description='Aktifkan integrasi Si Dobe'),
                    SystemSetting(key='sidobe_base_url', value='', description='Base URL server Si Dobe'),
                    SystemSetting(key='sidobe_api_key', value='', description='API key Si Dobe'),
                    SystemSetting(key='sidobe_session', value='', description='Nama session Si Dobe'),
                    SystemSetting(key='sidobe_group_chat_id', value='', description='Chat ID grup Si Dobe'),
                    SystemSetting(key='sidobe_daily_time', value='18:00', description='Jam ringkasan harian Si Dobe'),
                    SystemSetting(key='sidobe_last_daily_summary_date', value='', description='Tanggal ringkasan harian terakhir'),
                    SystemSetting(key='sidobe_schedule_template', value='Assalamualaikum dan selamat malam, tabe saudara dan saudari sekalian di grup ini, Jadwal Mata Kuliah {day_name}, {date_long}\n{schedule_lines}\n{deadline_section}(Sesuai jadwal dari pihak kampus)\n{extra_info_section}Sekian dan terimakasih', description='Template ringkasan jadwal Si Dobe'),
                    SystemSetting(key='sidobe_schedule_item_template', value='{index}. MK {subject} mulai jam {time_range}', description='Template item jadwal Si Dobe'),
                    SystemSetting(key='sidobe_schedule_deadline_item_template', value='{index}. Deadline {subject}: {title} jam {deadline_time}', description='Template item deadline Si Dobe'),
                    SystemSetting(key='sidobe_schedule_extra_info', value='', description='Info tambahan tetap di ringkasan Si Dobe'),
                    SystemSetting(key='sidobe_admin_header_enabled', value='true', description='Aktifkan header/pengenal admin di pesan Si Dobe'),
                    SystemSetting(key='sidobe_admin_header_text', value='*[PESAN RESMI ADMIN FAMOUSBYTEE]*\n{title_block}Pesan ini dikirim dari sistem admin.\n', description='Template header admin untuk pesan Si Dobe'),
                    SystemSetting(key='notification_channel_default', value='push', description='Channel default notifikasi: push, whatsapp, both'),
                    SystemSetting(key='sidobe_notification_channel_default', value='push', description='Channel default notifikasi Si Dobe: push, whatsapp, both'),
                    SystemSetting(key='schedule_notify_on_create', value='true', description='Kirim notifikasi Si Dobe saat jadwal baru dibuat'),
                    SystemSetting(key='schedule_notify_on_edit', value='false', description='Kirim notifikasi Si Dobe saat jadwal diedit (default: false untuk hindari spam)'),
                    SystemSetting(key='schedule_notify_on_delete', value='true', description='Kirim notifikasi Si Dobe saat jadwal dihapus'),
                    SystemSetting(key='notifications_legacy_migrated', value='false', description='Penanda migrasi konfigurasi notifikasi legacy ke model multi-kelas')
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

        try:
            migrated = get_setting_value('notifications_legacy_migrated', 'false').lower() == 'true'
            default_classroom = ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()
            if default_classroom and not migrated:
                policy = ClassroomNotificationConfig.query.filter_by(classroom_id=default_classroom.id).first()
                if not policy:
                    policy = ClassroomNotificationConfig(
                        classroom_id=default_classroom.id,
                        push_enabled=True,
                    whatsapp_enabled=get_sidobe_setting_value('enabled', 'false').lower() == 'true',
                        default_channel=get_notification_channel_mode(),
                        announcement_enabled=True,
                        assignment_enabled=True,
                        schedule_enabled=True,
                        finance_enabled=True,
                        emergency_enabled=True,
                    )
                    db.session.add(policy)
                    db.session.flush()

                session_name = get_sidobe_setting_value('session', '').strip()
                group_chat_id = get_sidobe_setting_value('group_chat_id', '').strip()
                if session_name:
                    bot = WhatsAppBot.query.filter_by(name='Legacy Default Bot').first()
                    if not bot:
                        bot = WhatsAppBot(
                            name='Legacy Default Bot',
                            provider='sidobe',
                            session_name=session_name,
                            base_url=get_sidobe_setting_value('base_url', '').strip() or None,
                            status='legacy-imported',
                            is_active=get_sidobe_setting_value('enabled', 'false').lower() == 'true',
                        )
                        db.session.add(bot)
                        db.session.flush()
                    else:
                        bot.session_name = session_name
                        bot.base_url = get_sidobe_setting_value('base_url', '').strip() or bot.base_url

                    if group_chat_id:
                        binding = ClassroomWhatsAppBinding.query.filter_by(classroom_id=default_classroom.id).first()
                        if not binding:
                            binding = ClassroomWhatsAppBinding(
                                classroom_id=default_classroom.id,
                                bot_id=bot.id,
                                chat_id=group_chat_id,
                                chat_label='Legacy Default Group',
                                is_default=True,
                            )
                            db.session.add(binding)
                        else:
                            binding.bot_id = bot.id
                            binding.chat_id = group_chat_id
                            binding.chat_label = binding.chat_label or 'Legacy Default Group'

                set_setting_value('notifications_legacy_migrated', 'true', 'Penanda migrasi konfigurasi notifikasi legacy ke model multi-kelas')
                db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"Peringatan: Gagal migrasi konfigurasi notifikasi legacy: {e}")

        # 5. Seeding Kategori Berita Default & Update Izin Admin
        try:
            admin_role = Role.query.filter_by(name='Admin').first()
            if admin_role and not admin_role.can_manage_news:
                admin_role.can_manage_news = True
                db.session.commit()
            
            if not NewsCategory.query.first():
                db.session.add_all([
                    NewsCategory(name='Akademik', slug='akademik', color='#3b82f6'),
                    NewsCategory(name='Kegiatan', slug='kegiatan', color='#10b981'),
                    NewsCategory(name='Pengumuman', slug='pengumuman', color='#f59e0b')
                ])
                db.session.commit()
                print("Status: Kategori berita default berhasil ditambahkan.")
        except Exception as e:
            db.session.rollback()
            print(f"Peringatan: Gagal seeding kategori berita: {e}")

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
    if not (current_user.role.can_manage_notifications or current_user.role.sidobe_enabled):
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
            send_sidobe_multichannel(title, body, sender_id=current_user.id, allow_sidobe=True, classroom_id=active_classroom.id if active_classroom else None, category='emergency')
            flash('Notifikasi siaran berhasil dikirim!')
        else:
            send_push(title, body, user_id=int(target), sender_id=current_user.id, classroom_id=active_classroom.id if active_classroom else None, category='emergency')
            flash('Notifikasi terkirim ke pengguna.')
            
        log_activity("Kirim Notifikasi", f"Judul: {title}, Target: {target}")
        return redirect(url_for('manage_notifications'))

    can_manage_multi = (
        current_user.role.can_manage_roles or
        getattr(current_user.role, 'can_manage_notifications_multi_class', False)
    )
    allowed_classrooms = ClassRoom.query.order_by(ClassRoom.name.asc()).all() if can_manage_multi else ([active_classroom] if active_classroom else [])

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
        'sidobe_base_url': get_sidobe_setting_value('base_url', ''),
        'sidobe_api_key_masked': ('*' * max(0, len(get_sidobe_setting_value('api_key', '')) - 4)) + get_sidobe_setting_value('api_key', '')[-4:],
        'sidobe_daily_time': get_sidobe_setting_value('daily_time', '18:00'),
        'sidobe_schedule_template': get_sidobe_setting_value(
            'schedule_template',
            'Assalamualaikum dan selamat malam, tabe saudara dan saudari sekalian di grup ini, Jadwal Mata Kuliah {day_name}, {date_long}\n{schedule_lines}\n{deadline_section}(Sesuai jadwal dari pihak kampus)\n{extra_info_section}Sekian dan terimakasih'
        ),
        'sidobe_schedule_item_template': get_sidobe_setting_value('schedule_item_template', '{index}. MK {subject} mulai jam {time_range}'),
        'sidobe_schedule_deadline_item_template': get_sidobe_setting_value('schedule_deadline_item_template', '{index}. Deadline {subject}: {title} jam {deadline_time}'),
        'sidobe_schedule_extra_info': get_sidobe_setting_value('schedule_extra_info', ''),
        'sidobe_admin_header_enabled': get_sidobe_setting_value('admin_header_enabled', 'true'),
        'sidobe_admin_header_text': get_sidobe_setting_value('admin_header_text', '*[PESAN RESMI ADMIN FAMOUSBYTEE]*\n{title_block}Pesan ini dikirim dari sistem admin.\n'),
        'legacy_migrated': get_setting_value('notifications_legacy_migrated', 'false'),
    }
    policies = {item.classroom_id: item for item in ClassroomNotificationConfig.query.all()}
    bindings = {item.classroom_id: item for item in ClassroomWhatsAppBinding.query.all()}
    bots = WhatsAppBot.query.order_by(WhatsAppBot.name.asc()).all()
    # Generate short-lived JWT token untuk AJAX calls dari halaman ini
    from datetime import timedelta
    page_token = create_access_token(identity=str(current_user.id), expires_delta=timedelta(hours=2))
    return render_template('notifications.html', history=history, users=users, settings=settings, classrooms=allowed_classrooms, active_classroom=active_classroom, policies=policies, bindings=bindings, bots=bots, can_manage_multi_notifications=can_manage_multi, page_token=page_token)

@app.route('/notifications/sidobe/save-config', methods=['POST'])
@login_required
def save_sidobe_config():
    if not current_user.role.sidobe_enabled:
        flash('Akses ditolak.')
        return redirect(url_for('manage_notifications'))

    sidobe_base_url = (request.form.get('sidobe_base_url') or '').strip()
    set_setting_value('sidobe_base_url', sidobe_base_url, 'Base URL server Si Dobe')
    new_api_key = (request.form.get('sidobe_api_key') or '').strip()
    if new_api_key:
        set_setting_value('sidobe_api_key', new_api_key, 'API key Si Dobe')
    daily_time = (request.form.get('sidobe_daily_time') or '18:00').strip()
    set_setting_value('sidobe_daily_time', daily_time, 'Jam ringkasan harian Si Dobe')
    schedule_template = request.form.get('sidobe_schedule_template') or ''
    schedule_item_template = request.form.get('sidobe_schedule_item_template') or ''
    schedule_deadline_item_template = request.form.get('sidobe_schedule_deadline_item_template') or ''
    schedule_extra_info = request.form.get('sidobe_schedule_extra_info') or ''
    admin_header_enabled = 'true' if request.form.get('sidobe_admin_header_enabled') == 'on' else 'false'
    admin_header_text = request.form.get('sidobe_admin_header_text') or ''
    set_setting_value('sidobe_schedule_template', schedule_template, 'Template ringkasan jadwal Si Dobe')
    set_setting_value('sidobe_schedule_item_template', schedule_item_template, 'Template item jadwal Si Dobe')
    set_setting_value('sidobe_schedule_deadline_item_template', schedule_deadline_item_template, 'Template item deadline Si Dobe')
    set_setting_value('sidobe_schedule_extra_info', schedule_extra_info, 'Info tambahan tetap di ringkasan Si Dobe')
    set_setting_value('sidobe_admin_header_enabled', admin_header_enabled, 'Aktifkan header/pengenal admin di pesan Si Dobe')
    set_setting_value('sidobe_admin_header_text', admin_header_text, 'Template header admin untuk pesan Si Dobe')
    db.session.commit()
    flash('Konfigurasi mesin Si Dobe berhasil disimpan.')
    return redirect(url_for('manage_notifications'))

@app.route('/notifications/sidobe/sessions')
@login_required
def get_sidobe_sessions():
    if not current_user.role.sidobe_enabled:
        return jsonify({'error': 'Unauthorized'}), 403
    result = _sidobe_request_with_auth_fallback('GET', '/api/sessions')
    if not result.get('ok'):
        return jsonify(result), 400

    raw_data = result.get('data') or []
    sessions = raw_data if isinstance(raw_data, list) else raw_data.get('sessions', [])
    normalized = []
    for item in sessions:
        if not isinstance(item, dict):
            continue
        normalized.append({
            'name': _sidobe_normalize_scalar(item.get('name') or item.get('session') or item.get('id') or '-'),
            'status': _sidobe_normalize_scalar(item.get('status') or item.get('state') or item.get('connectionStatus') or '-'),
            'status_reason': _sidobe_normalize_scalar(item.get('error') or item.get('message') or item.get('reason') or ''),
            'me': _sidobe_normalize_scalar(item.get('me') or item.get('meId') or item.get('phone') or '-'),
            'engine': _sidobe_normalize_scalar(item.get('engine') or item.get('type') or '-'),
            'qr': _sidobe_normalize_scalar(item.get('qr') or item.get('qrcode') or item.get('qrCode') or item.get('qr_code') or ''),
        })
    return jsonify({'ok': True, 'items': normalized, 'count': len(normalized)})

@app.route('/notifications/sidobe/sessions/create', methods=['POST'])
@login_required
def create_sidobe_session():
    if not current_user.role.sidobe_enabled:
        return jsonify({'error': 'Unauthorized'}), 403
    payload = request.get_json(silent=True) or (request.form.to_dict(flat=True) if request.form else {})
    session_name = (payload.get('session_name') or payload.get('name') or '').strip()
    if not session_name:
        return jsonify({'ok': False, 'error': 'Session name wajib diisi'}), 400
    base_url = (payload.get('base_url') or get_sidobe_setting_value('base_url', '')).strip() or None
    body = {
        'name': session_name,
        'session': session_name,
        'data': payload.get('data') if isinstance(payload.get('data'), dict) else {},
    }
    result = _sidobe_request_any('POST', [
        '/api/sessions',
        '/api/session',
    ], payload=body, base_url_override=base_url)
    if not result.get('ok'):
        return jsonify({'ok': False, 'error': result.get('error', 'Gagal membuat session Si Dobe')}), 400
    return jsonify({'ok': True, 'message': 'Session dibuat', 'path': result.get('path'), 'data': result.get('data')})

@app.route('/notifications/sidobe/sessions/<session_name>/start', methods=['POST'])
@login_required
def start_sidobe_session(session_name):
    if not current_user.role.sidobe_enabled:
        return jsonify({'error': 'Unauthorized'}), 403
    session_name = (session_name or '').strip()
    if not session_name:
        return jsonify({'ok': False, 'error': 'Session wajib diisi'}), 400
    base_url = (request.get_json(silent=True) or {}).get('base_url') or get_sidobe_setting_value('base_url', '')
    result = _sidobe_request_any('POST', [
        f'/api/sessions/{session_name}/start',
        f'/api/{session_name}/start',
        f'/api/sessions/{session_name}/connect',
    ], base_url_override=(base_url or '').strip() or None)
    return jsonify({'ok': bool(result.get('ok')), 'error': result.get('error'), 'path': result.get('path'), 'data': result.get('data')}), (200 if result.get('ok') else 400)

@app.route('/notifications/sidobe/sessions/<session_name>/stop', methods=['POST'])
@login_required
def stop_sidobe_session(session_name):
    if not current_user.role.sidobe_enabled:
        return jsonify({'error': 'Unauthorized'}), 403
    session_name = (session_name or '').strip()
    if not session_name:
        return jsonify({'ok': False, 'error': 'Session wajib diisi'}), 400
    base_url = (request.get_json(silent=True) or {}).get('base_url') or get_sidobe_setting_value('base_url', '')
    result = _sidobe_request_any('POST', [
        f'/api/sessions/{session_name}/stop',
        f'/api/{session_name}/stop',
        f'/api/sessions/{session_name}/disconnect',
    ], base_url_override=(base_url or '').strip() or None)
    return jsonify({'ok': bool(result.get('ok')), 'error': result.get('error'), 'path': result.get('path'), 'data': result.get('data')}), (200 if result.get('ok') else 400)

@app.route('/notifications/sidobe/sessions/<session_name>/restart', methods=['POST'])
@login_required
def restart_sidobe_session(session_name):
    if not current_user.role.sidobe_enabled:
        return jsonify({'error': 'Unauthorized'}), 403
    session_name = (session_name or '').strip()
    if not session_name:
        return jsonify({'ok': False, 'error': 'Session wajib diisi'}), 400
    base_url = (request.get_json(silent=True) or {}).get('base_url') or get_sidobe_setting_value('base_url', '')
    result = _sidobe_request_any('POST', [
        f'/api/sessions/{session_name}/restart',
        f'/api/{session_name}/restart',
        f'/api/sessions/{session_name}/start',
    ], base_url_override=(base_url or '').strip() or None)
    return jsonify({'ok': bool(result.get('ok')), 'error': result.get('error'), 'path': result.get('path'), 'data': result.get('data')}), (200 if result.get('ok') else 400)


@app.route('/notifications/sidobe/dashboard')
@login_required
def get_sidobe_dashboard():
    if not current_user.role.sidobe_enabled:
        return jsonify({'error': 'Unauthorized'}), 403

    base_url = get_sidobe_setting_value('base_url', '').strip() or None
    payload = {
        'ok': True,
        'workers': [],
        'sessions': [],
        'session_count': 0,
        'worker_count': 0,
        'base_url': base_url or '',
        'worker_error': '',
        'session_error': '',
    }

    workers_result = _sidobe_request_with_auth_fallback('GET', '/api/workers', base_url_override=base_url)
    if workers_result.get('ok'):
        raw_workers = workers_result.get('data') or []
        workers = raw_workers if isinstance(raw_workers, list) else raw_workers.get('workers', raw_workers.get('data', []))
        normalized_workers = []
        for item in workers or []:
            if not isinstance(item, dict):
                continue
            normalized_workers.append({
                'name': _sidobe_normalize_scalar(item.get('name') or item.get('id') or 'worker'),
                'api': _sidobe_normalize_scalar(item.get('api') or item.get('baseUrl') or base_url or ''),
                'status': _sidobe_normalize_scalar(item.get('status') or item.get('state') or 'unknown'),
                'info': _sidobe_normalize_scalar(item.get('info') or item.get('version') or item.get('build') or ''),
                'sessions': item.get('sessions') or item.get('sessionCount') or item.get('workingSessions') or 0,
            })
        payload['workers'] = normalized_workers
        payload['worker_count'] = len(normalized_workers)
    else:
        payload['worker_error'] = workers_result.get('error', 'Gagal memuat worker Si Dobe')

    sessions_result = _sidobe_request_with_auth_fallback('GET', '/api/sessions', base_url_override=base_url)
    if sessions_result.get('ok'):
        raw_sessions = sessions_result.get('data') or []
        sessions = raw_sessions if isinstance(raw_sessions, list) else raw_sessions.get('sessions', [])
        normalized_sessions = []
        for item in sessions:
            if not isinstance(item, dict):
                continue
            normalized_sessions.append({
                'name': _sidobe_normalize_scalar(item.get('name') or item.get('session') or item.get('id') or '-'),
                'status': _sidobe_normalize_scalar(item.get('status') or item.get('state') or item.get('connectionStatus') or 'unknown'),
                'account': _sidobe_normalize_scalar(item.get('me') or item.get('meId') or item.get('phone') or item.get('wid') or '-'),
                'server': _sidobe_normalize_scalar(item.get('server') or item.get('worker') or item.get('engine') or 'default'),
                'qr': _sidobe_normalize_scalar(item.get('qr') or item.get('qrcode') or item.get('qrCode') or item.get('qr_code') or ''),
            })
        payload['sessions'] = normalized_sessions
        payload['session_count'] = len(normalized_sessions)
    else:
        payload['session_error'] = sessions_result.get('error', 'Gagal memuat session Si Dobe')

    return jsonify(payload)


@app.route('/notifications/sidobe/session/<session_name>/qr')
@login_required
def get_sidobe_session_qr(session_name):
    if not current_user.role.sidobe_enabled:
        return jsonify({'error': 'Unauthorized'}), 403
    session_name = (session_name or '').strip()
    if not session_name:
        return jsonify({'ok': False, 'error': 'Session wajib diisi'}), 400
    result = _sidobe_request_with_auth_fallback('GET', '/api/sessions')
    if not result.get('ok'):
        return jsonify(result), 400
    raw_data = result.get('data') or []
    sessions = raw_data if isinstance(raw_data, list) else raw_data.get('sessions', [])
    matched = None
    for item in sessions:
        if not isinstance(item, dict):
            continue
        candidate = _sidobe_normalize_scalar(item.get('name') or item.get('session') or item.get('id') or '')
        if candidate == session_name:
            matched = item
            break
    if not matched:
        return jsonify({'ok': False, 'error': 'Session tidak ditemukan'}), 404
    qr_value = _sidobe_normalize_scalar(matched.get('qr') or matched.get('qrcode') or matched.get('qrCode') or matched.get('qr_code') or '')
    return jsonify({
        'ok': True,
        'session_name': session_name,
        'status': _sidobe_normalize_scalar(matched.get('status') or matched.get('state') or matched.get('connectionStatus') or 'unknown'),
        'qr': qr_value,
    })

@app.route('/notifications/sidobe/session/<session_name>/screenshot')
@login_required
def get_sidobe_session_screenshot(session_name):
    if not current_user.role.sidobe_enabled:
        return jsonify({'error': 'Unauthorized'}), 403
    session_name = (session_name or '').strip()
    if not session_name:
        return jsonify({'ok': False, 'error': 'Session wajib diisi'}), 400
    base_url = get_sidobe_setting_value('base_url', '').strip() or None
    result = _sidobe_request_any('GET', [
        '/api/screenshot',
        f'/api/{session_name}/screenshot',
        f'/api/sessions/{session_name}/screenshot',
        f'/api/{session_name}/qr',
        f'/api/sessions/{session_name}/qr',
    ], base_url_override=base_url)
    if not result.get('ok'):
        return jsonify({'ok': False, 'session_name': session_name, 'error': result.get('error', 'Screenshot/QR Si Dobe belum tersedia')})
    data = result.get('data')
    if isinstance(data, dict):
        image_data = data.get('screenshot') or data.get('qr') or data.get('image') or data.get('data') or ''
    else:
        image_data = data or ''
    return jsonify({'ok': True, 'session_name': session_name, 'path': result.get('path'), 'data': image_data})

@app.route('/notifications/sidobe/groups')
@login_required
def get_sidobe_groups():
    if not current_user.role.sidobe_enabled:
        return jsonify({'error': 'Unauthorized'}), 403
    session_name = get_sidobe_setting_value('session', '').strip()
    if not session_name:
        return jsonify({'ok': False, 'error': 'Session Si Dobe belum diatur'}), 400
    result = _sidobe_request_with_auth_fallback('GET', f'/api/{session_name}/groups')
    raw_data = []
    groups = []
    if result.get('ok'):
        raw_data = result.get('data') or []
        groups = raw_data if isinstance(raw_data, list) else raw_data.get('groups', raw_data.get('chats', []))
    if not groups:
        fallback = _sidobe_request_with_auth_fallback('GET', f'/api/{session_name}/chats')
        if fallback.get('ok'):
            raw_data = fallback.get('data') or []
            groups = raw_data if isinstance(raw_data, list) else fallback.get('data', {}).get('chats', [])
        elif not result.get('ok'):
            return jsonify({'ok': False, 'error': result.get('error', fallback.get('error', 'Gagal memuat grup Si Dobe'))}), 400
    normalized = []
    for item in groups:
        if not isinstance(item, dict):
            continue
        normalized.append({
            'name': _sidobe_normalize_scalar(item.get('name') or item.get('subject') or item.get('formattedTitle') or 'Tanpa Nama'),
            'chat_id': _sidobe_normalize_chat_id(item),
            'participants': item.get('participantsCount') or item.get('size') or len(item.get('participants', []) if isinstance(item.get('participants'), list) else []),
            'owner': _sidobe_normalize_scalar(item.get('owner') or item.get('ownerPn') or '-')
        })
    return jsonify({'ok': True, 'items': normalized, 'count': len(normalized), 'session': session_name})

@app.route('/notifications/sidobe/chats')
@login_required
def get_sidobe_chats():
    if not current_user.role.sidobe_enabled:
        return jsonify({'error': 'Unauthorized'}), 403
    session_name = get_sidobe_setting_value('session', '').strip()
    if not session_name:
        return jsonify({'ok': False, 'error': 'Session Si Dobe belum diatur'}), 400

    result = _sidobe_request_with_auth_fallback('GET', f'/api/{session_name}/chats')
    if not result.get('ok'):
        return jsonify(result), 400

    raw_data = result.get('data') or []
    chats = raw_data if isinstance(raw_data, list) else raw_data.get('chats', [])
    normalized = []
    for item in chats:
        if not isinstance(item, dict):
            continue
        chat_id = _sidobe_normalize_chat_id(item)
        if not chat_id:
            continue
        chat_type = 'group' if '@g.us' in chat_id else 'personal'
        if chat_type != 'group':
            continue
        normalized.append({
            'name': _sidobe_normalize_scalar(item.get('name') or item.get('pushName') or item.get('shortName') or item.get('formattedTitle') or chat_id),
            'chat_id': chat_id,
            'participants': item.get('participantsCount') or item.get('size') or 0,
            'owner': _sidobe_normalize_scalar(item.get('owner') or item.get('ownerPn') or '-'),
            'type': chat_type
        })
    return jsonify({'ok': True, 'items': normalized, 'count': len(normalized), 'session': session_name})


@app.errorhandler(404)
def page_not_found(error):
    site_settings = _get_site_settings()
    return render_template('404.html', site_settings=site_settings), 404


@app.errorhandler(500)
def internal_server_error(error):
    site_settings = _get_site_settings()
    return render_template('500.html', site_settings=site_settings), 500

@app.route('/notifications/test-push', methods=['POST'])
@login_required
def test_push_notification():
    if not current_user.role.can_manage_notifications:
        return jsonify({'error': 'Unauthorized'}), 403
    active_classroom = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    send_push('Test Push Famousbytee', 'Ini adalah notifikasi uji dari backend.', user_id=current_user.id, sender_id=current_user.id, extra_data={'source': 'test_push'}, classroom_id=active_classroom.id if active_classroom else None, category='emergency')
    return jsonify({'ok': True, 'message': 'Permintaan test push diproses'})

@app.route('/notifications/test-whatsapp', methods=['POST'])
@login_required
def test_whatsapp_notification():
    if not current_user.role.sidobe_enabled:
        return jsonify({'error': 'Unauthorized'}), 403
    payload = request.get_json(silent=True) or request.form or {}
    custom_chat_id = (payload.get('chat_id') or '').strip()
    custom_message = (payload.get('message') or 'Test pesan Si Dobe dari panel backend Famousbytee.').strip()
    active_classroom = current_user.classroom or (current_user.student.classroom if current_user.student else None)
    result = send_sidobe(custom_message, sender_id=current_user.id, title='Test Si Dobe', chat_id=custom_chat_id or None, classroom_id=active_classroom.id if active_classroom else None, category='emergency', force=bool(custom_chat_id))
    return jsonify(result), (200 if result.get('ok') else 400)

@app.route('/webhooks/sidobe', methods=['POST'])
def sidobe_webhook():
    webhook_secret = os.environ.get('SIDOBE_WEBHOOK_SECRET', '').strip()
    supplied_secret = request.headers.get('X-Webhook-Secret', '').strip()
    if not webhook_secret:
        app.logger.error('SIDOBE_WEBHOOK_SECRET belum dikonfigurasi; webhook ditolak.')
        return jsonify({'ok': False, 'error': 'Webhook belum dikonfigurasi'}), 503
    if not supplied_secret or not hmac.compare_digest(webhook_secret, supplied_secret):
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401

    payload = request.get_json(silent=True)
    if payload is None:
        payload = request.form.to_dict(flat=True) if request.form else {}
    if not payload:
        raw_body = request.get_data(cache=False, as_text=True) or ''
        payload = {'raw_body': raw_body}

    event_data = _sidobe_extract_event(payload)
    body = (event_data.get('body') or '').strip()
    chat_id = (event_data.get('chat_id') or '').strip()
    print(f"Si Dobe webhook received: event={event_data.get('event', '')}, chat_id={chat_id or '-'}, body={body[:80] or '-'}")

    # Allow self messages if they are commands (for admin testing)
    if event_data.get('from_me') is True and not body.startswith('/'):
        print("Si Dobe webhook ignored. Reason: outgoing/self non-command message.")
        return jsonify({
            'ok': True,
            'accepted': True,
            'ignored': True,
            'reason': 'outgoing-message',
            'event': event_data.get('event', '')
        }), 200

    if not body.startswith('/') or not chat_id:
        print(f"Si Dobe webhook ignored. Payload preview: {json.dumps(payload, ensure_ascii=True)[:300]}")
        return jsonify({
            'ok': True,
            'accepted': True,
            'ignored': True,
            'reason': 'not-command-or-missing-chat',
            'event': event_data.get('event', '')
        }), 200

    if _sidobe_is_duplicate_command(event_data):
        print(f"Si Dobe webhook ignored. Reason: duplicate command for {chat_id or '-'} body={body[:40]}")
        return jsonify({
            'ok': True,
            'accepted': True,
            'ignored': True,
            'reason': 'duplicate-command',
            'event': event_data.get('event', ''),
            'command': body,
            'chat_id': chat_id
        }), 200

    response_text = _sidobe_build_command_response(body, sender_ref=event_data.get('sender_ref') or chat_id)
    if not response_text:
        return jsonify({
            'ok': True,
            'accepted': True,
            'ignored': True,
            'reason': 'empty-response',
            'command': body
        }), 200

    result = send_sidobe(response_text, title=f"Si Dobe Bot {body.split()[0]}", chat_id=chat_id, force=True)
    print(f"Si Dobe webhook reply: target={chat_id or '-'}, sent={result.get('ok', False)}, error={result.get('error', '-') if not result.get('ok') else '-'}")
    return jsonify({
        'ok': True,
        'accepted': True,
        'command': body,
        'chat_id': chat_id,
        'reply_sent': result.get('ok', False),
        'result': result
    }), 200

@app.route('/notifications/clear', methods=['POST'])
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
    active_classroom = _requested_classroom(
        'classroom_id',
        _active_classroom_for_user(),
        'can_view_all_classrooms',
        'can_access_multi_classroom',
        'can_switch_classroom_context',
        'can_view_classroom_reports',
    )
    users_query = User.query.join(Role).outerjoin(Student, User.student_id == Student.id)
    if active_classroom:
        users_query = users_query.filter(
            (User.classroom_id == active_classroom.id) |
            (Student.classroom_id == active_classroom.id)
        )
    all_users = users_query.all()
    ranked_users = []
    for user in all_users:
        breakdown = calculate_user_points_breakdown(user)
        if breakdown['total_points'] <= 0:
            continue
        setattr(user, '_leaderboard_points', breakdown['total_points'])
        ranked_users.append((user, breakdown['total_points']))
    ranked_users.sort(key=lambda item: item[1], reverse=True)
    top_users = [item[0] for item in ranked_users[:20]]
    classrooms = _web_allowed_classrooms(
        'can_view_all_classrooms',
        'can_access_multi_classroom',
        'can_switch_classroom_context',
        'can_view_classroom_reports',
    )
    return render_template(
        'leaderboard.html',
        users=top_users,
        classrooms=classrooms,
        active_classroom=active_classroom,
    )

@app.route('/sitemap.xml')
def sitemap():
    """Generates sitemap.xml dynamically. Excludes API, webhook, and admin-only routes."""
    public_endpoints = ('index', 'login', 'public_gallery', 'news_public')
    pages = []
    for endpoint in public_endpoints:
        try:
            pages.append([url_for(endpoint, _external=True), datetime.now().date()])
        except Exception:
            continue

    sitemap_xml = render_template('sitemap.xml', pages=pages)
    response = make_response(sitemap_xml)
    response.headers["Content-Type"] = "application/xml"
    return response


@app.route('/robots.txt')
def robots_txt():
    body = "\n".join([
        "User-agent: *",
        "Allow: /",
        "Disallow: /api/",
        "Disallow: /dashboard",
        "Disallow: /notifications",
        "Disallow: /settings",
        "Disallow: /roles",
        "Disallow: /logs",
        f"Sitemap: {url_for('sitemap', _external=True)}",
        "",
    ])
    return app.response_class(body, mimetype='text/plain')


@app.route('/.well-known/security.txt')
def security_txt():
    contact = os.environ.get(
        'SECURITY_CONTACT',
        'https://famousbytee.arisdev.web.id/',
    )
    body = "\n".join([
        f"Contact: {contact}",
        "Preferred-Languages: id, en",
        f"Canonical: {url_for('security_txt', _external=True)}",
        "Policy: https://famousbytee.arisdev.web.id/",
        "",
    ])
    return app.response_class(body, mimetype='text/plain')

@app.route('/api/leaderboard', methods=['GET'])
@login_required
def get_leaderboard():
    active_classroom = _requested_classroom(
        'classroom_id',
        _active_classroom_for_user(),
        'can_view_all_classrooms',
        'can_access_multi_classroom',
        'can_switch_classroom_context',
        'can_view_classroom_reports',
    )
    users_query = User.query.outerjoin(Student, User.student_id == Student.id)
    if active_classroom:
        users_query = users_query.filter(
            (User.classroom_id == active_classroom.id) |
            (Student.classroom_id == active_classroom.id)
        )
    ranked = []
    for user in users_query.all():
        breakdown = calculate_user_points_breakdown(user)
        if breakdown['total_points'] > 0:
            setattr(user, '_leaderboard_points', breakdown['total_points'])
            ranked.append((user, breakdown['total_points']))
    ranked.sort(key=lambda item: item[1], reverse=True)
    top_users = [item[0] for item in ranked[:20]]
    return jsonify([{
        "id": u.id,
        "full_name": (u.student.full_name if u.student and u.student.full_name else u.full_name) or u.username,
        "points": getattr(u, '_leaderboard_points', calculate_user_points_breakdown(u)['total_points']),
        "role": u.role.name,
        "nim": u.student.nim if u.student else "-"
    } for u in top_users])

@app.route('/api/leaderboard/<int:user_id>', methods=['GET'])
@login_required
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


# ============================================================
# BERITA (NEWS) ROUTES
# ============================================================

def _generate_slug(title):
    """Generate a URL-friendly slug from title."""
    import unicodedata
    title = unicodedata.normalize('NFKD', str(title))
    title = title.encode('ascii', 'ignore').decode('ascii')
    title = title.lower().strip()
    title = re.sub(r'[^\w\s-]', '', title)
    title = re.sub(r'[\s_-]+', '-', title)
    title = re.sub(r'^-+|-+$', '', title)
    return title or 'artikel'

def _unique_slug(title, model, exclude_id=None):
    """Generate a unique slug, appending a number if a collision occurs."""
    base = _generate_slug(title)
    slug = base
    counter = 1
    while True:
        q = model.query.filter_by(slug=slug)
        if exclude_id:
            q = q.filter(model.id != exclude_id)
        if not q.first():
            return slug
        slug = f"{base}-{counter}"
        counter += 1

def _save_news_cover(file):
    """Save an uploaded cover image and return the filename, or None on failure."""
    if not file or not file.filename:
        return None
    ext = os.path.splitext(secure_filename(file.filename))[1].lower()
    if ext not in {'.jpg', '.jpeg', '.png', '.webp', '.gif'}:
        return None
    import uuid
    news_dir = os.path.join(app.root_path, 'static', 'uploads', 'news')
    os.makedirs(news_dir, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(news_dir, filename)
    try:
        file.stream.seek(0)
        img = Image.open(file.stream)
        img = ImageOps.exif_transpose(img)
        if ext in {'.jpg', '.jpeg'} and img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        elif ext == '.png' and img.mode not in ('RGB', 'RGBA', 'L', 'P'):
            img = img.convert('RGBA')
        img.thumbnail((1400, 1000), Image.LANCZOS)
        save_kwargs = {'optimize': True}
        if ext in {'.jpg', '.jpeg'}:
            save_kwargs['quality'] = 88
        img.save(filepath, **save_kwargs)
    except Exception as err:
        app.logger.exception('Gagal menyimpan cover berita: %s', err)
        if os.path.exists(filepath):
            os.remove(filepath)
        return None
    return filename


# --- Public: Daftar Berita ---
@app.route('/berita')
def news_public():
    page = request.args.get('page', 1, type=int)
    cat_slug = request.args.get('kategori', '')
    q = NewsArticle.query.filter_by(status='Published', is_public=True)
    active_cat = None
    if cat_slug:
        active_cat = NewsCategory.query.filter_by(slug=cat_slug).first()
        if active_cat:
            q = q.filter_by(category_id=active_cat.id)
    articles = q.order_by(
        NewsArticle.published_at.desc(), NewsArticle.created_at.desc()
    ).paginate(page=page, per_page=9, error_out=False)
    categories = NewsCategory.query.order_by(NewsCategory.name).all()
    site_settings = _get_site_settings()
    return render_template('news_public.html',
                           articles=articles, categories=categories,
                           active_cat=active_cat, site_settings=site_settings)


# --- Public: Detail Berita ---
@app.route('/berita/<slug>')
def news_detail(slug):
    article = NewsArticle.query.filter_by(
        slug=slug, status='Published', is_public=True
    ).first_or_404()
    try:
        article.views = (article.views or 0) + 1
        db.session.commit()
    except Exception:
        db.session.rollback()
    related = []
    if article.category_id:
        related = NewsArticle.query.filter(
            NewsArticle.status == 'Published',
            NewsArticle.is_public == True,
            NewsArticle.id != article.id,
            NewsArticle.category_id == article.category_id
        ).order_by(NewsArticle.published_at.desc()).limit(3).all()
    categories = NewsCategory.query.order_by(NewsCategory.name).all()
    site_settings = _get_site_settings()
    return render_template('news_detail.html',
                           article=article, related=related,
                           categories=categories, site_settings=site_settings)


# --- Admin: Daftar Manajemen Berita ---
@app.route('/berita/manage')
@login_required
def manage_news():
    if not current_user.role.can_manage_news:
        flash('Akses ditolak.')
        return redirect(url_for('dashboard'))
    page = request.args.get('page', 1, type=int)
    status_filter = request.args.get('status', '')
    cat_filter = request.args.get('cat', '')
    q = NewsArticle.query
    if status_filter:
        q = q.filter_by(status=status_filter)
    if cat_filter:
        cat_obj = NewsCategory.query.filter_by(slug=cat_filter).first()
        if cat_obj:
            q = q.filter_by(category_id=cat_obj.id)
    articles = q.order_by(NewsArticle.created_at.desc()).paginate(
        page=page, per_page=15, error_out=False)
    categories = NewsCategory.query.order_by(NewsCategory.name).all()
    stats = {
        'total': NewsArticle.query.count(),
        'published': NewsArticle.query.filter_by(status='Published').count(),
        'draft': NewsArticle.query.filter_by(status='Draft').count(),
        'archived': NewsArticle.query.filter_by(status='Archived').count(),
        'total_views': db.session.query(db.func.sum(NewsArticle.views)).scalar() or 0,
    }
    return render_template('news_manage.html',
                           articles=articles, categories=categories, stats=stats,
                           status_filter=status_filter, cat_filter=cat_filter)


# --- Admin: Tambah Berita Baru ---
@app.route('/berita/manage/new', methods=['GET', 'POST'])
@login_required
def news_new():
    if not current_user.role.can_manage_news:
        flash('Akses ditolak.')
        return redirect(url_for('dashboard'))
    categories = NewsCategory.query.order_by(NewsCategory.name).all()
    if request.method == 'POST':
        title   = request.form.get('title', '').strip()
        content = request.form.get('content', '').strip()
        excerpt = request.form.get('excerpt', '').strip()[:500]
        cat_id  = request.form.get('category_id') or None
        status  = request.form.get('status', 'Draft')
        is_pub  = 'is_public' in request.form
        if not title or not content:
            flash('Judul dan konten wajib diisi.')
            return render_template('news_form.html',
                                   categories=categories, article=None, mode='new')
        slug = _unique_slug(title, NewsArticle)
        cover_fn = _save_news_cover(request.files.get('cover_image'))
        pub_at = datetime.now() if status == 'Published' else None
        article = NewsArticle(
            title=title, slug=slug, content=content, excerpt=excerpt,
            category_id=int(cat_id) if cat_id else None,
            status=status, is_public=is_pub,
            author_id=current_user.id,
            cover_image=cover_fn,
            published_at=pub_at
        )
        db.session.add(article)
        db.session.commit()
        log_activity('Tambah Berita', f'Judul: {title}')
        flash('Berita berhasil dipublikasikan!' if status == 'Published' else 'Berita disimpan sebagai draft.')
        return redirect(url_for('manage_news'))
    return render_template('news_form.html', categories=categories, article=None, mode='new')


# --- Admin: Edit Berita ---
@app.route('/berita/manage/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def news_edit(id):
    if not current_user.role.can_manage_news:
        flash('Akses ditolak.')
        return redirect(url_for('dashboard'))
    article = NewsArticle.query.get_or_404(id)
    categories = NewsCategory.query.order_by(NewsCategory.name).all()
    if request.method == 'POST':
        article.title   = request.form.get('title', '').strip()
        article.content = request.form.get('content', '').strip()
        article.excerpt = request.form.get('excerpt', '').strip()[:500]
        cat_id = request.form.get('category_id') or None
        article.category_id = int(cat_id) if cat_id else None
        new_status = request.form.get('status', 'Draft')
        if new_status == 'Published' and article.status != 'Published':
            article.published_at = datetime.now()
        article.status    = new_status
        article.is_public = 'is_public' in request.form
        article.slug = _unique_slug(article.title, NewsArticle, exclude_id=article.id)
        cover_file = request.files.get('cover_image')
        if cover_file and cover_file.filename:
            new_cover = _save_news_cover(cover_file)
            if new_cover:
                if article.cover_image:
                    old = os.path.join(app.root_path, 'static', 'uploads', 'news', article.cover_image)
                    if os.path.exists(old):
                        try: os.remove(old)
                        except Exception: pass
                article.cover_image = new_cover
        try:
            db.session.commit()
            log_activity('Edit Berita', f'ID: {id}, Judul: {article.title}')
            flash('Berita berhasil diperbarui!')
        except Exception as e:
            db.session.rollback()
            flash(f'Gagal menyimpan: {e}')
        return redirect(url_for('manage_news'))
    return render_template('news_form.html', categories=categories, article=article, mode='edit')


# --- Admin: Hapus Berita ---
@app.route('/berita/manage/delete/<int:id>', methods=['POST'])
@login_required
def news_delete(id):
    if not current_user.role.can_manage_news:
        return redirect(url_for('dashboard'))
    article = NewsArticle.query.get_or_404(id)
    if article.cover_image:
        p = os.path.join(app.root_path, 'static', 'uploads', 'news', article.cover_image)
        if os.path.exists(p):
            try: os.remove(p)
            except Exception: pass
    log_activity('Hapus Berita', f'Judul: {article.title}')
    db.session.delete(article)
    db.session.commit()
    flash('Berita berhasil dihapus.')
    return redirect(url_for('manage_news'))


# --- Admin: Toggle Status Berita ---
@app.route('/berita/manage/toggle/<int:id>', methods=['POST'])
@login_required
def news_toggle_status(id):
    if not current_user.role.can_manage_news:
        return redirect(url_for('dashboard'))
    article = NewsArticle.query.get_or_404(id)
    if article.status == 'Published':
        article.status = 'Draft'
    else:
        article.status = 'Published'
        if not article.published_at:
            article.published_at = datetime.now()
    db.session.commit()
    flash(f'Status berita diubah ke {article.status}.')
    return redirect(url_for('manage_news'))


# --- Admin: Upload Gambar (TinyMCE image_upload_handler) ---
@app.route('/berita/manage/upload-image', methods=['POST'])
@login_required
def news_upload_image():
    if not current_user.role.can_manage_news:
        return jsonify({'error': 'Unauthorized'}), 403
    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify({'error': 'No file'}), 400
    ext = os.path.splitext(secure_filename(file.filename))[1].lower()
    if ext not in {'.jpg', '.jpeg', '.png', '.webp', '.gif'}:
        return jsonify({'error': 'Format tidak didukung'}), 400
    import uuid
    news_dir = os.path.join(app.root_path, 'static', 'uploads', 'news')
    os.makedirs(news_dir, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(news_dir, filename)
    try:
        file.stream.seek(0)
        img = Image.open(file.stream)
        img = ImageOps.exif_transpose(img)
        if ext in {'.jpg', '.jpeg'} and img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        elif ext == '.png' and img.mode not in ('RGB', 'RGBA', 'L', 'P'):
            img = img.convert('RGBA')
        img.thumbnail((1600, 1200), Image.LANCZOS)
        save_kwargs = {'optimize': True}
        if ext in {'.jpg', '.jpeg'}:
            save_kwargs['quality'] = 85
        img.save(filepath, **save_kwargs)
    except Exception as err:
        app.logger.exception('TinyMCE image upload failed: %s', err)
        if os.path.exists(filepath):
            os.remove(filepath)
        return jsonify({'error': 'File bukan gambar yang valid'}), 400
    return jsonify({'location': url_for('static', filename=f'uploads/news/{filename}', _external=True)})


# --- Admin: Manajemen Kategori Berita ---
@app.route('/berita/categories', methods=['GET', 'POST'])
@login_required
def manage_news_categories():
    if not current_user.role.can_manage_news:
        flash('Akses ditolak.')
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        action = request.form.get('action', 'add')
        if action == 'add':
            name  = request.form.get('name', '').strip()
            color = request.form.get('color', '#4361ee').strip()
            if name:
                slug = _unique_slug(name, NewsCategory)
                db.session.add(NewsCategory(name=name, slug=slug, color=color))
                db.session.commit()
                flash(f'Kategori "{name}" berhasil ditambahkan.')
            else:
                flash('Nama kategori wajib diisi.')
        elif action == 'delete':
            cat = NewsCategory.query.get_or_404(int(request.form.get('cat_id', 0)))
            NewsArticle.query.filter_by(category_id=cat.id).update({'category_id': None})
            db.session.delete(cat)
            db.session.commit()
            flash(f'Kategori dihapus.')
        elif action == 'edit':
            cat  = NewsCategory.query.get_or_404(int(request.form.get('cat_id', 0)))
            name = request.form.get('name', '').strip()
            if name:
                cat.name  = name
                cat.color = request.form.get('color', cat.color).strip()
                db.session.commit()
                flash('Kategori berhasil diperbarui.')
        return redirect(url_for('manage_news_categories'))
    categories = NewsCategory.query.order_by(NewsCategory.name).all()
    return render_template('news_categories.html', categories=categories)


# Inisialisasi database saat aplikasi dinyalakan (WSGI-safe)

try:
    init_db()
    with app.app_context():
        auto_recalculate_points()
    app.logger.info('Database initialization completed successfully.')
except Exception as _init_err:
    app.logger.critical(f'FATAL: Database initialization failed: {_init_err}\n'
                        f'The application may not function correctly. '
                        f'Check your database configuration and permissions.')

# Mod_wsgi akan mencari objek "application" secara langsung dari file ini.
# Gunakan "python -m flask run" atau jalankan "wsgi.py" untuk pengembangan lokal.
app.logger.info('Famousbytee application loaded and ready to serve requests.')
