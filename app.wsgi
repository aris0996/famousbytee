import sys
import os

# 🔥 PAKSA AKTIFKAN VENV
activate_this = '/home/famousbytee/venv/bin/activate_this.py'
with open(activate_this) as f:
    exec(f.read(), {'__file__': activate_this})

# path project
project_path = "/home/famousbytee/public_html/famousbytee"
if project_path not in sys.path:
    sys.path.insert(0, project_path)

# debug (optional, bisa hapus nanti)
print("PYTHON USED:", sys.executable)

from app import app as application
