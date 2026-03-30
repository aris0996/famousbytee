import os

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-famousbytee'
    
    # Database Settings
    # SQLite (default): 'sqlite:///campus.db'
    # PostgreSQL: 'postgresql://user:password@localhost/dbname'
    # MySQL/MariaDB: 'mysql+pymysql://user:password@localhost/dbname'
    
    DB_TYPE = os.environ.get('DB_TYPE') or 'mysql'
    DB_USER = os.environ.get('DB_USER') or 'famousbytee'
    DB_PASS = os.environ.get('DB_PASS') or 'aZOh3CXbldMtz67'
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
