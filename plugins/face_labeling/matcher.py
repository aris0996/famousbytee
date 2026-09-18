from datetime import datetime
import math

from models import User, db

from .config import AUTO_LINK_THRESHOLD, REVIEW_THRESHOLD
from .crypto import FaceDataUnavailable, decrypt_embedding, encrypt_embedding
from .models import ClassFaceProfile, FaceMatch, PhotoFace
from .scope import member_in_class


def cosine_similarity(left, right):
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def set_profile_embedding(profile, embedding):
    profile.embedding_ciphertext = encrypt_embedding(embedding)
    profile.active = True
    profile.consent_status = 'approved'
    profile.consented_at = profile.consented_at or datetime.utcnow()


def rematch_profile(profile, source_face_id=None):
    if not profile.active or not profile.embedding_ciphertext:
        return 0
    profile_user = User.query.get(profile.user_id)
    if not member_in_class(profile_user, profile.classroom_id):
        return 0
    profile_embedding = decrypt_embedding(profile.embedding_ciphertext)
    matches = 0
    faces = PhotoFace.query.filter_by(classroom_id=profile.classroom_id).all()
    for face in faces:
        try:
            face_embedding = decrypt_embedding(face.embedding_ciphertext)
        except FaceDataUnavailable:
            # A corrupt face record must not stop matching the remaining class.
            continue
        confidence = cosine_similarity(profile_embedding, face_embedding)
        if confidence < REVIEW_THRESHOLD:
            continue
        status = 'auto_linked' if confidence >= AUTO_LINK_THRESHOLD else 'suggested'
        existing = FaceMatch.query.filter_by(photo_face_id=face.id, profile_id=profile.id).first()
        if not existing:
            existing = FaceMatch(
                photo_face_id=face.id,
                profile_id=profile.id,
                classroom_id=profile.classroom_id,
            )
            db.session.add(existing)
        elif existing.classroom_id != profile.classroom_id:
            # Never preserve a stale cross-class scope on an existing match.
            existing.classroom_id = profile.classroom_id
        existing.confidence = confidence
        if face.id == source_face_id:
            existing.status = 'approved'
            existing.confidence = 1.0
        elif existing.status not in {'approved', 'rejected'}:
            existing.status = status
        matches += 1
    db.session.commit()
    return matches
