from flask import Blueprint, render_template, request, jsonify, current_app, url_for
import netaddr
import asyncio
import aioping
import jenkins
import requests
import logging
from xml.etree import ElementTree as ET
from urllib.parse import quote
import time
from datetime import datetime, timezone, timedelta
import json
import threading
from .config import Config
from .models import Job, JobBuildHistory, db
from .jenkins_sync import sync_jobs

main = Blueprint("main", __name__)
logger = logging.getLogger(__name__)


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
    return render_template("base.html")


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
    jobsname = [job.name for job in jobs]
    return render_template("jenkins.html", jobs=jobsname)


def _fetch_job_parameters(job_name):
    job_config_url = f"{Config.JENKINS_URL}/job/{quote(job_name)}/config.xml"
    response = requests.get(job_config_url, auth=(Config.JENKINS_USER, Config.JENKINS_API_TOKEN))
    parameters = []
    build_parameters = {}
    if response.status_code == 200:
        root = ET.fromstring(response.text)
        for param_def in root.findall(".//parameterDefinitions/*"):
            name = param_def.find("name").text if param_def.find("name") is not None else "Unknown"
            default_value = (
                param_def.find(".//defaultValue").text
                if param_def.find(".//defaultValue") is not None
                else ""
            )
            parameters.append({"name": name, "default": default_value})
            build_parameters[name] = default_value
    else:
        logger.warning("Failed to fetch job configuration: %s", response.status_code)
    return parameters, build_parameters


@main.route("/jenkins/get-job-parameters-batch", methods=["POST"])
def get_job_parameters_batch():
    job_names = request.json.get("job_names")
    if not job_names or not isinstance(job_names, list):
        return jsonify({"error": "job_names list is required"}), 400

    results = {}
    for job_name in job_names:
        try:
            parameters, _ = _fetch_job_parameters(job_name)
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


def _serialize_params(params):
    if params is None:
        return None
    try:
        return dict(params)
    except Exception:
        return params


def _record_build_start(job_name, build_number, build_parameters, started_at, jenkins_url=None):
    history = JobBuildHistory(
        job_name=job_name,
        build_number=build_number,
        status="RUNNING",
        triggered_by=None,
        parameters=_serialize_params(build_parameters),
        started_at=started_at,
        finished_at=None,
        duration_ms=None,
        jenkins_url=jenkins_url,
    )
    db.session.add(history)
    db.session.commit()
    return history


def _record_build_finish(history, status, finished_at, duration_ms=None, jenkins_url=None):
    history.status = status
    history.finished_at = finished_at
    history.duration_ms = duration_ms
    if jenkins_url:
        history.jenkins_url = jenkins_url
    db.session.commit()


def _extract_triggered_by(build_data):
    actions = build_data.get("actions") or []
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


def _trigger_build_track(job_name, build_parameters, history):
    start_time = datetime.now(timezone.utc)
    history.status = "RUNNING"
    history.parameters = _serialize_params(build_parameters)
    history.started_at = start_time
    history.triggered_by = history.triggered_by or Config.JENKINS_USER
    db.session.commit()

    build_url = f"{Config.JENKINS_URL}/job/{quote(job_name)}/buildWithParameters"
    bresponse = requests.post(
        build_url,
        auth=(Config.JENKINS_USER, Config.JENKINS_API_TOKEN),
        params=build_parameters,
        allow_redirects=False,
    )
    if bresponse.status_code not in (201, 302):
        _record_build_finish(
            history=history,
            status="FAILURE",
            finished_at=datetime.now(timezone.utc),
            duration_ms=None,
            jenkins_url=None,
        )
        return False

    queue_url = bresponse.headers.get("Location")
    if not queue_url:
        _record_build_finish(
            history=history,
            status="FAILURE",
            finished_at=datetime.now(timezone.utc),
            duration_ms=None,
            jenkins_url=None,
        )
        return False

    queue_api = f"{queue_url}api/json"
    start_poll = time.time()
    build_number = None
    build_url_final = None

    while time.time() - start_poll < 600:
        q = requests.get(queue_api, auth=(Config.JENKINS_USER, Config.JENKINS_API_TOKEN))
        if q.status_code != 200:
            _record_build_finish(
                history=history,
                status="FAILURE",
                finished_at=datetime.now(timezone.utc),
                duration_ms=None,
                jenkins_url=None,
            )
            return False
        qdata = q.json()
        if qdata.get("cancelled"):
            _record_build_finish(
                history=history,
                status="FAILURE",
                finished_at=datetime.now(timezone.utc),
                duration_ms=None,
                jenkins_url=None,
            )
            return False
        executable = qdata.get("executable")
        if executable and executable.get("number") is not None:
            build_number = executable.get("number")
            build_url_final = executable.get("url")
            history.build_number = build_number
            if build_url_final:
                history.jenkins_url = build_url_final
            db.session.commit()
            break
        time.sleep(5)

    if build_number is None:
        _record_build_finish(
            history=history,
            status="FAILURE",
            finished_at=datetime.now(timezone.utc),
            duration_ms=None,
            jenkins_url=build_url_final,
        )
        return False

    if not build_url_final:
        build_url_final = f"{Config.JENKINS_URL}/job/{quote(job_name)}/{build_number}/"

    build_api = f"{build_url_final}api/json"
    while time.time() - start_poll < 600:
        b = requests.get(build_api, auth=(Config.JENKINS_USER, Config.JENKINS_API_TOKEN))
        if b.status_code != 200:
            _record_build_finish(
                history=history,
                status="FAILURE",
                finished_at=datetime.now(timezone.utc),
                duration_ms=None,
                jenkins_url=build_url_final,
            )
            return False
        bdata = b.json()
        if history.triggered_by == Config.JENKINS_USER:
            triggered_by = _extract_triggered_by(bdata)
            if triggered_by and triggered_by != history.triggered_by:
                history.triggered_by = triggered_by
                db.session.commit()
        if not bdata.get("building", True):
            result = bdata.get("result") or "FAILURE"
            _record_build_finish(
                history=history,
                status=result,
                finished_at=datetime.now(timezone.utc),
                duration_ms=bdata.get("duration"),
                jenkins_url=build_url_final,
            )
            return result == "SUCCESS"
        time.sleep(5)

    _record_build_finish(
        history=history,
        status="FAILURE",
        finished_at=datetime.now(timezone.utc),
        duration_ms=None,
        jenkins_url=build_url_final,
    )
    return False


