import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy.exc import IntegrityError

from models import GalleryPhoto, db

from .config import is_enabled
from .crypto import FaceDataUnavailable, encrypt_embedding
from .engine import FaceEngineUnavailable, get_engine
from .matcher import rematch_profile
from .models import ClassFaceProfile, FaceMatch, FaceProcessingJob, PhotoFace


def _worker_python():
    configured = os.environ.get('FACE_LABELING_WORKER_PYTHON', '').strip()
    if configured:
        return configured
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    project_virtualenv_python = os.path.join(project_root, 'venv', 'bin', 'python')
    if os.path.exists(project_virtualenv_python):
        return project_virtualenv_python
    virtualenv_python = os.path.join(sys.prefix, 'bin', 'python')
    if os.path.exists(virtualenv_python):
        return virtualenv_python
    return sys.executable


def spawn_photo_worker(photo_id, force=False):
    """Claim a job and run native face detection outside Apache/mod_wsgi."""
    job = FaceProcessingJob.query.filter_by(photo_id=photo_id).first()
    if not job:
        return None

    now = datetime.utcnow()
    if job.status == 'processing':
        stale = job.locked_at and (now - job.locked_at) >= timedelta(minutes=5)
        if not force or not stale:
            return None
    elif job.status not in {'queued', 'failed', 'completed'}:
        return None

    job.status = 'processing'
    job.locked_at = now
    job.error_message = None
    db.session.commit()

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    worker_log_path = os.path.join(project_root, 'logs', 'face-worker.log')
    worker_env = os.environ.copy()
    worker_env['FAMOUSBYTEE_DISABLE_SCHEDULER'] = '1'
    command = [
        _worker_python(),
        '-m',
        'plugins.face_labeling.worker',
        '--photo-id',
        str(photo_id),
    ]
    try:
        with open(worker_log_path, 'ab') as worker_log:
            subprocess.Popen(
                command,
                cwd=project_root,
                env=worker_env,
                stdout=worker_log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
            )
    except Exception as exc:
        job.status = 'failed'
        job.error_message = 'Worker deteksi tidak dapat dijalankan.'
        job.locked_at = None
        db.session.commit()
        current_app.logger.exception('Could not start face worker for photo %s.', photo_id)
        return None
    return job


def reset_photo_scope(photo_id, classroom_id):
    """Invalidate detections when a gallery photo changes classroom scope."""
    photo_faces = PhotoFace.query.filter_by(photo_id=photo_id).all()
    face_ids = [face.id for face in photo_faces]
    if face_ids:
        FaceMatch.query.filter(FaceMatch.photo_face_id.in_(face_ids)).delete(
            synchronize_session=False,
        )
        PhotoFace.query.filter(PhotoFace.id.in_(face_ids)).delete(
            synchronize_session=False,
        )

    job = FaceProcessingJob.query.filter_by(photo_id=photo_id).first()
    if not job:
        job = FaceProcessingJob(photo_id=photo_id)
        db.session.add(job)
    job.classroom_id = classroom_id
    job.status = 'queued'
    job.error_message = None
    job.locked_at = None
    return job


def enqueue_photo(photo_id):
    if not is_enabled():
        return None
    photo = GalleryPhoto.query.get(photo_id)
    if not photo:
        return None
    job = FaceProcessingJob.query.filter_by(photo_id=photo.id).first()
    if not job:
        job = FaceProcessingJob(photo_id=photo.id, classroom_id=photo.classroom_id, status='queued')
        db.session.add(job)
    elif job.status in {'failed', 'completed'}:
        job.status = 'queued'
        job.error_message = None
        job.locked_at = None
    job.classroom_id = photo.classroom_id
    try:
        db.session.commit()
    except IntegrityError:
        # Two public requests can enqueue the same photo at the same time.
        # The unique photo_id constraint remains the source of truth.
        db.session.rollback()
        job = FaceProcessingJob.query.filter_by(photo_id=photo.id).first()
        if not job:
            raise
    return job


