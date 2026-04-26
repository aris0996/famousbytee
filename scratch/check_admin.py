import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db, User
with app.app_context():
    admin = User.query.filter_by(username='admin').first()
    if admin:
        print(f"User: {admin.username}")
        print(f"Password: {admin.password}")
        print(f"Role: {admin.role.name}")
        print(f"Status: {admin.status}")
    else:
        print("Admin user not found.")
