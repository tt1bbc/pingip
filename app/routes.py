from flask import Blueprint, render_template, request, jsonify, current_app, url_for, redirect
import netaddr
import asyncio
import aioping
import logging
from datetime import datetime, timezone, timedelta
import json
from .config import Config
from .change_plan_build_schedules import (
    ACTIVE_SCHEDULE_STATUSES,
    register_change_plan_build_schedule,
    unregister_change_plan_build_schedule,
)
from .jenkins_builds import fetch_job_parameters, fetch_latest_build_number
from .models import ChangePlan, ChangePlanBuildSchedule, Job, JobBuildHistory, db
from .jenkins_sync import sync_jobs
from .task_queue import get_rq_queue
from .tasks import run_batch_build_task
from .zerocode_sync import ensure_change_plan_source_id_column

main = Blueprint("main", __name__)
logger = logging.getLogger(__name__)

HIDDEN_JOB_PREFIXES = ("test", "dev", "pre", "offline", "mrotest")


def _is_hidden_job(job_name):
    normalized = (job_name or "").strip().lower()
    return normalized.startswith(HIDDEN_JOB_PREFIXES)


async def pingip(host):
    try:
        delay = await aioping.ping(host, timeout=2)
        return host, round(delay, 3)
    except TimeoutError:
        return host, "TimeOut"
    except OSError as e:
        return host, str(e)


@main.route("/")
def index():
    return render_template("home.html")


@main.route("/net/pingpage")
def pinglist():
    return render_template("ping.html", comrange=sorted(Config.COMRANGE, reverse=True))


@main.route("/net/ping", methods=["POST"])
async def ping():
    ip_range = request.form.get("ip_range")
    logger.info("Received IP range to ping: %s", ip_range)

    if not ip_range:
        return jsonify({"error": "IP range is required."}), 400

    if "-" in ip_range:
        ip_range = ip_range.split("-")[0].strip()

    success_results = {}
    fail_results = {}

    try:
        net = netaddr.IPNetwork(ip_range)
        tasks = [pingip(str(host)) for host in net.iter_hosts()]
        pingresults = await asyncio.gather(*tasks)

        for reshost, result in pingresults:
            if isinstance(result, float):
                success_results[reshost] = result
            else:
                fail_results[reshost] = result
    except Exception as e:
        logger.exception("Error processing IP range")
        return jsonify({"error": str(e)}), 400

    return jsonify({"success": success_results, "fail": fail_results})


@main.route("/jenkins/trigger", methods=["GET"])
def getjenkinsjobs():
    jobs = Job.query.order_by(Job.name).all()
    if not jobs:
        sync_jobs()
        jobs = Job.query.order_by(Job.name).all()
    jobsname = [
        job.name
        for job in jobs
        if not _is_hidden_job(job.name)
    ]
    return render_template("jenkins.html", jobs=jobsname)


@main.route("/jenkins/get-job-parameters-batch", methods=["POST"])
def get_job_parameters_batch():
    job_names = request.json.get("job_names")
    if not job_names or not isinstance(job_names, list):
        return jsonify({"error": "job_names list is required"}), 400

    results = {}
    for job_name in job_names:
        try:
            parameters, _ = fetch_job_parameters(job_name)
            results[job_name] = parameters
        except Exception as e:
            results[job_name] = [{"name": "Error", "default": str(e)}]

    return jsonify({"results": results})


@main.route("/jenkins/audit", methods=["GET"])
def jenkins_audit():
    jobs = Job.query.order_by(Job.name).all()
    if not jobs:
        sync_jobs()
        jobs = Job.query.order_by(Job.name).all()
    jobs = [job for job in jobs if not _is_hidden_job(job.name)]
    return render_template("jenkins_audit.html", jobs=jobs)


