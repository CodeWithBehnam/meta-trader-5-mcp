"""Append-only JSONL audit trail for trading tool calls."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def make_record(
    *,
    timestamp: datetime,
    tool: str,
    request: Mapping[str, Any],
    check: Mapping[str, Any] | None,
    send: Mapping[str, Any] | None,
    outcome: str,
) -> dict[str, Any]:
    return {
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "tool": tool,
        "request": dict(request),
        "check": dict(check) if check is not None else None,
        "send": dict(send) if send is not None else None,
        "outcome": outcome,
    }


def append_record(path: Path, record: Mapping[str, Any]) -> None:
    """Append one line durably. Failure is raised: an unauditable trade must not proceed.

    The directory is created 0700 and the file 0600 because the log records account
    activity. An existing, looser file is tightened.
    """
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
    line = json.dumps(record, default=str, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        if os.fstat(fd).st_mode & 0o077:
            os.fchmod(fd, 0o600)
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