def _run_batch_build(app, job_names):
    with app.app_context():
        now_utc = datetime.now(timezone.utc)
        pending_map = {}
        for idx, job_name in enumerate(job_names):
            history = JobBuildHistory(
                job_name=job_name,
                build_number=-(int(time.time()) + idx + 1),
                status="PENDING",
                triggered_by=None,
                parameters=None,
                started_at=now_utc,
                finished_at=None,
                duration_ms=None,
                jenkins_url=None,
            )
            db.session.add(history)
            pending_map[job_name] = history
        db.session.commit()

        for job_name in job_names:
            try:
                _, build_parameters = _fetch_job_parameters(job_name)
                history = pending_map.get(job_name)
                if not history:
                    history = _record_build_start(
                        job_name=job_name,
                        build_number=-(int(time.time())),
                        build_parameters=None,
                        started_at=datetime.now(timezone.utc),
                        jenkins_url=None,
                    )
                ok = _trigger_build_track(job_name, build_parameters, history)
                if not ok:
                    break
            except Exception:
                now_utc = datetime.now(timezone.utc)
                history = pending_map.get(job_name)
                if not history:
                    history = _record_build_start(
                        job_name=job_name,
                        build_number=-(int(time.time())),
                        build_parameters=None,
                        started_at=now_utc,
                        jenkins_url=None,
                    )
                _record_build_finish(
                    history=history,
                    status="FAILURE",
                    finished_at=now_utc,
                    duration_ms=None,
                    jenkins_url=None,
                )
                break


@main.route("/jenkins/build-batch", methods=["POST"])
def build_jobs_batch():
    job_names = request.json.get("job_names")
    if not job_names or not isinstance(job_names, list):
        return jsonify({"error": "job_names list is required"}), 400

    app = current_app._get_current_object()
    thread = threading.Thread(target=_run_batch_build, args=(app, job_names), daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": "Batch build started"})


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


@main.route("/jenkins/history", methods=["GET"])
def jenkins_history():
    job_name = (request.args.get("job_name") or "").strip()
    status = (request.args.get("status") or "").strip()
    audited = (request.args.get("is_audited") or "").strip().lower()
    system_name = (request.args.get("system_name") or "").strip()
    started_from = (request.args.get("started_from") or "").strip()
    started_to = (request.args.get("started_to") or "").strip()
    page = max(int(request.args.get("page", 1)), 1)
    per_page = max(min(int(request.args.get("per_page", 10)), 100), 1)

    query = JobBuildHistory.query.outerjoin(Job, JobBuildHistory.job_name == Job.name)
    if job_name:
        query = query.filter(JobBuildHistory.job_name.ilike(f"%{job_name}%"))
    if status:
        query = query.filter(JobBuildHistory.status == status)
    if system_name:
        query = query.filter(Job.system_name.ilike(f"%{system_name}%"))
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
        params = ""
        if item.parameters is not None:
            try:
                params = json.dumps(item.parameters, ensure_ascii=False)
            except Exception:
                params = str(item.parameters)
        rows.append(
            {
                "job_name": item.job_name,
                "build_number": item.build_number,
                "status": item.status,
                "triggered_by": item.triggered_by or "",
                "parameters": params,
                "started_at": _to_cst_str(item.started_at),
            }
        )

    total_pages = max((total + per_page - 1) // per_page, 1)

    query_params = request.args.to_dict()
    if "per_page" not in query_params:
        query_params["per_page"] = str(per_page)
    if "job_name" not in query_params and job_name:
        query_params["job_name"] = job_name
    if "status" not in query_params and status:
        query_params["status"] = status
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
            "system_name": system_name,
            "is_audited": audited,
            "started_from": started_from,
            "started_to": started_to,
        },
    )
