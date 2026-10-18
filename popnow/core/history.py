"""Task history on disk: one history.json in the add-on's own user directory (not in the .blend, no keys and no request bodies)."""

from __future__ import annotations

import json
import os
import tempfile

from .jobs import Job

MAX_ENTRIES = 60


def load(path: str) -> list[Job]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    jobs = []
    for d in data.get("jobs", []) if isinstance(data, dict) else []:
        try:
            jobs.append(Job.from_dict(d))
        except (TypeError, KeyError, ValueError):
            continue  # skip entries from older versions or damaged entries
    return jobs


def save(path: str, jobs: list[Job]) -> None:
    """Atomic write: write a temporary file, then rename, so a Blender crash never leaves half a JSON file"""
    keep = sorted(jobs, key=lambda j: j.created_at, reverse=True)[:MAX_ENTRIES]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".history-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "jobs": [j.to_dict() for j in keep]}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
