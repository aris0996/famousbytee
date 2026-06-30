import os
import secrets
from pathlib import Path


def _runtime_secret(name):
    value = os.environ.get(name)
    if value:
        return value
    instance_dir = Path(__file__).resolve().parent / 'instance'
    instance_dir.mkdir(parents=True, exist_ok=True)
    secret_file = instance_dir / f'.{name.lower()}'
    if secret_file.exists():
        return secret_file.read_text(encoding='utf-8').strip()
    value = secrets.token_urlsafe(64)
    secret_file.write_text(value, encoding='utf-8')
    try:
        secret_file.chmod(0o600)
    except OSError:
        pass
    return value

class Config:
    SECRET_KEY = _runtime_secret('SECRET_KEY')
    JWT_SECRET_KEY = _runtime_secret('JWT_SECRET_KEY')
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    REMEMBER_COOKIE_SECURE = True
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = 'Lax'
    MAX_CONTENT_LENGTH = int(os.environ.get('MAX_CONTENT_LENGTH', 16 * 1024 * 1024))
    
    # Database Settings
    # SQLite (default): 'sqlite:///campus.db'
    # PostgreSQL: 'postgresql://user:password@localhost/dbname'
    # MySQL/MariaDB: 'mysql+pymysql://user:password@localhost/dbname'
    
    DB_TYPE = os.environ.get('DB_TYPE') or 'sqlite'
    DB_USER = os.environ.get('DB_USER') or 'famousbytee'
    DB_PASS = os.environ.get('DB_PASS') or ''
    DB_HOST = os.environ.get('DB_HOST') or 'localhost'
    DB_PORT = os.environ.get('DB_PORT') or '3306'
    DB_NAME = os.environ.get('DB_NAME') or 'famousbytee'
    
    if DB_TYPE == 'sqlite':
        SQLALCHEMY_DATABASE_URI = 'sqlite:///campus.db'
    elif DB_TYPE == 'mysql' or DB_TYPE == 'mariadb':
        # Compatible with both MySQL and MariaDB
        SQLALCHEMY_DATABASE_URI = f'mysql+pymysql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}'
    elif DB_TYPE == 'postgresql':
        SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or f'postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:5432/{DB_NAME}'
    else:
        SQLALCHEMY_DATABASE_URI = 'sqlite:///campus.db'
        
    SQLALCHEMY_TRACK_MODIFICATIONS = False