def process_photo(photo_id):
    job = FaceProcessingJob.query.filter_by(photo_id=photo_id).first()
    photo = GalleryPhoto.query.get(photo_id)
    if not job or not photo:
        return {'ok': False, 'status': 'missing'}
    job.status = 'processing'
    job.attempts = (job.attempts or 0) + 1
    job.locked_at = datetime.utcnow()
    db.session.commit()
    try:
        path = os.path.join(current_app.config['UPLOAD_FOLDER'], 'gallery', photo.filename)
        detections = get_engine().detect(path)
        old_faces = PhotoFace.query.filter_by(photo_id=photo.id).all()
        old_ids = [face.id for face in old_faces]
        if old_ids:
            FaceMatch.query.filter(FaceMatch.photo_face_id.in_(old_ids)).delete(synchronize_session=False)
        PhotoFace.query.filter_by(photo_id=photo.id).delete(synchronize_session=False)
        for detection in detections:
            db.session.add(PhotoFace(
                photo_id=photo.id,
                classroom_id=photo.classroom_id,
                bbox_json=json.dumps(detection['bbox'], separators=(',', ':')),
                embedding_ciphertext=encrypt_embedding(detection['embedding']),
                quality_score=detection.get('quality_score'),
                model_version='insightface:' + os.environ.get('FACE_LABELING_MODEL', 'buffalo_l'),
            ))
        job.status = 'completed'
        job.error_message = None
        job.locked_at = None
        db.session.commit()
        if photo.classroom_id:
            active_profiles = ClassFaceProfile.query.filter_by(
                classroom_id=photo.classroom_id,
                consent_status='approved',
                active=True,
            ).all()
            for profile in active_profiles:
                try:
                    rematch_profile(profile)
                except FaceDataUnavailable:
                    current_app.logger.warning(
                        'Skipping face rematch for invalid profile %s.',
                        profile.id,
                    )
        return {'ok': True, 'status': 'completed', 'faces': len(detections)}
    except FaceEngineUnavailable as exc:
        db.session.rollback()
        job = FaceProcessingJob.query.filter_by(photo_id=photo_id).first()
        job.status = 'failed'
        job.error_message = str(exc)
        job.locked_at = None
        db.session.commit()
        return {'ok': False, 'status': 'failed', 'error': str(exc)}
    except Exception as exc:
        current_app.logger.exception('Face-labeling processing failed for photo %s', photo_id)
        db.session.rollback()
        job = FaceProcessingJob.query.filter_by(photo_id=photo_id).first()
        job.status = 'failed'
        job.error_message = 'Pemrosesan wajah gagal.'
        job.locked_at = None
        db.session.commit()
        return {'ok': False, 'status': 'failed', 'error': str(exc)}


def run_scheduled_jobs(app):
    if not app:
        return
    try:
        with app.app_context():
            if not is_enabled():
                return
            stale_before = datetime.utcnow() - timedelta(minutes=5)
            stale_jobs = FaceProcessingJob.query.filter(
                FaceProcessingJob.status == 'processing',
                FaceProcessingJob.locked_at.isnot(None),
                FaceProcessingJob.locked_at < stale_before,
            ).all()
            for stale_job in stale_jobs:
                stale_job.status = 'queued'
                stale_job.error_message = 'Worker sebelumnya berhenti sebelum selesai.'
                stale_job.locked_at = None
            if stale_jobs:
                db.session.commit()

            public_photos = GalleryPhoto.query.filter_by(is_public=True, status='Published').order_by(GalleryPhoto.created_at.desc()).limit(10).all()
            for photo in public_photos:
                job = FaceProcessingJob.query.filter_by(photo_id=photo.id).first()
                if not job or job.status in {'failed'}:
                    enqueue_photo(photo.id)
            job = FaceProcessingJob.query.join(
                GalleryPhoto,
                GalleryPhoto.id == FaceProcessingJob.photo_id,
            ).filter(
                FaceProcessingJob.status == 'queued',
                GalleryPhoto.is_public.is_(True),
                GalleryPhoto.status == 'Published',
            ).order_by(FaceProcessingJob.created_at.asc()).first()
            if job:
                spawn_photo_worker(job.photo_id)
    except Exception:
        app.logger.exception('Face-labeling scheduler failed.')
