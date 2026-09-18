"""add class-scoped face labeling plugin tables

Revision ID: c7e4b9a12f6d
Revises: f2a1c9d3e7b4
"""

from alembic import op
import sqlalchemy as sa


revision = 'c7e4b9a12f6d'
down_revision = 'f2a1c9d3e7b4'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if 'face_label_profile' not in tables:
        op.create_table(
            'face_label_profile',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('classroom_id', sa.Integer(), sa.ForeignKey('class_room.id'), nullable=False),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=False),
            sa.Column('embedding_ciphertext', sa.Text(), nullable=True),
            sa.Column('consent_status', sa.String(length=24), nullable=False, server_default='pending_consent'),
            sa.Column('consented_at', sa.DateTime(), nullable=True),
            sa.Column('consent_version', sa.String(length=32), nullable=True),
            sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('created_by', sa.Integer(), sa.ForeignKey('user.id'), nullable=True),
            sa.Column('revoked_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
            sa.UniqueConstraint('classroom_id', 'user_id', name='uq_face_profile_classroom_user'),
        )
        op.create_index('ix_face_label_profile_classroom_id', 'face_label_profile', ['classroom_id'])
        op.create_index('ix_face_label_profile_user_id', 'face_label_profile', ['user_id'])
        op.create_index('ix_face_label_profile_consent_status', 'face_label_profile', ['consent_status'])
        op.create_index('ix_face_label_profile_active', 'face_label_profile', ['active'])

    if 'face_label_photo_face' not in tables:
        op.create_table(
            'face_label_photo_face',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('photo_id', sa.Integer(), sa.ForeignKey('gallery_photo.id', ondelete='CASCADE'), nullable=False),
            sa.Column('classroom_id', sa.Integer(), sa.ForeignKey('class_room.id'), nullable=True),
            sa.Column('bbox_json', sa.Text(), nullable=False),
            sa.Column('embedding_ciphertext', sa.Text(), nullable=False),
            sa.Column('quality_score', sa.Float(), nullable=True),
            sa.Column('model_version', sa.String(length=80), nullable=True),
            sa.Column('status', sa.String(length=24), nullable=False, server_default='detected'),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        )
        op.create_index('ix_face_label_photo_face_photo_id', 'face_label_photo_face', ['photo_id'])
        op.create_index('ix_face_label_photo_face_classroom_id', 'face_label_photo_face', ['classroom_id'])
        op.create_index('ix_face_label_photo_face_status', 'face_label_photo_face', ['status'])

    if 'face_label_match' not in tables:
        op.create_table(
            'face_label_match',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('photo_face_id', sa.Integer(), sa.ForeignKey('face_label_photo_face.id', ondelete='CASCADE'), nullable=False),
            sa.Column('profile_id', sa.Integer(), sa.ForeignKey('face_label_profile.id', ondelete='CASCADE'), nullable=False),
            sa.Column('classroom_id', sa.Integer(), sa.ForeignKey('class_room.id'), nullable=False),
            sa.Column('confidence', sa.Float(), nullable=False, server_default='0'),
            sa.Column('status', sa.String(length=24), nullable=False, server_default='suggested'),
            sa.Column('reviewed_by', sa.Integer(), sa.ForeignKey('user.id'), nullable=True),
            sa.Column('reviewed_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
            sa.UniqueConstraint('photo_face_id', 'profile_id', name='uq_face_match_face_profile'),
        )
        op.create_index('ix_face_label_match_photo_face_id', 'face_label_match', ['photo_face_id'])
        op.create_index('ix_face_label_match_profile_id', 'face_label_match', ['profile_id'])
        op.create_index('ix_face_label_match_classroom_id', 'face_label_match', ['classroom_id'])
        op.create_index('ix_face_label_match_status', 'face_label_match', ['status'])

    if 'face_label_processing_job' not in tables:
        op.create_table(
            'face_label_processing_job',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('photo_id', sa.Integer(), sa.ForeignKey('gallery_photo.id', ondelete='CASCADE'), nullable=False, unique=True),
            sa.Column('classroom_id', sa.Integer(), sa.ForeignKey('class_room.id'), nullable=True),
            sa.Column('status', sa.String(length=24), nullable=False, server_default='queued'),
            sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('error_message', sa.Text(), nullable=True),
            sa.Column('locked_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
        )
        op.create_index('ix_face_label_processing_job_classroom_id', 'face_label_processing_job', ['classroom_id'])
        op.create_index('ix_face_label_processing_job_status', 'face_label_processing_job', ['status'])


def downgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table in ('face_label_processing_job', 'face_label_match', 'face_label_photo_face', 'face_label_profile'):
        if table in tables:
            op.drop_table(table)
