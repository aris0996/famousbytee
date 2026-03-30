import sys
import os

# Menambahkan direktori proyek ke sys.path
sys.path.insert(0, os.path.dirname(__file__))

# Import app dari app.py
from app import app as application
