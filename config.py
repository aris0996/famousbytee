import os

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-famousbytee'
    
    # Database Settings
    # SQLite (default): 'sqlite:///campus.db'
    # PostgreSQL: 'postgresql://user:password@localhost/dbname'
    # MySQL/MariaDB: 'mysql+pymysql://user:password@localhost/dbname'
    
    DB_TYPE = os.environ.get('DB_TYPE') or 'sqlite'
    
    if DB_TYPE == 'sqlite':
        SQLALCHEMY_DATABASE_URI = 'sqlite:///campus.db'
    elif DB_TYPE == 'postgresql':
        SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'postgresql://postgres:admin@localhost/famous_db'
    elif DB_TYPE == 'mysql':
        SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'mysql+pymysql://root:password@localhost/famous_db'
    else:
        SQLALCHEMY_DATABASE_URI = 'sqlite:///campus.db'
        
    SQLALCHEMY_TRACK_MODIFICATIONS = False
