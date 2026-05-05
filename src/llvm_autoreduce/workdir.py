"""Work directory management for issue reduction tasks."""

import json
import logging
import shutil
from pathlib import Path

from .config import WORK_ROOT

log = logging.getLogger(__name__)

TASKS_DIR = WORK_ROOT / "tasks"


def create(issue_id):
    path = TASKS_DIR / str(issue_id)
    path.mkdir(parents=True, exist_ok=True)
    log.info("workdir created: %s", path)
    return path


def read(filepath):
    return Path(filepath).read_text()


def read_json(filepath):
    return json.loads(Path(filepath).read_text())


def write(filepath, content):
    Path(filepath).write_text(content)


def write_json(filepath, obj):
    Path(filepath).write_text(json.dumps(obj, indent=2))


def cleanup(issue_id):
    path = TASKS_DIR / str(issue_id)
    if path.exists():
        shutil.rmtree(path)
        log.info("workdir cleaned: %s", path)
