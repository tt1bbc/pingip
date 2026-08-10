import logging
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy import text

from .config import Config
from .models import ChangePlan, db

logger = logging.getLogger(__name__)

ZERO_CODE_TIMEOUT = (3.05, 20)


def _week_start_cst_api_date():
    now = datetime.now(timezone(timedelta(hours=8)))
    week_start = now - timedelta(days=now.weekday())
    return f"{week_start.year}-{week_start.month}-{week_start.day}"


def _parse_api_datetime(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Invalid ZeroCode datetime: %s", value)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _person_names(value):
    people = []
    for item in _as_list(value):
        if isinstance(item, dict):
            name = (item.get("name") or "").strip()
            if name:
                people.append(name)
        elif item:
            people.append(str(item).strip())
    return people


def _extract_jobs(change_service_list):
    jobs = []
    for item in _as_list(change_service_list):
        if not isinstance(item, dict):
            continue
        job_name = (item.get("jenkins_job") or "").strip()
        if not job_name:
            continue
        people = _person_names(item.get("job_person"))
        jobs.append(
            {
                "jenkins_job": job_name,
                "job_person": people,
            }
        )
    return jobs


def _extract_plan_window(plan_change_time):
    for item in _as_list(plan_change_time):
        if not isinstance(item, dict):
            continue
        start_at = _parse_api_datetime(item.get("plan_start_time"))
        end_at = _parse_api_datetime(item.get("plan_end_time"))
        if start_at and end_at:
            return start_at, end_at
    return None, None


def _build_payload(start_date=None, limit=100):
    return {
        "app_id": Config.ZERO_CODE_APP_ID,
        "entry_id": Config.ZERO_CODE_ENTRY_ID,
        "limit": limit,
        "fields": [
            "creator",
            "number",
            "plan_change_time",
            "is_system_change",
            "system_name_all",
            "change_service_list",
        ],
        "filter": {
            "rel": "and",
            "cond": [
                {
                    "field": "createTime",
                    "type": "datetime",
                    "method": "range",
                    "value": [start_date or _week_start_cst_api_date(), None],
                },
                {
                    "field": "is_system_change",
                    "type": "radiogroup",
                    "method": "eq",
                    "value": ["是"],
                },
                {
                    "field": "flowState",
                    "type": "flowstate",
                    "method": "eq",
                    "value": [0],
                },
            ],
        },
    }


def ensure_change_plan_source_id_column():
    ChangePlan.__table__.create(db.engine, checkfirst=True)
    if db.engine.dialect.name == "postgresql":
        db.session.execute(
            text("ALTER TABLE change_plans ADD COLUMN IF NOT EXISTS source_id VARCHAR(128)")
        )
        db.session.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_change_plans_source_id "
                "ON change_plans (source_id)"
            )
        )
        db.session.commit()


def fetch_zerocode_change_plans(start_date=None, limit=100):
    headers = {
        "Authorization": f"Bearer {Config.ZERO_CODE_BEARER_TOKEN}",
        "Accept": "*/*",
        "Content-Type": "application/json",
    }
    if Config.ZERO_CODE_COOKIE:
        headers["Cookie"] = Config.ZERO_CODE_COOKIE

    response = requests.post(
        Config.ZERO_CODE_DATA_LIST_URL,
        json=_build_payload(start_date, limit),
        headers=headers,
        timeout=ZERO_CODE_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get("data") or []


def normalize_zerocode_change_plan(item):
    source_id = (item.get("_id") or "").strip()
    change_number = (item.get("number") or source_id).strip()
    creator = item.get("creator") if isinstance(item.get("creator"), dict) else {}
    requester = (creator.get("name") or "").strip()
    system_name = (item.get("system_name_all") or "").strip()
    plan_start_at, plan_end_at = _extract_plan_window(item.get("plan_change_time"))
    jenkins_jobs = _extract_jobs(item.get("change_service_list"))

    if not source_id or not change_number or not requester or not system_name:
        return None
    if not plan_start_at or not plan_end_at:
        return None

    return {
        "source_id": source_id,
        "change_number": change_number,
        "requester": requester,
        "system_name": system_name,
        "plan_start_at": plan_start_at,
        "plan_end_at": plan_end_at,
        "jenkins_jobs": jenkins_jobs,
    }


def sync_zerocode_change_plans(start_date=None, limit=100):
    ensure_change_plan_source_id_column()
    raw_items = fetch_zerocode_change_plans(start_date, limit)
    created = 0
    updated = 0
    skipped = 0

    for raw_item in raw_items:
        data = normalize_zerocode_change_plan(raw_item)
        if not data:
            skipped += 1
            continue

        plan = ChangePlan.query.filter_by(source_id=data["source_id"]).first()
        if not plan:
            plan = ChangePlan.query.filter_by(change_number=data["change_number"]).first()

        if plan:
            for key, value in data.items():
                setattr(plan, key, value)
            plan.updated_at = datetime.now(timezone.utc)
            updated += 1
        else:
            db.session.add(ChangePlan(**data))
            created += 1

    db.session.commit()
    logger.info(
        "ZeroCode change plan sync complete: fetched=%s created=%s updated=%s skipped=%s",
        len(raw_items),
        created,
        updated,
        skipped,
    )
    return {
        "fetched": len(raw_items),
        "created": created,
        "updated": updated,
        "skipped": skipped,
    }
