import logging
import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import Config

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = (3.05, 15)
RETRY_STATUS_CODES = (429, 500, 502, 503, 504)

_local = threading.local()


def _build_session(retry_enabled):
    session = requests.Session()
    if retry_enabled:
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            status=2,
            backoff_factor=0.5,
            status_forcelist=RETRY_STATUS_CODES,
            allowed_methods=frozenset(["GET", "HEAD", "OPTIONS"]),
            raise_on_status=False,
        )
    else:
        retry = Retry(total=0, raise_on_status=False)

    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _get_session(retry_enabled):
    attr = "jenkins_retry_session" if retry_enabled else "jenkins_no_retry_session"
    session = getattr(_local, attr, None)
    if session is None:
        session = _build_session(retry_enabled)
        setattr(_local, attr, session)
    return session


def jenkins_request(method, url, *, timeout=DEFAULT_TIMEOUT, retry=True, **kwargs):
    kwargs.setdefault("auth", (Config.JENKINS_USER, Config.JENKINS_API_TOKEN))
    kwargs.setdefault("timeout", timeout)

    session = _get_session(retry)
    try:
        return session.request(method, url, **kwargs)
    except requests.RequestException:
        logger.warning("Jenkins request failed: %s %s", method.upper(), url, exc_info=True)
        raise


def jenkins_get(url, **kwargs):
    return jenkins_request("GET", url, retry=True, **kwargs)


def jenkins_post(url, **kwargs):
    return jenkins_request("POST", url, retry=False, **kwargs)
