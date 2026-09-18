import os

from flask import current_app


def is_enabled():
    configured = current_app.config.get('FACE_LABELING_ENABLED')
    if configured is None:
        configured = os.environ.get('FACE_LABELING_ENABLED', '0')
    return str(configured).strip().lower() in {'1', 'true', 'yes', 'on'}


def model_root():
    return os.environ.get('FACE_LABELING_MODEL_DIR', '').strip()


def encryption_key():
    return os.environ.get('FACE_EMBEDDING_ENCRYPTION_KEY', '').strip()


def _env_float(name, default):
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, value))


REVIEW_THRESHOLD = _env_float('FACE_REVIEW_THRESHOLD', 0.75)
AUTO_LINK_THRESHOLD = _env_float('FACE_AUTO_LINK_THRESHOLD', 0.90)
if AUTO_LINK_THRESHOLD <= REVIEW_THRESHOLD:
    REVIEW_THRESHOLD = 0.75
    AUTO_LINK_THRESHOLD = 0.90
MODEL_NAME = os.environ.get('FACE_LABELING_MODEL', 'buffalo_l').strip() or 'buffalo_l'
