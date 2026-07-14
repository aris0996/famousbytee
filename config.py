import os
from pathlib import Path
from dotenv import load_dotenv
from urllib.parse import quote_plus


load_dotenv(Path(__file__).resolve().parent / '.env')


def _required_env(name):
    value = os.environ.get(name, '').strip()
    if not value:
        raise RuntimeError(f'{name} wajib disetel di file .env atau environment server.')
    return value


def _env_flag(name, default='1'):
    value = os.environ.get(name, default).strip().lower()
    return value not in {'0', 'false', 'no', 'off'}

class Config:
    SECRET_KEY = _required_env('SECRET_KEY')
    JWT_SECRET_KEY = _required_env('JWT_SECRET_KEY')
    SESSION_COOKIE_SECURE = _env_flag('SESSION_COOKIE_SECURE', '1')
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    REMEMBER_COOKIE_SECURE = _env_flag('REMEMBER_COOKIE_SECURE', '1')
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = 'Lax'
    MAX_CONTENT_LENGTH = int(os.environ.get('MAX_CONTENT_LENGTH', 16 * 1024 * 1024))
    
    # Database Settings
    # SQLite (default): 'sqlite:///campus.db'
    # PostgreSQL: 'postgresql://user:password@localhost/dbname'
    # MySQL/MariaDB: 'mysql+pymysql://user:password@localhost/dbname'
    
    DB_TYPE = 'mariadb'
    DB_USER = os.environ.get('DB_USER') or 'famousbytee'
    DB_PASS = _required_env('DB_PASS')
    DB_HOST = os.environ.get('DB_HOST') or 'localhost'
    DB_PORT = os.environ.get('DB_PORT') or '3306'
    DB_NAME = os.environ.get('DB_NAME') or 'famousbytee'
    DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
    
    if DATABASE_URL:
        SQLALCHEMY_DATABASE_URI = DATABASE_URL
    else:
        SQLALCHEMY_DATABASE_URI = (
            f'mysql+pymysql://{quote_plus(DB_USER)}:{quote_plus(DB_PASS)}'
            f'@{DB_HOST}:{DB_PORT}/{quote_plus(DB_NAME)}'
        )
        
    SQLALCHEMY_TRACK_MODIFICATIONS = False
