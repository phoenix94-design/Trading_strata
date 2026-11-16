import logging

import requests

log = logging.getLogger(__name__)


def ping(url: str | None, status: str = "", body: str = ""):
    """Healthchecks.io ping. status "" = alive, "fail" = alert now. Never raises."""
    if not url:
        return
    try:
        requests.post(url.rstrip("/") + (f"/{status}" if status else ""), data=body[:10000].encode(), timeout=10)
    except requests.RequestException as e:
        log.warning("healthcheck ping failed: %s", e)
