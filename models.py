from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

db = SQLAlchemy()

MEMBER_STATUSES = ('Aktif', 'Nonaktif', 'Cuti', 'Lulus', 'Pindah Kampus', 'Drop-out')


def normalize_member_status(value, default='Aktif'):
    text = str(value or default).strip()
    return text if text in MEMBER_STATUSES else default

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    role_id = db.Column(db.Integer, db.ForeignKey('role.id'))
    classroom_id = db.Column(db.Integer, db.ForeignKey('class_room.id'), nullable=True)
    
    # Personal Link to Student Data
    student_id = db.Column(db.Integer, db.ForeignKey('student.id'), nullable=True)
    student = db.relationship('Student', backref=db.backref('user', uselist=False), lazy=True)
    classroom = db.relationship('ClassRoom', backref=db.backref('users', lazy=True), lazy=True)
    
    full_name = db.Column(db.String(100))
    email = db.Column(db.String(120), unique=True)
    bio = db.Column(db.String(255))
    whatsapp = db.Column(db.String(20))
    status = db.Column(db.String(20), default='Active') # Active, Inactive
    last_login = db.Column(db.DateTime)
    fcm_token = db.Column(db.Text)
    points = db.Column(db.Integer, default=0)
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
    
    # Advanced Permissions
    can_view_logs = db.Column(db.Boolean, default=False)
    can_export_data = db.Column(db.Boolean, default=False)
    can_edit_settings = db.Column(db.Boolean, default=False)
    can_manage_gallery = db.Column(db.Boolean, default=False)
    can_manage_notifications = db.Column(db.Boolean, default=False)
    can_manage_whatsapp = db.Column(db.Boolean, default=False)
    can_manage_assignments = db.Column(db.Boolean, default=False)
    can_use_api = db.Column(db.Boolean, default=False) # New: API Access Control
    can_manage_news = db.Column(db.Boolean, default=False) # Manajemen Berita

    can_access_multi_classroom = db.Column(db.Boolean, default=False)
    can_switch_classroom_context = db.Column(db.Boolean, default=False)
    can_manage_classrooms = db.Column(db.Boolean, default=False)
    can_assign_users_to_classroom = db.Column(db.Boolean, default=False)
    can_move_users_between_classrooms = db.Column(db.Boolean, default=False)
    can_view_all_classrooms = db.Column(db.Boolean, default=False)
    can_manage_students_multi_class = db.Column(db.Boolean, default=False)
    can_manage_schedule_multi_class = db.Column(db.Boolean, default=False)
    can_manage_announcements_multi_class = db.Column(db.Boolean, default=False)
    can_manage_assignments_multi_class = db.Column(db.Boolean, default=False)
    can_manage_gallery_multi_class = db.Column(db.Boolean, default=False)
    can_manage_notifications_multi_class = db.Column(db.Boolean, default=False)
    can_view_classroom_reports = db.Column(db.Boolean, default=False)
    can_export_classroom_data = db.Column(db.Boolean, default=False)

    @property
    def sidobe_enabled(self):
        return self.can_manage_whatsapp

    @sidobe_enabled.setter
    def sidobe_enabled(self, value):
        self.can_manage_whatsapp = bool(value)


class GalleryAlbum(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text)
    cover_image = db.Column(db.String(255))
    is_public = db.Column(db.Boolean, default=True) # Public vs Member Only
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    photos = db.relationship('GalleryPhoto', backref='album', lazy=True, cascade='all, delete-orphan')

class GalleryPhoto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey('class_room.id'), nullable=True)
    album_id = db.Column(db.Integer, db.ForeignKey('gallery_album.id'), nullable=True)
    filename = db.Column(db.String(255), nullable=False)
    thumbnail = db.Column(db.String(255), nullable=False)
    caption = db.Column(db.String(255))
    is_public = db.Column(db.Boolean, default=True) # Public vs Member Only
    uploaded_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    user = db.relationship('User', backref='uploaded_photos', lazy=True)
    tags = db.Column(db.String(200)) # e.g., "#Kelompok3 #Ujian"
    likes_count = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default='Published') # Pending, Published, Rejected
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    comments = db.relationship('PhotoComment', backref='photo', lazy=True, cascade='all, delete-orphan')
    classroom = db.relationship('ClassRoom', backref=db.backref('gallery_photos', lazy=True), lazy=True)

