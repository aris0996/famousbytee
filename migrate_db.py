from app import app
from models import db
from sqlalchemy import text

with app.app_context():
    try:
        # Add fcm_token to user table
        db.session.execute(text("ALTER TABLE user ADD COLUMN fcm_token TEXT AFTER last_login"))
        db.session.commit()
        print("Success: Added fcm_token to user table.")
    except Exception as e:
        print(f"Error adding fcm_token: {e}")

    try:
        # Ensure Assignment table is created
        db.create_all()
        print("Success: Checked all tables (Assignment created if missing).")
    except Exception as e:
        print(f"Error creating tables: {e}")
