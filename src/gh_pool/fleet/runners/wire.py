from __future__ import annotations

import json
import logging

from gh_pool.fleet.runners.config import JOB_AVAILABLE, JOB_COMPLETED
from gh_pool.fleet.runners.models import to_int

log = logging.getLogger(__name__)


def read_jobs(raw_body: str) -> tuple[list[int], list[str]]:
    try:
        items = json.loads(raw_body or "[]")
    except json.JSONDecodeError:
        log.debug("не разобрал тело сообщения")
        return [], []
    if not isinstance(items, list):
        return [], []

    offered: list[int] = []
    retired: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("messageType") or "")
        request_id = item.get("runnerRequestId")
        log.info(
            "  %s: %s (run %s, request %s)",
            kind or "?",
            item.get("jobDisplayName"),
            item.get("workflowRunId"),
            request_id,
        )
        offer = to_int(request_id)
        if offer and JOB_AVAILABLE in kind:
            offered.append(offer)
        if JOB_COMPLETED in kind and item.get("runnerName"):
            retired.append(str(item["runnerName"]))
    return offered, retired
