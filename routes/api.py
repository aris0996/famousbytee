from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity
from models import db, User, Announcement, Schedule, BatchFund, Student, GalleryPhoto, SystemSetting, FundPeriod, ActivityLog, Assignment, NotificationHistory
from datetime import datetime, timedelta
import os
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash
from sqlalchemy import or_

api_bp = Blueprint('api', __name__, url_prefix='/api')


def _explore_contains(column, query):
    return db.func.lower(db.func.coalesce(column, '')).like(f"%{query}%")


def _explore_snippet(*values, fallback=''):
    for value in values:
        text = str(value or '').strip()
        if text:
            return text[:140]
    return fallback


def _day_index(day_name):
    order = {
        'senin': 0,
        'selasa': 1,
        'rabu': 2,
        'kamis': 3,
        'jumat': 4,
        'sabtu': 5,
        'minggu': 6,
    }
    return order.get((day_name or '').strip().lower(), 7)


def _next_schedule_sort_at(schedule):
    now = datetime.now()
    current_day = now.weekday()
    target_day = _day_index(schedule.day)
    delta_days = (target_day - current_day) % 7

    try:
        hour, minute = [int(part) for part in (schedule.time_start or '00:00').split(':')[:2]]
    except Exception:
        hour, minute = 0, 0

    next_occurrence = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=delta_days)
    if delta_days == 0 and next_occurrence < now:
        next_occurrence += timedelta(days=7)

    # Reverse closeness into descending-friendly value: sooner schedule gets larger timestamp.
    return now + timedelta(days=7) - (next_occurrence - now)


def _interleave_explore_items(items):
    grouped = {}
    for item in items:
        grouped.setdefault(item['type'], []).append(item)

    for bucket in grouped.values():
        bucket.sort(key=lambda value: value['sort_at'], reverse=True)

    group_order = sorted(
        grouped.keys(),
        key=lambda key: grouped[key][0]['sort_at'] if grouped[key] else datetime.min,
        reverse=True
    )

    mixed = []
    while True:
        added = False
        for key in group_order:
            if grouped[key]:
                mixed.append(grouped[key].pop(0))
                added = True
        if not added:
            break
    return mixed

@api_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or request.form or {}
    if not data:
        return jsonify({"msg": "Missing JSON in request"}), 400
        
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    
    if not username or not password:
        return jsonify({"msg": "Missing username or password"}), 400
    
    user = User.query.filter(
        or_(
            db.func.lower(User.username) == username.lower(),
            db.func.lower(User.email) == username.lower(),
            User.student.has(Student.nim == username)
        )
    ).first()
    
    # Check if user exists and password matches
    if user:
        stored_password = user.password or ''
        if stored_password.startswith('scrypt:') or stored_password.startswith('pbkdf2:'):
            is_valid = check_password_hash(stored_password, password)
        else:
            is_valid = (stored_password == password)

        if not is_valid:
            return jsonify({"msg": "Invalid credentials"}), 401

        if user.status != 'Active':
            return jsonify({"msg": "Account is disabled"}), 403
            
        # Enforce API Access Control from Role
        if not user.role.can_use_api:
            return jsonify({"msg": "Your role does not have API access permissions"}), 403

        student_data = None
        if user.student:
            target = get_fund_target()
            total_paid = db.session.query(db.func.sum(BatchFund.amount)).filter(
                BatchFund.student_id == user.student.id, 
                BatchFund.type == 'Masuk'
            ).scalar() or 0
            
            student_data = {
                "nim": user.student.nim,
                "full_name": user.student.full_name,
                "status": user.student.status,
                "financial": {
                    "paid": total_paid,
                    "target": target,
                    "arrears": max(0, target - total_paid)
                }
            }

        access_token = create_access_token(identity=str(user.id), expires_delta=timedelta(days=7))
        
        return jsonify({
            "access_token": access_token,
            "user": {
                "id": user.id,
                "username": user.username,
                "full_name": user.full_name,
                "email": user.email,
                "role": user.role.name,
                "points": user.points or 0,
                "permissions": {
                    "can_manage_students": user.role.can_manage_students,
                    "can_manage_schedule": user.role.can_manage_schedule,
                    "can_manage_fund": user.role.can_manage_fund,
                    "can_manage_announcements": user.role.can_manage_announcements,
                    "can_manage_notifications": user.role.can_manage_notifications,
                    "can_manage_whatsapp": user.role.can_manage_whatsapp,
                    "can_manage_gallery": user.role.can_manage_gallery,
                    "can_manage_assignments": user.role.can_manage_assignments,
                    "can_view_logs": user.role.can_view_logs,
                },
                "student": student_data
            }
        }), 200
    
    return jsonify({"msg": "Invalid credentials"}), 401

@api_bp.route('/change-password', methods=['POST'])
@jwt_required()
def change_password():
    data = request.get_json()
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    
    old_password = data.get('old_password')
    new_password = data.get('new_password')
    
    if not old_password or not new_password:
        return jsonify({"msg": "Old and new password required"}), 400
        
    if user.password != old_password:
        return jsonify({"msg": "Old password incorrect"}), 401
        
    user.password = new_password
    db.session.commit()
    
    return jsonify({"msg": "Password changed successfully"})

@api_bp.route('/profile', methods=['GET'])
@jwt_required()
def get_profile():
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    
    if not user:
        return jsonify({"msg": "User not found"}), 404
        
    student_data = None
    if user.student:
        target = get_fund_target()
        total_paid = db.session.query(db.func.sum(BatchFund.amount)).filter(
            BatchFund.student_id == user.student.id, 
            BatchFund.type == 'Masuk'
        ).scalar() or 0
        
        student_data = {
            "nim": user.student.nim,
            "full_name": user.student.full_name,
            "status": user.student.status,
            "financial": {
                "paid": total_paid,
                "target": target,
                "arrears": max(0, target - total_paid)
            }
        }

    return jsonify({
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "email": user.email,
        "role": user.role.name,
        "points": user.points or 0,
        "permissions": {
            "can_manage_students": user.role.can_manage_students,
            "can_manage_schedule": user.role.can_manage_schedule,
            "can_manage_fund": user.role.can_manage_fund,
            "can_manage_announcements": user.role.can_manage_announcements,
            "can_manage_notifications": user.role.can_manage_notifications,
            "can_manage_whatsapp": user.role.can_manage_whatsapp,
            "can_manage_gallery": user.role.can_manage_gallery,
            "can_manage_assignments": user.role.can_manage_assignments,
            "can_view_logs": user.role.can_view_logs,
        },
        "student": student_data
    })

@api_bp.route('/update-fcm-token', methods=['POST'])
@jwt_required()
def update_fcm_token():
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    data = request.get_json()
    
    fcm_token = data.get('fcm_token') or data.get('token')
    if fcm_token:
        user.fcm_token = fcm_token
        db.session.commit()
        print(f"DEBUG: Updated FCM Token for user {user.username}")
        return jsonify({"msg": "Token updated"}), 200
        
    return jsonify({"msg": "Token missing"}), 400