class PhotoComment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    photo_id = db.Column(db.Integer, db.ForeignKey('gallery_photo.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship('User', backref='comments_made', lazy=True)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())

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
    classroom_id = db.Column(db.Integer, db.ForeignKey('class_room.id'), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    user = db.relationship('User', backref='logs', lazy=True)
    classroom = db.relationship('ClassRoom', backref=db.backref('activity_logs', lazy=True), lazy=True)
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

class SchedulePreset(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey('class_room.id'), nullable=True)
    name = db.Column(db.String(120), nullable=False)
    subject = db.Column(db.String(100), nullable=False)
    lecturer = db.Column(db.String(100))
    room = db.Column(db.String(50))
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())
    classroom = db.relationship('ClassRoom', backref='schedule_presets', lazy=True)
    creator = db.relationship('User', backref='schedule_presets', lazy=True)

class ScheduleTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey('class_room.id'), nullable=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    is_default = db.Column(db.Boolean, default=False)
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())
    classroom = db.relationship('ClassRoom', backref='schedule_templates', lazy=True)
    creator = db.relationship('User', backref='schedule_templates', lazy=True)
    items = db.relationship(
        'ScheduleTemplateItem',
        backref='template',
        lazy=True,
        cascade='all, delete-orphan',
        order_by='ScheduleTemplateItem.sort_order'
    )

class ScheduleTemplateItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey('schedule_template.id'), nullable=False)
    day = db.Column(db.String(20), nullable=False)
    time_start = db.Column(db.String(10))
    time_end = db.Column(db.String(10))
    subject = db.Column(db.String(100), nullable=False)
    lecturer = db.Column(db.String(100))
    room = db.Column(db.String(50))
    sort_order = db.Column(db.Integer, default=0)

class Announcement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey('class_room.id'), nullable=True)
    title = db.Column(db.String(150), nullable=False)
    content = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(50), default='Info') # Info, Penting, Event
    is_pinned = db.Column(db.Boolean, default=False)
    is_public = db.Column(db.Boolean, default=True) # Public vs Internal
    date_posted = db.Column(db.DateTime, default=db.func.current_timestamp())
    classroom = db.relationship('ClassRoom', backref=db.backref('announcements', lazy=True), lazy=True)

class BatchFund(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey('class_room.id'), nullable=True)
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
    original_amount = db.Column(db.Float)
    original_description = db.Column(db.String(200))
    tags = db.Column(db.String(100)) # e.g., "#Event #Futsal"
    classroom = db.relationship('ClassRoom', backref=db.backref('batch_funds', lazy=True), lazy=True)

class FundPeriod(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey('class_room.id'), nullable=True)
    title = db.Column(db.String(100), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    daily_rate = db.Column(db.Integer, nullable=False, default=1000)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    classroom = db.relationship('ClassRoom', backref=db.backref('fund_periods', lazy=True), lazy=True)

class SystemSetting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.Text)
    description = db.Column(db.String(255))

class ClassroomNotificationConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey('class_room.id'), nullable=False, unique=True)
    push_enabled = db.Column(db.Boolean, default=True)
    whatsapp_enabled = db.Column(db.Boolean, default=False)
    default_channel = db.Column(db.String(20), default='push')
    announcement_enabled = db.Column(db.Boolean, default=True)
    assignment_enabled = db.Column(db.Boolean, default=True)
    schedule_enabled = db.Column(db.Boolean, default=True)
    finance_enabled = db.Column(db.Boolean, default=True)
    emergency_enabled = db.Column(db.Boolean, default=True)
    updated_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())
    classroom = db.relationship('ClassRoom', backref=db.backref('notification_config', uselist=False), lazy=True)
    updater = db.relationship('User', backref='updated_classroom_notification_configs', lazy=True)

    @property
    def sidobe_enabled(self):
        return self.whatsapp_enabled

    @sidobe_enabled.setter
    def sidobe_enabled(self, value):
        self.whatsapp_enabled = bool(value)

