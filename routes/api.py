from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity, verify_jwt_in_request
from flask_login import current_user
from models import db, User, Announcement, Schedule, SchedulePreset, BatchFund, Student, GalleryPhoto, SystemSetting, FundPeriod, ActivityLog, Assignment, NotificationHistory, ClassRoom, ClassroomNotificationConfig, WhatsAppBot, ClassroomWhatsAppBinding, normalize_member_status
from datetime import datetime, timedelta
import os
import json
from werkzeug.utils import secure_filename
from security_utils import hash_password, verify_password
from sqlalchemy import or_
from markupsafe import escape

api_bp = Blueprint('api', __name__, url_prefix='/api')


def _explore_contains(column, query):
    return db.func.lower(db.func.coalesce(column, '')).like(f"%{query}%")


def _explore_snippet(*values, fallback=''):
    for value in values:
        text = str(value or '').strip()
        if text:
            return text[:140]
    return fallback

def _absolute_public_url(path_or_url):
    value = str(path_or_url or '').strip()
    if value.startswith(('http://', 'https://')):
        return value
    if not value:
        return ''
    return request.host_url.rstrip('/') + '/' + value.lstrip('/')


def _get_json_payload(required=False):
    data = request.get_json(silent=True)
    if data is None:
        if request.form:
            data = request.form.to_dict(flat=True)
        else:
            data = {}
    if required and not data:
        raise ValueError('Payload JSON atau form wajib diisi')
    return data


def _parse_iso_datetime(value, field_name='deadline'):
    if not value:
        raise ValueError(f'{field_name} wajib diisi')
    normalized = str(value).strip().replace('Z', '+00:00')
    try:
        return datetime.fromisoformat(normalized)
    except Exception:
        raise ValueError(f'Format {field_name} tidak valid')


def _default_classroom():
    return ClassRoom.query.filter_by(name='Famousbytee.b').first() or ClassRoom.query.first()


def _user_classroom(user):
    if getattr(user, 'classroom_id', None):
        classroom = ClassRoom.query.get(user.classroom_id)
        if classroom:
            return classroom
    if user.student and user.student.classroom_id:
        return user.student.classroom
    return _default_classroom()


def _permission_payload(role):
    return {
        "can_manage_students": role.can_manage_students,
        "can_manage_schedule": role.can_manage_schedule,
        "can_manage_fund": role.can_manage_fund,
        "can_manage_announcements": role.can_manage_announcements,
        "can_manage_notifications": role.can_manage_notifications,
        "can_manage_whatsapp": role.can_manage_whatsapp,
        "sidobe_enabled": getattr(role, 'sidobe_enabled', role.can_manage_whatsapp),
        "can_manage_gallery": role.can_manage_gallery,
        "can_manage_assignments": role.can_manage_assignments,
        "can_view_logs": role.can_view_logs,
        "can_access_multi_classroom": getattr(role, 'can_access_multi_classroom', False),
        "can_switch_classroom_context": getattr(role, 'can_switch_classroom_context', False),
        "can_manage_classrooms": getattr(role, 'can_manage_classrooms', False),
        "can_view_all_classrooms": getattr(role, 'can_view_all_classrooms', False),
        "can_assign_users_to_classroom": getattr(role, 'can_assign_users_to_classroom', False),
        "can_move_users_between_classrooms": getattr(role, 'can_move_users_between_classrooms', False),
        "can_manage_students_multi_class": getattr(role, 'can_manage_students_multi_class', False),
        "can_manage_schedule_multi_class": getattr(role, 'can_manage_schedule_multi_class', False),
        "can_manage_announcements_multi_class": getattr(role, 'can_manage_announcements_multi_class', False),
        "can_manage_assignments_multi_class": getattr(role, 'can_manage_assignments_multi_class', False),
        "can_manage_gallery_multi_class": getattr(role, 'can_manage_gallery_multi_class', False),
        "can_manage_notifications_multi_class": getattr(role, 'can_manage_notifications_multi_class', False),
        "can_view_classroom_reports": getattr(role, 'can_view_classroom_reports', False),
        "can_export_classroom_data": getattr(role, 'can_export_classroom_data', False),
    }


def _can_access_multi_class_data(role):
    return bool(
        getattr(role, 'can_manage_roles', False) or
        getattr(role, 'can_access_multi_classroom', False) or
        getattr(role, 'can_view_all_classrooms', False) or
        getattr(role, 'can_switch_classroom_context', False)
    )


def _allowed_classrooms_for_user(user):
    current = _user_classroom(user)
    if user.role and _can_access_multi_class_data(user.role):
        return ClassRoom.query.order_by(ClassRoom.name.asc()).all()
    return [current] if current else []


def _can_manage_students_across_classes(role):
    return bool(
        getattr(role, 'can_manage_roles', False) or
        getattr(role, 'can_manage_students_multi_class', False) or
        getattr(role, 'can_assign_users_to_classroom', False) or
        getattr(role, 'can_move_users_between_classrooms', False) or
        getattr(role, 'can_view_all_classrooms', False)
    )


def _allowed_classroom_ids_for_student_management(user):
    if user.role and _can_manage_students_across_classes(user.role):
        return {item.id for item in ClassRoom.query.with_entities(ClassRoom.id).all()}
    classroom = _user_classroom(user)
    return {classroom.id} if classroom else set()


def _classroom_from_request(data, fallback_classroom, allowed_ids):
    raw_classroom_id = data.get('classroom_id')
    if raw_classroom_id in (None, ''):
        return fallback_classroom

    try:
        classroom_id = int(raw_classroom_id)
    except Exception:
        raise ValueError('Format classroom_id tidak valid')

    if classroom_id not in allowed_ids:
        raise PermissionError('Kelas tidak diizinkan')

    classroom = ClassRoom.query.get(classroom_id)
    if not classroom:
        raise LookupError('Kelas tidak ditemukan')
    return classroom


def _can_manage_notification_across_classes(role):
    return bool(
        getattr(role, 'can_manage_roles', False) or
        getattr(role, 'can_manage_notifications_multi_class', False)
    )


def _allowed_notification_classrooms_for_user(user):
    current = _user_classroom(user)
    if user.role and _can_manage_notification_across_classes(user.role):
        return ClassRoom.query.order_by(ClassRoom.name.asc()).all()
    return [current] if current else []


def _can_manage_fund_across_classes(role):
    return bool(
        getattr(role, 'can_manage_roles', False) or
        getattr(role, 'can_access_multi_classroom', False) or
        getattr(role, 'can_view_all_classrooms', False) or
        getattr(role, 'can_switch_classroom_context', False) or
        getattr(role, 'can_view_classroom_reports', False)
    )


def _allowed_fund_classrooms_for_user(user):
    current = _user_classroom(user)
    if user.role and _can_manage_fund_across_classes(user.role):
        return ClassRoom.query.order_by(ClassRoom.name.asc()).all()
    return [current] if current else []


def _requested_fund_classroom_for_user(user, data_source=None):
    fallback = _user_classroom(user)
    allowed = _allowed_fund_classrooms_for_user(user)
    allowed_ids = {item.id for item in allowed}
    raw_classroom_id = None
    if data_source:
        raw_classroom_id = data_source.get('classroom_id')
    if raw_classroom_id in (None, ''):
        raw_classroom_id = request.args.get('classroom_id')
    if raw_classroom_id not in (None, ''):
        try:
            classroom_id = int(raw_classroom_id)
        except Exception:
            raise ValueError('Format classroom_id tidak valid')
        if classroom_id not in allowed_ids:
            raise PermissionError('Kelas tidak diizinkan')
        classroom = ClassRoom.query.get(classroom_id)
        if not classroom:
            raise LookupError('Kelas tidak ditemukan')
        return classroom
    return fallback


def _apply_fund_classroom_filter(query, classroom, include_legacy_default=True):
    if not classroom:
        return query
    default_classroom = _default_classroom()
    if include_legacy_default and default_classroom and classroom.id == default_classroom.id:
        return query.filter(
            (BatchFund.classroom_id == classroom.id) |
            (BatchFund.classroom_id.is_(None))
        )
    return query.filter(BatchFund.classroom_id == classroom.id)


def _apply_fund_period_classroom_filter(query, classroom, include_legacy_default=True):
    if not classroom:
        return query
    default_classroom = _default_classroom()
    try:
        if include_legacy_default and default_classroom and classroom.id == default_classroom.id:
            return query.filter(
                (FundPeriod.classroom_id == classroom.id) |
                (FundPeriod.classroom_id.is_(None))
            )
        return query.filter(FundPeriod.classroom_id == classroom.id)
    except Exception:
        return query


def _is_fund_record_in_scope(record_classroom_id, classroom):
    if not classroom:
        return True
    if record_classroom_id == classroom.id:
        return True
    default_classroom = _default_classroom()
    return bool(record_classroom_id is None and default_classroom and classroom.id == default_classroom.id)


@api_bp.route('/leaderboard', methods=['GET'])
@jwt_required(optional=True)
def get_mobile_leaderboard():
    user = _api_request_user_or_session(require_api_access=True)
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        classroom = _requested_fund_classroom_for_user(user)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404

    from app import calculate_user_points_breakdown

    query = User.query.outerjoin(Student, User.student_id == Student.id)
    if classroom:
        query = query.filter(
            (User.classroom_id == classroom.id) |
            (Student.classroom_id == classroom.id)
        )

    ranked = []
    for member in query.all():
        breakdown = calculate_user_points_breakdown(member)
        if breakdown['total_points'] > 0:
            ranked.append((member, breakdown['total_points']))
    ranked.sort(key=lambda item: item[1], reverse=True)

    return jsonify([{
        "id": member.id,
        "full_name": (
            member.student.full_name
            if member.student and member.student.full_name
            else member.full_name
        ) or member.username,
        "points": points,
        "role": member.role.name if member.role else "-",
        "nim": member.student.nim if member.student else "-",
    } for member, points in ranked[:20]])