@api_bp.route('/announcements', methods=['GET', 'POST'])
@jwt_required()
def get_announcements():
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))

    if request.method == 'POST':
        if not user.role.can_manage_announcements:
            return jsonify({"error": "Unauthorized"}), 403

        data = request.get_json() or {}
        title = (data.get('title') or '').strip()
        content = (data.get('content') or '').strip()
        if not title or not content:
            return jsonify({"error": "Judul dan isi pengumuman wajib diisi"}), 400

        ann = Announcement(
            title=title,
            content=content,
            category=(data.get('category') or 'Info').strip(),
            is_pinned=bool(data.get('is_pinned', False)),
            is_public=bool(data.get('is_public', True)),
        )
        db.session.add(ann)
        db.session.commit()

        from app import send_push, log_activity
        title_prefix = "Pengumuman Baru!" if ann.category != 'Penting' else "PENTING: Pengumuman!"
        send_push(title_prefix, ann.title, sender_id=user.id)
        log_activity("Tambah Pengumuman API", f"Judul: {ann.title}")
        return jsonify({"status": "success", "id": ann.id})

    announcements = Announcement.query.order_by(Announcement.is_pinned.desc(), Announcement.date_posted.desc()).all()
    return jsonify([{
        "id": a.id,
        "title": a.title,
        "content": a.content,
        "category": a.category,
        "is_pinned": a.is_pinned,
        "is_public": a.is_public,
        "date_posted": a.date_posted.isoformat()
    } for a in announcements])

@api_bp.route('/announcements/<int:id>', methods=['PUT', 'DELETE'])
@jwt_required()
def modify_announcement(id):
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    if not user.role.can_manage_announcements:
        return jsonify({"error": "Unauthorized"}), 403

    ann = Announcement.query.get_or_404(id)
    if request.method == 'DELETE':
        title = ann.title
        db.session.delete(ann)
        db.session.commit()
        from app import log_activity
        log_activity("Hapus Pengumuman API", f"Judul: {title}")
        return jsonify({"status": "success"})

    data = request.get_json() or {}
    ann.title = (data.get('title') or ann.title).strip() or ann.title
    ann.content = (data.get('content') or ann.content).strip() or ann.content
    ann.category = (data.get('category') or ann.category or 'Info').strip()
    ann.is_pinned = bool(data.get('is_pinned', ann.is_pinned))
    ann.is_public = bool(data.get('is_public', ann.is_public))
    db.session.commit()
    from app import log_activity
    log_activity("Edit Pengumuman API", f"Judul: {ann.title}")
    return jsonify({"status": "success"})


@api_bp.route('/explore', methods=['GET'])
@jwt_required()
def get_explore():
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    query = (request.args.get('q') or '').strip().lower()
    filter_type = (request.args.get('type') or 'all').strip().lower()
    page = max(1, int(request.args.get('page') or 1))
    per_page = min(50, max(1, int(request.args.get('per_page') or 20)))

    allowed_types = {'all', 'announcement', 'schedule', 'assignment', 'fund', 'gallery', 'member'}
    if filter_type not in allowed_types:
        filter_type = 'all'

    items = []

    if filter_type in {'all', 'announcement'}:
        announcement_query = Announcement.query
        if query:
            announcement_query = announcement_query.filter(or_(
                _explore_contains(Announcement.title, query),
                _explore_contains(Announcement.content, query),
                _explore_contains(Announcement.category, query),
            ))

        for announcement in announcement_query.order_by(Announcement.date_posted.desc()).limit(40).all():
            items.append({
                'id': announcement.id,
                'type': 'announcement',
                'title': announcement.title,
                'subtitle': announcement.category or 'Pengumuman',
                'snippet': _explore_snippet(announcement.content, fallback='Pengumuman kelas'),
                'date_label': announcement.date_posted.strftime('%d %b %Y %H:%M'),
                'sort_at': announcement.date_posted,
                'thumbnail_url': None,
                'route_hint': 'announcement',
                'route_params': {'id': announcement.id},
                'badge': 'Pengumuman',
                'meta': {
                    'category': announcement.category or 'Info',
                    'is_pinned': bool(announcement.is_pinned),
                    'content': announcement.content or '',
                    'date_posted': announcement.date_posted.isoformat(),
                }
            })

    if filter_type in {'all', 'schedule'}:
        schedule_query = Schedule.query
        if user.student and user.student.classroom_id:
            schedule_query = schedule_query.filter_by(classroom_id=user.student.classroom_id)
        if query:
            schedule_query = schedule_query.filter(or_(
                _explore_contains(Schedule.subject, query),
                _explore_contains(Schedule.lecturer, query),
                _explore_contains(Schedule.room, query),
                _explore_contains(Schedule.day, query),
                _explore_contains(Schedule.time_start, query),
                _explore_contains(Schedule.time_end, query),
            ))

        for schedule in schedule_query.all():
            sort_at = _next_schedule_sort_at(schedule)
            items.append({
                'id': schedule.id,
                'type': 'schedule',
                'title': schedule.subject,
                'subtitle': f"{schedule.day}, {schedule.time_start}-{schedule.time_end}",
                'snippet': _explore_snippet(f"Dosen: {schedule.lecturer or '-'} | Ruang: {schedule.room or '-'}"),
                'date_label': schedule.day or '-',
                'sort_at': sort_at,
                'thumbnail_url': None,
                'route_hint': 'schedule',
                'route_params': {'id': schedule.id},
                'badge': 'Jadwal',
                'meta': {
                    'day': schedule.day,
                    'time_start': schedule.time_start,
                    'time_end': schedule.time_end,
                    'room': schedule.room or '-',
                    'lecturer': schedule.lecturer or '-',
                }
            })

    if filter_type in {'all', 'assignment'}:
        assignment_query = Assignment.query
        if query:
            assignment_query = assignment_query.filter(or_(
                _explore_contains(Assignment.title, query),
                _explore_contains(Assignment.subject, query),
                _explore_contains(Assignment.description, query),
            ))

        for assignment in assignment_query.order_by(Assignment.deadline.desc()).limit(40).all():
            items.append({
                'id': assignment.id,
                'type': 'assignment',
                'title': assignment.title,
                'subtitle': assignment.subject or 'Tugas',
                'snippet': _explore_snippet(assignment.description, fallback='Tugas kelas'),
                'date_label': assignment.deadline.strftime('%d %b %Y %H:%M'),
                'sort_at': assignment.deadline,
                'thumbnail_url': None,
                'route_hint': 'assignment',
                'route_params': {'id': assignment.id},
                'badge': 'Tugas',
                'meta': {
                    'subject': assignment.subject or 'Tugas',
                    'deadline': assignment.deadline.isoformat(),
                    'description': assignment.description or '',
                }
            })

    if filter_type in {'all', 'fund'}:
        fund_query = BatchFund.query
        if query:
            fund_query = fund_query.outerjoin(Student, BatchFund.student_id == Student.id).filter(or_(
                _explore_contains(BatchFund.description, query),
                _explore_contains(BatchFund.category, query),
                _explore_contains(BatchFund.tags, query),
                _explore_contains(Student.full_name, query),
            ))

        for fund in fund_query.order_by(BatchFund.date.desc()).limit(40).all():
            items.append({
                'id': fund.id,
                'type': 'fund',
                'title': fund.description,
                'subtitle': f"{fund.type} • {fund.category or 'Kas'}",
                'snippet': _explore_snippet(f"Nominal: Rp {int(fund.amount):,}".replace(',', '.'), fund.tags, fund.student.full_name if fund.student else ''),
                'date_label': fund.date.strftime('%d %b %Y'),
                'sort_at': datetime.combine(fund.date, datetime.min.time()),
                'thumbnail_url': None,
                'route_hint': 'fund',
                'route_params': {'id': fund.id},
                'badge': 'Kas',
                'meta': {
                    'amount': fund.amount,
                    'fund_type': fund.type,
                    'category': fund.category or 'Kas',
                    'student_name': fund.student.full_name if fund.student else None,
                }
            })

    if filter_type in {'all', 'gallery'}:
        if user.role.can_manage_gallery:
            gallery_query = GalleryPhoto.query
        else:
            gallery_query = GalleryPhoto.query.filter(
                (GalleryPhoto.status == 'Published') | (GalleryPhoto.uploaded_by == int(user_id))
            )

        if query:
            gallery_query = gallery_query.outerjoin(User, GalleryPhoto.uploaded_by == User.id).filter(or_(
                _explore_contains(GalleryPhoto.caption, query),
                _explore_contains(GalleryPhoto.tags, query),
                _explore_contains(User.full_name, query),
                _explore_contains(User.username, query),
            ))

        for photo in gallery_query.order_by(GalleryPhoto.created_at.desc()).limit(40).all():
            items.append({
                'id': photo.id,
                'type': 'gallery',
                'title': photo.caption or 'Foto Galeri',
                'subtitle': photo.user.full_name if photo.user and photo.user.full_name else (photo.user.username if photo.user else 'Galeri'),
                'snippet': _explore_snippet(photo.tags, fallback='Foto galeri kelas'),
                'date_label': photo.created_at.strftime('%d %b %Y %H:%M'),
                'sort_at': photo.created_at,
                'thumbnail_url': photo.filename,
                'route_hint': 'gallery',
                'route_params': {
                    'id': photo.id,
                    'filename': photo.filename,
                    'caption': photo.caption or '',
                    'uploaded_by': photo.user.full_name if photo.user and photo.user.full_name else (photo.user.username if photo.user else 'System'),
                    'tags': photo.tags or '',
                    'status': photo.status,
                    'is_public': photo.is_public,
                    'created_at': photo.created_at.isoformat(),
                    'comments': [],
                },
                'badge': 'Galeri',
                'meta': {
                    'status': photo.status,
                    'tags': photo.tags or '',
                }
            })

    if filter_type in {'all', 'member'}:
        member_query = Student.query
        if user.student and user.student.classroom_id:
            member_query = member_query.filter_by(classroom_id=user.student.classroom_id)
        if query:
            member_query = member_query.filter(or_(
                _explore_contains(Student.full_name, query),
                _explore_contains(Student.nim, query),
                _explore_contains(Student.status, query),
            ))

        for member in member_query.order_by(Student.full_name.asc()).limit(40).all():
            items.append({
                'id': member.id,
                'type': 'member',
                'title': member.full_name,
                'subtitle': member.nim,
                'snippet': _explore_snippet(member.status, fallback='Anggota kelas'),
                'date_label': member.status or 'Aktif',
                'sort_at': datetime(1970, 1, 1),
                'thumbnail_url': None,
                'route_hint': 'member',
                'route_params': {'id': member.id},
                'badge': 'Anggota',
                'meta': {
                    'nim': member.nim,
                    'status': member.status or 'Aktif',
                }
            })

    if query:
        ordered_items = sorted(items, key=lambda value: value['sort_at'], reverse=True)
    else:
        ordered_items = _interleave_explore_items(sorted(items, key=lambda value: value['sort_at'], reverse=True))

    total = len(ordered_items)
    start = (page - 1) * per_page
    paged_items = ordered_items[start:start + per_page]

    return jsonify({
        'items': [{
            **item,
            'sort_at': item['sort_at'].isoformat(),
        } for item in paged_items],
        'page': page,
        'per_page': per_page,
        'has_more': start + per_page < total,
        'total': total,
    })

