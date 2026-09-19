"""Optional face-labeling plugin for class-scoped gallery matching."""

from .config import is_enabled


def register_face_labeling(app, scheduler=None):
    """Register the plugin without making its optional AI dependency mandatory."""
    from . import models  # noqa: F401 - register SQLAlchemy models before migrations
    from .routes import face_labeling_bp
    from .api import face_labeling_api_bp

    app.register_blueprint(face_labeling_bp)
    app.register_blueprint(face_labeling_api_bp)

    @app.context_processor
    def inject_face_labeling_context():
        return {'face_labeling_enabled': is_enabled()}

    if scheduler is not None:
        from .jobs import run_scheduled_jobs

        scheduler.add_job(
            func=run_scheduled_jobs,
            args=[app],
            trigger='interval',
            seconds=45,
            id='face_labeling_jobs',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
