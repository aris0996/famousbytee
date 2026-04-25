from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity
from models import db, User, Announcement, Schedule, BatchFund, Student, GalleryPhoto, SystemSetting, ActivityLog
from datetime import datetime, timedelta
import os
from werkzeug.utils import secure_filename

api_bp = Blueprint('api', __name__, url_prefix='/api')

def get_fund_target():
    """Calculates cumulative target based on 1000/day rule (Mon-Fri)"""
    try:
        start_setting = SystemSetting.query.filter_by(key='fund_start_date').first()
        rate_setting = SystemSetting.query.filter_by(key='fund_daily_rate').first()
        
        start_date = datetime.strptime(start_setting.value, '%Y-%m-%d').date() if start_setting else datetime(2024, 3, 30).date()
        daily_rate = int(rate_setting.value) if rate_setting else 1000
    except:
        start_date = datetime(2024, 3, 30).date()
        daily_rate = 1000

    today = datetime.now().date()
    target = 0
    if today >= start_date:
        curr = start_date
        while curr <= today:
            if curr.weekday() < 5: # Monday (0) to Friday (4)
                target += daily_rate
            curr += timedelta(days=1)
    return target

@api_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    if not data:
        return jsonify({"msg": "Missing JSON in request"}), 400
        
    username = data.get('username')
    password = data.get('password')
    
    if not username or not password:
        return jsonify({"msg": "Missing username or password"}), 400
    
    user = User.query.filter_by(username=username).first()
    
    # Check if user exists and password matches
    if user and user.password == password:
        if user.status != 'Active':
            return jsonify({"msg": "Account is disabled"}), 403
            
        # Enforce API Access Control from Role
        if not user.role.can_use_api:
            return jsonify({"msg": "Your role does not have API access permissions"}), 403

        access_token = create_access_token(identity=str(user.id), expires_delta=timedelta(days=7))
        
        return jsonify({
            "access_token": access_token,
            "user": {
                "id": user.id,
                "username": user.username,
                "full_name": user.full_name,
                "role": user.role.name
            }
        }), 200
    
    return jsonify({"msg": "Invalid credentials"}), 401

@api_bp.route('/change-password', methods=['POST'])
@jwt_required()
def change_password():
    data = request.get_json()
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    
    old_password = data.get('old_password')
    new_password = data.get('new_password')
    
    if not old_password or not new_password:
        return jsonify({"msg": "Old and new password required"}), 400
        
    if user.password != old_password:
        return jsonify({"msg": "Old password incorrect"}), 401
        
    user.password = new_password
    db.session.commit()
    
    return jsonify({"msg": "Password changed successfully"})

@api_bp.route('/profile', methods=['GET'])
@jwt_required()
def get_profile():
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    
    if not user:
        return jsonify({"msg": "User not found"}), 404
        
    student_data = None
    if user.student:
        target = get_fund_target()
        total_paid = db.session.query(db.func.sum(BatchFund.amount)).filter(
            BatchFund.student_id == user.student.id, 
            BatchFund.type == 'Masuk'
        ).scalar() or 0
        
        student_data = {
            "nim": user.student.nim,
            "full_name": user.student.full_name,
            "status": user.student.status,
            "financial": {
                "paid": total_paid,
                "target": target,
                "arrears": max(0, target - total_paid)
            }
        }

    return jsonify({
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role.name,
        "student": student_data
    })

@api_bp.route('/announcements', methods=['GET'])
@jwt_required()
def get_announcements():
    announcements = Announcement.query.order_by(Announcement.is_pinned.desc(), Announcement.date_posted.desc()).all()
    return jsonify([{
        "id": a.id,
        "title": a.title,
        "content": a.content,
        "category": a.category,
        "is_pinned": a.is_pinned,
        "is_public": a.is_public,
        "date_posted": a.date_posted.isoformat()
    } for a in announcements])

