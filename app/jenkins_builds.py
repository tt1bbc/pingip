import logging
import time
from datetime import datetime, timezone
from urllib.parse import quote
from xml.etree import ElementTree as ET

from .config import Config
from .jenkins_client import jenkins_get, jenkins_post
from .models import ChangePlanBuildSchedule, JobBuildHistory, db

logger = logging.getLogger(__name__)

QUEUE_POLL_TIMEOUT_SECONDS = 600
BUILD_POLL_TIMEOUT_SECONDS = 1800
POLL_INTERVAL_SECONDS = 5


def fetch_job_parameters(job_name):
    job_config_url = f"{Config.JENKINS_URL}/job/{quote(job_name)}/config.xml"
    response = jenkins_get(job_config_url)
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


def fetch_latest_build_number(job_name):
    job_api = f"{Config.JENKINS_URL}/job/{quote(job_name)}/api/json"
    response = jenkins_get(job_api, params={"tree": "lastBuild[number]"})
    if response.status_code != 200:
        logger.warning(
            "Failed to fetch latest Jenkins build number for %s: %s",
            job_name,
            response.status_code,
        )
        return None
    data = response.json() or {}
    last_build = data.get("lastBuild") or {}
    return last_build.get("number")


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


def _safe_jenkins_get(url, **kwargs):
    try:
        return jenkins_get(url, **kwargs)
    except Exception:
        logger.warning("Failed to poll Jenkins API: %s", url, exc_info=True)
        return None


def _record_from_build_data(history, build_data, build_url_final):
    result = build_data.get("result") or "UNKNOWN"
    _record_build_finish(
        history=history,
        status=result,
        finished_at=datetime.now(timezone.utc),
        duration_ms=build_data.get("duration"),
        jenkins_url=build_url_final,
    )
    return result == "SUCCESS"


def _cancel_pending_jobs(job_names, pending_map):
    now_utc = datetime.now(timezone.utc)
    for job_name in job_names:
        history = pending_map.get(job_name)
        if not history or history.status != "PENDING":
            continue
        history.status = "CANCELLED"
        history.finished_at = now_utc
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


def _build_trigger_requests(job_name, build_parameters):
    if build_parameters:
        return [
            (
                f"{Config.JENKINS_URL}/job/{quote(job_name)}/buildWithParameters",
                {"params": build_parameters},
            )
        ]
    return [
        (f"{Config.JENKINS_URL}/job/{quote(job_name)}/build", {}),
        (f"{Config.JENKINS_URL}/job/{quote(job_name)}/buildWithParameters", {}),
    ]


def _trigger_build(job_name, build_parameters):
    last_response = None
    for build_url, request_kwargs in _build_trigger_requests(job_name, build_parameters):
        response = jenkins_post(
            build_url,
            allow_redirects=False,
            **request_kwargs,
        )
        if response.status_code in (201, 302):
            return response

        last_response = response
        body = response.text[:500] if response.text else ""
        logger.warning(
            "Failed to trigger Jenkins job %s via %s: status=%s body=%s",
            job_name,
            build_url,
            response.status_code,
            body,
        )

    return last_response