def _coerce_bool(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        val = value.strip().lower()
        if val in ("true", "1", "yes", "y", "on"):
            return True
        if val in ("false", "0", "no", "n", "off"):
            return False
    return None


@main.route("/jenkins/audit/update", methods=["POST"])
def jenkins_audit_update():
    data = request.get_json(silent=True) or {}
    job_name = (data.get("job_name") or "").strip()
    if not job_name:
        return jsonify({"error": "job_name is required"}), 400

    job = Job.query.filter_by(name=job_name).first()
    if not job:
        return jsonify({"error": "job not found"}), 404

    is_audited = _coerce_bool(data.get("is_audited"))
    system_name = data.get("system_name")
    if isinstance(system_name, str):
        system_name = system_name.strip() or None

    job.is_audited = is_audited
    job.system_name = system_name
    job.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({"ok": True, "is_audited": job.is_audited, "system_name": job.system_name})


@main.route("/jenkins/build-batch", methods=["POST"])
def build_jobs_batch():
    job_names = request.json.get("job_names")
    if not job_names or not isinstance(job_names, list):
        return jsonify({"error": "job_names list is required"}), 400

    queue = get_rq_queue(current_app)
    rq_job = queue.enqueue(
        run_batch_build_task,
        job_names,
        job_timeout=1800,
        result_ttl=86400,
        failure_ttl=604800,
    )
    return jsonify({"ok": True, "message": "Batch build queued", "job_id": rq_job.id})


def _parse_local_datetime(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    tz_cst = timezone(timedelta(hours=8))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz_cst)
    return dt.astimezone(timezone.utc)


def _to_cst_str(dt):
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    tz_cst = timezone(timedelta(hours=8))
    return dt.astimezone(tz_cst).strftime("%Y-%m-%d %H:%M:%S")


def _to_cst_minute_str(dt):
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    tz_cst = timezone(timedelta(hours=8))
    return dt.astimezone(tz_cst).strftime("%Y-%m-%d %H:%M")


def _date_input_value(value):
    value = (value or "").strip()
    if not value:
        return ""
    return value[:10]


def _compact_json_value(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return ""
    return str(value)


def _parameter_entries(parameters):
    if not parameters:
        return []
    if isinstance(parameters, dict):
        return [
            {"key": str(key), "value": _compact_json_value(value)}
            for key, value in parameters.items()
        ]
    if isinstance(parameters, list):
        return [
            {"key": str(index + 1), "value": _compact_json_value(value)}
            for index, value in enumerate(parameters)
        ]
    return [{"key": "value", "value": _compact_json_value(parameters)}]


def _parse_jenkins_jobs(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if not isinstance(value, str):
        return []

    raw = value.strip()
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, list):
        return [str(item).strip() for item in decoded if str(item).strip()]

    jobs = []
    for line in raw.replace(",", "\n").splitlines():
        job = line.strip()
        if job:
            jobs.append(job)
    return jobs


def _change_plan_job_names(plan):
    job_names = []
    for item in plan.jenkins_jobs or []:
        if isinstance(item, dict):
            job_name = (item.get("jenkins_job") or "").strip()
        else:
            job_name = str(item).strip()
        if job_name:
            job_names.append(job_name)
    return job_names


def _schedule_status_label(status):
    labels = {
        "SCHEDULED": "已创建",
        "QUEUED": "已入队",
        "RUNNING": "执行中",
        "SUCCESS": "已完成",
        "FAILURE": "失败",
        "CANCELLED": "已取消",
    }
    return labels.get(status or "", status or "未创建")


def _schedule_job_status_label(status):
    labels = {
        "SCHEDULED": "待执行",
        "RUNNING": "构建中",
        "SUCCESS": "成功",
        "FAILURE": "失败",
        "SKIPPED_MANUAL": "已手动构建",
        "CANCELLED": "已取消",
    }
    return labels.get(status or "", status or "待执行")


def _schedule_job_status_class(status):
    classes = {
        "SCHEDULED": "scheduled",
        "RUNNING": "running",
        "SUCCESS": "success",
        "FAILURE": "failure",
        "SKIPPED_MANUAL": "manual",
        "CANCELLED": "cancelled",
    }
    return classes.get(status or "", "scheduled")


def _schedule_view(schedule):
    if not schedule:
        return None
    return {
        "id": schedule.id,
        "status": schedule.status,
        "status_label": _schedule_status_label(schedule.status),
        "scheduled_at": _to_cst_minute_str(schedule.scheduled_at),
        "message": schedule.message or "",
        "can_cancel": schedule.status in ACTIVE_SCHEDULE_STATUSES,
        "job_states": [
            {
                **dict(state),
                "status_label": _schedule_job_status_label(state.get("status")),
            }
            for state in (schedule.job_states or [])
        ],
    }


def _jobs_with_schedule_states(jenkins_jobs, schedule):
    states_by_name = {}
    if schedule:
        for state in schedule.job_states or []:
            job_name = (state.get("job_name") or "").strip()
            if job_name:
                states_by_name[job_name] = state

    jobs = []
    for job in jenkins_jobs or []:
        if isinstance(job, dict):
            item = dict(job)
            job_name = (item.get("jenkins_job") or "").strip()
        else:
            item = {"jenkins_job": str(job).strip(), "job_person": None}
            job_name = item["jenkins_job"]

        state = states_by_name.get(job_name)
        if state:
            status = state.get("status")
            item["schedule_status"] = status
            item["schedule_status_label"] = _schedule_job_status_label(status)
            item["schedule_status_class"] = _schedule_job_status_class(status)
            item["baseline_build_number"] = state.get("baseline_build_number")
        jobs.append(item)
    return jobs


def _ensure_change_plan_table():
    db.create_all()
    ensure_change_plan_source_id_column()


@main.route("/change-plans", methods=["POST"])
def create_change_plan():
    _ensure_change_plan_table()
    data = request.get_json(silent=True) if request.is_json else request.form
    data = data or {}

    change_number = (data.get("change_number") or "").strip()
    requester = (data.get("requester") or "").strip()
    system_name = (data.get("system_name") or "").strip()
    plan_start_at = _parse_local_datetime((data.get("plan_start_at") or "").strip())
    plan_end_at = _parse_local_datetime((data.get("plan_end_at") or "").strip())
    jenkins_jobs = _parse_jenkins_jobs(data.get("jenkins_jobs"))

    errors = {}
    if not change_number:
        errors["change_number"] = "变更单编号不能为空"
    if not requester:
        errors["requester"] = "发起人不能为空"
    if not plan_start_at:
        errors["plan_start_at"] = "计划开始时间格式无效"
    if not plan_end_at:
        errors["plan_end_at"] = "计划结束时间格式无效"
    if plan_start_at and plan_end_at and plan_end_at <= plan_start_at:
        errors["plan_end_at"] = "计划结束时间必须晚于计划开始时间"
    if not system_name:
        errors["system_name"] = "系统名称不能为空"

    if errors:
        return jsonify({"ok": False, "errors": errors}), 400

    exists = ChangePlan.query.filter_by(change_number=change_number).first()
    if exists:
        return jsonify({"ok": False, "errors": {"change_number": "变更单编号已存在"}}), 409

    plan = ChangePlan(
        change_number=change_number,
        requester=requester,
        plan_start_at=plan_start_at,
        plan_end_at=plan_end_at,
        system_name=system_name,
        jenkins_jobs=jenkins_jobs,
    )
    db.session.add(plan)
    db.session.commit()

    return jsonify(
        {
            "ok": True,
            "id": plan.id,
            "change_number": plan.change_number,
            "jenkins_jobs_count": len(plan.jenkins_jobs or []),
        }
    ), 201


@main.route("/change-plans/dashboard", methods=["GET"])
def change_plan_dashboard_legacy():
    return redirect(url_for("main.change_plan_dashboard"), code=302)


@main.route("/jenkins/change-plans/<int:plan_id>/build-schedule", methods=["POST"])
def create_change_plan_build_schedule(plan_id):
    db.create_all()
    plan = db.session.get(ChangePlan, plan_id)
    if not plan:
        return jsonify({"ok": False, "error": "change plan not found"}), 404

    data = request.get_json(silent=True) or {}
    scheduled_at_input = (data.get("scheduled_at") or "").strip()
    scheduled_at = _parse_local_datetime(scheduled_at_input) if scheduled_at_input else plan.plan_start_at
    if not scheduled_at:
        return jsonify({"ok": False, "error": "执行时间格式无效"}), 400
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    now_utc = datetime.now(timezone.utc)
    if scheduled_at <= now_utc:
        return jsonify({"ok": False, "error": "执行时间必须晚于当前时间"}), 400

    job_names = _change_plan_job_names(plan)
    if not job_names:
        return jsonify({"ok": False, "error": "变更计划没有 Jenkins 清单"}), 400

    schedule = ChangePlanBuildSchedule.query.filter_by(change_plan_id=plan.id).first()
    if schedule and schedule.status in ACTIVE_SCHEDULE_STATUSES:
        return jsonify({"ok": False, "error": "该变更计划已存在未结束的定时任务"}), 409

    job_states = []
    for index, job_name in enumerate(job_names, start=1):
        try:
            baseline = fetch_latest_build_number(job_name)
            message = (
                f"创建时最后构建号：{baseline}"
                if baseline is not None
                else "创建时未获取到最后构建号"
            )
        except Exception as exc:
            logger.exception("Failed to fetch latest build number for %s", job_name)
            baseline = None
            message = f"创建时获取最后构建号失败：{exc}"
        job_states.append(
            {
                "job_name": job_name,
                "order": index,
                "baseline_build_number": baseline,
                "status": "SCHEDULED",
                "message": message,
            }
        )

    if not schedule:
        schedule = ChangePlanBuildSchedule(change_plan_id=plan.id)
        db.session.add(schedule)

    schedule.status = "SCHEDULED"
    schedule.scheduled_at = scheduled_at
    schedule.job_states = job_states
    schedule.message = "定时任务已创建"
    schedule.rq_job_id = None
    schedule.enqueued_at = None
    schedule.started_at = None
    schedule.finished_at = None
    schedule.updated_at = now_utc
    db.session.commit()
    register_change_plan_build_schedule(current_app._get_current_object(), schedule)
    return jsonify({"ok": True, "schedule": _schedule_view(schedule)})


@main.route("/jenkins/change-plans/<int:plan_id>/build-schedule/cancel", methods=["POST"])
def cancel_change_plan_build_schedule(plan_id):
    db.create_all()
    schedule = ChangePlanBuildSchedule.query.filter_by(change_plan_id=plan_id).first()
    if not schedule:
        return jsonify({"ok": False, "error": "定时任务不存在"}), 404
    if schedule.status not in ACTIVE_SCHEDULE_STATUSES:
        return jsonify({"ok": False, "error": "当前状态不能取消"}), 400

    now_utc = datetime.now(timezone.utc)
    schedule.status = "CANCELLED"
    schedule.message = "任务已取消"
    schedule.finished_at = now_utc
    schedule.updated_at = now_utc
    schedule.job_states = [
        {
            **dict(state),
            "status": "CANCELLED" if state.get("status") in ("SCHEDULED", "RUNNING") else state.get("status"),
            "message": "任务已取消" if state.get("status") in ("SCHEDULED", "RUNNING") else state.get("message"),
        }
        for state in (schedule.job_states or [])
    ]
    db.session.commit()
    unregister_change_plan_build_schedule(current_app._get_current_object(), schedule.id)
    return jsonify({"ok": True, "schedule": _schedule_view(schedule)})


@main.route("/jenkins/change-plans", methods=["GET"])
def change_plan_dashboard():
    _ensure_change_plan_table()
    plan_start_from = (request.args.get("plan_start_from") or "").strip()
    plan_start_to = (request.args.get("plan_start_to") or "").strip()
    requester = (request.args.get("requester") or "").strip()
    system_name = (request.args.get("system_name") or "").strip()
    jenkins_job = (request.args.get("jenkins_job") or "").strip()
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except ValueError:
        page = 1
    try:
        per_page = int(request.args.get("per_page", 30))
    except ValueError:
        per_page = 30
    if per_page not in (30, 50, 100):
        per_page = 30

    query = ChangePlan.query
    dt_from = _parse_local_datetime(plan_start_from)
    if dt_from:
        query = query.filter(ChangePlan.plan_start_at >= dt_from)

    dt_to = _parse_local_datetime(plan_start_to)
    if dt_to:
        if len(plan_start_to) <= 10:
            dt_to = dt_to + timedelta(days=1)
            query = query.filter(ChangePlan.plan_start_at < dt_to)
        else:
            query = query.filter(ChangePlan.plan_start_at <= dt_to)

    if requester:
        query = query.filter(ChangePlan.requester.ilike(f"%{requester}%"))
    if system_name:
        query = query.filter(ChangePlan.system_name.ilike(f"%{system_name}%"))
    if jenkins_job:
        query = query.filter(ChangePlan.jenkins_jobs.cast(db.Text).ilike(f"%{jenkins_job}%"))

    total = query.count()
    total_pages = max((total + per_page - 1) // per_page, 1)
    if page > total_pages:
        page = total_pages
    plans = (
        query.order_by(ChangePlan.plan_start_at.desc(), ChangePlan.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    schedules_by_plan_id = {}
    if plans:
        plan_ids = [plan.id for plan in plans]
        schedules = ChangePlanBuildSchedule.query.filter(
            ChangePlanBuildSchedule.change_plan_id.in_(plan_ids)
        ).all()
        schedules_by_plan_id = {schedule.change_plan_id: schedule for schedule in schedules}
    rows = []
    for plan in plans:
        schedule = schedules_by_plan_id.get(plan.id)
        rows.append(
            {
                "id": plan.id,
                "change_number": plan.change_number,
                "requester": plan.requester,
                "plan_start_at": _to_cst_minute_str(plan.plan_start_at),
                "plan_end_at": _to_cst_minute_str(plan.plan_end_at),
                "system_name": plan.system_name,
                "jenkins_jobs": _jobs_with_schedule_states(plan.jenkins_jobs, schedule),
                "schedule": _schedule_view(schedule),
                "created_at": _to_cst_str(plan.created_at),
            }
        )

    query_params = request.args.to_dict()
    query_params["per_page"] = str(per_page)

    def _change_plan_url(target_page):
        params = dict(query_params)
        params["page"] = target_page
        return url_for("main.change_plan_dashboard", **params)

    prev_url = _change_plan_url(page - 1) if page > 1 else None
    next_url = _change_plan_url(page + 1) if page < total_pages else None
    return render_template(
        "change_plan_dashboard.html",
        rows=rows,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        prev_url=prev_url,
        next_url=next_url,
        filters={
            "plan_start_from": plan_start_from,
            "plan_start_to": plan_start_to,
            "requester": requester,
            "system_name": system_name,
            "jenkins_job": jenkins_job,
        },
    )


@main.route("/jenkins/history", methods=["GET"])
def jenkins_history():
    job_name = (request.args.get("job_name") or "").strip()
    status = (request.args.get("status") or "").strip()
    triggered_by = (request.args.get("triggered_by") or "").strip()
    audited = (request.args.get("is_audited") or "").strip().lower()
    system_name = (request.args.get("system_name") or "").strip()
    started_from = (request.args.get("started_from") or "").strip()
    started_to = (request.args.get("started_to") or "").strip()
    page = max(int(request.args.get("page", 1)), 1)
    per_page = max(min(int(request.args.get("per_page", 30)), 100), 1)

    query = JobBuildHistory.query.outerjoin(Job, JobBuildHistory.job_name == Job.name)
    if job_name:
        query = query.filter(JobBuildHistory.job_name.ilike(f"%{job_name}%"))
    if status:
        query = query.filter(JobBuildHistory.status == status)
    if triggered_by:
        query = query.filter(JobBuildHistory.triggered_by.ilike(f"%{triggered_by}%"))
    if system_name:
        query = query.filter(Job.system_name == system_name)
    if audited == "true":
        query = query.filter(Job.is_audited.is_(True))
    elif audited == "false":
        query = query.filter(Job.is_audited.is_(False))
    elif audited == "unset":
        query = query.filter(Job.is_audited.is_(None))

    dt_from = _parse_local_datetime(started_from)
    if dt_from:
        query = query.filter(JobBuildHistory.started_at >= dt_from)

    dt_to = _parse_local_datetime(started_to)
    if dt_to:
        # If date-only, treat as end of day by adding 1 day and using <
        if len(started_to) <= 10:
            dt_to = dt_to + timedelta(days=1)
            query = query.filter(JobBuildHistory.started_at < dt_to)
        else:
            query = query.filter(JobBuildHistory.started_at <= dt_to)

    total = query.count()
    items = (
        query.order_by(JobBuildHistory.started_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    rows = []
    for item in items:
        rows.append(
            {
                "job_name": item.job_name,
                "build_number": item.build_number,
                "status": item.status,
                "triggered_by": item.triggered_by or "",
                "parameter_entries": _parameter_entries(item.parameters),
                "started_at": _to_cst_str(item.started_at),
            }
        )

    total_pages = max((total + per_page - 1) // per_page, 1)
    system_names = [
        row[0]
        for row in (
            db.session.query(Job.system_name)
            .filter(Job.system_name.isnot(None), Job.system_name != "")
            .distinct()
            .order_by(Job.system_name)
            .all()
        )
    ]

    query_params = request.args.to_dict()
    if "per_page" not in query_params:
        query_params["per_page"] = str(per_page)
    if "job_name" not in query_params and job_name:
        query_params["job_name"] = job_name
    if "status" not in query_params and status:
        query_params["status"] = status
    if "triggered_by" not in query_params and triggered_by:
        query_params["triggered_by"] = triggered_by
    if "system_name" not in query_params and system_name:
        query_params["system_name"] = system_name
    if "is_audited" not in query_params and audited:
        query_params["is_audited"] = audited
    if "started_from" not in query_params and started_from:
        query_params["started_from"] = started_from
    if "started_to" not in query_params and started_to:
        query_params["started_to"] = started_to

    def _history_url(target_page):
        params = dict(query_params)
        params["page"] = target_page
        return url_for("main.jenkins_history", **params)

    prev_url = _history_url(page - 1) if page > 1 else None
    next_url = _history_url(page + 1) if page < total_pages else None
    return render_template(
        "jenkins_history.html",
        rows=rows,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        prev_url=prev_url,
        next_url=next_url,
        filters={
            "job_name": job_name,
            "status": status,
            "triggered_by": triggered_by,
            "system_name": system_name,
            "is_audited": audited,
            "started_from": _date_input_value(started_from),
            "started_to": _date_input_value(started_to),
        },
        system_names=system_names,
    )
