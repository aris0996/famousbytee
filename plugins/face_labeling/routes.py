import json
from datetime import datetime

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from models import ClassRoom, GalleryPhoto, User, db

from .crypto import FaceDataUnavailable, decrypt_embedding
from .jobs import enqueue_photo, spawn_photo_worker
from .matcher import rematch_profile, set_profile_embedding
from .models import ClassFaceProfile, FaceMatch, FaceProcessingJob, PhotoFace
from .scope import (
    allowed_classroom,
    audit,
    classroom_members,
    ensure_photo_class,
    get_scoped_photo,
    member_in_class,
    require_enabled,
    require_superadmin,
)


face_labeling_bp = Blueprint(
    'face_labeling',
    __name__,
    url_prefix='/face-labeling',
    template_folder='templates',
    static_folder='static',
    static_url_path='/static',
)


def _payload():
    return request.get_json(silent=True) or request.form.to_dict(flat=True)


def _face_json(face):
    try:
        return json.loads(face.bbox_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _photo_matches(photo_faces):
    face_rows = []
    for face in photo_faces:
        if not face.classroom_id:
            matches = []
        else:
            matches = FaceMatch.query.join(
                ClassFaceProfile,
                ClassFaceProfile.id == FaceMatch.profile_id,
            ).filter(
                FaceMatch.photo_face_id == face.id,
                FaceMatch.classroom_id == face.classroom_id,
                ClassFaceProfile.classroom_id == face.classroom_id,
            ).order_by(FaceMatch.confidence.desc()).all()
        profile_ids = [match.profile_id for match in matches]
        profiles = ClassFaceProfile.query.filter(ClassFaceProfile.id.in_(profile_ids)).all() if profile_ids else []
        profile_map = {profile.id: profile for profile in profiles}
        user_ids = [profile.user_id for profile in profiles]
        users = {user.id: user for user in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}
        face_rows.append({
            'face': face,
            'bbox': _face_json(face),
            'matches': [
                {
                    'match': match,
                    'profile': profile_map.get(match.profile_id),
                    'user': users.get(profile_map[match.profile_id].user_id) if match.profile_id in profile_map else None,
                }
                for match in matches
            ],
        })
    return face_rows


@face_labeling_bp.route('')
@login_required
def index():
    require_superadmin()
    classrooms = ClassRoom.query.order_by(ClassRoom.name.asc()).all()
    active_classroom = allowed_classroom(request.args.get('classroom_id')) if request.args.get('classroom_id') else None
    photos = []
    if active_classroom:
        photos = GalleryPhoto.query.filter_by(classroom_id=active_classroom.id).order_by(GalleryPhoto.created_at.desc()).all()
    unassigned_public_count = GalleryPhoto.query.filter_by(
        classroom_id=None,
        is_public=True,
        status='Published',
    ).count()
    return render_template(
        'face_labeling/index.html',
        classrooms=classrooms,
        active_classroom=active_classroom,
        photos=photos,
        unassigned_public_count=unassigned_public_count,
    )


@face_labeling_bp.route('/photos/<int:photo_id>')
@login_required
def photo_detail(photo_id):
    require_superadmin()
    photo = get_scoped_photo(photo_id)
    active_classroom = allowed_classroom(photo.classroom_id)
    faces = PhotoFace.query.filter_by(photo_id=photo.id).order_by(PhotoFace.id.asc()).all()
    job = FaceProcessingJob.query.filter_by(photo_id=photo.id).first()
    members = classroom_members(active_classroom.id)
    return render_template(
        'face_labeling/photo.html',
        photo=photo,
        active_classroom=active_classroom,
        faces=_photo_matches(faces),
        job=job,
        members=members,
    )


@face_labeling_bp.route('/photos/<int:photo_id>/detect', methods=['POST'])
@login_required
def detect_photo(photo_id):
    require_superadmin()
    photo = get_scoped_photo(photo_id)
    ensure_photo_class(photo)
    enqueue_photo(photo.id)
    worker = spawn_photo_worker(photo.id, force=True)
    if worker:
        flash('Deteksi dimasukkan ke antrean. Halaman akan menampilkan hasil setelah worker selesai.', 'success')
    else:
        flash('Deteksi foto sedang diproses atau worker belum tersedia. Coba lagi setelah beberapa saat.', 'warning')
    return redirect(url_for('face_labeling.photo_detail', photo_id=photo.id))


@face_labeling_bp.route('/faces/<int:face_id>/link', methods=['POST'])
@login_required
def link_face(face_id):
    require_superadmin()
    face = PhotoFace.query.get(face_id)
    if not face:
        # Re-detection replaces face rows, so an already-open page can submit a
        # valid-looking but stale face_id. Never silently map it to another face.
        raw_photo_id = request.form.get('photo_id')
        try:
            stale_photo_id = int(raw_photo_id)
        except (TypeError, ValueError):
            stale_photo_id = None
        stale_photo = GalleryPhoto.query.filter_by(id=stale_photo_id).first() if stale_photo_id else None
        if stale_photo and stale_photo.classroom_id:
            flash('Hasil deteksi berubah. Halaman foto sudah dimuat ulang; pilih wajah lalu tautkan kembali.', 'warning')
            return redirect(url_for('face_labeling.photo_detail', photo_id=stale_photo.id))
        abort(404, description='Data wajah tidak ditemukan. Muat ulang halaman foto dan coba lagi.')
    photo = get_scoped_photo(face.photo_id)
    classroom_id = ensure_photo_class(photo)
    if face.classroom_id != classroom_id:
        abort(403, description='Scope wajah tidak sesuai dengan kelas foto.')
    data = _payload()
    try:
        user_id = int(data.get('user_id', 0))
    except (TypeError, ValueError):
        abort(400, description='Akun tidak valid.')
    user = User.query.get(user_id)
    if not user:
        flash('Akun kelas sudah berubah. Muat ulang halaman foto lalu pilih akun yang tersedia.', 'warning')
        return redirect(url_for('face_labeling.photo_detail', photo_id=photo.id))
    if not member_in_class(user, classroom_id):
        abort(403, description='Akun bukan anggota kelas foto.')

    profile = ClassFaceProfile.query.filter_by(classroom_id=classroom_id, user_id=user.id).first()
    if not profile:
        profile = ClassFaceProfile(
            classroom_id=classroom_id,
            user_id=user.id,
            created_by=current_user.id,
            consent_status='pending_consent',
            active=False,
        )
        db.session.add(profile)
        db.session.flush()

    existing = FaceMatch.query.filter_by(photo_face_id=face.id, profile_id=profile.id).first()
    if not existing:
        existing = FaceMatch(
            photo_face_id=face.id,
            profile_id=profile.id,
            classroom_id=classroom_id,
        )
        db.session.add(existing)
    elif existing.classroom_id != classroom_id:
        abort(403, description='Match wajah memiliki scope kelas yang tidak valid.')
    existing.confidence = 1.0
    if profile.consent_status == 'approved' and profile.active and profile.embedding_ciphertext:
        try:
            set_profile_embedding(profile, decrypt_embedding(face.embedding_ciphertext))
            existing.status = 'approved'
            db.session.commit()
            rematch_profile(profile, source_face_id=face.id)
            status = 'approved'
        except FaceDataUnavailable as exc:
            db.session.rollback()
            abort(503, description=str(exc))
    else:
        existing.status = 'pending_consent'
        profile.consent_status = 'pending_consent'
        profile.active = False
        db.session.commit()
        status = 'pending_consent'
    audit('Label Wajah', f'Foto {photo.id}, wajah {face.id}, akun {user.id}, status {status}', classroom_id)
    db.session.commit()
    if request.is_json:
        return jsonify({'ok': True, 'status': status, 'classroom_id': classroom_id})
    flash('Wajah ditautkan dan menunggu persetujuan anggota.' if status == 'pending_consent' else 'Wajah berhasil ditautkan.', 'success')
    return redirect(url_for('face_labeling.photo_detail', photo_id=photo.id))


@face_labeling_bp.route('/matches/<int:match_id>/approve', methods=['POST'])
@login_required
def approve_match(match_id):
    require_superadmin()
    match = FaceMatch.query.get_or_404(match_id)
    profile = ClassFaceProfile.query.get_or_404(match.profile_id)
    if profile.consent_status != 'approved' or not profile.active:
        abort(403, description='Profil wajah belum mendapat consent.')
    face = PhotoFace.query.get_or_404(match.photo_face_id)
    photo = get_scoped_photo(face.photo_id)
    photo_classroom_id = ensure_photo_class(photo)
    profile_user = User.query.get_or_404(profile.user_id)
    if any(item != profile.classroom_id for item in (
        match.classroom_id,
        face.classroom_id,
        photo_classroom_id,
    )) or not member_in_class(profile_user, profile.classroom_id):
        abort(403)
    match.status = 'approved'
    match.reviewed_by = current_user.id
    match.reviewed_at = datetime.utcnow()
    db.session.commit()
    audit('Approve Kecocokan Wajah', f'Match {match.id}', profile.classroom_id)
    db.session.commit()
    return redirect(url_for('face_labeling.photo_detail', photo_id=photo.id))


@face_labeling_bp.route('/matches/<int:match_id>/reject', methods=['POST'])
@login_required
def reject_match(match_id):
    require_superadmin()
    match = FaceMatch.query.get_or_404(match_id)
    profile = ClassFaceProfile.query.get_or_404(match.profile_id)
    face = PhotoFace.query.get_or_404(match.photo_face_id)
    photo = get_scoped_photo(face.photo_id)
    photo_classroom_id = ensure_photo_class(photo)
    profile_user = User.query.get_or_404(profile.user_id)
    if any(item != profile.classroom_id for item in (
        match.classroom_id,
        face.classroom_id,
        photo_classroom_id,
    )) or not member_in_class(profile_user, profile.classroom_id):
        abort(403)
    match.status = 'rejected'
    match.reviewed_by = current_user.id
    match.reviewed_at = datetime.utcnow()
    db.session.commit()
    audit('Reject Kecocokan Wajah', f'Match {match.id}', profile.classroom_id)
    db.session.commit()
    return redirect(url_for('face_labeling.photo_detail', photo_id=photo.id))


@face_labeling_bp.route('/profiles/<int:profile_id>/revoke', methods=['POST'])
@login_required
def revoke_profile(profile_id):
    require_enabled()
    profile = ClassFaceProfile.query.get_or_404(profile_id)
    if not current_user.role.can_manage_roles and profile.user_id != current_user.id:
        abort(403)
    profile.active = False
    profile.consent_status = 'revoked'
    profile.revoked_at = datetime.utcnow()
    profile.embedding_ciphertext = None
    FaceMatch.query.filter_by(profile_id=profile.id).update({'status': 'revoked'})
    db.session.commit()
    audit('Cabut Consent Wajah', f'Profil {profile.id}', profile.classroom_id)
    db.session.commit()
    if current_user.role.can_manage_roles:
        return redirect(url_for('face_labeling.photo_detail', photo_id=request.args.get('photo_id'))) if request.args.get('photo_id') else redirect(url_for('face_labeling.index', classroom_id=profile.classroom_id))
    return redirect(url_for('face_labeling.consent_page'))


@face_labeling_bp.route('/consent', methods=['GET', 'POST'])
@login_required
def consent_page():
    require_enabled()
    if request.method == 'POST':
        profile_id = request.form.get('profile_id', type=int)
        profile = ClassFaceProfile.query.filter_by(id=profile_id, user_id=current_user.id).first_or_404()
        if not member_in_class(current_user, profile.classroom_id):
            abort(403)
        pending = FaceMatch.query.filter_by(profile_id=profile.id, status='pending_consent').order_by(FaceMatch.id.asc()).first()
        if not pending:
            flash('Tidak ada permintaan consent yang menunggu.', 'error')
            return redirect(url_for('face_labeling.consent_page'))
        face = PhotoFace.query.get_or_404(pending.photo_face_id)
        if face.classroom_id != profile.classroom_id:
            abort(403)
        profile.consent_status = 'approved'
        profile.consented_at = datetime.utcnow()
        profile.consent_version = 'face-labeling-v1'
        try:
            set_profile_embedding(profile, decrypt_embedding(face.embedding_ciphertext))
            pending.status = 'approved'
            db.session.commit()
            rematch_profile(profile, source_face_id=face.id)
            audit('Setujui Consent Wajah', f'Profil {profile.id}', profile.classroom_id)
            db.session.commit()
            flash('Persetujuan wajah disimpan.', 'success')
        except FaceDataUnavailable as exc:
            db.session.rollback()
            flash(str(exc), 'error')
        return redirect(url_for('face_labeling.consent_page'))
    profiles = [
        profile for profile in ClassFaceProfile.query.filter_by(
            user_id=current_user.id,
            consent_status='pending_consent',
        ).all()
        if member_in_class(current_user, profile.classroom_id)
    ]
    return render_template('face_labeling/consent.html', profiles=profiles)


@face_labeling_bp.route('/public/photos/<int:photo_id>')
def public_photo_faces(photo_id):
    require_enabled()
    photo = GalleryPhoto.query.get_or_404(photo_id)
    if photo.status != 'Published' or not photo.is_public:
        abort(404)
    job = FaceProcessingJob.query.filter_by(photo_id=photo.id).first()
    if not job:
        enqueue_photo(photo.id)
        return jsonify({'ok': True, 'status': 'queued', 'face_count': None})
    if job.status != 'completed':
        return jsonify({'ok': True, 'status': job.status, 'face_count': None})
    faces = PhotoFace.query.filter_by(photo_id=photo.id).all()
    return jsonify({
        'ok': True,
        'status': 'completed',
        'face_count': len(faces),
    })
