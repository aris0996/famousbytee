import sys
import os

# Tambahkan project path ke sys.path
project_path = '/home/famousbytee/public_html/famousbytee'
if project_path not in sys.path:
    sys.path.insert(0, project_path)

# Gunakan virtualenv (Python 3.10) dengan mod_wsgi
venv_path = '/home/famousbytee/venv'
activate = os.path.join(venv_path, 'bin', 'activate_this.py')
if os.path.exists(activate):
    with open(activate) as f:
        exec(f.read(), {'__file__': activate})

# Import Flask app
from app import app as application