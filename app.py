from flask import Flask, render_template, redirect, url_for, request, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from flask_migrate import Migrate
from models import db, User, Role, ClassRoom, Student, Schedule, Announcement, BatchFund, ActivityLog, SystemSetting, GalleryAlbum, GalleryPhoto, PhotoComment, AnnouncementRead, Assignment, NotificationHistory
import os
from PIL import Image
import csv
from io import StringIO
from flask import send_from_directory, make_response
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta

import firebase_admin
from firebase_admin import credentials, messaging

from config import Config
from flask_jwt_extended import JWTManager
from flask_cors import CORS
from routes.api import api_bp

app = Flask(__name__)
app.config.from_object(Config)

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
            print("Firebase Admin initialized.")
            return True
        else:
            print("serviceAccountKey.json not found. Push notifications disabled.")
            return False
    except Exception as e:
        print(f"Firebase Init Error: {e}")
        return False

# Try initial load
_initialize_firebase()

def _log_notification_history(title, body, user_id, sender_id, status):
    """Helper to log notification history."""
    try:
        # Safer truncation (20 chars) to support old database schema if not migrated
        safe_status = str(status)[:19]
        
        history = NotificationHistory(
            title=title,
            body=body,
            target=str(user_id) if user_id else "All",
            sent_by=sender_id,
            status=safe_status
        )
        db.session.add(history)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"History Log Error: {e}")

def send_push(title, body, user_id=None, sender_id=None):
    """Sends push notification to a specific user or everyone."""
    if not _initialize_firebase():
        _log_notification_history(title, body, user_id, sender_id, "Failed (Config)")
        return

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
                        token=user.fcm_token
                    )
                    messaging.send(message)
                    status = "Success"
                except Exception as e:
                    status = f"Error: {str(e)[:15]}"
                    if "registration-token-not-registered" in str(e).lower():
                        user.fcm_token = None
                        db.session.commit()
            else:
                status = "No Token"
        else:
            # Broadcast
            users = User.query.filter(User.fcm_token.isnot(None)).all()
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
        status = f"System Error"
    
    _log_notification_history(title, body, user_id, sender_id, status)

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