def _trigger_build_track(job_name, build_parameters, history):
    start_time = datetime.now(timezone.utc)
    history.status = "RUNNING"
    history.parameters = _serialize_params(build_parameters)
    history.started_at = start_time
    history.triggered_by = history.triggered_by or Config.JENKINS_USER
    db.session.commit()

    bresponse = _trigger_build(job_name, build_parameters)
    if bresponse is None or bresponse.status_code not in (201, 302):
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

    while time.time() - start_poll < QUEUE_POLL_TIMEOUT_SECONDS:
        q = _safe_jenkins_get(queue_api)
        if q is None or q.status_code != 200:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        qdata = q.json()
        if qdata.get("cancelled"):
            _record_build_finish(
                history=history,
                status="CANCELLED",
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
        time.sleep(POLL_INTERVAL_SECONDS)

    if build_number is None:
        _record_build_finish(
            history=history,
            status="TIMEOUT",
            finished_at=datetime.now(timezone.utc),
            duration_ms=None,
            jenkins_url=build_url_final,
        )
        return False

    if not build_url_final:
        build_url_final = f"{Config.JENKINS_URL}/job/{quote(job_name)}/{build_number}/"

    build_api = f"{build_url_final}api/json"
    build_poll = time.time()
    while time.time() - build_poll < BUILD_POLL_TIMEOUT_SECONDS:
        b = _safe_jenkins_get(build_api)
        if b is None or b.status_code != 200:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        bdata = b.json()
        if history.triggered_by == Config.JENKINS_USER:
            triggered_by = _extract_triggered_by(bdata)
            if triggered_by and triggered_by != history.triggered_by:
                history.triggered_by = triggered_by
                db.session.commit()
        if not bdata.get("building", True):
            return _record_from_build_data(history, bdata, build_url_final)
        time.sleep(POLL_INTERVAL_SECONDS)

    b = _safe_jenkins_get(build_api)
    if b is not None and b.status_code == 200:
        bdata = b.json()
        if not bdata.get("building", True):
            return _record_from_build_data(history, bdata, build_url_final)

    _record_build_finish(
        history=history,
        status="TIMEOUT",
        finished_at=datetime.now(timezone.utc),
        duration_ms=None,
        jenkins_url=build_url_final,
    )
    return False


def run_batch_build(job_names):
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

    for index, job_name in enumerate(job_names):
        try:
            _, build_parameters = fetch_job_parameters(job_name)
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
                _cancel_pending_jobs(job_names[index + 1 :], pending_map)
                break
        except Exception:
            logger.exception("Failed to run Jenkins build for job %s", job_name)
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
            _cancel_pending_jobs(job_names[index + 1 :], pending_map)
            break


def _set_schedule_job_state(schedule, job_name, **updates):
    job_states = []
    for state in schedule.job_states or []:
        item = dict(state)
        if item.get("job_name") == job_name:
            item.update(updates)
        job_states.append(item)
    schedule.job_states = job_states
    schedule.updated_at = datetime.now(timezone.utc)
    db.session.commit()


def _set_schedule_status(schedule, status, **updates):
    schedule.status = status
    schedule.updated_at = datetime.now(timezone.utc)
    for key, value in updates.items():
        setattr(schedule, key, value)
    db.session.commit()


def run_change_plan_build_schedule(schedule_id):
    schedule = db.session.get(ChangePlanBuildSchedule, schedule_id)
    if not schedule:
        logger.warning("Change plan build schedule not found: %s", schedule_id)
        return
    if schedule.status == "CANCELLED":
        return

    now_utc = datetime.now(timezone.utc)
    _set_schedule_status(schedule, "RUNNING", started_at=now_utc, message="定时任务执行中")

    for state in schedule.job_states or []:
        db.session.refresh(schedule)
        job_name = state.get("job_name")
        if not job_name:
            continue
        if schedule.status == "CANCELLED":
            _set_schedule_job_state(schedule, job_name, status="CANCELLED", message="任务已取消")
            continue

        baseline = state.get("baseline_build_number")
        try:
            latest_number = fetch_latest_build_number(job_name)
            if latest_number is not None and baseline is not None and latest_number > baseline:
                _set_schedule_job_state(
                    schedule,
                    job_name,
                    status="SKIPPED_MANUAL",
                    latest_build_number=latest_number,
                    message=f"已手动构建，当前最后构建号 {latest_number} 大于创建时 {baseline}",
                )
                continue

            _set_schedule_job_state(schedule, job_name, status="RUNNING", message="构建中")
            _, build_parameters = fetch_job_parameters(job_name)
            history = _record_build_start(
                job_name=job_name,
                build_number=-(int(time.time())),
                build_parameters=build_parameters,
                started_at=datetime.now(timezone.utc),
                jenkins_url=None,
            )
            ok = _trigger_build_track(job_name, build_parameters, history)
            _set_schedule_job_state(
                schedule,
                job_name,
                status="SUCCESS" if ok else "FAILURE",
                build_number=history.build_number,
                message="构建成功" if ok else f"构建结束，状态 {history.status}",
            )
        except Exception as exc:
            logger.exception("Failed to run scheduled Jenkins build for job %s", job_name)
            _set_schedule_job_state(
                schedule,
                job_name,
                status="FAILURE",
                message=str(exc),
            )

    final_schedule = db.session.get(ChangePlanBuildSchedule, schedule_id)
    states = final_schedule.job_states or []
    if final_schedule.status != "CANCELLED":
        if any(state.get("status") == "FAILURE" for state in states):
            status = "FAILURE"
            message = "部分 Job 构建失败"
        else:
            status = "SUCCESS"
            message = "定时任务执行完成"
        _set_schedule_status(
            final_schedule,
            status,
            finished_at=datetime.now(timezone.utc),
            message=message,
        )
