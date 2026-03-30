import sys
import os
import logging

# 🚀 PAKSA SEMUA OUTPUT KE ERROR LOG (Mengatasi "Truncated headers")
sys.stdout = sys.stderr

# Setup Logging
logging.basicConfig(stream=sys.stderr, level=logging.DEBUG)

# Konfigurasi Path
venv_path = '/home/famousbytee/public_html/venv'
project_path = '/home/famousbytee/public_html/famousbytee'

# 1. Tambahkan site-packages VENV secara dinamis
# Skrip ini akan otomatis mencari folder python3.x yang tersedia
lib_path = os.path.join(venv_path, 'lib')
if os.path.exists(lib_path):
    for folder in os.listdir(lib_path):
        if folder.startswith('python3.'):
            site_packages = os.path.join(lib_path, folder, 'site-packages')
            if site_packages not in sys.path:
                sys.path.insert(0, site_packages)
                break

# 2. Tambahkan Project Path
if project_path not in sys.path:
    sys.path.insert(0, project_path)

# 3. Import Aplikasi dengan penanganan error ketat
try:
    from app import app as application
except Exception:
    logging.exception("FATAL ERROR: Aplikasi gagal dimuat!")
    raise
