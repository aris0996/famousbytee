from models import db


class ClassFaceProfile(db.Model):
    __tablename__ = 'face_label_profile'

    id = db.Column(db.Integer, primary_key=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey('class_room.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    embedding_ciphertext = db.Column(db.Text, nullable=True)
    consent_status = db.Column(db.String(24), nullable=False, default='pending_consent', index=True)
    consented_at = db.Column(db.DateTime, nullable=True)
    consent_version = db.Column(db.String(32), nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=False, index=True)
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())
    __table_args__ = (
        db.UniqueConstraint('classroom_id', 'user_id', name='uq_face_profile_classroom_user'),
    )


class PhotoFace(db.Model):
    __tablename__ = 'face_label_photo_face'

    id = db.Column(db.Integer, primary_key=True)
    photo_id = db.Column(db.Integer, db.ForeignKey('gallery_photo.id', ondelete='CASCADE'), nullable=False, index=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey('class_room.id'), nullable=True, index=True)
    bbox_json = db.Column(db.Text, nullable=False)
    embedding_ciphertext = db.Column(db.Text, nullable=False)
    quality_score = db.Column(db.Float, nullable=True)
    model_version = db.Column(db.String(80), nullable=True)
    status = db.Column(db.String(24), nullable=False, default='detected', index=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())


class FaceMatch(db.Model):
    __tablename__ = 'face_label_match'

    id = db.Column(db.Integer, primary_key=True)
    photo_face_id = db.Column(db.Integer, db.ForeignKey('face_label_photo_face.id', ondelete='CASCADE'), nullable=False, index=True)
    profile_id = db.Column(db.Integer, db.ForeignKey('face_label_profile.id', ondelete='CASCADE'), nullable=False, index=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey('class_room.id'), nullable=False, index=True)
    confidence = db.Column(db.Float, nullable=False, default=0)
    status = db.Column(db.String(24), nullable=False, default='suggested', index=True)
    reviewed_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())

    __table_args__ = (
        db.UniqueConstraint('photo_face_id', 'profile_id', name='uq_face_match_face_profile'),
    )


class FaceProcessingJob(db.Model):
    __tablename__ = 'face_label_processing_job'

    id = db.Column(db.Integer, primary_key=True)
    photo_id = db.Column(db.Integer, db.ForeignKey('gallery_photo.id', ondelete='CASCADE'), nullable=False, unique=True)
    classroom_id = db.Column(db.Integer, db.ForeignKey('class_room.id'), nullable=True, index=True)
    status = db.Column(db.String(24), nullable=False, default='queued', index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    error_message = db.Column(db.Text, nullable=True)
    locked_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())
