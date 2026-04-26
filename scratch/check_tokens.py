from app import app
from models import User

with app.app_context():
    users_with_token = User.query.filter(User.fcm_token.isnot(None)).all()
    print(f"Total users: {User.query.count()}")
    print(f"Users with FCM token: {len(users_with_token)}")
    for u in users_with_token:
        print(f"User: {u.username}, Token prefix: {u.fcm_token[:20]}...")
