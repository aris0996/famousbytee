import json
import os
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy.exc import IntegrityError

from models import GalleryPhoto, db

from .config import is_enabled
from .crypto import FaceDataUnavailable, encrypt_embedding
from .engine import FaceEngineUnavailable, get_engine
from .matcher import rematch_profile
from .models import ClassFaceProfile, FaceMatch, FaceProcessingJob, PhotoFace


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
                process_photo(job.photo_id)
    except Exception:
        app.logger.exception('Face-labeling scheduler failed.')
