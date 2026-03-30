import sys
import os
import logging

# Setup Logging agar pesan error Python muncul di error_log Apache
logging.basicConfig(stream=sys.stderr)

# Konfigurasi Path
venv_path = '/home/famousbytee/venv'
project_path = '/home/famousbytee/public_html/famousbytee'

# 1. Tambahkan site-packages VENV (PENTING!)
# Pastikan folder python3.11 sesuai dengan versi di server Anda
site_packages = os.path.join(venv_path, 'lib', 'python3.11', 'site-packages')
if os.path.exists(site_packages):
    sys.path.insert(0, site_packages)

# 2. Tambahkan Project Path
if project_path not in sys.path:
    sys.path.insert(0, project_path)

# 3. Import Aplikasi
try:
    from app import app as application
except Exception as e:
    # Jika gagal import, error akan tercatat di log Apache
    print(f"Error starting application: {e}")
    raise