@api_bp.route('/schedules', methods=['GET'])
@jwt_required()
def get_schedules():
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    
    if user.student and user.student.classroom_id:
        schedules = Schedule.query.filter_by(classroom_id=user.student.classroom_id).all()
    else:
        schedules = Schedule.query.all()
        
    return jsonify([{
        "id": s.id,
        "day": s.day,
        "time_start": s.time_start,
        "time_end": s.time_end,
        "subject": s.subject,
        "lecturer": s.lecturer,
        "room": s.room
    } for s in schedules])

def get_fund_target():
    """Calculates cumulative target based on 1000/day rule (Mon-Fri)"""
    try:
        from app import SystemSetting
        start_setting = SystemSetting.query.filter_by(key='fund_start_date').first()
        rate_setting = SystemSetting.query.filter_by(key='fund_daily_rate').first()
        
        start_date = datetime.strptime(start_setting.value, '%Y-%m-%d').date() if start_setting else datetime(2024, 3, 30).date()
        daily_rate = int(rate_setting.value) if rate_setting else 1000
    except:
        start_date = datetime(2024, 3, 30).date()
        daily_rate = 1000

    today = datetime.now().date()
    target = 0
    if today >= start_date:
        curr = start_date
        while curr <= today:
            if curr.weekday() < 5: # Monday (0) to Friday (4)
                target += daily_rate
            curr += timedelta(days=1)
    return target

@api_bp.route('/funds/summary', methods=['GET'])
@jwt_required()
def get_funds_summary():
    total_in = db.session.query(db.func.sum(BatchFund.amount)).filter(BatchFund.type == 'Masuk').scalar() or 0
    total_out = db.session.query(db.func.sum(BatchFund.amount)).filter(BatchFund.type == 'Keluar').scalar() or 0
    balance = total_in - total_out
    
    return jsonify({
        "total_in": total_in,
        "total_out": total_out,
        "balance": balance
    })

@api_bp.route('/funds/history', methods=['GET'])
@jwt_required()
def get_funds_history():
    history = BatchFund.query.order_by(BatchFund.date.desc()).all()
    return jsonify([{
        "id": f.id,
        "description": f.description,
        "amount": f.amount,
        "type": f.type,
        "category": f.category,
        "date": f.date.isoformat(),
        "tags": f.tags,
        "student_name": f.student.full_name if f.student else None
    } for f in history])

@api_bp.route('/funds/audit', methods=['GET'])
@jwt_required()
def get_funds_audit():
    students = Student.query.order_by(Student.full_name).all()
    target_payment = get_fund_target()
    
    audit_data = []
    for s in students:
        total_paid = db.session.query(db.func.sum(BatchFund.amount)).filter(
            BatchFund.student_id == s.id, 
            BatchFund.type == 'Masuk'
        ).scalar() or 0
        
        audit_data.append({
            "student_name": s.full_name,
            "nim": s.nim,
            "paid": total_paid,
            "target": target_payment,
            "arrears": max(0, target_payment - total_paid)
        })
        
    return jsonify(audit_data)

@api_bp.route('/members', methods=['GET'])
@api_bp.route('/students', methods=['GET'])
@jwt_required()
def get_members():
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    
    if user.student and user.student.classroom_id:
        students = Student.query.filter_by(classroom_id=user.student.classroom_id).order_by(Student.full_name).all()
    else:
        students = Student.query.order_by(Student.full_name).all()
        
    return jsonify([{
        "id": s.id,
        "nim": s.nim,
        "full_name": s.full_name,
        "status": s.status
    } for s in students])

@api_bp.route('/gallery', methods=['GET'])
@jwt_required()
def get_gallery():
    # User can see published photos, or their own pending photos
    user_id = int(get_jwt_identity())
    user = User.query.get(user_id)
    
    if user.role.name in ['Admin', 'Pengurus']:
        photos = GalleryPhoto.query.order_by(GalleryPhoto.created_at.desc()).all()
    else:
        # Published OR owned by user
        photos = GalleryPhoto.query.filter((GalleryPhoto.status == 'Published') | (GalleryPhoto.user_id == user_id)).order_by(GalleryPhoto.created_at.desc()).all()

    return jsonify([{
        "id": p.id,
        "filename": p.filename,
        "thumbnail": p.thumbnail,
        "caption": p.caption,
        "tags": p.tags,
        "status": p.status,
        "is_public": p.is_public,
        "uploaded_by": p.user.full_name if p.user else "System",
        "created_at": p.created_at.isoformat(),
        "comments": [{
            "id": c.id,
            "user": c.user.student.full_name if c.user.student else c.user.full_name or c.user.username,
            "body": c.body,
            "time": c.created_at.strftime('%d %b %H:%M')
        } for c in p.comments]
    } for p in photos])

