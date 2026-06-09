from datetime import datetime, timezone, timedelta
import logging
import jenkins
from urllib.parse import quote
from sqlalchemy.exc import IntegrityError
from .config import Config
from .jenkins_client import jenkins_get
from .models import db, Job, JobBuildHistory

logger = logging.getLogger(__name__)


def _fetch_job_names():
    server = jenkins.Jenkins(
        Config.JENKINS_URL,
        username=Config.JENKINS_USER,
        password=Config.JENKINS_API_TOKEN,
    )
    jobs = server.get_all_jobs()
    return [job["name"] for job in jobs]


def sync_jobs():
    try:
        job_names = _fetch_job_names()
    except Exception as exc:
        logger.exception("Failed to fetch Jenkins jobs: %s", exc)
        return

    existing_jobs = Job.query.all()
    existing_by_name = {job.name: job for job in existing_jobs}
    incoming_set = set(job_names)
    existing_set = set(existing_by_name.keys())

    now = datetime.now(timezone.utc)

    # add new
    for name in incoming_set - existing_set:
        db.session.add(Job(name=name, created_at=now, updated_at=now))
        logger.info("新增job: %s", name)

    # update seen
    # for name in incoming_set & existing_set:
    #     existing_by_name[name].updated_at = now
    #     logger.info("更新job: %s", name)

    # remove missing
    for name in existing_set - incoming_set:
        db.session.delete(existing_by_name[name])
        logger.info("删除job: %s", name)

    db.session.commit()


def _fetch_job_builds_since(job_name, since_dt):
    job_api = f"{Config.JENKINS_URL}/job/{quote(job_name)}/api/json"
    params = {
        "tree": "builds[number,url,timestamp,duration,result,building,actions[causes[*],parameters[*]]]"
    }
    try:
        resp = jenkins_get(
            job_api,
            params=params,
        )
    except Exception as exc:
        logger.warning("Failed to fetch job builds for %s: %s", job_name, exc)
        return []

    if resp.status_code != 200:
        logger.warning("Failed to fetch job builds for %s: %s", job_name, resp.status_code)
        return []

    data = resp.json() or {}
    builds = data.get("builds") or []
    since_ms = int(since_dt.timestamp() * 1000)
    return [b for b in builds if (b.get("timestamp") or 0) >= since_ms]


def _extract_triggered_by(build):
    actions = build.get("actions") or []
    for action in actions:
        causes = action.get("causes")
        if isinstance(causes, list):
            for cause in causes:
                user = cause.get("userName") or cause.get("userId")
                if user:
                    return user
                desc = cause.get("shortDescription")
                if desc:
                    return desc
    return None


def _extract_parameters(build):
    actions = build.get("actions") or []
    for action in actions:
        params = action.get("parameters")
        if isinstance(params, list):
            result = {}
            for param in params:
                name = param.get("name")
                if name is None:
                    continue
                result[name] = param.get("value")
            return result
    return None


def sync_audited_job_history(months=3):
    audited_jobs = Job.query.filter(Job.is_audited.is_(True)).all()
    if not audited_jobs:
        return

    since_dt = datetime.now(timezone.utc) - timedelta(days=30 * months)

    for job in audited_jobs:
        builds = _fetch_job_builds_since(job.name, since_dt)
        for build in builds:
            build_number = build.get("number")
            if build_number is None:
                continue

            exists = (
                JobBuildHistory.query.filter_by(job_name=job.name, build_number=build_number)
                .with_entities(JobBuildHistory.id)
                .first()
            )
            if exists:
                continue

            timestamp_ms = build.get("timestamp") or 0
            started_at = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
            duration_ms = build.get("duration")
            finished_at = None
            if duration_ms is not None:
                finished_at = started_at + timedelta(milliseconds=duration_ms)

            status = "RUNNING" if build.get("building") else (build.get("result") or "UNKNOWN")

            history = JobBuildHistory(
                job_name=job.name,
                build_number=build_number,
                status=status,
                triggered_by=_extract_triggered_by(build),
                parameters=_extract_parameters(build),
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                jenkins_url=build.get("url"),
            )
            db.session.add(history)
            logger.info("新增构建历史: job %s build #%d", job.name, build_number)

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            logger.info("Skipped duplicate build history for job %s", job.name)
