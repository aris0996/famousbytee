from datetime import datetime

from flask import abort, current_app
from flask_login import current_user
from sqlalchemy import or_

from models import ActivityLog, ClassRoom, GalleryPhoto, Student, User, db
from .config import is_enabled


def require_enabled():
    if not is_enabled():
        abort(404)


def require_superadmin():
    require_enabled()
    if not current_user.is_authenticated or not getattr(current_user.role, 'can_manage_roles', False):
        abort(403)


def allowed_classroom(classroom_id=None):
    require_superadmin()
    if not classroom_id:
        return None
    try:
        classroom_id = int(classroom_id)
    except (TypeError, ValueError):
        abort(400, description='Kelas tidak valid.')
    classroom = ClassRoom.query.get_or_404(classroom_id)
    return classroom


def get_scoped_photo(photo_id, allow_unassigned_public=False):
    photo = GalleryPhoto.query.get_or_404(photo_id)
    if photo.classroom_id is None and allow_unassigned_public and photo.is_public and photo.status == 'Published':
        return photo
    if not photo.classroom_id:
        abort(403, description='Foto belum memiliki kelas.')
    require_superadmin()
    return photo


def ensure_photo_class(photo, classroom_id=None):
    if not photo.classroom_id:
        abort(403, description='Foto harus ditautkan ke kelas sebelum diberi label.')
    if classroom_id is not None and photo.classroom_id != int(classroom_id):
        abort(403, description='Foto berada di kelas yang berbeda.')
    return photo.classroom_id


def classroom_members(classroom_id):
    return User.query.filter(
        User.status != 'Inactive',
        or_(
            User.classroom_id == classroom_id,
            User.student.has(Student.classroom_id == classroom_id),
        ),
    ).order_by(User.full_name.asc(), User.username.asc()).all()


def member_in_class(user, classroom_id):
    if not user or not classroom_id:
        return False
    return bool(
        user.classroom_id == classroom_id
        or (user.student and user.student.classroom_id == classroom_id)
    )


def audit(action, details, classroom_id=None):
    if not current_user.is_authenticated:
        return
    db.session.add(ActivityLog(
        user_id=current_user.id,
        action=action,
        details=f'[{current_user.role.name}] {details}',
        classroom_id=classroom_id,
        timestamp=datetime.utcnow(),
    ))