@api_bp.route('/schedules', methods=['GET', 'POST'])
@jwt_required()
def manage_schedules():
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    
    if request.method == 'POST':
        if not user.role.can_manage_schedule:
            return jsonify({"error": "Unauthorized"}), 403
            
        data = request.get_json()
        from models import ClassRoom
        class_fb = ClassRoom.query.filter_by(name='Famousbytee.b').first()
        
        s = Schedule(
            classroom_id=class_fb.id if class_fb else 1,
            day=data.get('day'),
            time_start=data.get('time_start'),
            time_end=data.get('time_end'),
            subject=data.get('subject'),
            lecturer=data.get('lecturer', '-'),
            room=data.get('room', '-')
        )
        db.session.add(s)
        db.session.commit()
        
        from app import send_multichannel_notification, get_setting_value
        # Check if WhatsApp notification is enabled for create
        # Note: Sends daily summary, not per-subject message
        should_notify = get_setting_value('schedule_notify_on_create', 'true') == 'true'
        if should_notify:
            # Send push notification immediately
            send_multichannel_notification(
                "Jadwal Baru Ditambahkan",
                f"Jadwal {s.subject} ditambahkan pada hari {s.day} pukul {s.time_start}.",
                sender_id=user.id,
                allow_whatsapp=False,  # Don't send per-subject WhatsApp
            )
            # WhatsApp will be sent via daily summary at scheduled time
        
        return jsonify({"status": "success", "id": s.id})

    # GET logic
    if user.student and user.student.classroom_id:
        schedules = Schedule.query.filter_by(classroom_id=user.student.classroom_id).all()
    else:
        schedules = Schedule.query.all()
        
    return jsonify([{
        "id": s.id,
        "day": s.day,
        "time_start": s.time_start,
        "time_end": s.time_end,
        "subject": s.subject,
        "lecturer": s.lecturer,
        "room": s.room
    } for s in schedules])

