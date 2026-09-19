"""JWT API for the member-facing face-labeling experience.

This module deliberately stays inside the optional plugin. It never returns
face embeddings or another member's identity to the mobile client.
"""

import json
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from models import ActivityLog, ClassRoom, GalleryPhoto, User, db

from .config import is_enabled
from .crypto import FaceDataUnavailable, decrypt_embedding
from .matcher import rematch_profile, set_profile_embedding
from .models import ClassFaceProfile, FaceMatch, FaceProcessingJob, PhotoFace
from .scope import member_in_class


face_labeling_api_bp = Blueprint(
    'face_labeling_api',
    __name__,
    url_prefix='/api/face-labeling',
)

APPROVED_MATCH_STATUSES = {'approved', 'auto_linked'}


def _jwt_user():
    """Resolve only an API JWT user; Flask sessions are intentionally ignored."""
    identity = get_jwt_identity()
    if not identity:
        return None
    try:
        user_id = int(identity)
    except (TypeError, ValueError):
        return None
    user = User.query.get(user_id)
    if not user or not getattr(user.role, 'can_use_api', False):
        return None
    return user


def _classroom_for_user(user, raw_classroom_id=None):
    """Return a class explicitly allowed for the authenticated user."""
    primary = user.classroom_id or (
        user.student.classroom_id if user.student else None
    )
    allowed_ids = _member_class_ids(user)

    if raw_classroom_id in (None, ''):
        return ClassRoom.query.get(primary) if primary else None
    try:
        classroom_id = int(raw_classroom_id)
    except (TypeError, ValueError):
        return 'invalid'
    if classroom_id not in allowed_ids:
        return 'forbidden'
    return ClassRoom.query.get(classroom_id)


def _member_class_ids(user):
    """Face results are member-scoped even when the role can switch classes."""
    ids = set()
    if user.classroom_id:
        ids.add(user.classroom_id)
    if user.student and user.student.classroom_id:
        ids.add(user.student.classroom_id)
    return ids


def _auth_error(user):
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
    if not is_enabled():
        return jsonify({'error': 'Fitur wajah sedang dinonaktifkan'}), 404
    return None


