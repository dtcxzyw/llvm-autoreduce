"""Work directory management for issue reduction tasks."""

import json
import logging
from pathlib import Path

from .config import WORK_ROOT

log = logging.getLogger(__name__)

TASKS_DIR = WORK_ROOT / "tasks"


def create(issue_id):
    path = TASKS_DIR / str(issue_id)
    path.mkdir(parents=True, exist_ok=True)
    log.info("workdir created: %s", path)
    return path


# ACCEPTED RISK (F33): read and read_json enforce per-call size caps to prevent
# the daemon process from OOM-ing on unexpectedly large agent-generated files.
# The caps are intentionally generous for structured outputs; report.md (the
# largest expected output — containing reduced IR) is capped at 200 KB, while
# JSON metadata files are capped at 100 KB. Exceeding these limits indicates
# an anomalous agent output and is treated as a fatal error for the issue.
_MAX_READ_BYTES = 204800
_MAX_JSON_BYTES = 102400


def read(filepath):
    p = Path(filepath)
    if p.stat().st_size > _MAX_READ_BYTES:
        raise ValueError(f"{filepath}: {p.stat().st_size} bytes exceeds read limit {_MAX_READ_BYTES}")
    return p.read_text()


def read_json(filepath):
    p = Path(filepath)
    if p.stat().st_size > _MAX_JSON_BYTES:
        raise ValueError(f"{filepath}: {p.stat().st_size} bytes exceeds json read limit {_MAX_JSON_BYTES}")
    return json.loads(p.read_text())


def write(filepath, content):
    Path(filepath).write_text(content)


def write_json(filepath, obj):
    Path(filepath).write_text(json.dumps(obj, indent=2))