@api_bp.route('/schedules/<int:id>', methods=['PUT', 'DELETE'])
@jwt_required()
def modify_schedule(id):
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    if not user.role.can_manage_schedule:
        return jsonify({"error": "Unauthorized"}), 403
        
    s = Schedule.query.get_or_404(id)
    
    if request.method == 'DELETE':
        subject_name = s.subject
        db.session.delete(s)
        db.session.commit()
        
        from app import send_multichannel_notification, get_setting_value
        # Check if WhatsApp notification is enabled for delete
        should_notify = get_setting_value('schedule_notify_on_delete', 'true') == 'true'
        # Only send push notification, WhatsApp via daily summary
        send_multichannel_notification(
            "Jadwal Dihapus",
            f"Jadwal {subject_name} telah dihapus dari sistem.",
            sender_id=user.id,
            allow_whatsapp=False,  # Don't send per-subject WhatsApp
        )
        
        return jsonify({"status": "success"})
        
    if request.method == 'PUT':
        data = request.get_json()
        s.day = data.get('day', s.day)
        s.time_start = data.get('time_start', s.time_start)
        s.time_end = data.get('time_end', s.time_end)
        s.subject = data.get('subject', s.subject)
        s.lecturer = data.get('lecturer', s.lecturer)
        s.room = data.get('room', s.room)
        
        db.session.commit()
        
        from app import send_multichannel_notification, get_setting_value
        # Check if WhatsApp notification is enabled for edit (default: false to avoid spam)
        should_notify = get_setting_value('schedule_notify_on_edit', 'false') == 'true'
        if should_notify:
            # Only send push notification, WhatsApp via daily summary
            send_multichannel_notification(
                "Jadwal Diperbarui",
                f"Jadwal {s.subject} telah diperbarui menjadi hari {s.day} pukul {s.time_start}.",
                sender_id=user.id,
                allow_whatsapp=False,  # Don't send per-subject WhatsApp
            )
        else:
            # Still send push notification
            send_multichannel_notification(
                "Jadwal Diperbarui",
                f"Jadwal {s.subject} telah diperbarui menjadi hari {s.day} pukul {s.time_start}.",
                sender_id=user.id,
                allow_whatsapp=False,
            )
        
        return jsonify({"status": "success"})

@api_bp.route('/notifications/preferences', methods=['GET'])
@jwt_required()
def get_notification_preferences():
    """Get notification preferences for schedule management"""
    from app import get_setting_value
    return jsonify({
        'schedule_notify_on_create': get_setting_value('schedule_notify_on_create', 'true') == 'true',
        'schedule_notify_on_edit': get_setting_value('schedule_notify_on_edit', 'false') == 'true',
        'schedule_notify_on_delete': get_setting_value('schedule_notify_on_delete', 'true') == 'true',
    })

@api_bp.route('/notifications/preferences', methods=['POST'])
@jwt_required()
def update_notification_preferences():
    """Update notification preferences for schedule management"""
    from app import set_setting_value, db
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    
    if not user.role.can_manage_schedule:
        return jsonify({"error": "Unauthorized"}), 403
    
    data = request.get_json()
    
    if 'schedule_notify_on_create' in data:
        set_setting_value('schedule_notify_on_create', 
                         'true' if data['schedule_notify_on_create'] else 'false')
    if 'schedule_notify_on_edit' in data:
        set_setting_value('schedule_notify_on_edit', 
                         'true' if data['schedule_notify_on_edit'] else 'false')
    if 'schedule_notify_on_delete' in data:
        set_setting_value('schedule_notify_on_delete', 
                         'true' if data['schedule_notify_on_delete'] else 'false')
    
    db.session.commit()
    return jsonify({"status": "success"})

@api_bp.route('/schedules/<int:id>/send-whatsapp', methods=['POST'])
@jwt_required()
def send_schedule_whatsapp(id):
    """Manually send WhatsApp notification for a specific schedule"""
    from app import send_whatsapp, get_setting_value
    from models import Schedule
    
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    
    if not user.role.can_manage_schedule:
        return jsonify({"error": "Unauthorized"}), 403
    
    schedule = Schedule.query.get_or_404(id)
    
    data = request.get_json() or {}
    action = data.get('action', 'update')  # create, update, delete, custom
    
    whatsapp_text = data.get('message')
    if not whatsapp_text:
        if action == 'create':
            whatsapp_text = f"Jadwal baru\n{schedule.subject}\nHari: {schedule.day}\nJam: {schedule.time_start}-{schedule.time_end}\nRuang: {schedule.room}"
        elif action == 'delete':
            whatsapp_text = f"Jadwal dihapus\nMata kuliah: {schedule.subject}"
        else:  # update
            whatsapp_text = f"Perubahan jadwal\n{schedule.subject}\nHari: {schedule.day}\nJam: {schedule.time_start}-{schedule.time_end}\nRuang: {schedule.room}"
    
    result = send_whatsapp(
        whatsapp_text,
        sender_id=user.id,
        title=f"Jadwal: {schedule.subject}"
    )
    
    return jsonify(result), (200 if result.get('ok') else 400)

@api_bp.route('/notifications/send-daily-summary', methods=['POST'])
@jwt_required()
def send_daily_summary_on_demand():
    """Send daily schedule summary WhatsApp on demand"""
    from app import send_whatsapp, _build_schedule_summary_message, get_setting_value
    
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    
    if not user.role.can_manage_schedule and not user.role.can_manage_whatsapp:
        return jsonify({"error": "Unauthorized"}), 403
    
    data = request.get_json() or {}
    target_date_str = data.get('target_date')  # Optional: YYYY-MM-DD format
    
    if target_date_str:
        from datetime import datetime
        try:
            target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
        except ValueError:
            return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400
    else:
        from datetime import datetime, timedelta
        target_date = datetime.now().date() + timedelta(days=1)  # Default: tomorrow
    
    # Build summary message
    message = _build_schedule_summary_message(target_date)
    
    if not message:
        return jsonify({
            "ok": False,
            "error": f"Tidak ada jadwal untuk tanggal {target_date.strftime('%d/%m/%Y')}"
        }), 400
    
    # Send WhatsApp
    result = send_whatsapp(
        message,
        sender_id=user.id,
        title=f"Ringkasan Jadwal {target_date.strftime('%d/%m/%Y')}"
    )
    
    return jsonify(result), (200 if result.get('ok') else 400)

def _count_weekdays_between(start_date, end_date):
    total = 0
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:
            total += 1
        current += timedelta(days=1)
    return total

def get_fund_target(as_of=None):
    """Calculates cumulative target based on advanced periods, with legacy fallback."""
    today = as_of or datetime.now().date()
    if isinstance(today, datetime):
        today = today.date()

    periods = FundPeriod.query.filter_by(is_active=True).order_by(FundPeriod.start_date.asc(), FundPeriod.id.asc()).all()
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

