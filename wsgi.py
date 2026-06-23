"""
WSGI entry point for Apache/mod_wsgi or Gunicorn deployment.

Apache VirtualHost example (Virtualmin):
    <VirtualHost *:80>
        ServerName yourdomain.com
        WSGIDaemonProcess famousbytee python-path=/path/to/famousbytee python-home=/path/to/venv
        WSGIProcessGroup famousbytee
        WSGIScriptAlias / /path/to/famousbytee/wsgi.py
        <Directory /path/to/famousbytee>
            Require all granted
        </Directory>
        ErrorLog ${APACHE_LOG_DIR}/famousbytee_error.log
        CustomLog ${APACHE_LOG_DIR}/famousbytee_access.log combined
    </VirtualHost>
"""
import sys
import os
import logging

# Add project directory to sys.path
_project_dir = os.path.dirname(os.path.abspath(__file__))
if _project_dir not in sys.path:
    sys.path.insert(0, _project_dir)

# Import app from app.py
from app import app as application

if __name__ == '__main__':
    application.run(debug=True, host='0.0.0.0', port=5000)