@api_bp.route('/gallery/comment/<int:photo_id>', methods=['POST'])
@jwt_required()
def add_gallery_comment(photo_id):
    user_id = int(get_jwt_identity())
    data = request.get_json()
    if not data or not data.get('body'):
        return jsonify({"error": "Pesan komentar diperlukan"}), 400
    
    from models import PhotoComment
    comment = PhotoComment(photo_id=photo_id, user_id=user_id, body=data['body'])
    db.session.add(comment)
    db.session.commit()
    return jsonify({"status": "success"})

@api_bp.route('/gallery/moderate/<int:photo_id>', methods=['POST'])
@jwt_required()
def moderate_gallery(photo_id):
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    if user.role.name not in ['Admin', 'Pengurus']:
        return jsonify({"error": "Unauthorized"}), 403
    
    data = request.get_json()
    photo = GalleryPhoto.query.get_or_404(photo_id)
    photo.status = data.get('status', 'Published')
    db.session.commit()
    return jsonify({"status": "success"})

import uuid
from PIL import Image

def process_image_upload(file):
    if not file: return None
    try:
        # Using current_app.config
        gallery_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'gallery')
        thumb_dir = os.path.join(gallery_dir, 'thumbnails')
        os.makedirs(gallery_dir, exist_ok=True)
        os.makedirs(thumb_dir, exist_ok=True)

        filename_base = uuid.uuid4().hex
        filename = f"{filename_base}.webp"
        filepath = os.path.join(gallery_dir, filename)
        thumbpath = os.path.join(thumb_dir, filename)

        img = Image.open(file.stream)
        if img.mode in ("RGBA", "P"): img = img.convert("RGB")
        
        # Save Preview/Standard
        preview_img = img.copy()
        preview_img.thumbnail((1200, 1200))
        preview_img.save(filepath, 'WEBP', quality=50)
        
        # Create and Save Thumbnail
        thumb_img = img.copy()
        thumb_img.thumbnail((300, 300))
        thumb_img.save(thumbpath, 'WEBP', quality=45)
        return filename
    except Exception as e:
        print(f"Error processing image: {e}")
        return None

@api_bp.route('/gallery/upload', methods=['POST'])
@jwt_required()
def upload_gallery_api():
    user_id = int(get_jwt_identity())
    user = User.query.get(user_id)
    
    files = request.files.getlist('photos')
    if not files:
        return jsonify({"error": "Tidak ada foto terpilih"}), 400
        
    caption = request.form.get('caption', '')
    tags = request.form.get('tags', '')
    is_public = request.form.get('is_public') == 'true'
    
    # Non-admin uploads are pending
    status = 'Published' if user.role.name in ['Admin', 'Pengurus'] else 'Pending'
    
    count = 0
    for file in files:
        if file and file.filename != '':
            filename = process_image_upload(file)
            if filename:
                photo = GalleryPhoto(
                    filename=filename,
                    thumbnail=filename,
                    caption=caption,
                    tags=tags,
                    uploaded_by=user_id,
                    status=status,
                    is_public=is_public
                )
                db.session.add(photo)
                count += 1
    
    db.session.commit()
    return jsonify({"status": "success", "count": count})

@api_bp.route('/logs', methods=['GET'])
@jwt_required()
def get_logs():
    logs = ActivityLog.query.order_by(ActivityLog.timestamp.desc()).limit(50).all()
    return jsonify([{
        "id": l.id,
        "action": l.action,
        "details": l.details,
        "timestamp": l.timestamp.isoformat(),
        "username": l.user.username if l.user else "System"
    } for l in logs])