@api_bp.route('/funds/summary', methods=['GET'])
@jwt_required()
def get_funds_summary():
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    total_in = db.session.query(db.func.sum(BatchFund.amount)).filter(BatchFund.type == 'Masuk').scalar() or 0
    total_out = db.session.query(db.func.sum(BatchFund.amount)).filter(BatchFund.type == 'Keluar').scalar() or 0
    balance = total_in - total_out
    target_payment = get_fund_target()

    if user.student and user.student.classroom_id:
        students = Student.query.filter_by(
            classroom_id=user.student.classroom_id
        ).order_by(Student.full_name.asc()).all()
    else:
        students = Student.query.order_by(Student.full_name.asc()).all()

    members_total = len(students)
    members_settled = 0
    members_unsettled = 0
    total_paid_all = 0
    total_arrears_all = 0

    for student in students:
        paid = db.session.query(db.func.sum(BatchFund.amount)).filter(
            BatchFund.student_id == student.id,
            BatchFund.type == 'Masuk'
        ).scalar() or 0
        arrears = max(0, target_payment - paid)
        total_paid_all += paid
        total_arrears_all += arrears
        if arrears == 0:
            members_settled += 1
        else:
            members_unsettled += 1

    progress_percent = 0.0
    denominator = target_payment * members_total
    if denominator > 0:
        progress_percent = max(0.0, min((total_paid_all / denominator) * 100.0, 100.0))

    my_financial = None
    if user.student:
        my_paid = db.session.query(db.func.sum(BatchFund.amount)).filter(
            BatchFund.student_id == user.student.id,
            BatchFund.type == 'Masuk'
        ).scalar() or 0
        my_arrears = max(0, target_payment - my_paid)
        my_financial = {
            "paid": my_paid,
            "target": target_payment,
            "arrears": my_arrears,
            "is_settled": my_arrears == 0
        }
    
    return jsonify({
        "total_in": total_in,
        "total_out": total_out,
        "balance": balance,
        "target": target_payment,
        "members_total": members_total,
        "members_settled": members_settled,
        "members_unsettled": members_unsettled,
        "total_paid_all": total_paid_all,
        "total_arrears_all": total_arrears_all,
        "progress_percent": round(progress_percent, 1),
        "my_financial": my_financial
    })

@api_bp.route('/funds/history', methods=['GET'])
@jwt_required()
def get_funds_history():
    history = BatchFund.query.order_by(BatchFund.date.desc()).all()
    return jsonify([{
        "id": f.id,
        "description": f.description,
        "amount": f.amount,
        "type": f.type,
        "category": f.category,
        "date": f.date.isoformat(),
        "tags": f.tags,
        "student_id": f.student_id,
        "student_name": f.student.full_name if f.student else None,
        "note": f.evidence_note,
        "recorded_by": f.recorded_by,
        "added_by_name": f.recorded_by,
        "is_edited": f.is_edited,
        "edit_reason": f.edit_reason,
        "original_amount": f.original_amount,
        "original_description": f.original_description
    } for f in history])

@api_bp.route('/funds/audit', methods=['GET'])
@jwt_required()
def get_funds_audit():
    students = Student.query.order_by(Student.full_name).all()
    target_payment = get_fund_target()
    
    audit_data = []
    for s in students:
        total_paid = db.session.query(db.func.sum(BatchFund.amount)).filter(
            BatchFund.student_id == s.id, 
            BatchFund.type == 'Masuk'
        ).scalar() or 0
        
        audit_data.append({
            "student_name": s.full_name,
            "nim": s.nim,
            "paid": total_paid,
            "target": target_payment,
            "arrears": max(0, target_payment - total_paid)
        })
        
    return jsonify(audit_data)

@api_bp.route('/members', methods=['GET'])
@api_bp.route('/students', methods=['GET'])
@jwt_required()
def get_members():
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    
    if user.student and user.student.classroom_id:
        students = Student.query.filter_by(classroom_id=user.student.classroom_id).order_by(Student.full_name).all()
    else:
        students = Student.query.order_by(Student.full_name).all()
        
    return jsonify([{
        "id": s.id,
        "nim": s.nim,
        "full_name": s.full_name,
        "status": s.status
    } for s in students])


def _get_member_detail_for_requester(request_user, member_id):
    member = Student.query.get_or_404(member_id)

    if request_user.student and request_user.student.classroom_id:
        if member.classroom_id != request_user.student.classroom_id:
            return None
    elif request_user.role.name not in ['Admin', 'Pengurus', 'Staff']:
        return None

    linked_user = member.user
    total_paid = db.session.query(db.func.sum(BatchFund.amount)).filter(
        BatchFund.student_id == member.id,
        BatchFund.type == 'Masuk'
    ).scalar() or 0
    target_payment = get_fund_target()
    arrears = max(0, target_payment - total_paid)

    return {
        "id": member.id,
        "full_name": member.full_name,
        "nim": member.nim,
        "status": member.status or 'Aktif',
        "whatsapp": linked_user.whatsapp if linked_user and linked_user.whatsapp else None,
        "financial": {
            "paid": total_paid,
            "target": target_payment,
            "arrears": arrears,
            "is_settled": arrears == 0
        }
    }


@api_bp.route('/members/<int:member_id>', methods=['GET'])
@jwt_required()
def get_member_detail(member_id):
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))

    payload = _get_member_detail_for_requester(user, member_id)
    if payload is None:
        return jsonify({"error": "Member tidak ditemukan atau tidak dapat diakses"}), 404

    return jsonify(payload)


@api_bp.route('/fund-periods', methods=['GET'])
@jwt_required()
def get_fund_periods_api():
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    if not user.role.can_manage_fund:
        return jsonify({"error": "Unauthorized"}), 403

    periods = FundPeriod.query.order_by(FundPeriod.start_date.asc(), FundPeriod.id.asc()).all()
    return jsonify([{
        "id": period.id,
        "title": period.title,
        "start_date": period.start_date.isoformat(),
        "end_date": period.end_date.isoformat(),
        "daily_rate": period.daily_rate,
        "is_active": period.is_active
    } for period in periods])


@api_bp.route('/fund-periods', methods=['POST'])
@jwt_required()
def create_fund_period_api():
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    if not user.role.can_manage_fund:
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json(silent=True) or request.form or {}
    title = (data.get('title') or '').strip()
    if not title:
        return jsonify({"error": "Nama periode wajib diisi"}), 400

    try:
        start_date = datetime.strptime((data.get('start_date') or '').strip(), '%Y-%m-%d').date()
        end_date = datetime.strptime((data.get('end_date') or '').strip(), '%Y-%m-%d').date()
    except Exception:
        return jsonify({"error": "Format tanggal tidak valid"}), 400

    if end_date < start_date:
        return jsonify({"error": "Tanggal akhir periode tidak boleh sebelum tanggal mulai"}), 400

    try:
        daily_rate = int(data.get('daily_rate') or 0)
    except Exception:
        daily_rate = 0
    if daily_rate <= 0:
        return jsonify({"error": "Nominal harian wajib lebih dari 0"}), 400

    period = FundPeriod(
        title=title,
        start_date=start_date,
        end_date=end_date,
        daily_rate=daily_rate,
        is_active=str(data.get('is_active', 'true')).lower() in {'true', '1', 'yes', 'on'}
    )
    db.session.add(period)
    db.session.commit()

    from app import log_activity
    log_activity("Tambah Periode Kas (Mobile)", f"{period.title} ({period.start_date} - {period.end_date})")
    return jsonify({"status": "success", "id": period.id})