@api_bp.route('/leaderboard/<int:user_id>', methods=['GET'])
@jwt_required(optional=True)
def get_mobile_leaderboard_detail(user_id):
    requester = _api_request_user_or_session(require_api_access=True)
    member = User.query.get_or_404(user_id)
    if not requester:
        return jsonify({"error": "Unauthorized"}), 401

    allowed_ids = {item.id for item in _allowed_fund_classrooms_for_user(requester)}
    member_classroom_id = member.classroom_id or (
        member.student.classroom_id if member.student else None
    )
    if member_classroom_id not in allowed_ids:
        return jsonify({"error": "Kelas tidak diizinkan"}), 403

    from app import calculate_user_points_breakdown
    breakdown = calculate_user_points_breakdown(member)
    return jsonify({
        "id": member.id,
        "full_name": (
            member.student.full_name
            if member.student and member.student.full_name
            else member.full_name
        ) or member.username,
        "username": member.username,
        "nim": member.student.nim if member.student else "-",
        "role": member.role.name if member.role else "-",
        "points": breakdown['total_points'],
        "breakdown": {
            "fund_points": breakdown['fund_points'],
            "gallery_points": breakdown['gallery_points'],
            "arrears_penalty": 0,
            "total_paid": breakdown['total_paid'],
            "target_payment": breakdown['target_payment'],
            "arrears": breakdown['arrears'],
            "published_photos": breakdown['published_photos'],
        },
    })


def _api_request_user(require_api_access=False):
    verify_jwt_in_request(optional=True)
    identity = get_jwt_identity()
    user = User.query.get(int(identity)) if identity else None

    if not user and current_user.is_authenticated:
        user = current_user

    if not user:
        return None

    if require_api_access and not getattr(user.role, 'can_use_api', False):
        return None

    return user


def _api_request_user_or_session(require_api_access=False):
    """Resolve API user from JWT, then fall back to the Flask login session."""
    user = _api_request_user(require_api_access=require_api_access)
    if user:
        return user
    if current_user.is_authenticated:
        if require_api_access and not getattr(current_user.role, 'can_use_api', False):
            return None
        return current_user
    return None

@api_bp.route('/app/releases/windows/latest', methods=['GET'])
def latest_windows_release():
    manifest_path = os.path.join(
        current_app.static_folder,
        'releases',
        'windows',
        'latest.json',
    )
    default_manifest = {
        "version": "1.1.0",
        "build_number": 2,
        "installer_url": "/static/releases/windows/Famousbytee_Setup_1.1.0.exe",
        "sha256": "",
        "release_notes": "Rilis Windows terbaru Famousbytee.",
        "mandatory": False,
    }

    manifest = default_manifest
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, 'r', encoding='utf-8') as file:
                loaded_manifest = json.load(file)
            if isinstance(loaded_manifest, dict):
                manifest = {**default_manifest, **loaded_manifest}
        except (OSError, json.JSONDecodeError):
            manifest = default_manifest

    manifest["installer_url"] = _absolute_public_url(manifest.get("installer_url"))
    manifest["build_number"] = int(manifest.get("build_number") or 0)
    manifest["mandatory"] = bool(manifest.get("mandatory"))
    return jsonify(manifest)


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
    from app import _login_rate_limited, _clear_login_attempts
    client_ip = request.remote_addr or 'unknown'
    if _login_rate_limited(client_ip):
        return jsonify({"msg": "Too many login attempts. Try again later."}), 429

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
        classroom = _user_classroom(user)
        is_valid, needs_rehash = verify_password(user.password, password)

        if not is_valid:
            return jsonify({"msg": "Invalid credentials"}), 401

        if user.status != 'Active':
            return jsonify({"msg": "Invalid credentials"}), 401
            
        # Enforce API Access Control from Role
        if not user.role.can_use_api:
            return jsonify({"msg": "Your role does not have API access permissions"}), 403

        if needs_rehash:
            user.password = hash_password(password)
            db.session.commit()
        _clear_login_attempts(client_ip)

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
                "classroom_id": user.classroom_id or (user.student.classroom_id if user.student else None),
                "classroom_name": classroom.name if classroom else None,
                "classroom_batch": classroom.batch if classroom else None,
                "permissions": _permission_payload(user.role),
                "student": student_data
            }
        }), 200
    
    return jsonify({"msg": "Invalid credentials"}), 401


@api_bp.route('/classrooms', methods=['GET'])
@jwt_required()
def list_classrooms():
    user = User.query.get(int(get_jwt_identity()))
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    classrooms = _allowed_classrooms_for_user(user)
    if not classrooms:
        return jsonify([])

    return jsonify([
        {
            "id": item.id,
            "name": item.name,
            "batch": item.batch,
        }
        for item in classrooms
    ])

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
        
    valid_password, _ = verify_password(user.password, old_password)
    if not valid_password:
        return jsonify({"msg": "Old password incorrect"}), 401
        
    user.password = hash_password(new_password)
    db.session.commit()
    
    return jsonify({"msg": "Password changed successfully"})

@api_bp.route('/profile', methods=['GET'])
@jwt_required(optional=True)
def get_profile():
    user = _api_request_user_or_session(require_api_access=True)
    
    if not user:
        return jsonify({"msg": "User not found"}), 404
    
    classroom = _user_classroom(user)
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
        "classroom_id": user.classroom_id or (user.student.classroom_id if user.student else None),
        "classroom_name": classroom.name if classroom else None,
        "classroom_batch": classroom.batch if classroom else None,
        "permissions": _permission_payload(user.role),
        "student": student_data
    })

@api_bp.route('/profile/classroom', methods=['PUT'])
@jwt_required(optional=True)
def update_profile_classroom():
    user = _api_request_user_or_session(require_api_access=True)
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    try:
        classroom_id = int(data.get('classroom_id'))
    except Exception:
        return jsonify({"error": "classroom_id wajib diisi"}), 400

    allowed_classrooms = _allowed_classrooms_for_user(user)

    allowed_ids = {c.id for c in allowed_classrooms}
    if classroom_id not in allowed_ids:
        return jsonify({"error": "Kelas tidak diizinkan"}), 403

    classroom = ClassRoom.query.get(classroom_id)
    if not classroom:
        return jsonify({"error": "Kelas tidak ditemukan"}), 404

    user.classroom_id = classroom.id
    db.session.commit()
    return jsonify({
        "status": "success",
        "classroom_id": classroom.id,
        "classroom_name": classroom.name,
        "classroom_batch": classroom.batch,
    })


@api_bp.route('/classrooms', methods=['POST'])
@jwt_required()
def create_classroom_api():
    user = User.query.get(int(get_jwt_identity()))
    if not user or not (user.role.can_manage_roles or getattr(user.role, 'can_manage_classrooms', False)):
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json(silent=True) or request.form or {}
    name = (data.get('name') or '').strip()
    batch = (data.get('batch') or '').strip()
    if not name:
        return jsonify({"error": "Nama kelas wajib diisi"}), 400

    existing = ClassRoom.query.filter(db.func.lower(ClassRoom.name) == name.lower()).first()
    if existing:
        return jsonify({"error": "Nama kelas sudah digunakan"}), 400

    classroom = ClassRoom(name=name, batch=batch or None)
    db.session.add(classroom)
    db.session.commit()

    from app import log_activity
    log_activity("Tambah Kelas (Mobile)", f"{classroom.name}")
    return jsonify({
        "status": "success",
        "id": classroom.id,
        "name": classroom.name,
        "batch": classroom.batch,
    }), 201


