import sys
import os

# Menentukan path venv dan project
venv_path = '/home/famousbytee/venv'
project_path = "/home/famousbytee/public_html/famousbytee"

# Tambahkan site-packages venv ke sys.path
site_packages = os.path.join(venv_path, 'lib', 'python3.11', 'site-packages')
if site_packages not in sys.path:
    sys.path.insert(0, site_packages)

# Tambahkan path project ke sys.path
if project_path not in sys.path:
    sys.path.insert(0, project_path)

# Debug (opsional)
print("PYTHON PATH:", sys.path)

from app import app as application
