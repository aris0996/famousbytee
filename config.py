import os
import secrets
import hashlib
import tempfile
from pathlib import Path
from dotenv import load_dotenv
from urllib.parse import quote_plus


load_dotenv(Path(__file__).resolve().parent / '.env')


def _runtime_secret(name):
    value = os.environ.get(name)
    if value:
        return value

    project_dir = Path(__file__).resolve().parent
    project_hash = hashlib.sha256(str(project_dir).encode()).hexdigest()[:12]
    candidate_dirs = (
        project_dir / 'instance',
        Path(tempfile.gettempdir()) / f'famousbytee-{project_hash}',
    )

    for secret_dir in candidate_dirs:
        try:
            secret_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            secret_file = secret_dir / f'.{name.lower()}'
            try:
                existing = secret_file.read_text(encoding='utf-8').strip()
                if existing:
                    return existing
            except FileNotFoundError:
                pass

            generated = secrets.token_urlsafe(64)
            try:
                descriptor = os.open(
                    secret_file,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                    handle.write(generated)
                return generated
            except FileExistsError:
                existing = secret_file.read_text(encoding='utf-8').strip()
                if existing:
                    return existing
        except (OSError, PermissionError):
            continue

    raise RuntimeError(
        f'{name} tidak disetel dan tidak ada direktori runtime yang writable.'
    )

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
    
    DB_TYPE = (os.environ.get('DB_TYPE') or 'mariadb').strip().lower()
    DB_USER = os.environ.get('DB_USER') or 'famousbytee'
    DB_PASS = os.environ.get('DB_PASS') or ''
    DB_HOST = os.environ.get('DB_HOST') or 'localhost'
    DB_PORT = os.environ.get('DB_PORT') or '3306'
    DB_NAME = os.environ.get('DB_NAME') or 'famousbytee'
    DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
    
    if DATABASE_URL:
        SQLALCHEMY_DATABASE_URI = DATABASE_URL
    elif DB_TYPE == 'sqlite':
        SQLALCHEMY_DATABASE_URI = 'sqlite:///campus.db'
    elif DB_TYPE == 'mysql' or DB_TYPE == 'mariadb':
        SQLALCHEMY_DATABASE_URI = (
            f'mysql+pymysql://{quote_plus(DB_USER)}:{quote_plus(DB_PASS)}'
            f'@{DB_HOST}:{DB_PORT}/{quote_plus(DB_NAME)}'
        )
    elif DB_TYPE == 'postgresql':
        SQLALCHEMY_DATABASE_URI = (
            f'postgresql://{quote_plus(DB_USER)}:{quote_plus(DB_PASS)}'
            f'@{DB_HOST}:5432/{quote_plus(DB_NAME)}'
        )
    else:
        SQLALCHEMY_DATABASE_URI = 'sqlite:///campus.db'
        
    SQLALCHEMY_TRACK_MODIFICATIONS = False
