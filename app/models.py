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


class ChangePlan(db.Model):
    __tablename__ = "change_plans"
    __table_args__ = (
        Index("ix_change_plans_plan_window", "plan_start_at", "plan_end_at"),
        Index("ix_change_plans_system_name", "system_name"),
    )

    id = db.Column(db.BigInteger, primary_key=True)
    source_id = db.Column(db.String(128), unique=True, nullable=True, index=True)
    change_number = db.Column(db.String(128), unique=True, nullable=False, index=True)
    requester = db.Column(db.String(128), nullable=False)
    plan_start_at = db.Column(db.DateTime, nullable=False)
    plan_end_at = db.Column(db.DateTime, nullable=False)
    system_name = db.Column(db.String(255), nullable=False)
    jenkins_jobs = db.Column(JSONB, nullable=False, default=list)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class ChangePlanBuildSchedule(db.Model):
    __tablename__ = "change_plan_build_schedules"
    __table_args__ = (
        UniqueConstraint("change_plan_id", name="uq_change_plan_build_schedule_plan"),
        Index("ix_change_plan_build_schedules_status", "status"),
        Index("ix_change_plan_build_schedules_scheduled_at", "scheduled_at"),
    )

    id = db.Column(db.BigInteger, primary_key=True)
    change_plan_id = db.Column(
        db.BigInteger,
        db.ForeignKey("change_plans.id", ondelete="CASCADE"),
        nullable=False,
    )
    status = db.Column(db.String(32), nullable=False, default="SCHEDULED")
    scheduled_at = db.Column(db.DateTime, nullable=False)
    job_states = db.Column(JSONB, nullable=False, default=list)
    message = db.Column(db.Text, nullable=True)
    rq_job_id = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    enqueued_at = db.Column(db.DateTime, nullable=True)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