# Initialize Scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(func=run_automated_reminders, trigger="interval", minutes=1)
scheduler.start()

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
            print("Database schema is up to date.")
        except Exception as e:
            print(f"Migration auto-run skipped or failed: {e}")
    else:
        print("Warning: 'migrations' folder not found. Auto-upgrade skipped.")
    
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
                    'can_manage_gallery': True, 'can_manage_notifications': True,
                    'can_manage_assignments': True, 'can_use_api': True
                }
            },
            'Pengurus': {
                'description': 'Pengurus dengan akses manajemen operasional.',
                'perms': {
                    'can_manage_students': True, 'can_manage_schedule': True,
                    'can_manage_fund': True, 'can_manage_announcements': True,
                    'can_manage_roles': False, 'can_view_logs': False,
                    'can_export_data': True, 'can_edit_settings': False,
                    'can_manage_gallery': True, 'can_manage_notifications': True,
                    'can_manage_assignments': True, 'can_use_api': True
                }
            },
            'Member': {
                'description': 'Anggota biasa dengan akses portal dasar.',
                'perms': {
                    'can_manage_students': False, 'can_manage_schedule': False,
                    'can_manage_fund': False, 'can_manage_announcements': False,
                    'can_manage_roles': False, 'can_view_logs': False,
                    'can_export_data': False, 'can_edit_settings': False,
                    'can_manage_gallery': False, 'can_manage_notifications': False,
                    'can_manage_assignments': False, 'can_use_api': True
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

    def auto_recalculate_points():
        try:
            print("Auto-recalculating points for all users...")
            users = User.query.all()
            for u in users:
                u.points = 0
            
            # +50 for Kas
            funds = BatchFund.query.filter_by(type='Masuk').all()
            for f in funds:
                if f.student_id:
                    student = Student.query.get(f.student_id)
                    if student and student.user:
                        student.user.points += 50
                        
            # +10 for Gallery
            photos = GalleryPhoto.query.filter_by(status='Published').all()
            for p in photos:
                if p.uploaded_by:
                    uploader = User.query.get(p.uploaded_by)
                    if uploader:
                        uploader.points += 10
            
            db.session.commit()
            print("Point Recalculation: Success.")
        except Exception as e:
            db.session.rollback()
            print(f"Point Recalculation Error: {e}")
            
    auto_recalculate_points()


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
    return render_template('errors/404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    # Log the error details here if needed
    error_msg = str(e)
    return render_template('errors/500.html', error=error_msg), 500

@app.route('/report-error', methods=['POST'])
def report_error():
    err_body = request.form.get('error_details')
    page = request.form.get('page_url')
    # Auto log the error report
    log_activity("Error Report", f"User reported error on {page}: {err_body[:200]}")
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

def get_fund_target():
    """Calculates cumulative target based on 1000/day rule (Mon-Fri)"""
    try:
        start_setting = SystemSetting.query.filter_by(key='fund_start_date').first()
        rate_setting = SystemSetting.query.filter_by(key='fund_daily_rate').first()
        
        start_date = datetime.strptime(start_setting.value, '%Y-%m-%d').date() if start_setting else datetime(2024, 3, 30).date()
        daily_rate = int(rate_setting.value) if rate_setting else 1000
    except:
        start_date = datetime(2024, 3, 30).date()
        daily_rate = 1000

    today = datetime.now().date()
    target = 0
    if today >= start_date:
        curr = start_date
        while curr <= today:
            if curr.weekday() < 5: # Monday (0) to Friday (4)
                target += daily_rate
            curr += timedelta(days=1)
    return target


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
    recent_announcements = Announcement.query.order_by(Announcement.is_pinned.desc(), Announcement.date_posted.desc()).limit(5).all()
    
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
    gallery_preview = GalleryPhoto.query.filter_by(status='Published').order_by(GalleryPhoto.created_at.desc()).limit(4).all()

    # 4. NEXT CLASS COUNTDOWN
    # Map Indonesian days to weekday numbers
    day_map = {'Senin': 0, 'Selasa': 1, 'Rabu': 2, 'Kamis': 3, 'Jumat': 4, 'Sabtu': 5, 'Minggu': 6}
    now = datetime.now()
    curr_day_num = now.weekday()
    curr_time_str = now.strftime('%H:%M')
    
    schedules = Schedule.query.all()
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
    
    class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first()
    
    if request.method == 'POST':
        new_m = Student(
            nim=request.form['nim'], 
            full_name=request.form['full_name'], 
            status=request.form['status'], 
            classroom_id=class_fb.id
        )
        db.session.add(new_m)
        db.session.commit()
        log_activity("Tambah Member", f"NIM: {new_m.nim}, Nama: {new_m.full_name}")
        return redirect(url_for('manage_members'))
    
    members = Student.query.filter_by(classroom_id=class_fb.id).all()
    return render_template('members.html', students=members)

@app.route('/members/bulk', methods=['POST'])
@login_required
def bulk_add_members():
    if not current_user.role.can_manage_students: return redirect(url_for('dashboard'))
    data = request.form.get('bulk_data')
    class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first()
    
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
    class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first()
    if request.method == 'POST' and not current_user.role.can_manage_schedule:
        flash('Akses ditolak.')
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        # Single Add logic (remains as is)
        sched = Schedule(
            classroom_id=class_fb.id, 
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
    schedules = Schedule.query.filter_by(classroom_id=class_fb.id).all()
    
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
                         assignments=Assignment.query.order_by(Assignment.deadline.asc()).all())

@app.route('/assignments', methods=['GET', 'POST'])
@login_required
def manage_assignments():
    if not current_user.role.can_manage_assignments:
        flash('Akses ditolak.')
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        a = Assignment(
            title=request.form['title'],
            subject=request.form['subject'],
            deadline=datetime.strptime(request.form['deadline'], '%Y-%m-%dT%H:%M'),
            description=request.form.get('description', '')
        )
        db.session.add(a)
        db.session.commit()
        
        send_push("Tugas Baru!", f"Tugas {a.subject}: {a.title}. Deadline: {a.deadline.strftime('%d %b %H:%M')}")
        log_activity("Tambah Tugas", f"Judul: {a.title}")
        flash('Tugas berhasil ditambahkan!')
        return redirect(url_for('manage_assignments'))
    
    assignments = Assignment.query.order_by(Assignment.deadline.asc()).all()
    return render_template('assignments.html', assignments=assignments)

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
    class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first()
    
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
    class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first()
    
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
    s.day = request.form['day']
    s.time_start = request.form['time_start']
    s.time_end = request.form['time_end']
    s.subject = request.form['subject']
    s.lecturer = request.form['lecturer']
    s.room = request.form['room']
    db.session.commit()
    send_push("Jadwal Diubah!", f"Jadwal {s.subject} telah diperbarui oleh pengurus.")
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
    send_push("Jadwal Dihapus", f"Jadwal {subject_name} telah dihapus dari sistem.")
    return redirect(url_for('manage_schedule'))

# Suggestion #15: Download Template CSV with Current Data
@app.route('/schedule/template')
@login_required
def download_schedule_template():
    if not current_user.role.can_manage_schedule: return redirect(url_for('dashboard'))
    class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first()
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
        ann = Announcement(
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
    
    announcements = Announcement.query.order_by(Announcement.is_pinned.desc(), Announcement.date_posted.desc()).all()
    return render_template('announcements.html', announcements=announcements)

@app.route('/announcements/edit/<int:id>', methods=['POST'])
@login_required
def edit_announcement(id):
    if not current_user.role.can_manage_announcements: return redirect(url_for('dashboard'))
    ann = Announcement.query.get_or_404(id)
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
    log_activity("Hapus Pengumuman", f"Judul: {ann.title}")
    db.session.delete(ann)
    db.session.commit()
    return redirect(url_for('manage_announcements'))

@app.route('/fund', methods=['GET', 'POST'])
@login_required
def manage_fund():
    class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first()
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
            if s and s.user:
                s.user.points = (s.user.points or 0) + 50
                log_activity("Point Awarded", f"+50 Poin untuk {s.full_name} (Bayar Kas)")
                
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
                         target_daily=int(SystemSetting.query.filter_by(key='fund_daily_rate').first().value) if SystemSetting.query.filter_by(key='fund_daily_rate').first() else 1000,
                         today_date=datetime.now().strftime('%Y-%m-%d'))

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
        text_keys = ['web_title', 'web_logo', 'favicon_url', 'fund_start_date', 'fund_daily_rate', 
                     'web_desc', 'social_ig', 'social_wa', 'seo_keywords']
        for key in text_keys:
            if key in request.form:
                val = request.form[key]
                setting = SystemSetting.query.filter_by(key=key).first()
                if setting: setting.value = val
                else: db.session.add(SystemSetting(key=key, value=val))
        
        # 2. Handle Branding File Uploads (Logo/Favicon) with Auto-Compression
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
    return render_template('settings.html', settings=settings)

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
                can_edit_settings='can_edit_settings' in request.form
            )
            db.session.add(new_role)
            db.session.commit()
            log_activity("Tambah Role", f"Nama: {new_role.name}")
            flash(f'Role {new_role.name} berhasil dibuat.')
            
        # 2. Buat User Baru (Manual)
        elif 'username' in request.form and 'password' in request.form:
            s_id = request.form.get('student_id')
            new_user = User(
                username=request.form['username'], 
                password=request.form['password'], 
                role_id=request.form['role_id'], 
                full_name=request.form['full_name'], 
                email=request.form['email'],
                student_id=int(s_id) if s_id and s_id != 'none' else None,
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
    return render_template('roles.html', roles=roles, users=users, students=students)

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
    role.can_use_api = 'can_use_api' in request.form
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
    # Admin/Pengurus can see all (including pending), Members can only see Published
    if current_user.role.name in ['Admin', 'Pengurus']:
        photos = GalleryPhoto.query.order_by(GalleryPhoto.created_at.desc()).all()
    else:
        # Show Published ones, PLUS photos uploaded by user that are still Pending
        photos = GalleryPhoto.query.filter(
            (GalleryPhoto.status == 'Published') | 
            ((GalleryPhoto.status == 'Pending') & (GalleryPhoto.uploaded_by == current_user.id))
        ).order_by(GalleryPhoto.created_at.desc()).all()
        
    return render_template('gallery.html', photos=photos)

@app.route('/gallery/upload', methods=['POST'])
@login_required
def upload_gallery():
    files = request.files.getlist('photos')
    if not files or files[0].filename == '':
        flash('Tidak ada file yang dipilih.')
        return redirect(url_for('manage_gallery'))
    
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
    class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first()
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
                if 'bio' not in user_cols:
                    conn.execute(text("ALTER TABLE user ADD COLUMN bio VARCHAR(255) NULL"))
                if 'whatsapp' not in user_cols:
                    conn.execute(text("ALTER TABLE user ADD COLUMN whatsapp VARCHAR(20) NULL"))
                if 'points' not in user_cols:
                    conn.execute(text("ALTER TABLE user ADD COLUMN points INTEGER DEFAULT 0"))
                
                # B. Sinkronisasi tabel 'role'
                role_cols = [c['name'] for c in inspector.get_columns('role')]
                for col in ['can_view_logs', 'can_export_data', 'can_edit_settings', 'can_manage_gallery', 'can_use_api']:
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
                if 'is_public' not in ann_cols:
                    conn.execute(text("ALTER TABLE announcement ADD COLUMN is_public BOOLEAN DEFAULT 1"))
                
                # E. Sinkronisasi tabel 'gallery_photo'
                gallery_cols = [c['name'] for c in inspector.get_columns('gallery_photo')]
                if 'status' not in gallery_cols:
                    conn.execute(text("ALTER TABLE gallery_photo ADD COLUMN status VARCHAR(20) DEFAULT 'Published'"))

                # F. Sinkronisasi tabel 'announcement_read'
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
            if not Role.query.first():
                # Definisikan Role Default dengan izin baru
                admin_r = Role(
                    name='Admin', description='Akses penuh koordinasi sistem.', 
                    can_manage_students=True, can_manage_schedule=True, can_manage_fund=True, 
                    can_manage_announcements=True, can_manage_roles=True,
                    can_view_logs=True, can_export_data=True, can_edit_settings=True, can_manage_gallery=True,
                    can_use_api=True
                )
                staff_r = Role(
                    name='Pengurus', description='Manajemen data operasional.', 
                    can_manage_students=True, can_manage_schedule=True, can_manage_fund=True, 
                    can_manage_announcements=True, can_manage_roles=False,
                    can_view_logs=True, can_export_data=True, can_edit_settings=False, can_manage_gallery=True,
                    can_use_api=False
                )
                member_r = Role(
                    name='Member', description='Akses dashboard anggota.', 
                    can_manage_students=False, can_manage_schedule=False, can_manage_fund=False, 
                    can_manage_announcements=False, can_manage_roles=False,
                    can_view_logs=False, can_export_data=False, can_edit_settings=False, can_manage_gallery=False,
                    can_use_api=False
                )
                db.session.add_all([admin_r, staff_r, member_r])
                db.session.commit()
                
                # Buat Admin Default
                db.session.add(User(username='admin', password='admin', role_id=admin_r.id))
                
                # Seeding Awal
                fb = ClassRoom(name='Famousbytee.b', batch='TI 2024')
                db.session.add(fb)
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
                    SystemSetting(key='fund_daily_rate', value='1000', description='Iuran Harian Kas (Senin-Jumat)'),
                    SystemSetting(key='web_desc', value='Portal Resmi Manajemen Kelas Famousbytee.b', description='Deskripsi Web (SEO)'),
                    SystemSetting(key='social_ig', value='#', description='Link Instagram Kelas'),
                    SystemSetting(key='social_wa', value='#', description='Link WhatsApp Group'),
                    SystemSetting(key='seo_keywords', value='famousbytee, portal, kelas, manajemen', description='Kata Kunci SEO (Pisahkan dengan koma)')
                ])
                db.session.commit()
                print("Status: Pengaturan sistem berhasil diinisialisasi.")
        except Exception as e:
            print(f"Peringatan: Gagal inisialisasi pengaturan: {e}")

# ----------------API & SITEMAP----------------
from flask import jsonify

@app.route('/api/students')
@login_required
def api_students():
    if not current_user.role.can_use_api: return jsonify({'error': 'Unauthorized'}), 403
    students = Student.query.all()
    return jsonify([{'id': s.id, 'nim': s.nim, 'name': s.full_name, 'status': s.status} for s in students])

@app.route('/api/announcements')
@login_required
def api_announcements():
    if not current_user.role.can_use_api: return jsonify({'error': 'Unauthorized'}), 403
    anns = Announcement.query.all()
    return jsonify([{'id': a.id, 'title': a.title, 'category': a.category, 'date': a.date_posted} for a in anns])

@app.route('/notifications', methods=['GET', 'POST'])
@login_required
def manage_notifications():
    if not current_user.role.can_manage_notifications:
        flash('Akses ditolak.')
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        if request.is_json:
            data = request.get_json()
            title = data.get('title')
            body = data.get('body')
            target = data.get('target')
        else:
            title = request.form.get('title')
            body = request.form.get('body')
            target = request.form.get('target') # "all" or user_id
        
        if target == 'all':
            send_push(title, body, sender_id=current_user.id)
            flash('Notifikasi siaran berhasil dikirim!')
        else:
            send_push(title, body, user_id=int(target), sender_id=current_user.id)
            flash('Notifikasi terkirim ke pengguna.')
            
        log_activity("Kirim Notifikasi", f"Judul: {title}, Target: {target}")
        return redirect(url_for('manage_notifications'))
        
    history = NotificationHistory.query.order_by(NotificationHistory.sent_at.desc()).limit(50).all()
    users = User.query.filter(User.fcm_token.isnot(None)).all()
    return render_template('notifications.html', history=history, users=users)

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

# Inisialisasi database saat aplikasi dinyalakan
init_db()

# Mod_wsgi akan mencari objek "application" secara langsung dari file ini.
# Gunakan "python -m flask run" atau jalankan "wsgi.py" untuk pengembangan lokal.
