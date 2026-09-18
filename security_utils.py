from werkzeug.security import check_password_hash, generate_password_hash
import re


_HASH_PREFIXES = ('scrypt:', 'pbkdf2:')


def is_password_hash(value):
    return bool(value) and str(value).startswith(_HASH_PREFIXES)


def hash_password(value):
    return generate_password_hash(str(value), method='scrypt')


def password_validation_error(value):
    password = str(value or '')
    if len(password) < 8:
        return 'Password minimal 8 karakter.'
    if len(password) > 128:
        return 'Password maksimal 128 karakter.'
    if not re.search(r'[A-Za-z]', password) or not re.search(r'\d', password):
        return 'Password harus mengandung huruf dan angka.'
    return None


def verify_password(stored_value, candidate):
    stored = str(stored_value or '')
    if not stored:
        return False, False
    if is_password_hash(stored):
        return check_password_hash(stored, candidate), False
    return hmac_safe_equal(stored, candidate), True


def hmac_safe_equal(left, right):
    import hmac
    return hmac.compare_digest(str(left), str(right))
