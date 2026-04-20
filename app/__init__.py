from flask import Flask
import logging
import os
from logging.handlers import RotatingFileHandler
from apscheduler.schedulers.background import BackgroundScheduler
from .models import db
from .jenkins_sync import sync_jobs, sync_audited_job_history


def create_app():
    app = Flask(__name__, template_folder="templates")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app.config.from_object("app.config.Config")

    db.init_app(app)

    from .routes import main

    app.register_blueprint(main)

    def scheduled_sync_jobs():
        with app.app_context():
            db.create_all()
            sync_jobs()

    def scheduled_sync_history():
        with app.app_context():
            db.create_all()
            sync_audited_job_history()

    if (not app.debug) or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(
            scheduled_sync_jobs,
            "interval",
            minutes=10,
            id="sync_jenkins_jobs",
            replace_existing=True,
        )
        scheduler.add_job(
            scheduled_sync_history,
            "interval",
            minutes=30,
            id="sync_audited_job_history",
            replace_existing=True,
        )
        scheduler.start()
        app.extensions["scheduler"] = scheduler

        scheduled_sync_jobs()
        scheduled_sync_history()

    return app
