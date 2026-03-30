from flask import Flask, render_template, redirect, url_for, request, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from models import db, User, Role, ClassRoom, Student, Schedule, Announcement, BatchFund, ActivityLog
import os
from datetime import datetime, timedelta

from config import Config

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)
login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.route('/login', methods=['GET', 'POST'])
def login():
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
            # Create user account for member
            member_role = Role.query.filter_by(name='Member').first()
            new_user = User(
                username=nim,
                password=password,
                role_id=member_role.id,
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
    stats = {
        'total_members': Student.query.count(),
        'total_announcements': Announcement.query.count(),
        'balance': sum(f.amount if f.type == 'Masuk' else -f.amount for f in BatchFund.query.all())
    }
    recent_announcements = Announcement.query.order_by(Announcement.is_pinned.desc(), Announcement.date_posted.desc()).limit(3).all()
    recent_members = Student.query.order_by(Student.id.desc()).limit(5).all()
    return render_template('dashboard.html', stats=stats, recent_students=recent_members, recent_announcements=recent_announcements)

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
    if request.method == 'POST' and not current_user.role.can_manage_schedule:
        flash('Akses ditolak.')
        return redirect(url_for('dashboard'))
    
    class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first()
    
    if request.method == 'POST':
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
        log_activity("Tambah Jadwal", f"Matkul: {sched.subject}, Hari: {sched.day}")
        return redirect(url_for('manage_schedule'))
    
    schedules = Schedule.query.filter_by(classroom_id=class_fb.id).all()
    return render_template('schedule.html', schedules=schedules)

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
            
        student_id = request.form.get('student_id')
        fund = BatchFund(
            description=request.form['desc'], 
            amount=float(request.form['amount']), 
            type=request.form['type'], 
            category=request.form['category'],
            evidence_note=request.form['note'],
            recorded_by=current_user.username,
            date=datetime.strptime(request.form['date'], '%Y-%m-%d'),
            student_id=int(student_id) if student_id and student_id != 'none' else None
        )
        db.session.add(fund)
        db.session.commit()
        log_activity("Tambah Kas", f"Nominal: {fund.amount}, Ket: {fund.description}")
        return redirect(url_for('manage_fund'))
    
    funds = BatchFund.query.order_by(BatchFund.date.desc()).all()
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
    for s in students:
        total_paid = db.session.query(db.func.sum(BatchFund.amount)).filter(BatchFund.student_id == s.id, BatchFund.type == 'Masuk').scalar() or 0
        member_statuses.append({'student': s,'paid': total_paid,'target': target_payment,'arrears': target_payment - total_paid})
        
    return render_template('batch_fund.html', funds=funds, balance=balance, students=students, member_statuses=member_statuses, target_daily=1000)

@app.route('/fund/edit/<int:id>', methods=['POST'])
@login_required
def edit_fund(id):
    if not current_user.role.can_manage_fund: return redirect(url_for('dashboard'))
    f = BatchFund.query.get_or_404(id)
    reason = request.form.get('reason')
    if not reason:
        flash('Alasan perubahan wajib diisi.')
        return redirect(url_for('manage_fund'))
        
    f.is_edited = True
    f.edit_reason = reason
    f.last_edited_by = current_user.username
    f.description = request.form['desc']
    f.amount = float(request.form['amount'])
    f.type = request.form['type']
    f.category = request.form['category']
    db.session.commit()
    
    # Auto Announcement for Edit
    new_ann = Announcement(
        title=f"Update Transaksi Kas: {f.description}",
        content=f"Transaksi ID: {f.id} telah diperbarui oleh {current_user.username}.\nAlasan: {reason}\nNilai Baru: Rp {f.amount:,.0f}",
        category='Penting'
    )
    db.session.add(new_ann)
    db.session.commit()
    
    log_activity("Edit Kas", f"ID: {id}, Alasan: {reason}")
    flash('Transaksi diperbarui dan diumumkan.')
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

@app.route('/roles', methods=['GET', 'POST'])
@login_required
def manage_roles():
    if not current_user.role.can_manage_roles:
        flash('Akses ditolak.')
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        if 'role_name' in request.form:
            new_role = Role(
                name=request.form['role_name'], 
                description=request.form['role_desc'], 
                can_manage_students='can_manage_students' in request.form, 
                can_manage_schedule='can_manage_schedule' in request.form, 
                can_manage_fund='can_manage_fund' in request.form,
                can_manage_announcements='can_manage_announcements' in request.form,
                can_manage_roles='can_manage_roles' in request.form
            )
            db.session.add(new_role)
            db.session.commit()
            log_activity("Tambah Role", f"Nama: {new_role.name}")
        elif 'username' in request.form:
            new_user = User(username=request.form['username'], password=request.form['password'], role_id=request.form['role_id'], full_name=request.form['full_name'], email=request.form['email'])
            db.session.add(new_user)
            db.session.commit()
            log_activity("Tambah User", f"Username: {new_user.username}")
        return redirect(url_for('manage_roles'))
    roles = Role.query.all()
    users = User.query.all()
    return render_template('roles.html', roles=roles, users=users)

@app.route('/roles/edit/user/<int:id>', methods=['POST'])
@login_required
def edit_user(id):
    if not current_user.role.can_manage_roles: return redirect(url_for('dashboard'))
    user = User.query.get_or_404(id)
    user.full_name = request.form['full_name']
    user.email = request.form['email']
    user.role_id = request.form['role_id']
    user.status = request.form['status']
    if request.form['password']:
        user.password = request.form['password']
    db.session.commit()
    log_activity("Edit User", f"Username: {user.username}")
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

def init_db():
    with app.app_context():
        db.create_all()
        if not Role.query.first():
            admin_r = Role(name='Admin', description='Akses penuh koordinasi.', can_manage_students=True, can_manage_schedule=True, can_manage_fund=True, can_manage_announcements=True, can_manage_roles=True)
            staff_r = Role(name='Pengurus', description='Manajemen data mahasiswa & jadwal.', can_manage_students=True, can_manage_schedule=True, can_manage_fund=True, can_manage_announcements=True, can_manage_roles=False)
            member_r = Role(name='Member', description='Akses dashboard anggota.', can_manage_students=False, can_manage_schedule=False, can_manage_fund=False, can_manage_announcements=False, can_manage_roles=False)
            db.session.add_all([admin_r, staff_r, member_r])
            db.session.commit()
            db.session.add(User(username='admin', password='admin', role_id=admin_r.id))
            fb = ClassRoom(name='Famousbytee.b', batch='TI 2024')
            db.session.add(fb)
            a1 = Announcement(title='Selamat Datang di Portal Famousbytee.b', content='Ini adalah portal resmi khusus untuk anggota kelas Famousbytee.b.', category='Penting', is_pinned=True)
            db.session.add(a1)
            f1 = BatchFund(description='Saldo Awal Kas Kelas', amount=1000000, type='Masuk', category='Iuran Mingguan', date=datetime.now(), recorded_by='System')
            db.session.add(f1)
            db.session.commit()

# Mod_wsgi akan mencari objek "application" secara langsung dari file ini.
# Gunakan "python -m flask run" atau jalankan "wsgi.py" untuk pengembangan lokal.