@api_bp.route('/fund-periods/<int:period_id>', methods=['PUT'])
@jwt_required()
def update_fund_period_api(period_id):
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    if not user.role.can_manage_fund:
        return jsonify({"error": "Unauthorized"}), 403

    period = FundPeriod.query.get_or_404(period_id)
    data = request.get_json(silent=True) or request.form or {}

    title = (data.get('title') or period.title).strip() or period.title
    try:
        start_date = datetime.strptime((data.get('start_date') or period.start_date.isoformat()).strip(), '%Y-%m-%d').date()
        end_date = datetime.strptime((data.get('end_date') or period.end_date.isoformat()).strip(), '%Y-%m-%d').date()
    except Exception:
        return jsonify({"error": "Format tanggal tidak valid"}), 400

    if end_date < start_date:
        return jsonify({"error": "Tanggal akhir periode tidak boleh sebelum tanggal mulai"}), 400

    try:
        daily_rate = int(data.get('daily_rate') or period.daily_rate or 0)
    except Exception:
        daily_rate = 0
    if daily_rate <= 0:
        return jsonify({"error": "Nominal harian wajib lebih dari 0"}), 400

    period.title = title
    period.start_date = start_date
    period.end_date = end_date
    period.daily_rate = daily_rate
    period.is_active = str(data.get('is_active', period.is_active)).lower() in {'true', '1', 'yes', 'on'}
    db.session.commit()

    from app import log_activity
    log_activity("Edit Periode Kas (Mobile)", f"ID {period.id}: {period.title}")
    return jsonify({"status": "success"})

@api_bp.route('/gallery', methods=['GET'])
@jwt_required()
def get_gallery():
    # User can see published photos, or their own pending photos
    user_id = int(get_jwt_identity())
    user = User.query.get(user_id)
    
    if user.role.can_manage_gallery:
        photos = GalleryPhoto.query.order_by(GalleryPhoto.created_at.desc()).all()
    else:
        # Published OR owned by user
        photos = GalleryPhoto.query.filter((GalleryPhoto.status == 'Published') | (GalleryPhoto.uploaded_by == user_id)).order_by(GalleryPhoto.created_at.desc()).all()


    return jsonify([{
        "id": p.id,
        "filename": p.filename,
        "thumbnail": p.thumbnail,
        "caption": p.caption,
        "tags": p.tags,
        "status": p.status,
        "is_public": p.is_public,
        "uploaded_by": p.user.full_name if p.user else "System",
        "created_at": p.created_at.isoformat(),
        "comments": [{
            "id": c.id,
            "user": c.user.student.full_name if c.user.student else c.user.full_name or c.user.username,
            "body": c.body,
            "time": c.created_at.strftime('%d %b %H:%M')
        } for c in p.comments]
    } for p in photos])

@api_bp.route('/gallery/comment/<int:photo_id>', methods=['POST'])
@jwt_required()
def add_gallery_comment(photo_id):
    user_id = int(get_jwt_identity())
    data = request.get_json()
    if not data or not data.get('body'):
        return jsonify({"error": "Pesan komentar diperlukan"}), 400
    
    from models import PhotoComment
    comment = PhotoComment(photo_id=photo_id, user_id=user_id, body=data['body'])
    db.session.add(comment)
    db.session.commit()
    return jsonify({"status": "success"})

@api_bp.route('/gallery/moderate/<int:photo_id>', methods=['POST'])
@jwt_required()
def moderate_gallery(photo_id):
    user_id = int(get_jwt_identity())
    user = User.query.get(user_id)
    if not user.role.can_manage_gallery:
        return jsonify({"error": "Unauthorized"}), 403
    
    data = request.get_json() or {}
    status = data.get('status')
    
    print(f"DEBUG: Moderating photo {photo_id} by user {user_id}. Status: {status}")
    
    if status not in ['Published', 'Rejected', 'Pending']:
        return jsonify({"error": f"Status '{status}' tidak valid"}), 400

        
    photo = GalleryPhoto.query.get_or_404(photo_id)
    
    if status == 'Rejected':
        try:
            # Permanent Delete files
            gallery_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'gallery')
            thumb_dir = os.path.join(gallery_dir, 'thumbnails')
            
            filepath = os.path.join(gallery_dir, photo.filename)
            thumbpath = os.path.join(thumb_dir, photo.filename)
            
            if os.path.exists(filepath): os.remove(filepath)
            if os.path.exists(thumbpath): os.remove(thumbpath)
            
            db.session.delete(photo)
            db.session.commit()
            try:
                from app import auto_recalculate_points
                auto_recalculate_points()
            except Exception:
                pass
            return jsonify({"status": "deleted", "message": "Foto ditolak dan dihapus permanen"})
        except Exception as e:
            db.session.rollback()
            return jsonify({"error": str(e)}), 500
    
    photo.status = status
            
    db.session.commit()
    try:
        from app import auto_recalculate_points
        auto_recalculate_points()
    except Exception:
        pass
    return jsonify({"status": "success", "new_status": photo.status})


@api_bp.route('/gallery/<int:photo_id>', methods=['DELETE'])
@jwt_required()
def delete_gallery_photo(photo_id):
    user_id = int(get_jwt_identity())
    user = User.query.get(user_id)
    photo = GalleryPhoto.query.get_or_404(photo_id)
    
    # Allow if admin OR the one who uploaded it
    if not user.role.can_manage_gallery and photo.uploaded_by != user_id:
        return jsonify({"error": "Unauthorized"}), 403
        
    try:
        # Delete files
        gallery_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'gallery')
        thumb_dir = os.path.join(gallery_dir, 'thumbnails')
        
        filepath = os.path.join(gallery_dir, photo.filename)
        thumbpath = os.path.join(thumb_dir, photo.filename)
        
        if os.path.exists(filepath): os.remove(filepath)
        if os.path.exists(thumbpath): os.remove(thumbpath)
        
        db.session.delete(photo)
        db.session.commit()
        try:
            from app import auto_recalculate_points
            auto_recalculate_points()
        except Exception:
            pass
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


import uuid
from PIL import Image

def process_image_upload(file):
    if not file: return None
    try:
        # Using current_app.config
        gallery_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'gallery')
        thumb_dir = os.path.join(gallery_dir, 'thumbnails')
        os.makedirs(gallery_dir, exist_ok=True)
        os.makedirs(thumb_dir, exist_ok=True)

        filename_base = uuid.uuid4().hex
        filename = f"{filename_base}.webp"
        filepath = os.path.join(gallery_dir, filename)
        thumbpath = os.path.join(thumb_dir, filename)

        img = Image.open(file.stream)
        if img.mode in ("RGBA", "P"): img = img.convert("RGB")
        
        # Save Preview/Standard
        preview_img = img.copy()
        preview_img.thumbnail((1200, 1200))
        preview_img.save(filepath, 'WEBP', quality=50)
        
        # Create and Save Thumbnail
        thumb_img = img.copy()
        thumb_img.thumbnail((300, 300))
        thumb_img.save(thumbpath, 'WEBP', quality=45)
        return filename
    except Exception as e:
        print(f"Error processing image: {e}")
        return None

