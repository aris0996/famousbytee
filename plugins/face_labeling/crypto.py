import base64
import json

from cryptography.fernet import Fernet, InvalidToken

from .config import encryption_key


class FaceDataUnavailable(RuntimeError):
    pass


def _fernet():
    key = encryption_key()
    if not key:
        raise FaceDataUnavailable('FACE_EMBEDDING_ENCRYPTION_KEY belum dikonfigurasi.')
    try:
        return Fernet(key.encode('ascii'))
    except Exception as exc:
        raise FaceDataUnavailable('FACE_EMBEDDING_ENCRYPTION_KEY tidak valid.') from exc


def encrypt_embedding(values):
    payload = json.dumps([float(value) for value in values], separators=(',', ':')).encode('utf-8')
    return _fernet().encrypt(payload).decode('ascii')


def decrypt_embedding(ciphertext):
    try:
        payload = _fernet().decrypt(ciphertext.encode('ascii'))
        return json.loads(payload.decode('utf-8'))
    except (InvalidToken, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise FaceDataUnavailable('Embedding wajah tidak dapat dibaca.') from exc
