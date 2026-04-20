from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint, Index
from sqlalchemy.dialects.postgresql import JSONB

db = SQLAlchemy()


class Job(db.Model):
    __tablename__ = "jobs"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False, index=True)
    is_audited = db.Column(db.Boolean, nullable=True)
    system_name = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class JobBuildHistory(db.Model):
    __tablename__ = "job_build_history"
    __table_args__ = (
        UniqueConstraint("job_name", "build_number", name="uq_job_build_number"),
        Index("ix_job_build_history_job_started", "job_name", "started_at"),
        Index("ix_job_build_history_status", "status"),
    )

    id = db.Column(db.BigInteger, primary_key=True)
    job_name = db.Column(db.String(255), nullable=False, index=True)
    build_number = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(32), nullable=False, default="RUNNING")
    triggered_by = db.Column(db.String(128), nullable=True)
    parameters = db.Column(JSONB, nullable=True)
    started_at = db.Column(db.DateTime, nullable=False)
    finished_at = db.Column(db.DateTime, nullable=True)
    duration_ms = db.Column(db.Integer, nullable=True)
    jenkins_url = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