@api_bp.route('/classrooms/<int:classroom_id>', methods=['PUT', 'DELETE'])
@jwt_required()
def update_classroom_api(classroom_id):
    user = User.query.get(int(get_jwt_identity()))
    if not user or not (user.role.can_manage_roles or getattr(user.role, 'can_manage_classrooms', False)):
        return jsonify({"error": "Unauthorized"}), 403

    classroom = ClassRoom.query.get_or_404(classroom_id)

    if request.method == 'DELETE':
        if Student.query.filter_by(classroom_id=classroom.id).count() > 0:
            return jsonify({"error": "Kelas masih dipakai member dan tidak bisa dihapus"}), 400
        if Schedule.query.filter_by(classroom_id=classroom.id).count() > 0:
            return jsonify({"error": "Kelas masih dipakai jadwal dan tidak bisa dihapus"}), 400
        db.session.delete(classroom)
        db.session.commit()
        from app import log_activity
        log_activity("Hapus Kelas (Mobile)", classroom.name)
        return jsonify({"status": "success"})

    data = request.get_json(silent=True) or request.form or {}
    name = (data.get('name') or classroom.name).strip()
    batch = (data.get('batch') or '').strip()
    if not name:
        return jsonify({"error": "Nama kelas wajib diisi"}), 400

    duplicate = ClassRoom.query.filter(
        db.func.lower(ClassRoom.name) == name.lower(),
        ClassRoom.id != classroom.id,
    ).first()
    if duplicate:
        return jsonify({"error": "Nama kelas sudah digunakan"}), 400

    classroom.name = name
    classroom.batch = batch or None
    db.session.commit()
    from app import log_activity
    log_activity("Edit Kelas (Mobile)", classroom.name)
    return jsonify({
        "status": "success",
        "id": classroom.id,
        "name": classroom.name,
        "batch": classroom.batch,
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
@jwt_required(optional=True)
def get_announcements():
    user = _api_request_user_or_session(require_api_access=True)
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    classroom = _user_classroom(user)

    if request.method == 'POST':
        if not user.role.can_manage_announcements:
            return jsonify({"error": "Unauthorized"}), 403

        data = request.get_json() or {}
        title = (data.get('title') or '').strip()
        content = (data.get('content') or '').strip()
        if not title or not content:
            return jsonify({"error": "Judul dan isi pengumuman wajib diisi"}), 400

        ann = Announcement(
            classroom_id=classroom.id if classroom else None,
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
        send_push(
            title_prefix,
            ann.title,
            sender_id=user.id,
            classroom_id=ann.classroom_id,
            category='announcement',
        )
        log_activity("Tambah Pengumuman API", f"Judul: {ann.title}")
        return jsonify({"status": "success", "id": ann.id})

    announcements_query = Announcement.query
    if classroom:
        announcements_query = announcements_query.filter(
            (Announcement.classroom_id == classroom.id) | (Announcement.classroom_id.is_(None))
        )
    announcements = announcements_query.order_by(Announcement.is_pinned.desc(), Announcement.date_posted.desc()).all()
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
    classroom = _user_classroom(user)
    if classroom and ann.classroom_id not in (classroom.id, None):
        return jsonify({"error": "Not found"}), 404
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
        classroom = _user_classroom(user)
        if classroom:
            announcement_query = announcement_query.filter(
                (Announcement.classroom_id == classroom.id) |
                (Announcement.classroom_id.is_(None))
            )
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
        classroom = _user_classroom(user)
        if classroom:
            schedule_query = schedule_query.filter_by(classroom_id=classroom.id)
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
        classroom = _user_classroom(user)
        if classroom:
            assignment_query = assignment_query.filter(
                (Assignment.classroom_id == classroom.id) |
                (Assignment.classroom_id.is_(None))
            )
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
        classroom = _user_classroom(user)
        fund_query = _apply_fund_classroom_filter(fund_query, classroom)
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

        classroom = _user_classroom(user)
        if classroom:
            default_classroom = _default_classroom()
            if default_classroom and classroom.id == default_classroom.id:
                gallery_query = gallery_query.filter(
                    (GalleryPhoto.classroom_id == classroom.id) |
                    (GalleryPhoto.classroom_id.is_(None))
                )
            else:
                gallery_query = gallery_query.filter_by(classroom_id=classroom.id)

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
        classroom = _user_classroom(user)
        if classroom:
            member_query = member_query.filter_by(classroom_id=classroom.id)
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
@jwt_required(optional=True)
def manage_schedules():
    user = _api_request_user_or_session(require_api_access=True)
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    
    if request.method == 'POST':
        if not user.role.can_manage_schedule:
            return jsonify({"error": "Unauthorized"}), 403
            
        data = request.get_json()
        from models import ClassRoom
        class_fb = _user_classroom(user)
        
        s = Schedule(
            classroom_id=class_fb.id if class_fb else None,
            day=data.get('day'),
            time_start=data.get('time_start'),
            time_end=data.get('time_end'),
            subject=data.get('subject'),
            lecturer=data.get('lecturer', '-'),
            room=data.get('room', '-')
        )
        db.session.add(s)
        db.session.commit()
        
        preferences = get_notification_preferences_for_classroom(s.classroom_id)
        if preferences['schedule_notify_on_create']:
            from app import send_multichannel_notification
            send_multichannel_notification(
                "Jadwal Baru Ditambahkan",
                f"Jadwal {s.subject} ditambahkan pada hari {s.day} pukul {s.time_start}.",
                sender_id=user.id,
                allow_whatsapp=True,
                classroom_id=s.classroom_id,
                category='schedule',
            )
        
        return jsonify({"status": "success", "id": s.id})

    # GET logic
    classroom = _user_classroom(user)
    schedules_query = Schedule.query
    if classroom:
        schedules_query = schedules_query.filter_by(classroom_id=classroom.id)
    schedules = schedules_query.all()
        
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
        classroom_id = s.classroom_id
        db.session.delete(s)
        db.session.commit()

        preferences = get_notification_preferences_for_classroom(classroom_id)
        if preferences['schedule_notify_on_delete']:
            from app import send_multichannel_notification
            send_multichannel_notification(
                "Jadwal Dihapus",
                f"Jadwal {subject_name} telah dihapus dari sistem.",
                sender_id=user.id,
                allow_whatsapp=True,
                classroom_id=classroom_id,
                category='schedule',
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
        
        preferences = get_notification_preferences_for_classroom(s.classroom_id)
        if preferences['schedule_notify_on_edit']:
            from app import send_multichannel_notification
            send_multichannel_notification(
                "Jadwal Diperbarui",
                f"Jadwal {s.subject} telah diperbarui menjadi hari {s.day} pukul {s.time_start}.",
                sender_id=user.id,
                allow_whatsapp=True,
                classroom_id=s.classroom_id,
                category='schedule',
            )
        
        return jsonify({"status": "success"})

def _schedule_classroom_for_user(user):
    return _user_classroom(user)

def _schedule_preset_payload(preset):
    return {
        "id": preset.id,
        "name": preset.name,
        "subject": preset.subject,
        "lecturer": preset.lecturer or "",
        "room": preset.room or "",
        "classroom_id": preset.classroom_id,
        "created_by": preset.created_by,
        "created_at": preset.created_at.isoformat() if preset.created_at else None,
        "updated_at": preset.updated_at.isoformat() if preset.updated_at else None,
    }

@api_bp.route('/schedules/presets', methods=['GET', 'POST'])
@jwt_required(optional=True)
def schedule_presets():
    user = _api_request_user_or_session(require_api_access=True)
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    if not user.role.can_manage_schedule:
        return jsonify({"error": "Unauthorized"}), 403

    classroom = _schedule_classroom_for_user(user)

    if request.method == 'POST':
        data = request.get_json() or {}
        name = (data.get('name') or '').strip()
        subject = (data.get('subject') or '').strip()
        if not name or not subject:
            return jsonify({"error": "Nama preset dan mata kuliah wajib diisi"}), 400

        preset = SchedulePreset(
            classroom_id=classroom.id if classroom else None,
            name=name,
            subject=subject,
            lecturer=(data.get('lecturer') or '').strip(),
            room=(data.get('room') or '').strip(),
            created_by=user.id
        )
        db.session.add(preset)
        db.session.commit()
        return jsonify({"status": "success", "preset": _schedule_preset_payload(preset)}), 201

    query = SchedulePreset.query
    if classroom:
        query = query.filter(
            (SchedulePreset.classroom_id == classroom.id) |
            (SchedulePreset.classroom_id.is_(None))
        )
    presets = query.order_by(SchedulePreset.name.asc()).all()
    return jsonify([_schedule_preset_payload(preset) for preset in presets])

@api_bp.route('/schedules/presets/<int:preset_id>', methods=['PUT', 'DELETE'])
@jwt_required()
def modify_schedule_preset(preset_id):
    user = User.query.get(int(get_jwt_identity()))
    if not user.role.can_manage_schedule:
        return jsonify({"error": "Unauthorized"}), 403

    preset = SchedulePreset.query.get_or_404(preset_id)
    classroom = _schedule_classroom_for_user(user)
    if classroom and preset.classroom_id not in (classroom.id, None):
        return jsonify({"error": "Not found"}), 404

    if request.method == 'DELETE':
        db.session.delete(preset)
        db.session.commit()
        return jsonify({"status": "success"})

    data = request.get_json() or {}
    name = (data.get('name') or preset.name or '').strip()
    subject = (data.get('subject') or preset.subject or '').strip()
    if not name or not subject:
        return jsonify({"error": "Nama preset dan mata kuliah wajib diisi"}), 400

    preset.name = name
    preset.subject = subject
    preset.lecturer = (data.get('lecturer') or '').strip()
    preset.room = (data.get('room') or '').strip()
    db.session.commit()
    return jsonify({"status": "success", "preset": _schedule_preset_payload(preset)})

def _schedule_template_payload(template):
    return {
        "id": template.id,
        "name": template.name,
        "description": template.description or "",
        "classroom_id": template.classroom_id,
        "items_count": len(template.items),
        "items": [{
            "id": item.id,
            "day": item.day,
            "time_start": item.time_start,
            "time_end": item.time_end,
            "subject": item.subject,
            "lecturer": item.lecturer,
            "room": item.room,
            "sort_order": item.sort_order,
        } for item in template.items]
    }

@api_bp.route('/schedules/templates', methods=['GET', 'POST'])
@jwt_required()
def schedule_templates():
    user = User.query.get(int(get_jwt_identity()))
    if not user.role.can_manage_schedule:
        return jsonify({"error": "Unauthorized"}), 403

    from models import ScheduleTemplate
    classroom = _schedule_classroom_for_user(user)

    if request.method == 'POST':
        data = request.get_json() or {}
        name = (data.get('name') or '').strip()
        if not name:
            return jsonify({"error": "Nama template wajib diisi"}), 400

        template = ScheduleTemplate(
            classroom_id=classroom.id if classroom else None,
            name=name,
            description=(data.get('description') or '').strip(),
            created_by=user.id
        )
        db.session.add(template)
        db.session.commit()
        return jsonify({"status": "success", "template": _schedule_template_payload(template)}), 201

    query = ScheduleTemplate.query
    if classroom:
        query = query.filter(
            (ScheduleTemplate.classroom_id == classroom.id) |
            (ScheduleTemplate.classroom_id.is_(None))
        )
    templates = query.order_by(ScheduleTemplate.updated_at.desc(), ScheduleTemplate.name.asc()).all()
    return jsonify([_schedule_template_payload(template) for template in templates])

@api_bp.route('/schedules/templates/from-current', methods=['POST'])
@jwt_required()
def schedule_template_from_current():
    user = User.query.get(int(get_jwt_identity()))
    if not user.role.can_manage_schedule:
        return jsonify({"error": "Unauthorized"}), 403

    from models import ScheduleTemplate, ScheduleTemplateItem
    classroom = _schedule_classroom_for_user(user)
    schedules = Schedule.query.filter_by(classroom_id=classroom.id).order_by(Schedule.day.asc(), Schedule.time_start.asc()).all() if classroom else []
    if not schedules:
        return jsonify({"error": "Belum ada jadwal aktif untuk dijadikan template"}), 400

    data = request.get_json() or {}
    template = ScheduleTemplate(
        classroom_id=classroom.id,
        name=(data.get('name') or f"Template Jadwal").strip(),
        description=(data.get('description') or 'Dibuat dari jadwal aktif.').strip(),
        created_by=user.id
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
    return jsonify({"status": "success", "template": _schedule_template_payload(template)}), 201

@api_bp.route('/schedules/templates/<int:template_id>/items', methods=['POST'])
@jwt_required()
def add_schedule_template_item_api(template_id):
    user = User.query.get(int(get_jwt_identity()))
    if not user.role.can_manage_schedule:
        return jsonify({"error": "Unauthorized"}), 403

    from models import ScheduleTemplate, ScheduleTemplateItem
    template = ScheduleTemplate.query.get_or_404(template_id)
    data = request.get_json() or {}
    required = ['day', 'time_start', 'time_end', 'subject']
    if any(not (data.get(key) or '').strip() for key in required):
        return jsonify({"error": "Hari, jam, dan mata kuliah wajib diisi"}), 400

    last_item = ScheduleTemplateItem.query.filter_by(template_id=template.id).order_by(ScheduleTemplateItem.sort_order.desc()).first()
    item = ScheduleTemplateItem(
        template_id=template.id,
        day=data['day'],
        time_start=data['time_start'],
        time_end=data['time_end'],
        subject=data['subject'],
        lecturer=data.get('lecturer') or '-',
        room=data.get('room') or '-',
        sort_order=(last_item.sort_order + 1) if last_item else 1
    )
    db.session.add(item)
    db.session.commit()
    return jsonify({"status": "success", "template": _schedule_template_payload(template)})

@api_bp.route('/schedules/templates/items/<int:item_id>', methods=['DELETE'])
@jwt_required()
def delete_schedule_template_item_api(item_id):
    user = User.query.get(int(get_jwt_identity()))
    if not user.role.can_manage_schedule:
        return jsonify({"error": "Unauthorized"}), 403

    from models import ScheduleTemplateItem
    item = ScheduleTemplateItem.query.get_or_404(item_id)
    db.session.delete(item)
    db.session.commit()
    return jsonify({"status": "success"})

@api_bp.route('/schedules/templates/<int:template_id>/apply', methods=['POST'])
@jwt_required()
def apply_schedule_template_api(template_id):
    user = User.query.get(int(get_jwt_identity()))
    if not user.role.can_manage_schedule:
        return jsonify({"error": "Unauthorized"}), 403

    from models import ScheduleTemplate
    classroom = _schedule_classroom_for_user(user)
    template = ScheduleTemplate.query.get_or_404(template_id)
    if not template.items:
        return jsonify({"error": "Template belum memiliki item jadwal"}), 400

    data = request.get_json() or {}
    if data.get('replace_existing', True):
        Schedule.query.filter_by(classroom_id=classroom.id).delete(synchronize_session=False)

    for item in template.items:
        db.session.add(Schedule(
            classroom_id=classroom.id,
            day=item.day,
            time_start=item.time_start,
            time_end=item.time_end,
            subject=item.subject,
            lecturer=item.lecturer or '-',
            room=item.room or '-'
        ))
    db.session.commit()
    return jsonify({"status": "success", "applied_items": len(template.items)})

@api_bp.route('/schedules/templates/<int:template_id>/duplicate', methods=['POST'])
@jwt_required()
def duplicate_schedule_template_api(template_id):
    user = User.query.get(int(get_jwt_identity()))
    if not user.role.can_manage_schedule:
        return jsonify({"error": "Unauthorized"}), 403

    from models import ScheduleTemplate, ScheduleTemplateItem
    original = ScheduleTemplate.query.get_or_404(template_id)
    data = request.get_json() or {}
    duplicate = ScheduleTemplate(
        classroom_id=original.classroom_id,
        name=(data.get('name') or f"Salinan {original.name}").strip(),
        description=original.description,
        created_by=user.id
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
    return jsonify({"status": "success", "template": _schedule_template_payload(duplicate)}), 201

@api_bp.route('/schedules/templates/<int:template_id>', methods=['DELETE'])
@jwt_required()
def delete_schedule_template_api(template_id):
    user = User.query.get(int(get_jwt_identity()))
    if not user.role.can_manage_schedule:
        return jsonify({"error": "Unauthorized"}), 403

    from models import ScheduleTemplate
    template = ScheduleTemplate.query.get_or_404(template_id)
    db.session.delete(template)
    db.session.commit()
    return jsonify({"status": "success"})

@api_bp.route('/notifications/preferences', methods=['GET'])
def get_notification_preferences():
    """Get notification preferences for schedule management"""
    from app import get_classroom_notification_policy
    user = _api_request_user_or_session(require_api_access=True)
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    classroom = _user_classroom(user)
    policy = get_classroom_notification_policy(classroom.id if classroom else None)

    def preference(name, default):
        if not classroom:
            return default
        setting = SystemSetting.query.filter_by(
            key=f'notify_schedule_{name}_{classroom.id}'
        ).first()
        if not setting:
            return default
        return str(setting.value).strip().lower() == 'true'

    master_enabled = policy.schedule_enabled if policy else True
    return jsonify({
        'schedule_notify_on_create': preference('create', master_enabled),
        'schedule_notify_on_edit': preference('edit', False),
        'schedule_notify_on_delete': preference('delete', master_enabled),
    })

@api_bp.route('/notifications/preferences', methods=['POST'])
def update_notification_preferences():
    """Update notification preferences for schedule management"""
    from app import get_classroom_notification_policy
    user = _api_request_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    
    if not (user.role.can_manage_schedule and user.role.can_manage_notifications):
        return jsonify({"error": "Perlu izin kelola jadwal dan notifikasi"}), 403
    
    data = request.get_json(silent=True) or {}
    
    classroom = _user_classroom(user)
    if not classroom:
        return jsonify({"error": "Kelas aktif tidak ditemukan"}), 400
    policy = get_classroom_notification_policy(classroom.id)
    if not policy:
        policy = ClassroomNotificationConfig(classroom_id=classroom.id)
        db.session.add(policy)

    allowed_keys = {
        'schedule_notify_on_create': 'create',
        'schedule_notify_on_edit': 'edit',
        'schedule_notify_on_delete': 'delete',
    }
    for request_key, setting_suffix in allowed_keys.items():
        if request_key not in data:
            continue
        value = data[request_key]
        enabled = str(value).strip().lower() == 'true' if isinstance(value, str) else bool(value)
        setting_key = f'notify_schedule_{setting_suffix}_{classroom.id}'
        setting = SystemSetting.query.filter_by(key=setting_key).first()
        if setting:
            setting.value = 'true' if enabled else 'false'
        else:
            db.session.add(SystemSetting(
                key=setting_key,
                value='true' if enabled else 'false',
                description=f'Notifikasi jadwal {setting_suffix} untuk kelas {classroom.name}',
            ))

    values = get_notification_preferences_for_classroom(classroom.id, data)
    policy.schedule_enabled = any(values.values())
    policy.updated_by = user.id
    db.session.commit()
    return jsonify({"status": "success", **values})


def get_notification_preferences_for_classroom(classroom_id, overrides=None):
    overrides = overrides or {}
    result = {}
    defaults = {'create': True, 'edit': False, 'delete': True}
    for suffix, default in defaults.items():
        request_key = f'schedule_notify_on_{suffix}'
        if request_key in overrides:
            raw = overrides[request_key]
            result[request_key] = str(raw).strip().lower() == 'true' if isinstance(raw, str) else bool(raw)
            continue
        setting = SystemSetting.query.filter_by(
            key=f'notify_schedule_{suffix}_{classroom_id}'
        ).first()
        result[request_key] = (
            str(setting.value).strip().lower() == 'true' if setting else default
        )
    return result


@api_bp.route('/notifications/classrooms/<int:classroom_id>/policy', methods=['GET', 'PUT'])
def classroom_notification_policy_api(classroom_id):
    user = _api_request_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    if not user or not user.role.can_manage_notifications:
        return jsonify({"error": "Unauthorized"}), 403

    allowed_ids = {item.id for item in _allowed_notification_classrooms_for_user(user)}
    if classroom_id not in allowed_ids:
        return jsonify({"error": "Kelas tidak diizinkan"}), 403

    classroom = ClassRoom.query.get_or_404(classroom_id)
    policy = ClassroomNotificationConfig.query.filter_by(classroom_id=classroom_id).first()

    if request.method == 'GET':
        return jsonify({
            'classroom_id': classroom.id,
            'classroom_name': classroom.name,
            'classroom_batch': classroom.batch,
            'push_enabled': policy.push_enabled if policy else True,
            'sidobe_enabled': policy.whatsapp_enabled if policy else False,
            'whatsapp_enabled': policy.whatsapp_enabled if policy else False,
            'default_channel': policy.default_channel if policy else 'push',
            'announcement_enabled': policy.announcement_enabled if policy else True,
            'assignment_enabled': policy.assignment_enabled if policy else True,
            'schedule_enabled': policy.schedule_enabled if policy else True,
            'finance_enabled': policy.finance_enabled if policy else True,
            'emergency_enabled': policy.emergency_enabled if policy else True,
        })

    data = request.get_json(silent=True) or request.form or {}
    if not policy:
        policy = ClassroomNotificationConfig(classroom_id=classroom_id)
        db.session.add(policy)

    policy.push_enabled = str(data.get('push_enabled', policy.push_enabled)).lower() == 'true' if isinstance(data.get('push_enabled'), str) else bool(data.get('push_enabled', policy.push_enabled))
    incoming_sidobe_enabled = data.get('sidobe_enabled', data.get('whatsapp_enabled', policy.whatsapp_enabled))
    policy.whatsapp_enabled = str(incoming_sidobe_enabled).lower() == 'true' if isinstance(incoming_sidobe_enabled, str) else bool(incoming_sidobe_enabled)
    default_channel = (data.get('default_channel') or policy.default_channel or 'push').strip().lower()
    policy.default_channel = default_channel if default_channel in {'push', 'whatsapp', 'both'} else 'push'
    for field in ['announcement_enabled', 'assignment_enabled', 'schedule_enabled', 'finance_enabled', 'emergency_enabled']:
        incoming = data.get(field, getattr(policy, field))
        setattr(policy, field, str(incoming).lower() == 'true' if isinstance(incoming, str) else bool(incoming))
    policy.updated_by = user.id
    db.session.commit()
    return jsonify({'status': 'success'})


@api_bp.route('/notifications/classrooms/<int:classroom_id>/whatsapp-binding', methods=['GET', 'PUT'])
def classroom_whatsapp_binding_api(classroom_id):
    user = _api_request_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    if not user or not user.role.sidobe_enabled:
        return jsonify({"error": "Unauthorized"}), 403

    allowed_ids = {item.id for item in _allowed_notification_classrooms_for_user(user)}
    if classroom_id not in allowed_ids:
        return jsonify({"error": "Kelas tidak diizinkan"}), 403

    classroom = ClassRoom.query.get_or_404(classroom_id)
    binding = ClassroomWhatsAppBinding.query.filter_by(classroom_id=classroom_id).first()

    if request.method == 'GET':
        return jsonify({
            'classroom_id': classroom.id,
            'classroom_name': classroom.name,
            'classroom_batch': classroom.batch,
            'bot_id': binding.bot_id if binding else None,
            'chat_id': binding.chat_id if binding else '',
            'chat_label': binding.chat_label if binding else '',
            'is_default': binding.is_default if binding else True,
        })

    data = request.get_json(silent=True) or request.form or {}
    bot_id = int(data.get('bot_id')) if data.get('bot_id') not in (None, '') else None
    chat_id = (data.get('chat_id') or '').strip()
    chat_label = (data.get('chat_label') or '').strip()
    if not bot_id or not chat_id:
        return jsonify({'error': 'bot_id dan chat_id wajib diisi'}), 400
    bot = WhatsAppBot.query.get(bot_id)
    if not bot:
        return jsonify({'error': 'Bot tidak ditemukan'}), 404
    if not binding:
        binding = ClassroomWhatsAppBinding(
            classroom_id=classroom_id,
            bot_id=bot_id,
            chat_id=chat_id,
            chat_label=chat_label or classroom.name,
            is_default=True,
            updated_by=user.id,
        )
        db.session.add(binding)
    else:
        binding.bot_id = bot_id
        binding.chat_id = chat_id
        binding.chat_label = chat_label or binding.chat_label or classroom.name
        binding.updated_by = user.id
    db.session.commit()
    return jsonify({'status': 'success'})


@api_bp.route('/notifications/bots', methods=['GET', 'POST'])
def notification_bots_api():
    user = _api_request_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    if not user or not user.role.sidobe_enabled:
        return jsonify({"error": "Unauthorized"}), 403

    if request.method == 'GET':
        bots = WhatsAppBot.query.order_by(WhatsAppBot.name.asc()).all()
        return jsonify([{
            'id': bot.id,
            'name': bot.name,
            'provider': bot.provider,
            'session_name': bot.session_name,
            'base_url': bot.base_url or '',
            'status': bot.status or 'unknown',
            'is_active': bot.is_active,
            'last_seen_at': bot.last_seen_at.isoformat() if bot.last_seen_at else None,
        } for bot in bots])

    data = request.get_json(silent=True) or request.form or {}
    name = (data.get('name') or '').strip()
    session_name = (data.get('session_name') or '').strip()
    if not name or not session_name:
        return jsonify({'error': 'Nama bot dan session_name wajib diisi'}), 400
    bot = WhatsAppBot(
        name=name,
        provider=(data.get('provider') or 'sidobe').strip() or 'sidobe',
        session_name=session_name,
        base_url=(data.get('base_url') or '').strip() or None,
        status=(data.get('status') or 'configured').strip() or 'configured',
        is_active=str(data.get('is_active', 'true')).lower() == 'true',
    )
    db.session.add(bot)
    db.session.commit()
    return jsonify({'status': 'success', 'id': bot.id}), 201


@api_bp.route('/notifications/bots/<int:bot_id>', methods=['PUT'])
def update_notification_bot_api(bot_id):
    user = _api_request_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    if not user or not user.role.sidobe_enabled:
        return jsonify({"error": "Unauthorized"}), 403
    bot = WhatsAppBot.query.get_or_404(bot_id)
    data = request.get_json(silent=True) or request.form or {}
    if data.get('name') is not None:
        bot.name = (data.get('name') or bot.name).strip() or bot.name
    if data.get('session_name') is not None:
        bot.session_name = (data.get('session_name') or bot.session_name).strip() or bot.session_name
    if data.get('base_url') is not None:
        bot.base_url = (data.get('base_url') or '').strip() or None
    if data.get('status') is not None:
        bot.status = (data.get('status') or 'configured').strip() or 'configured'
    if data.get('is_active') is not None:
        bot.is_active = str(data.get('is_active')).lower() == 'true' if isinstance(data.get('is_active'), str) else bool(data.get('is_active'))
    db.session.commit()
    return jsonify({'status': 'success'})


@api_bp.route('/notifications/bots/<int:bot_id>', methods=['DELETE'])
def delete_notification_bot_api(bot_id):
    user = _api_request_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    if not user or not user.role.sidobe_enabled:
        return jsonify({"error": "Unauthorized"}), 403
    bot = WhatsAppBot.query.get_or_404(bot_id)
    if ClassroomWhatsAppBinding.query.filter_by(bot_id=bot.id).first():
        return jsonify({'error': 'Bot masih dipakai binding kelas'}), 400
    db.session.delete(bot)
    db.session.commit()
    return jsonify({'status': 'success'})


@api_bp.route('/notifications/bots/<int:bot_id>/health', methods=['GET'])
def notification_bot_health_api(bot_id):
    user = _api_request_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    if not user or not user.role.sidobe_enabled:
        return jsonify({"error": "Unauthorized"}), 403
    from app import _sidobe_request_with_auth_fallback, _sidobe_normalize_scalar
    bot = WhatsAppBot.query.get_or_404(bot_id)
    result = _sidobe_request_with_auth_fallback('GET', '/api/sessions', base_url_override=bot.base_url or None)
    if not result.get('ok'):
        return jsonify({'ok': False, 'error': result.get('error', 'Gagal cek bot')}), 400
    raw_data = result.get('data') or []
    sessions = raw_data if isinstance(raw_data, list) else raw_data.get('sessions', [])
    matched = None
    for item in sessions:
        session_name = _sidobe_normalize_scalar(item.get('name') or item.get('session') or item.get('id') or '')
        if session_name == bot.session_name:
            matched = item
            break
    return jsonify({
        'ok': bool(matched),
        'bot_id': bot.id,
        'session_name': bot.session_name,
        'status': _sidobe_normalize_scalar((matched or {}).get('status') or (matched or {}).get('state') or 'not-found'),
    })


@api_bp.route('/notifications/bots/<int:bot_id>/groups', methods=['GET'])
def notification_bot_groups_api(bot_id):
    user = _api_request_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    if not user or not user.role.sidobe_enabled:
        return jsonify({"error": "Unauthorized"}), 403
    bot = WhatsAppBot.query.get_or_404(bot_id)
    from app import _sidobe_request_with_auth_fallback, _sidobe_normalize_scalar, _sidobe_normalize_chat_id
    result = _sidobe_request_with_auth_fallback('GET', f'/api/{bot.session_name}/groups', base_url_override=bot.base_url or None)
    raw_data = []
    chats = []
    if result.get('ok'):
        raw_data = result.get('data') or []
        chats = raw_data if isinstance(raw_data, list) else raw_data.get('groups', raw_data.get('chats', []))
    if not chats:
        fallback = _sidobe_request_with_auth_fallback('GET', f'/api/{bot.session_name}/chats', base_url_override=bot.base_url or None)
        if fallback.get('ok'):
            raw_data = fallback.get('data') or []
            chats = raw_data if isinstance(raw_data, list) else raw_data.get('chats', [])
        elif not result.get('ok'):
            return jsonify({'ok': False, 'error': result.get('error', fallback.get('error', 'Gagal memuat grup Si Dobe'))}), 400
    normalized = []
    for item in chats:
        if not isinstance(item, dict):
            continue
        chat_id = _sidobe_normalize_chat_id(item)
        if not chat_id or '@g.us' not in chat_id:
            continue
        normalized.append({
            'name': _sidobe_normalize_scalar(item.get('name') or item.get('pushName') or item.get('shortName') or item.get('formattedTitle') or chat_id),
            'chat_id': chat_id,
            'participants': item.get('participantsCount') or item.get('size') or 0,
            'owner': _sidobe_normalize_scalar(item.get('owner') or item.get('ownerPn') or '-'),
        })
    return jsonify({'ok': True, 'items': normalized, 'count': len(normalized), 'session': bot.session_name, 'bot_id': bot.id})


@api_bp.route('/notifications/sidobe/dashboard', methods=['GET'])
def notification_sidobe_dashboard_api():
    user = _api_request_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    if not user or not user.role.sidobe_enabled:
        return jsonify({"error": "Unauthorized"}), 403
    from app import get_sidobe_setting_value, _sidobe_request_with_auth_fallback, _sidobe_normalize_scalar
    base_url = get_sidobe_setting_value('base_url', '').strip() or None

    dashboard = {
        'ok': True,
        'base_url': base_url or '',
        'workers': [],
        'sessions': [],
        'worker_error': '',
        'session_error': '',
    }

    workers_result = _sidobe_request_with_auth_fallback('GET', '/api/workers', base_url_override=base_url)
    if workers_result.get('ok'):
        raw_workers = workers_result.get('data') or []
        workers = raw_workers if isinstance(raw_workers, list) else raw_workers.get('workers', raw_workers.get('data', []))
        dashboard['workers'] = [{
            'name': _sidobe_normalize_scalar(item.get('name') or item.get('id') or 'worker'),
            'api': _sidobe_normalize_scalar(item.get('api') or item.get('baseUrl') or base_url or ''),
            'status': _sidobe_normalize_scalar(item.get('status') or item.get('state') or 'unknown'),
            'info': _sidobe_normalize_scalar(item.get('info') or item.get('version') or item.get('build') or ''),
            'sessions': item.get('sessions') or item.get('sessionCount') or item.get('workingSessions') or 0,
        } for item in workers if isinstance(item, dict)]
    else:
        dashboard['worker_error'] = workers_result.get('error', 'Gagal memuat worker Si Dobe')

    sessions_result = _sidobe_request_with_auth_fallback('GET', '/api/sessions', base_url_override=base_url)
    if sessions_result.get('ok'):
        raw_sessions = sessions_result.get('data') or []
        sessions = raw_sessions if isinstance(raw_sessions, list) else raw_sessions.get('sessions', [])
        dashboard['sessions'] = [{
            'name': _sidobe_normalize_scalar(item.get('name') or item.get('session') or item.get('id') or '-'),
            'status': _sidobe_normalize_scalar(item.get('status') or item.get('state') or item.get('connectionStatus') or 'unknown'),
            'account': _sidobe_normalize_scalar(item.get('me') or item.get('meId') or item.get('phone') or item.get('wid') or '-'),
            'server': _sidobe_normalize_scalar(item.get('server') or item.get('worker') or item.get('engine') or 'default'),
            'qr': _sidobe_normalize_scalar(item.get('qr') or item.get('qrcode') or item.get('qrCode') or item.get('qr_code') or ''),
        } for item in sessions if isinstance(item, dict)]
    else:
        dashboard['session_error'] = sessions_result.get('error', 'Gagal memuat session Si Dobe')

    return jsonify(dashboard)

@api_bp.route('/notifications/sidobe/sessions/create', methods=['POST'])
@jwt_required()
def notification_sidobe_create_session_api():
    user = _api_request_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    if not user or not user.role.sidobe_enabled:
        return jsonify({"error": "Unauthorized"}), 403
    from app import _sidobe_request_any, get_sidobe_setting_value
    data = request.get_json(silent=True) or {}
    session_name = (data.get('session_name') or data.get('name') or '').strip()
    if not session_name:
        return jsonify({'ok': False, 'error': 'Session name wajib diisi'}), 400
    base_url = (data.get('base_url') or get_sidobe_setting_value('base_url', '')).strip() or None
    result = _sidobe_request_any('POST', ['/api/sessions', '/api/session'], payload={'name': session_name, 'session': session_name}, base_url_override=base_url)
    if not result.get('ok'):
        return jsonify({'ok': False, 'error': result.get('error', 'Gagal membuat session Si Dobe')}), 400
    return jsonify({'ok': True, 'path': result.get('path'), 'data': result.get('data')})

@api_bp.route('/notifications/sidobe/sessions/<string:session_name>/start', methods=['POST'])
@jwt_required()
def notification_sidobe_start_session_api(session_name):
    user = _api_request_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    if not user or not user.role.sidobe_enabled:
        return jsonify({"error": "Unauthorized"}), 403
    from app import _sidobe_request_any, get_sidobe_setting_value
    base_url = (request.get_json(silent=True) or {}).get('base_url') or get_sidobe_setting_value('base_url', '')
    result = _sidobe_request_any('POST', [
        f'/api/sessions/{session_name}/start',
        f'/api/{session_name}/start',
        f'/api/sessions/{session_name}/connect',
    ], base_url_override=(base_url or '').strip() or None)
    if not result.get('ok'):
        return jsonify({'ok': False, 'error': result.get('error', 'Gagal start session Si Dobe')}), 400
    return jsonify({'ok': True, 'path': result.get('path'), 'data': result.get('data')})

@api_bp.route('/notifications/sidobe/sessions/<string:session_name>/stop', methods=['POST'])
@jwt_required()
def notification_sidobe_stop_session_api(session_name):
    user = _api_request_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    if not user or not user.role.sidobe_enabled:
        return jsonify({"error": "Unauthorized"}), 403
    from app import _sidobe_request_any, get_sidobe_setting_value
    base_url = (request.get_json(silent=True) or {}).get('base_url') or get_sidobe_setting_value('base_url', '')
    result = _sidobe_request_any('POST', [
        f'/api/sessions/{session_name}/stop',
        f'/api/{session_name}/stop',
        f'/api/sessions/{session_name}/disconnect',
    ], base_url_override=(base_url or '').strip() or None)
    if not result.get('ok'):
        return jsonify({'ok': False, 'error': result.get('error', 'Gagal stop session Si Dobe')}), 400
    return jsonify({'ok': True, 'path': result.get('path'), 'data': result.get('data')})

@api_bp.route('/notifications/sidobe/sessions/<string:session_name>/restart', methods=['POST'])
@jwt_required()
def notification_sidobe_restart_session_api(session_name):
    user = _api_request_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    if not user or not user.role.sidobe_enabled:
        return jsonify({"error": "Unauthorized"}), 403
    from app import _sidobe_request_any, get_sidobe_setting_value
    base_url = (request.get_json(silent=True) or {}).get('base_url') or get_sidobe_setting_value('base_url', '')
    result = _sidobe_request_any('POST', [
        f'/api/sessions/{session_name}/restart',
        f'/api/{session_name}/restart',
        f'/api/sessions/{session_name}/start',
    ], base_url_override=(base_url or '').strip() or None)
    if not result.get('ok'):
        return jsonify({'ok': False, 'error': result.get('error', 'Gagal restart session Si Dobe')}), 400
    return jsonify({'ok': True, 'path': result.get('path'), 'data': result.get('data')})

@api_bp.route('/notifications/sidobe/sessions/<string:session_name>/screenshot', methods=['GET'])
@jwt_required()
def notification_sidobe_session_screenshot_api(session_name):
    user = _api_request_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    if not user or not user.role.sidobe_enabled:
        return jsonify({"error": "Unauthorized"}), 403
    from app import _sidobe_request_any, get_sidobe_setting_value
    base_url = get_sidobe_setting_value('base_url', '').strip() or None
    result = _sidobe_request_any('GET', [
        '/api/screenshot',
        f'/api/{session_name}/screenshot',
        f'/api/sessions/{session_name}/screenshot',
        f'/api/{session_name}/qr',
        f'/api/sessions/{session_name}/qr',
    ], base_url_override=base_url)
    if not result.get('ok'):
        return jsonify({'ok': False, 'error': result.get('error', 'Gagal ambil screenshot/QR Si Dobe')}), 200
    data = result.get('data')
    if isinstance(data, dict):
        data = data.get('screenshot') or data.get('qr') or data.get('image') or data.get('data') or ''
    return jsonify({'ok': True, 'path': result.get('path'), 'data': data})

@api_bp.route('/schedules/<int:id>/send-whatsapp', methods=['POST'])
@jwt_required()
def send_schedule_whatsapp(id):
    """Manually send Si Dobe notification for a specific schedule"""
    from app import send_sidobe
    from models import Schedule
    
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    
    if not (user.role.can_manage_schedule and user.role.sidobe_enabled):
        return jsonify({"error": "Perlu izin kelola jadwal dan Si Dobe"}), 403
    
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
    
    result = send_sidobe(
        whatsapp_text,
        sender_id=user.id,
        title=f"Jadwal: {schedule.subject}",
        classroom_id=schedule.classroom_id,
        category='schedule',
    )
    
    return jsonify(result), (200 if result.get('ok') else 400)

@api_bp.route('/notifications/send-daily-summary', methods=['POST'])
@jwt_required()
def send_daily_summary_on_demand():
    """Send daily schedule summary Si Dobe on demand"""
    from app import send_sidobe, _build_schedule_summary_message
    
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    
    if not (user.role.can_manage_schedule and user.role.sidobe_enabled):
        return jsonify({"error": "Perlu izin kelola jadwal dan Si Dobe"}), 403
    
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
    classroom = _user_classroom(user)
    message = _build_schedule_summary_message(
        target_date,
        classroom_id=classroom.id if classroom else None,
    )
    
    if not message:
        return jsonify({
            "ok": False,
            "error": f"Tidak ada jadwal untuk tanggal {target_date.strftime('%d/%m/%Y')}"
        }), 400
    
    # Append note if provided
    note = data.get('note')
    if note and note.strip():
        message += f"\n\n📝 *Info Tambahan:*\n{note.strip()}"
    
    # Send Si Dobe
    result = send_sidobe(
        message,
        sender_id=user.id,
        title=f"Ringkasan Jadwal {target_date.strftime('%d/%m/%Y')}",
        classroom_id=classroom.id if classroom else None,
        category='schedule',
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

def get_fund_target(as_of=None, classroom_id=None):
    """Calculates cumulative target based on advanced periods, with legacy fallback."""
    today = as_of or datetime.now().date()
    if isinstance(today, datetime):
        today = today.date()

    periods_query = FundPeriod.query.filter_by(is_active=True)
    periods_query = _apply_fund_period_classroom_filter(
        periods_query,
        ClassRoom.query.get(classroom_id) if classroom_id else None,
    )
    periods = periods_query.order_by(FundPeriod.start_date.asc(), FundPeriod.id.asc()).all()
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
@jwt_required(optional=True)
def get_funds_summary():
    user = _api_request_user_or_session(require_api_access=True)
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    classroom = _user_classroom(user)
    try:
        classroom = _requested_fund_classroom_for_user(user)
    except (ValueError, PermissionError, LookupError) as exc:
        return jsonify({"error": str(exc)}), 400

    total_in = _apply_fund_classroom_filter(
        db.session.query(db.func.sum(BatchFund.amount)).filter(BatchFund.type == 'Masuk'),
        classroom,
    ).scalar() or 0
    total_out = _apply_fund_classroom_filter(
        db.session.query(db.func.sum(BatchFund.amount)).filter(BatchFund.type == 'Keluar'),
        classroom,
    ).scalar() or 0
    balance = total_in - total_out
    target_payment = get_fund_target(classroom_id=classroom.id if classroom else None)

    if classroom:
        students = Student.query.filter_by(
            classroom_id=classroom.id
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
@jwt_required(optional=True)
def get_funds_history():
    user = _api_request_user_or_session(require_api_access=True)
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        classroom = _requested_fund_classroom_for_user(user)
    except (ValueError, PermissionError, LookupError) as exc:
        return jsonify({"error": str(exc)}), 400
    history = _apply_fund_classroom_filter(BatchFund.query, classroom).order_by(BatchFund.date.desc()).all()
    return jsonify([{
        "id": f.id,
        "classroom_id": f.classroom_id,
        "classroom_name": f.classroom.name if f.classroom else (_default_classroom().name if _default_classroom() else None),
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
@jwt_required(optional=True)
def get_funds_audit():
    user = _api_request_user_or_session(require_api_access=True)
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        classroom = _requested_fund_classroom_for_user(user)
    except (ValueError, PermissionError, LookupError) as exc:
        return jsonify({"error": str(exc)}), 400

    students_query = Student.query.order_by(Student.full_name)
    if classroom:
        students_query = students_query.filter_by(classroom_id=classroom.id)
    students = students_query.all()
    target_payment = get_fund_target(classroom_id=classroom.id if classroom else None)
    
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

    students_query = Student.query
    classroom = _user_classroom(user)
    requested_classroom_id = request.args.get('classroom_id')
    if requested_classroom_id not in (None, ''):
        try:
            requested_classroom_id = int(requested_classroom_id)
        except Exception:
            return jsonify({"error": "Format classroom_id tidak valid"}), 400

        allowed_ids = _allowed_classroom_ids_for_student_management(user)
        if requested_classroom_id not in allowed_ids:
            return jsonify({"error": "Kelas tidak diizinkan"}), 403
        students_query = students_query.filter_by(classroom_id=requested_classroom_id)
    elif classroom:
        students_query = students_query.filter_by(classroom_id=classroom.id)

    students = students_query.order_by(Student.full_name).all()

    return jsonify([{
        "id": s.id,
        "nim": s.nim,
        "full_name": s.full_name,
        "status": s.status,
        "classroom_id": s.classroom_id,
        "classroom_name": s.classroom.name if s.classroom else None,
        "classroom_batch": s.classroom.batch if s.classroom else None,
        "has_linked_account": bool(s.user),
    } for s in students])


def _get_member_detail_for_requester(request_user, member_id):
    member = Student.query.get_or_404(member_id)

    allowed_ids = _allowed_classroom_ids_for_student_management(request_user)
    if allowed_ids:
        if member.classroom_id not in allowed_ids:
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
        "classroom_id": member.classroom_id,
        "classroom_name": member.classroom.name if member.classroom else None,
        "classroom_batch": member.classroom.batch if member.classroom else None,
        "whatsapp": linked_user.whatsapp if linked_user and linked_user.whatsapp else None,
        "financial": {
            "paid": total_paid,
            "target": target_payment,
            "arrears": arrears,
            "is_settled": arrears == 0
        }
    }


@api_bp.route('/members/<int:member_id>', methods=['GET'])
@api_bp.route('/students/<int:member_id>', methods=['GET'])
@jwt_required()
def get_member_detail(member_id):
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))

    payload = _get_member_detail_for_requester(user, member_id)
    if payload is None:
        return jsonify({"error": "Member tidak ditemukan atau tidak dapat diakses"}), 404

    return jsonify(payload)


@api_bp.route('/students', methods=['POST'])
@jwt_required()
def create_member_api():
    user = User.query.get(int(get_jwt_identity()))
    if not user or not user.role.can_manage_students:
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json(silent=True) or request.form or {}
    nim = (data.get('nim') or '').strip()
    full_name = (data.get('full_name') or '').strip()
    status = normalize_member_status(data.get('status'))
    if not nim or not full_name:
        return jsonify({"error": "NIM dan nama lengkap wajib diisi"}), 400

    if Student.query.filter(db.func.lower(Student.nim) == nim.lower()).first():
        return jsonify({"error": "NIM sudah digunakan"}), 400

    allowed_ids = _allowed_classroom_ids_for_student_management(user)
    fallback_classroom = _user_classroom(user)
    try:
        classroom = _classroom_from_request(data, fallback_classroom, allowed_ids)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404

    if not classroom:
        return jsonify({"error": "Kelas tujuan tidak tersedia"}), 400

    member = Student(
        nim=nim,
        full_name=full_name,
        status=status,
        classroom_id=classroom.id,
    )
    db.session.add(member)
    db.session.commit()

    from app import log_activity
    log_activity("Tambah Member (Mobile)", f"{member.full_name} - {classroom.name}")

    return jsonify({
        "status": "success",
        "id": member.id,
        "nim": member.nim,
        "full_name": member.full_name,
        "status_label": member.status,
        "classroom_id": member.classroom_id,
        "classroom_name": classroom.name,
        "classroom_batch": classroom.batch,
    }), 201


@api_bp.route('/students/<int:member_id>', methods=['PUT', 'DELETE'])
@jwt_required()
def update_member_api(member_id):
    user = User.query.get(int(get_jwt_identity()))
    if not user or not user.role.can_manage_students:
        return jsonify({"error": "Unauthorized"}), 403

    member = Student.query.get_or_404(member_id)
    allowed_ids = _allowed_classroom_ids_for_student_management(user)
    if allowed_ids and member.classroom_id not in allowed_ids:
        return jsonify({"error": "Member tidak dapat diakses"}), 403

    if request.method == 'DELETE':
        if member.user:
            return jsonify({"error": "Member masih terhubung ke akun login dan tidak bisa dihapus"}), 400
        db.session.delete(member)
        db.session.commit()
        from app import log_activity
        log_activity("Hapus Member (Mobile)", member.full_name)
        return jsonify({"status": "success"})

    data = request.get_json(silent=True) or request.form or {}
    nim = (data.get('nim') or member.nim).strip()
    full_name = (data.get('full_name') or member.full_name).strip()
    status = normalize_member_status(data.get('status'), member.status or 'Aktif')
    if not nim or not full_name:
        return jsonify({"error": "NIM dan nama lengkap wajib diisi"}), 400

    duplicate = Student.query.filter(
        db.func.lower(Student.nim) == nim.lower(),
        Student.id != member.id,
    ).first()
    if duplicate:
        return jsonify({"error": "NIM sudah digunakan"}), 400

    fallback_classroom = member.classroom or _user_classroom(user)
    try:
        classroom = _classroom_from_request(data, fallback_classroom, allowed_ids)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404

    member.nim = nim
    member.full_name = full_name
    member.status = status
    if member.user:
        member.user.status = 'Active' if status == 'Aktif' else 'Inactive'
    if classroom:
        member.classroom_id = classroom.id
        if member.user and getattr(user.role, 'can_move_users_between_classrooms', False):
            member.user.classroom_id = classroom.id
    db.session.commit()

    from app import log_activity
    log_activity("Edit Member (Mobile)", f"{member.full_name} - {classroom.name if classroom else '-'}")

    return jsonify({
        "status": "success",
        "id": member.id,
        "nim": member.nim,
        "full_name": member.full_name,
        "status_label": member.status,
        "classroom_id": member.classroom_id,
        "classroom_name": member.classroom.name if member.classroom else None,
        "classroom_batch": member.classroom.batch if member.classroom else None,
    })


@api_bp.route('/fund-periods', methods=['GET'])
@jwt_required(optional=True)
def get_fund_periods_api():
    user = _api_request_user_or_session(require_api_access=True)
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    if not user.role.can_manage_fund:
        return jsonify({"error": "Unauthorized"}), 403

    try:
        classroom = _requested_fund_classroom_for_user(user)
    except (ValueError, PermissionError, LookupError) as exc:
        return jsonify({"error": str(exc)}), 400

    periods = _apply_fund_period_classroom_filter(FundPeriod.query, classroom).order_by(FundPeriod.start_date.asc(), FundPeriod.id.asc()).all()
    return jsonify([{
        "id": period.id,
        "classroom_id": period.classroom_id,
        "classroom_name": period.classroom.name if period.classroom else (_default_classroom().name if _default_classroom() else None),
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

    try:
        classroom = _requested_fund_classroom_for_user(user, data)
    except (ValueError, PermissionError, LookupError) as exc:
        return jsonify({"error": str(exc)}), 400

    period = FundPeriod(
        classroom_id=classroom.id if classroom else None,
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
    try:
        classroom = _requested_fund_classroom_for_user(user, data)
    except (ValueError, PermissionError, LookupError) as exc:
        return jsonify({"error": str(exc)}), 400
    if not _is_fund_record_in_scope(period.classroom_id, classroom):
        return jsonify({"error": "Periode kas tidak termasuk kelas aktif"}), 403

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
@jwt_required(optional=True)
def get_gallery():
    # User can see published photos, or their own pending photos
    user = _api_request_user_or_session(require_api_access=True)
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    user_id = user.id
    classroom = _user_classroom(user)
    
    if user.role.can_manage_gallery:
        photos_query = GalleryPhoto.query
    else:
        # Published OR owned by user
        photos_query = GalleryPhoto.query.filter((GalleryPhoto.status == 'Published') | (GalleryPhoto.uploaded_by == user_id))

    if classroom:
        photos_query = photos_query.filter(
            (GalleryPhoto.classroom_id == classroom.id) | (GalleryPhoto.classroom_id.is_(None))
        )

    photos = photos_query.order_by(GalleryPhoto.created_at.desc()).all()


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
    user = User.query.get(user_id)
    classroom = _user_classroom(user)
    photo = GalleryPhoto.query.get_or_404(photo_id)
    if classroom and photo.classroom_id not in (classroom.id, None):
        return jsonify({"error": "Not found"}), 404
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
    classroom = _user_classroom(user)
    if classroom and photo.classroom_id not in (classroom.id, None):
        return jsonify({"error": "Not found"}), 404
    
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
    classroom = _user_classroom(user)
    if classroom and photo.classroom_id not in (classroom.id, None):
        return jsonify({"error": "Not found"}), 404
    
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
    classroom = _user_classroom(user)
    
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
        classroom_id=classroom.id if classroom else None,
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
            send_push("Foto Baru di Galeri!", f"{user.full_name} baru saja mengunggah foto baru. Cek sekarang!", classroom_id=photo.classroom_id, category='announcement')
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
    classroom = _user_classroom(user)
    
    # Show notifications that are for "All" or for this specific user
    history_query = NotificationHistory.query.filter(
        ((NotificationHistory.target == 'All') | (NotificationHistory.target == str(user_id))) &
        ((NotificationHistory.title != '') | (NotificationHistory.body != ''))
    )
    if classroom:
        history_query = history_query.filter(
            (NotificationHistory.classroom_id == classroom.id) |
            (NotificationHistory.classroom_id.is_(None))
        )
    history = history_query.order_by(NotificationHistory.sent_at.desc()).limit(30).all()
    
    return jsonify([{
        "id": h.id,
        "title": h.title,
        "body": h.body,
        "channel": h.channel or 'push',
        "category": h.category,
        "delivery_mode": h.delivery_mode,
        "chat_id": h.chat_id,
        "bot_name": h.bot.name if h.bot else None,
        "classroom_name": h.classroom.name if h.classroom else None,
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

    classroom = _user_classroom(user)
    if classroom:
        students = Student.query.filter_by(
            classroom_id=classroom.id
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
            "nim": student.nim,
            "classroom_name": student.classroom.name if student.classroom else None,
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
        if classroom and extra_user.classroom_id not in (classroom.id, None):
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
            "nim": extra_user.student.nim if extra_user.student else None,
            "classroom_name": extra_user.classroom.name if extra_user.classroom else (extra_user.student.classroom.name if extra_user.student and extra_user.student.classroom else None)
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
    
    classroom = _user_classroom(user)

    if target == 'all':
        send_multichannel_notification(
            title,
            body,
            sender_id=user.id,
            allow_whatsapp=True,
            classroom_id=classroom.id if classroom else None,
            category='emergency',
        )
    else:
        target_user_id = None
        if target.startswith('student:'):
            student = Student.query.get(int(target.split(':', 1)[1]))
            if not student:
                return jsonify({"error": "Member tidak ditemukan"}), 404
            if not student.user:
                return jsonify({"error": "Member ini belum memiliki akun aplikasi"}), 400
            if classroom and student.classroom_id != classroom.id:
                return jsonify({"error": "Not found"}), 404
            target_user_id = student.user.id
        elif target.startswith('user:'):
            target_user_id = int(target.split(':', 1)[1])
        else:
            target_user_id = int(target)

        target_user = User.query.get(target_user_id)
        if not target_user:
            return jsonify({"error": "User penerima tidak ditemukan"}), 404
        if classroom and target_user.classroom_id not in (classroom.id, None):
            if not (target_user.student and target_user.student.classroom_id == classroom.id):
                return jsonify({"error": "Not found"}), 404
        if not target_user.fcm_token:
            return jsonify({"error": "Penerima belum memiliki token push aktif"}), 400

        send_push(
            title,
            body,
            user_id=target_user_id,
            sender_id=user.id,
            classroom_id=classroom.id if classroom else None,
            category='emergency',
        )
        
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

    try:
        classroom = _requested_fund_classroom_for_user(user, data)
    except (ValueError, PermissionError, LookupError) as exc:
        return jsonify({"error": str(exc)}), 400

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
    
    student = None
    if student_id_val and str(student_id_val).lower() != 'none':
        student = Student.query.get(int(student_id_val))
        if not student:
            return jsonify({"error": "Member tidak ditemukan"}), 404
        if classroom and student.classroom_id != classroom.id:
            return jsonify({"error": "Member tidak termasuk kelas aktif"}), 403

    fund_classroom = student.classroom if student and student.classroom else classroom

    fund = BatchFund(
        classroom_id=fund_classroom.id if fund_classroom else None,
        description=description, 
        amount=amount, 
        type=type_val, 
        category=category,
        evidence_note=evidence_note,
        recorded_by=user.username,
        date=date_val,
        student_id=student.id if student else None,
        tags=tags
    )
    db.session.add(fund)
    db.session.commit()

    from app import send_push, log_activity, auto_recalculate_points
    # Notify student if it's a payment
    if fund.type == 'Masuk' and fund.student_id:
        student_user = User.query.filter_by(student_id=fund.student_id).first()
        if student_user:
            send_push("Pembayaran Berhasil!", f"Halo {student_user.full_name}, pembayaran kas Rp {fund.amount:,.0f} telah dikonfirmasi.", user_id=student_user.id, classroom_id=student_user.classroom_id or (student_user.student.classroom_id if student_user.student else None), category='finance')

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
    try:
        classroom = _requested_fund_classroom_for_user(user, data)
    except (ValueError, PermissionError, LookupError) as exc:
        return jsonify({"error": str(exc)}), 400
    if not _is_fund_record_in_scope(fund.classroom_id, classroom):
        return jsonify({"error": "Transaksi tidak termasuk kelas aktif"}), 403
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
    student = Student.query.get(student_id) if student_id else None
    if student and classroom and student.classroom_id != classroom.id:
        return jsonify({"error": "Member tidak termasuk kelas aktif"}), 403

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
    fund.classroom_id = student.classroom_id if student else (fund.classroom_id or (classroom.id if classroom else None))
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
    try:
        classroom = _requested_fund_classroom_for_user(user)
    except (ValueError, PermissionError, LookupError) as exc:
        return jsonify({"error": str(exc)}), 400
    if not _is_fund_record_in_scope(fund.classroom_id, classroom):
        return jsonify({"error": "Transaksi tidak termasuk kelas aktif"}), 403
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
    try:
        classroom = _requested_fund_classroom_for_user(user)
    except (ValueError, PermissionError, LookupError) as exc:
        return jsonify({"error": str(exc)}), 400
    if not _is_fund_record_in_scope(fund.classroom_id, classroom):
        return jsonify({"error": "Transaksi tidak termasuk kelas aktif"}), 403
    duplicated = BatchFund(
        classroom_id=fund.classroom_id or (classroom.id if classroom else None),
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
@jwt_required(optional=True)
def get_assignments():
    user = _api_request_user_or_session(require_api_access=True)
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
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
        
    data = _get_json_payload(required=True)
    title = (data.get('title') or '').strip()
    subject = (data.get('subject') or '').strip()
    description = (data.get('description') or '').strip()
    if not title or not subject:
        return jsonify({"error": "Judul dan mata kuliah wajib diisi"}), 400
    try:
        deadline = _parse_iso_datetime(data.get('deadline'))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    a = Assignment(
        title=title,
        subject=subject,
        deadline=deadline,
        description=description
    )
    db.session.add(a)
    db.session.commit()
    
    from app import send_multichannel_notification
    send_multichannel_notification(
        "Tugas Baru!",
        f"Tugas {a.subject}: {a.title}. Deadline: {a.deadline.strftime('%d %b %H:%M')}",
        sender_id=user.id,
        allow_whatsapp=True,
        whatsapp_text=f"Tugas baru\nMata kuliah: {a.subject}\nJudul: {a.title}\nDeadline: {a.deadline.strftime('%d %b %Y %H:%M')}",
        classroom_id=a.classroom_id,
        category='assignment',
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
        data = _get_json_payload(required=True)
        if data.get('title'):
            a.title = data.get('title')
        if data.get('subject'):
            a.subject = data.get('subject')
        if data.get('deadline'):
            try:
                a.deadline = _parse_iso_datetime(data.get('deadline'))
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
        if data.get('description') is not None:
            a.description = data.get('description')
        
        db.session.commit()
        
        from app import send_multichannel_notification
        send_multichannel_notification(
            "Tugas Diperbarui",
            f"Tugas {a.subject}: {a.title} telah diperbarui. Deadline: {a.deadline.strftime('%d %b %H:%M')}",
            sender_id=user.id,
            allow_whatsapp=True,
            whatsapp_text=f"Update tugas\nMata kuliah: {a.subject}\nJudul: {a.title}\nDeadline: {a.deadline.strftime('%d %b %Y %H:%M')}",
            classroom_id=a.classroom_id,
            category='assignment',
        )
        
        return jsonify({"status": "success"})