def _bbox(face):
    try:
        value = json.loads(face.bbox_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    keys = ('left', 'top', 'right', 'bottom')
    if any(key not in value for key in keys):
        return None
    try:
        return {key: max(0.0, min(1.0, float(value[key]))) for key in keys}
    except (TypeError, ValueError):
        return None


def _audit(user, action, details, classroom_id=None):
    role_name = user.role.name if user.role else 'Anggota'
    db.session.add(ActivityLog(
        user_id=user.id,
        action=action,
        details=f'[{role_name}] {details}',
        classroom_id=classroom_id,
        timestamp=datetime.utcnow(),
    ))


def _profile_payload(profile):
    classroom = ClassRoom.query.get(profile.classroom_id)
    return {
        'id': profile.id,
        'classroom_id': profile.classroom_id,
        'classroom_name': classroom.name if classroom else None,
        'classroom_batch': classroom.batch if classroom else None,
        'consent_status': profile.consent_status,
        'active': bool(profile.active),
        'consented_at': profile.consented_at.isoformat() if profile.consented_at else None,
    }


def _job_payload(job, face_count=None):
    return {
        'status': job.status if job else 'not_requested',
        'face_count': face_count if job and job.status == 'completed' else None,
    }


def _face_metadata(photos, user):
    """Bulk-load safe face metadata for a gallery page."""
    if not photos:
        return {}
    if not is_enabled():
        return {photo.id: {'enabled': False} for photo in photos}

    photo_ids = [photo.id for photo in photos]
    jobs = FaceProcessingJob.query.filter(FaceProcessingJob.photo_id.in_(photo_ids)).all()
    job_map = {job.photo_id: job for job in jobs}
    completed_ids = {
        photo.id for photo in photos
        if job_map.get(photo.id) and job_map[photo.id].status == 'completed'
    }
    faces = PhotoFace.query.filter(
        PhotoFace.photo_id.in_(completed_ids or {-1}),
    ).order_by(PhotoFace.id.asc()).all()
    faces_by_photo = {}
    for face in faces:
        faces_by_photo.setdefault(face.photo_id, []).append(face)

    face_ids = [face.id for face in faces]
    own_matches = FaceMatch.query.join(
        ClassFaceProfile,
        ClassFaceProfile.id == FaceMatch.profile_id,
    ).join(
        PhotoFace,
        PhotoFace.id == FaceMatch.photo_face_id,
    ).filter(
        FaceMatch.photo_face_id.in_(face_ids or {-1}),
        FaceMatch.status.in_(APPROVED_MATCH_STATUSES),
        ClassFaceProfile.user_id == user.id,
        FaceMatch.classroom_id == ClassFaceProfile.classroom_id,
        ClassFaceProfile.classroom_id == PhotoFace.classroom_id,
    ).all()
    own_face_ids = {match.photo_face_id for match in own_matches}

    payload = {}
    for photo in photos:
        photo_faces = faces_by_photo.get(photo.id, [])
        member_view = bool(
            photo.classroom_id
            and photo.status == 'Published'
            and member_in_class(user, photo.classroom_id)
        )
        face_rows = []
        if member_view and job_map.get(photo.id) and job_map[photo.id].status == 'completed':
            for face in photo_faces:
                bbox = _bbox(face)
                if bbox:
                    row = {'bbox': bbox}
                    if face.id in own_face_ids:
                        row['label'] = 'Anda'
                    face_rows.append(row)
        payload[photo.id] = {
            'enabled': True,
            **_job_payload(job_map.get(photo.id), len(photo_faces)),
            'faces': face_rows,
        }
    return payload


def face_metadata_for_photos(photos, user):
    """Public integration used by the existing gallery API."""
    return _face_metadata(photos, user)


def _photo_payload(photo, face_data=None, match_status=None, face_bbox=None):
    return {
        'id': photo.id,
        'filename': photo.filename,
        'thumbnail': photo.thumbnail,
        'caption': photo.caption,
        'tags': photo.tags,
        'status': photo.status,
        'is_public': bool(photo.is_public),
        'classroom_id': photo.classroom_id,
        'uploaded_by': (photo.user.full_name if photo.user and photo.user.full_name else 'System'),
        'created_at': photo.created_at.isoformat(),
        'face': face_data or {
            'enabled': is_enabled(),
            'status': 'not_requested',
            'face_count': None,
            'faces': [],
        },
        **({'match_status': match_status, 'match_bbox': face_bbox} if match_status else {}),
    }


@face_labeling_api_bp.route('/status', methods=['GET'])
@jwt_required()
def status():
    user = _jwt_user()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
    if not is_enabled():
        return jsonify({'enabled': False, 'pending_consent_count': 0, 'profiles': []})
    classroom = _classroom_for_user(user, request.args.get('classroom_id'))
    if classroom in {'invalid', 'forbidden'}:
        return jsonify({'error': 'Kelas tidak diizinkan'}), 403
    profiles = ClassFaceProfile.query.filter_by(user_id=user.id).all()
    profiles = [
        profile for profile in profiles
        if profile.classroom_id in _member_class_ids(user)
    ]
    return jsonify({
        'enabled': True,
        'current_classroom_id': classroom.id if classroom else None,
        'pending_consent_count': sum(
            profile.consent_status == 'pending_consent' for profile in profiles
        ),
        'profiles': [_profile_payload(profile) for profile in profiles],
    })


@face_labeling_api_bp.route('/consent', methods=['GET'])
@jwt_required()
def consent_list():
    user = _jwt_user()
    error = _auth_error(user)
    if error:
        return error
    profiles = ClassFaceProfile.query.filter_by(user_id=user.id).all()
    profiles = [
        profile for profile in profiles
        if profile.classroom_id in _member_class_ids(user)
    ]
    return jsonify({'profiles': [_profile_payload(profile) for profile in profiles]})


@face_labeling_api_bp.route('/consent/<int:profile_id>/approve', methods=['POST'])
@jwt_required()
def approve_consent(profile_id):
    user = _jwt_user()
    error = _auth_error(user)
    if error:
        return error
    profile = ClassFaceProfile.query.filter_by(id=profile_id, user_id=user.id).first()
    if not profile or not member_in_class(user, profile.classroom_id):
        return jsonify({'error': 'Profil wajah tidak ditemukan'}), 404
    if profile.consent_status != 'pending_consent':
        return jsonify({'error': 'Profil tidak menunggu consent'}), 409
    pending = FaceMatch.query.filter_by(
        profile_id=profile.id,
        status='pending_consent',
        classroom_id=profile.classroom_id,
    ).order_by(FaceMatch.id.asc()).first()
    if not pending:
        return jsonify({'error': 'Permintaan consent tidak ditemukan'}), 404
    face = PhotoFace.query.get(pending.photo_face_id)
    if not face or face.classroom_id != profile.classroom_id:
        return jsonify({'error': 'Data wajah tidak sesuai kelas'}), 403
    try:
        set_profile_embedding(profile, decrypt_embedding(face.embedding_ciphertext))
    except FaceDataUnavailable as exc:
        db.session.rollback()
        return jsonify({'error': str(exc)}), 503

    profile.consent_version = 'face-labeling-v1'
    pending.status = 'approved'
    _audit(user, 'Setujui Consent Wajah (Mobile)', f'Profil {profile.id}', profile.classroom_id)
    db.session.commit()
    rematch_profile(profile, source_face_id=face.id)
    return jsonify({'ok': True, 'profile': _profile_payload(profile)})


@face_labeling_api_bp.route('/profiles/<int:profile_id>/revoke', methods=['POST'])
@jwt_required()
def revoke_profile(profile_id):
    user = _jwt_user()
    error = _auth_error(user)
    if error:
        return error
    profile = ClassFaceProfile.query.filter_by(id=profile_id, user_id=user.id).first()
    if not profile or not member_in_class(user, profile.classroom_id):
        return jsonify({'error': 'Profil wajah tidak ditemukan'}), 404
    profile.active = False
    profile.consent_status = 'revoked'
    profile.revoked_at = datetime.utcnow()
    profile.embedding_ciphertext = None
    FaceMatch.query.filter_by(profile_id=profile.id).update({'status': 'revoked'})
    _audit(user, 'Cabut Consent Wajah (Mobile)', f'Profil {profile.id}', profile.classroom_id)
    db.session.commit()
    return jsonify({'ok': True, 'profile': _profile_payload(profile)})


@face_labeling_api_bp.route('/my-photos', methods=['GET'])
@jwt_required()
def my_photos():
    user = _jwt_user()
    error = _auth_error(user)
    if error:
        return error
    classroom = _classroom_for_user(user, request.args.get('classroom_id'))
    if classroom in {'invalid', 'forbidden'}:
        return jsonify({'error': 'Kelas tidak diizinkan'}), 403
    query = FaceMatch.query.join(
        ClassFaceProfile,
        ClassFaceProfile.id == FaceMatch.profile_id,
    ).join(
        PhotoFace,
        PhotoFace.id == FaceMatch.photo_face_id,
    ).join(
        GalleryPhoto,
        GalleryPhoto.id == PhotoFace.photo_id,
    ).filter(
        ClassFaceProfile.user_id == user.id,
        FaceMatch.status.in_(APPROVED_MATCH_STATUSES),
        FaceMatch.classroom_id == ClassFaceProfile.classroom_id,
        PhotoFace.classroom_id == ClassFaceProfile.classroom_id,
        GalleryPhoto.classroom_id == ClassFaceProfile.classroom_id,
        GalleryPhoto.status == 'Published',
    )
    member_class_ids = _member_class_ids(user)
    query = query.filter(
        ClassFaceProfile.classroom_id.in_(member_class_ids or {-1}),
    )
    if classroom:
        query = query.filter(FaceMatch.classroom_id == classroom.id)
    rows = query.with_entities(
        FaceMatch,
        PhotoFace,
        GalleryPhoto,
    ).order_by(GalleryPhoto.created_at.desc(), FaceMatch.id.desc()).all()
    photos_by_id = {photo.id: photo for _, _, photo in rows}
    metadata = _face_metadata(list(photos_by_id.values()), user)
    seen = set()
    result = []
    for match, face, photo in rows:
        if photo.id in seen:
            continue
        seen.add(photo.id)
        bbox = _bbox(face)
        result.append(_photo_payload(
            photo,
            face_data=metadata.get(photo.id),
            match_status=match.status,
            face_bbox=bbox,
        ))
    return jsonify({'photos': result})


@face_labeling_api_bp.route('/photos/<int:photo_id>', methods=['GET'])
@jwt_required()
def photo_faces(photo_id):
    user = _jwt_user()
    error = _auth_error(user)
    if error:
        return error
    photo = GalleryPhoto.query.get_or_404(photo_id)
    if photo.status != 'Published':
        return jsonify({'error': 'Foto tidak ditemukan'}), 404
    if photo.classroom_id and not member_in_class(user, photo.classroom_id):
        return jsonify({'error': 'Foto tidak ditemukan'}), 404
    if not photo.classroom_id and not photo.is_public:
        return jsonify({'error': 'Foto tidak ditemukan'}), 404
    return jsonify(_photo_payload(photo, _face_metadata([photo], user).get(photo.id)))
