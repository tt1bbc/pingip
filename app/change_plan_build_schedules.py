from datetime import datetime, timezone

from .models import ChangePlanBuildSchedule, db
from .task_queue import get_rq_queue


ACTIVE_SCHEDULE_STATUSES = ("SCHEDULED", "QUEUED", "RUNNING")
RQ_ACTIVE_STATUSES = {"queued", "deferred", "started", "scheduled"}
TASK_FUNC_PATH = "app.tasks.run_change_plan_build_schedule_task"


def apscheduler_job_id(schedule_id):
    return f"change_plan_build_schedule_{schedule_id}"


def _rq_status_value(status):
    return getattr(status, "value", str(status)).lower()


def enqueue_change_plan_build_schedule(app, schedule_id):
    with app.app_context():
        schedule = db.session.get(ChangePlanBuildSchedule, schedule_id)
        if not schedule or schedule.status != "SCHEDULED":
            return

        queue = get_rq_queue(app)
        rq_job = queue.enqueue(
            TASK_FUNC_PATH,
            schedule_id,
            job_timeout=7200,
            result_ttl=86400,
            failure_ttl=604800,
        )

        now_utc = datetime.now(timezone.utc)
        schedule.status = "QUEUED"
        schedule.rq_job_id = rq_job.id
        schedule.enqueued_at = now_utc
        schedule.updated_at = now_utc
        schedule.message = "已入队，等待 Worker 执行"
        db.session.commit()


def _rq_job_is_active(app, schedule):
    if not schedule.rq_job_id:
        return False
    try:
        from rq.job import Job

        queue = get_rq_queue(app)
        job = Job.fetch(schedule.rq_job_id, connection=queue.connection)
        return _rq_status_value(job.get_status()) in RQ_ACTIVE_STATUSES
    except Exception:
        return False


def register_change_plan_build_schedule(app, schedule):
    scheduler = app.extensions.get("scheduler")
    if not scheduler or schedule.status != "SCHEDULED":
        return

    run_at = schedule.scheduled_at
    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=timezone.utc)

    scheduler.add_job(
        enqueue_change_plan_build_schedule,
        "date",
        run_date=run_at,
        args=[app, schedule.id],
        id=apscheduler_job_id(schedule.id),
        replace_existing=True,
        misfire_grace_time=3600,
    )


def unregister_change_plan_build_schedule(app, schedule_id):
    scheduler = app.extensions.get("scheduler")
    if not scheduler:
        return
    job_id = apscheduler_job_id(schedule_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


def restore_change_plan_build_schedules(app):
    with app.app_context():
        db.create_all()
        schedules = ChangePlanBuildSchedule.query.filter(
            ChangePlanBuildSchedule.status.in_(("SCHEDULED", "QUEUED"))
        ).all()
        now_utc = datetime.now(timezone.utc)

        for schedule in schedules:
            if schedule.status == "QUEUED":
                if _rq_job_is_active(app, schedule):
                    continue
                schedule.status = "SCHEDULED"
                schedule.rq_job_id = None
                schedule.message = "入队任务未执行，已重新等待调度"
                schedule.updated_at = now_utc
                db.session.commit()

            scheduled_at = schedule.scheduled_at
            if scheduled_at.tzinfo is None:
                scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)

            if scheduled_at <= now_utc:
                enqueue_change_plan_build_schedule(app, schedule.id)
            else:
                register_change_plan_build_schedule(app, schedule)
