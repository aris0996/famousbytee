from flask import Flask, render_template, redirect, url_for, request, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from models import db, User, Role, ClassRoom, Student, Schedule, Announcement, BatchFund, ActivityLog, SystemSetting
import os
import csv
from io import StringIO
from flask import send_from_directory, make_response
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta

from config import Config

app = Flask(__name__)
app.config.from_object(Config)

UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

db.init_app(app)
login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)

# Ensure database tables are created for new features
with app.app_context():
    db.create_all()

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
    if 'favicon_url' not in settings: settings['favicon_url'] = '/static/favicon.ico'
    return dict(site_settings=settings, datetime=datetime)

@app.errorhandler(404)
def page_not_found(e):
    return render_template('errors/404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    return render_template('errors/500.html'), 500

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
            
            if user.password == password:
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
    return redirect(url_for('login'))

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    announcements = Announcement.query.order_by(Announcement.is_pinned.desc(), Announcement.date_posted.desc()).limit(5).all()
    return render_template('index.html', announcements=announcements)

def log_activity(action, details=None):
    log = ActivityLog(user_id=current_user.id, action=action, details=details)
    db.session.add(log)
    db.session.commit()

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
        start_date = datetime(2024, 3, 30).date()
        today = datetime.now().date()
        target_payment = 0
        if today >= start_date:
            curr = start_date
            while curr <= today:
                if curr.weekday() < 5: target_payment += 1500 # Tarif default
                curr += timedelta(days=1)
        
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

    # 3. Data Statistik Admin/Pengurus (jika punya izin)
    admin_stats = None
    recent_students = []
    if current_user.role.name in ['Admin', 'Pengurus']:
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
                         recent_students=recent_students)

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
                         today_indo=today_indo)

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
    log_activity("Edit Jadwal", f"Matkul: {s.subject}")
    return redirect(url_for('manage_schedule'))

@app.route('/schedule/delete/<int:id>')
@login_required
def delete_schedule(id):
    if not current_user.role.can_manage_schedule: return redirect(url_for('dashboard'))
    s = Schedule.query.get_or_404(id)
    log_activity("Hapus Jadwal", f"Matkul: {s.subject}")
    db.session.delete(s)
    db.session.commit()
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
            is_pinned='is_pinned' in request.form
        )
        db.session.add(ann)
        db.session.commit()
        log_activity("Tambah Pengumuman", f"Judul: {ann.title}")
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
            
        # Handle Tags
        tags = request.form.get('tags', '').strip()
        if tags and not tags.startswith('#'): tags = '#' + tags
        
        fund = BatchFund(
            description=request.form['desc'], 
            amount=float(request.form['amount']), 
            type=request.form['type'], 
            category=request.form['category'],
            evidence_note=request.form['note'],
            recorded_by=current_user.username,
            date=datetime.strptime(request.form['date'], '%Y-%m-%d'),
            student_id=int(student_id) if student_id and student_id != 'none' else None,
            tags=tags
        )
        db.session.add(fund)
        
        # Suggestion #1: Auto-Announcement on Keluar
        if fund.type == 'Keluar':
            ann = Announcement(
                title=f"[PENGELUARAN] {fund.description}",
                content=f"Diberitahukan bahwa dana kas sebesar Rp {fund.amount:,.0f} telah digunakan untuk: {fund.description}. Kategori: {fund.category}. Dicatat oleh: {fund.recorded_by}.",
                category='Penting'
            )
            db.session.add(ann)
            
        db.session.commit()
        log_activity("Tambah Kas", f"Nominal: {fund.amount}, Ket: {fund.description}")
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
    
    start_date = datetime(2024, 3, 30).date()
    today = datetime.now().date()
    target_payment = 0
    if today >= start_date:
        current = start_date
        while current <= today:
            if current.weekday() < 5: target_payment += 1000
            current += timedelta(days=1)
    
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
                         target_daily=1000,
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
        for key in ['web_title', 'web_logo', 'favicon_url']:
            if key in request.form:
                setting = SystemSetting.query.filter_by(key=key).first()
                if setting: setting.value = request.form[key]
                else: db.session.add(SystemSetting(key=key, value=request.form[key]))
        
        # 2. Handle File Uploads (Logo/Favicon)
        for key in ['logo_file', 'favicon_file']:
            if key in request.files:
                file = request.files[key]
                if file.filename != '':
                    filename = secure_filename(f"{key.split('_')[0]}_{file.filename}")
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                    
                    db_key = 'web_logo_path' if 'logo' in key else 'favicon_path'
                    setting = SystemSetting.query.filter_by(key=db_key).first()
                    if setting: setting.value = f"/static/uploads/{filename}"
                    else: db.session.add(SystemSetting(key=db_key, value=f"/static/uploads/{filename}"))
                    
        db.session.commit()
        flash('Pengaturan sistem diperbarui.')
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
            db.session.add(new_user)
            db.session.commit()
            log_activity("Tambah User", f"Username: {new_user.username}")
            flash(f'User {new_user.username} berhasil didaftarkan.')
            
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
    db.session.commit()
    log_activity("Edit User", f"Username: {user.username}")
    flash(f'Data user {user.username} telah diperbarui.')
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
                
                # B. Sinkronisasi tabel 'role'
                role_cols = [c['name'] for c in inspector.get_columns('role')]
                for col in ['can_view_logs', 'can_export_data', 'can_edit_settings']:
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
                    can_view_logs=True, can_export_data=True, can_edit_settings=True
                )
                staff_r = Role(
                    name='Pengurus', description='Manajemen data operasional.', 
                    can_manage_students=True, can_manage_schedule=True, can_manage_fund=True, 
                    can_manage_announcements=True, can_manage_roles=False,
                    can_view_logs=True, can_export_data=True, can_edit_settings=False
                )
                member_r = Role(
                    name='Member', description='Akses dashboard anggota.', 
                    can_manage_students=False, can_manage_schedule=False, can_manage_fund=False, 
                    can_manage_announcements=False, can_manage_roles=False,
                    can_view_logs=False, can_export_data=False, can_edit_settings=False
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
                    SystemSetting(key='favicon_url', value='/static/favicon.ico', description='URL Favicon')
                ])
                db.session.commit()
                print("Status: Pengaturan sistem berhasil diinisialisasi.")
        except Exception as e:
            print(f"Peringatan: Gagal inisialisasi pengaturan: {e}")

# Inisialisasi database saat aplikasi dinyalakan
init_db()

# Mod_wsgi akan mencari objek "application" secara langsung dari file ini.
# Gunakan "python -m flask run" atau jalankan "wsgi.py" untuk pengembangan lokal.