@api_bp.route('/gallery/upload', methods=['POST'])
@jwt_required()
def upload_gallery_api():
    user_id = int(get_jwt_identity())
    user = User.query.get(user_id)
    
    # User must at least have use_api (already checked by jwt) 
    # and we check if they have specific gallery upload status
    
    file = request.files.get('photo') # Changed from getlist('photos')
    if not file:
        # Fallback for old mobile apps still sending 'photos'
        files = request.files.getlist('photos')
        if files: file = files[0]
        else: return jsonify({"error": "Tidak ada foto terpilih"}), 400
        
    caption = request.form.get('caption', '')
    tags = request.form.get('tags', '')
    is_public = request.form.get('is_public') == 'true'
    
    # Status logic: if can_manage_gallery -> Published, else Pending
    status = 'Published' if user.role.can_manage_gallery else 'Pending'
    
    filename = process_image_upload(file)
    if not filename:
        return jsonify({"error": "Gagal memproses gambar"}), 500
        
    photo = GalleryPhoto(
        filename=filename,
        thumbnail=filename,
        caption=caption,
        tags=tags,
        uploaded_by=user_id,
        status=status,
        is_public=is_public
    )
    db.session.add(photo)
    db.session.commit()
    try:
        from app import auto_recalculate_points
        auto_recalculate_points()
    except Exception:
        pass
    
    if status == 'Published':
        try:
            from app import send_push
            send_push("Foto Baru di Galeri!", f"{user.full_name} baru saja mengunggah foto baru. Cek sekarang!")
        except:
            pass

    return jsonify({"status": "success", "id": photo.id, "status": photo.status})



@api_bp.route('/logs', methods=['GET'])
@jwt_required()
def get_logs():
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    if user.role.name not in ['Admin', 'Pengurus']:
        return jsonify({"error": "Unauthorized"}), 403

    logs = ActivityLog.query.order_by(ActivityLog.timestamp.desc()).limit(50).all()
    return jsonify([{
        "id": l.id,
        "action": l.action,
        "details": l.details,
        "timestamp": l.timestamp.isoformat(),
        "username": l.user.username if l.user else "System"
    } for l in logs])

@api_bp.route('/notifications/history', methods=['GET'])
@jwt_required()
def get_notification_history():
    user_id = get_jwt_identity()
    db.session.rollback()
    user = User.query.get(int(user_id))
    
    # Show notifications that are for "All" or for this specific user
    history = NotificationHistory.query.filter(
        ((NotificationHistory.target == 'All') | (NotificationHistory.target == str(user_id))) &
        ((NotificationHistory.title != '') | (NotificationHistory.body != ''))
    ).order_by(NotificationHistory.sent_at.desc()).limit(30).all()
    
    return jsonify([{
        "id": h.id,
        "title": h.title,
        "body": h.body,
        "channel": h.channel or 'push',
        "target": h.target,
        "sent_at": h.sent_at.isoformat(),
        "status": h.status
    } for h in history])

@api_bp.route('/notifications/recipients', methods=['GET'])
@jwt_required()
def get_notification_recipients():
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    if not user.role.can_manage_notifications:
        return jsonify({"error": "Unauthorized"}), 403

    if user.student and user.student.classroom_id:
        students = Student.query.filter_by(
            classroom_id=user.student.classroom_id
        ).order_by(Student.full_name.asc()).all()
    else:
        students = Student.query.order_by(Student.full_name.asc()).all()

    recipients = []
    seen_user_ids = set()

    for student in students:
        linked_user = student.user
        if linked_user:
            seen_user_ids.add(linked_user.id)

        recipients.append({
            "id": f"student:{student.id}",
            "student_id": student.id,
            "user_id": linked_user.id if linked_user else None,
            "full_name": student.full_name,
            "username": linked_user.username if linked_user else None,
            "has_account": bool(linked_user),
            "has_token": bool(linked_user and linked_user.fcm_token),
            "status": student.status or 'Aktif',
            "nim": student.nim
        })

    # Tetap tampilkan user non-member khusus seperti admin/staff agar bisa dites juga.
    extra_users = User.query.order_by(
        User.full_name.is_(None),
        User.full_name.asc(),
        User.username.asc()
    ).all()
    for extra_user in extra_users:
        if extra_user.id in seen_user_ids:
            continue
        recipients.append({
            "id": f"user:{extra_user.id}",
            "student_id": extra_user.student_id,
            "user_id": extra_user.id,
            "full_name": extra_user.full_name or extra_user.username,
            "username": extra_user.username,
            "has_account": True,
            "has_token": bool(extra_user.fcm_token),
            "status": extra_user.status or 'Active',
            "nim": extra_user.student.nim if extra_user.student else None
        })

    return jsonify(recipients)

@api_bp.route('/notifications/send', methods=['POST'])
@jwt_required()
def api_send_notifications():
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    if not user.role.can_manage_notifications:
        return jsonify({"error": "Unauthorized"}), 403
    
    from app import send_push, send_multichannel_notification
    if request.is_json:
        data = request.get_json()
    else:
        data = request.form

    title = (data.get('title') or '').strip()
    body = (data.get('body') or '').strip()
    target = (data.get('target') or '').strip() # "all", "student:<id>", or "user:<id>"
    if not title and not body:
        return jsonify({"error": "Judul atau isi notifikasi wajib diisi"}), 400
    if not title:
        title = "Notifikasi"
    if not body:
        body = title
    
    if target == 'all':
        send_multichannel_notification(title, body, sender_id=user.id, allow_whatsapp=True)
    else:
        target_user_id = None
        if target.startswith('student:'):
            student = Student.query.get(int(target.split(':', 1)[1]))
            if not student:
                return jsonify({"error": "Member tidak ditemukan"}), 404
            if not student.user:
                return jsonify({"error": "Member ini belum memiliki akun aplikasi"}), 400
            target_user_id = student.user.id
        elif target.startswith('user:'):
            target_user_id = int(target.split(':', 1)[1])
        else:
            target_user_id = int(target)

        target_user = User.query.get(target_user_id)
        if not target_user:
            return jsonify({"error": "User penerima tidak ditemukan"}), 404
        if not target_user.fcm_token:
            return jsonify({"error": "Penerima belum memiliki token push aktif"}), 400

        send_push(title, body, user_id=target_user_id, sender_id=user.id)
        
    return jsonify({"status": "success"})

@api_bp.route('/fund/add', methods=['POST'])
@jwt_required()
def api_manage_fund():
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    if not user.role.can_manage_fund:
        return jsonify({"error": "Unauthorized"}), 403
        
    if request.is_json:
        data = request.get_json()
    else:
        data = request.form

    description = data.get('desc')
    amount = float(data.get('amount', 0))
    type_val = data.get('type')
    category = data.get('category')
    
    date_str = data.get('date')
    if date_str:
        try:
            date_val = datetime.strptime(date_str, '%Y-%m-%d')
        except:
            date_val = datetime.now()
    else:
        date_val = datetime.now()
        
    student_id_val = data.get('student_id')
    tags = data.get('tags', '')
    evidence_note = data.get('note', '')

    if tags and not tags.startswith('#'): tags = '#' + tags
    
    fund = BatchFund(
        description=description, 
        amount=amount, 
        type=type_val, 
        category=category,
        evidence_note=evidence_note,
        recorded_by=user.username,
        date=date_val,
        student_id=int(student_id_val) if student_id_val and str(student_id_val).lower() != 'none' else None,
        tags=tags
    )
    db.session.add(fund)
    db.session.commit()

    from app import send_push, log_activity, auto_recalculate_points
    # Notify student if it's a payment
    if fund.type == 'Masuk' and fund.student_id:
        student_user = User.query.filter_by(student_id=fund.student_id).first()
        if student_user:
            send_push("Pembayaran Berhasil!", f"Halo {student_user.full_name}, pembayaran kas Rp {fund.amount:,.0f} telah dikonfirmasi.", user_id=student_user.id)

    log_activity("Input Kas (Mobile)", f"{fund.description}: Rp {fund.amount:,.0f}")
    auto_recalculate_points()
    return jsonify({"status": "success", "id": fund.id})


