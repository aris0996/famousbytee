import sys
import os

project_path = '/home/famousbytee/public_html/famousbytee'
if project_path not in sys.path:
    sys.path.insert(0, project_path)

from app import app as application