class WhatsAppBot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    provider = db.Column(db.String(30), default='sidobe')
    # Legacy column name retained to avoid a destructive migration. For Sidobe
    # this stores sender_phone (a registered device number in E.164 format).
    session_name = db.Column(db.String(120), nullable=False)
    base_url = db.Column(db.String(255))
    status = db.Column(db.String(30), default='unknown')
    is_active = db.Column(db.Boolean, default=True)
    last_seen_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())

    @property
    def sidobe_provider(self):
        return self.provider

    @sidobe_provider.setter
    def sidobe_provider(self, value):
        self.provider = value

class ClassroomWhatsAppBinding(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey('class_room.id'), nullable=False, unique=True)
    bot_id = db.Column(db.Integer, db.ForeignKey('whats_app_bot.id'), nullable=False)
    chat_id = db.Column(db.String(255), nullable=False)
    chat_label = db.Column(db.String(120))
    is_default = db.Column(db.Boolean, default=True)
    updated_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())
    classroom = db.relationship('ClassRoom', backref=db.backref('whatsapp_binding', uselist=False), lazy=True)
    bot = db.relationship('WhatsAppBot', backref=db.backref('classroom_bindings', lazy=True), lazy=True)
    updater = db.relationship('User', backref='updated_classroom_whatsapp_bindings', lazy=True)

    @property
    def sidobe_binding(self):
        return self

class Assignment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey('class_room.id'), nullable=True)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text)
    subject = db.Column(db.String(100))
    deadline = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    is_public = db.Column(db.Boolean, default=True)
    classroom = db.relationship('ClassRoom', backref=db.backref('assignments', lazy=True), lazy=True)

class AnnouncementRead(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    announcement_id = db.Column(db.Integer, db.ForeignKey('announcement.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    read_at = db.Column(db.DateTime, default=db.func.current_timestamp())

    __table_args__ = (db.UniqueConstraint('announcement_id', 'user_id', name='_ann_user_read_uc'),)

class NotificationHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey('class_room.id'), nullable=True)
    bot_id = db.Column(db.Integer, db.ForeignKey('whats_app_bot.id'), nullable=True)
    title = db.Column(db.String(150), nullable=False)
    body = db.Column(db.Text, nullable=False)
    channel = db.Column(db.String(20), default='push') # push, whatsapp, multi
    category = db.Column(db.String(50), nullable=True)
    delivery_mode = db.Column(db.String(30), nullable=True)
    chat_id = db.Column(db.String(255), nullable=True)
    target = db.Column(db.String(50)) # "All" or a specific user_id
    sent_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    user = db.relationship('User', backref='notifications_sent', lazy=True)
    sent_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    status = db.Column(db.String(100)) # Success, Failed, Error Details
    classroom = db.relationship('ClassRoom', backref=db.backref('notification_histories', lazy=True), lazy=True)
    bot = db.relationship('WhatsAppBot', backref=db.backref('notification_histories', lazy=True), lazy=True)


# Compatibility aliases for the Si Dobe migration layer.
SidobeBot = WhatsAppBot
SidobeBinding = ClassroomWhatsAppBinding


class NewsCategory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    slug = db.Column(db.String(100), nullable=False, unique=True)
    color = db.Column(db.String(20), default='#4361ee')  # hex color for badge
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    articles = db.relationship('NewsArticle', backref='category', lazy=True)


class NewsArticle(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey('news_category.id'), nullable=True)
    title = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(250), nullable=False, unique=True)
    content = db.Column(db.Text, nullable=False)
    excerpt = db.Column(db.String(500))
    cover_image = db.Column(db.String(255))   # filename relative to static/uploads/news/
    status = db.Column(db.String(20), default='Draft')  # Draft, Published, Archived
    is_public = db.Column(db.Boolean, default=True)     # True = visible to guests
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    author = db.relationship('User', backref='news_articles', lazy=True)
    views = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(),
                           onupdate=db.func.current_timestamp())
    published_at = db.Column(db.DateTime, nullable=True)
