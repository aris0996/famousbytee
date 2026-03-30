from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

db = SQLAlchemy()

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(120), nullable=False)
    role_id = db.Column(db.Integer, db.ForeignKey('role.id'))
    
    # Detailed fields
    full_name = db.Column(db.String(100))
    email = db.Column(db.String(120), unique=True)
    status = db.Column(db.String(20), default='Active') # Active, Inactive
    last_login = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())

class Role(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    description = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    users = db.relationship('User', backref='role', lazy=True)
    
    # Granular Permissions
    can_manage_students = db.Column(db.Boolean, default=False)
    can_manage_schedule = db.Column(db.Boolean, default=False)
    can_manage_fund = db.Column(db.Boolean, default=False)
    can_manage_announcements = db.Column(db.Boolean, default=False)
    can_manage_roles = db.Column(db.Boolean, default=False)

class ClassRoom(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False) # e.g., 2A, 2B
    batch = db.Column(db.String(50)) # e.g., 2024
    students = db.relationship('Student', backref='classroom', lazy=True)
    schedules = db.relationship('Schedule', backref='classroom', lazy=True)

class Student(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nim = db.Column(db.String(20), unique=True, nullable=False)
    full_name = db.Column(db.String(100), nullable=False)
    classroom_id = db.Column(db.Integer, db.ForeignKey('class_room.id'))
    status = db.Column(db.String(20), default='Aktif') # Aktif, Cuti, Lulus, Drop-out

class ActivityLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    user = db.relationship('User', backref='logs', lazy=True)
    action = db.Column(db.String(255), nullable=False) # e.g., "Menambah Kas", "Mengubah Jadwal"
    timestamp = db.Column(db.DateTime, default=db.func.current_timestamp())
    details = db.Column(db.Text) # e.g., "ID Transaksi: 5, Alasan: Typo"

class Schedule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey('class_room.id'))
    day = db.Column(db.String(20), nullable=False)
    time_start = db.Column(db.String(10))
    time_end = db.Column(db.String(10))
    subject = db.Column(db.String(100), nullable=False)
    lecturer = db.Column(db.String(100))
    room = db.Column(db.String(50))

class Announcement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    content = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(50), default='Info') # Info, Penting, Event
    is_pinned = db.Column(db.Boolean, default=False)
    date_posted = db.Column(db.DateTime, default=db.func.current_timestamp())

class BatchFund(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(200), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    type = db.Column(db.String(10)) # Masuk, Keluar
    category = db.Column(db.String(50), default='Iuran') # Iuran, Perlengkapan, Event, Lain-lain
    evidence_note = db.Column(db.String(255)) # Bukti transaksi/catatan
    date = db.Column(db.Date, nullable=False)
    recorded_by = db.Column(db.String(100)) # Nama pengurus yang input
    
    # Financial tracking link
    student_id = db.Column(db.Integer, db.ForeignKey('student.id'), nullable=True)
    student = db.relationship('Student', backref='payments', lazy=True)

    # Audit fields
    is_edited = db.Column(db.Boolean, default=False)
    edit_reason = db.Column(db.String(255))
    last_edited_by = db.Column(db.String(100))
