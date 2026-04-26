import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db, User
with app.app_context():
    admin = User.query.filter_by(username='admin').first()
    if admin:
        admin.password = 'admin123'
        db.session.commit()
        print("Admin password has been reset to: admin123")
    else:
        print("Admin user not found.")