@api_bp.route('/fund/<int:fund_id>', methods=['PUT'])
@jwt_required()
def update_fund_api(fund_id):
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    if not user.role.can_manage_fund:
        return jsonify({"error": "Unauthorized"}), 403

    fund = BatchFund.query.get_or_404(fund_id)
    data = request.get_json(silent=True) or request.form or {}
    reason = (data.get('reason') or '').strip()
    if not reason:
        return jsonify({"error": "Alasan perubahan wajib diisi"}), 400

    description = (data.get('desc') or '').strip()
    type_val = (data.get('type') or '').strip()
    category = (data.get('category') or '').strip()
    if not description or not type_val or not category:
        return jsonify({"error": "Deskripsi, tipe, dan kategori wajib diisi"}), 400

    try:
        amount = float(data.get('amount') or 0)
    except Exception:
        amount = 0
    if amount <= 0:
        return jsonify({"error": "Nominal wajib lebih dari 0"}), 400

    tags = (data.get('tags') or '').strip()
    if tags and not tags.startswith('#'):
        tags = '#' + tags

    student_id_val = data.get('student_id')
    student_id = fund.student_id
    if student_id_val is not None:
        student_id = None if str(student_id_val).lower() == 'none' or str(student_id_val).strip() == '' else int(student_id_val)

    note = data.get('note')

    if not fund.is_edited:
        fund.original_amount = fund.amount
        fund.original_description = fund.description

    fund.is_edited = True
    fund.edit_reason = reason
    fund.last_edited_by = user.username
    fund.description = description
    fund.amount = amount
    fund.type = type_val
    fund.category = category
    fund.student_id = student_id
    fund.tags = tags or None
    if note is not None:
        fund.evidence_note = str(note).strip() or None
    db.session.commit()

    from app import auto_recalculate_points, log_activity
    auto_recalculate_points()

    new_ann = Announcement(
        title=f"Update Transaksi: {fund.description}",
        content=f"ID: {fund.id} diperbarui oleh {user.username}.\nAlasan: {fund.edit_reason}\nNilai Baru: Rp {fund.amount:,.0f}",
        category='Penting'
    )
    db.session.add(new_ann)
    db.session.commit()

    log_activity("Edit Kas (Mobile)", f"ID: {fund.id}, Alasan: {fund.edit_reason}")
    return jsonify({"status": "success"})


@api_bp.route('/fund/<int:fund_id>', methods=['DELETE'])
@jwt_required()
def delete_fund_api(fund_id):
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    if not user.role.can_manage_fund:
        return jsonify({"error": "Unauthorized"}), 403

    fund = BatchFund.query.get_or_404(fund_id)
    description = fund.description
    db.session.delete(fund)
    db.session.commit()

    from app import auto_recalculate_points, log_activity
    auto_recalculate_points()
    log_activity("Hapus Kas (Mobile)", f"ID: {fund_id}, Deskripsi: {description}")
    return jsonify({"status": "success"})


@api_bp.route('/fund/<int:fund_id>/duplicate', methods=['POST'])
@jwt_required()
def duplicate_fund_api(fund_id):
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    if not user.role.can_manage_fund:
        return jsonify({"error": "Unauthorized"}), 403

    fund = BatchFund.query.get_or_404(fund_id)
    duplicated = BatchFund(
        description=f"{fund.description} (Copy)",
        amount=fund.amount,
        type=fund.type,
        category=fund.category,
        date=datetime.now(),
        recorded_by=user.username,
        student_id=fund.student_id,
        tags=fund.tags,
        evidence_note=fund.evidence_note
    )
    db.session.add(duplicated)
    db.session.commit()

    from app import auto_recalculate_points, log_activity
    auto_recalculate_points()
    log_activity("Duplikat Kas (Mobile)", f"Sumber ID: {fund.id}, Baru ID: {duplicated.id}")
    return jsonify({"status": "success", "id": duplicated.id})

@api_bp.route('/assignments', methods=['GET'])
@jwt_required()
def get_assignments():
    assignments = Assignment.query.order_by(Assignment.deadline.asc()).all()
    return jsonify([{
        "id": a.id,
        "title": a.title,
        "description": a.description,
        "subject": a.subject,
        "deadline": a.deadline.isoformat(),
        "is_public": a.is_public
    } for a in assignments])

@api_bp.route('/assignments', methods=['POST'])
@jwt_required()
def create_assignment():
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    if not user.role.can_manage_assignments:
        return jsonify({"error": "Unauthorized"}), 403
        
    data = request.get_json()
    a = Assignment(
        title=data.get('title'),
        subject=data.get('subject'),
        deadline=datetime.fromisoformat(data.get('deadline').replace('Z', '')),
        description=data.get('description', '')
    )
    db.session.add(a)
    db.session.commit()
    
    from app import send_multichannel_notification
    send_multichannel_notification(
        "Tugas Baru!",
        f"Tugas {a.subject}: {a.title}. Deadline: {a.deadline.strftime('%d %b %H:%M')}",
        sender_id=user.id,
        allow_whatsapp=True,
        whatsapp_text=f"Tugas baru\nMata kuliah: {a.subject}\nJudul: {a.title}\nDeadline: {a.deadline.strftime('%d %b %Y %H:%M')}"
    )
    
    return jsonify({"status": "success", "id": a.id})

@api_bp.route('/assignments/<int:id>', methods=['PUT', 'DELETE'])
@jwt_required()
def modify_assignment(id):
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    if not user.role.can_manage_assignments:
        return jsonify({"error": "Unauthorized"}), 403
        
    a = Assignment.query.get_or_404(id)
    
    if request.method == 'DELETE':
        db.session.delete(a)
        db.session.commit()
        return jsonify({"status": "success"})
        
    if request.method == 'PUT':
        data = request.get_json()
        a.title = data.get('title', a.title)
        a.subject = data.get('subject', a.subject)
        if data.get('deadline'):
            a.deadline = datetime.fromisoformat(data.get('deadline').replace('Z', ''))
        a.description = data.get('description', a.description)
        
        db.session.commit()
        
        from app import send_multichannel_notification
        send_multichannel_notification(
            "Tugas Diperbarui",
            f"Tugas {a.subject}: {a.title} telah diperbarui. Deadline: {a.deadline.strftime('%d %b %H:%M')}",
            sender_id=user.id,
            allow_whatsapp=True,
            whatsapp_text=f"Update tugas\nMata kuliah: {a.subject}\nJudul: {a.title}\nDeadline: {a.deadline.strftime('%d %b %Y %H:%M')}"
        )
        
        return jsonify({"status": "success"})
