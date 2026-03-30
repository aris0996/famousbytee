import sys
import os

# path project
project_path = "/home/famousbytee/public_html/famousbytee"
if project_path not in sys.path:
    sys.path.insert(0, project_path)

# pakai python dari venv secara eksplisit
os.environ['PYTHONHOME'] = '/home/famousbytee/venv'
os.environ['PATH'] = '/home/famousbytee/venv/bin:' + os.environ['PATH']

from app import app as application