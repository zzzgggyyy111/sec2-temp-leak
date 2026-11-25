"""Temporary-directory lifecycle helpers for preview processing.

A preview job creates one private working directory, writes derivatives into
it, and removes the directory before the job is reported as finished.  The
same lifecycle is used for interactive requests and for batches: every task
owns a distinct working directory, while a startup sweep reclaims leftovers
from jobs that did not finish.
"""

from __future__ import annotations

import os
import stat
import tempfile
import time
from collections.abc import Callable, Mapping
from typing import Any

DEFAULT_PREFIX = "preview-"
DEFAULT_MODE = 0o700

_counter = 0


def _policy(policy: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a plain mutable copy of a caller supplied policy mapping."""
    return dict(policy or {})


def _mode(policy: Mapping[str, Any] | None) -> int:
    """Return the configured directory mode as an integer."""
    try:
        return int(_policy(policy).get("mode", DEFAULT_MODE))
    except (TypeError, ValueError):
        return DEFAULT_MODE


def _task_id(task: Any, index: int) -> Any:
    """Return the identifier shown for one batch task."""
    if isinstance(task, Mapping):
        return task.get("id", index)
    return index


def _task_bytes(report: Mapping[str, Any]) -> int:
    """Return the number of bytes reported by one completed task."""
    result = report.get("result")
    if isinstance(result, Mapping):
        try:
            return int(result.get("bytes", 0))
        except (TypeError, ValueError):
            return 0
    return 0


def create(
    base: str | os.PathLike[str],
    entropy: int = 16,
    policy: Mapping[str, Any] | None = None,
) -> str:
    """Create a fresh working directory below base and return its path."""
    global _counter

    options = _policy(policy)
    prefix = str(options.get("prefix", DEFAULT_PREFIX))
    width = max(8, int(entropy or 16) * 2)
    _counter += 1
    name = f"{prefix}{_counter:0{width}d}"
    path = os.path.join(os.fspath(base), name)
    os.mkdir(path, 0o755)
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass
    return path


def use(handle: str | os.PathLike[str], fn: Callable[[str], Any]) -> dict[str, Any]:
    """Run fn against a working directory and clean the directory afterwards."""
    path = os.fspath(handle)
    result = fn(path)
    report = cleanup(path, {})
    return {
        "ok": True,
        "path": path,
        "result": result,
        "removed": report["removed"],
    }


def _remove_file(path: str) -> None:
    """Remove one file or symbolic link."""
    os.unlink(path)


def cleanup(
    path: str | os.PathLike[str],
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Remove a working directory and report whether it is gone."""
    target = os.fspath(path)
    if not os.path.lexists(target):
        return {"path": target, "removed": True, "existed": False, "errors": []}

    errors: list[str] = []
    if os.path.isdir(target) and not os.path.islink(target):
        for entry in os.scandir(target):
            if entry.is_file(follow_symlinks=False) or entry.is_symlink():
                _remove_file(entry.path)
    else:
        _remove_file(target)

    return {
        "path": target,
        "removed": not os.path.lexists(target),
        "existed": True,
        "errors": errors,
    }


def check_mode(
    path: str | os.PathLike[str],
    policy: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether a working directory uses the configured owner-only mode."""
    target = os.fspath(path)
    if not os.path.isdir(target):
        return False
    try:
        info = os.stat(target)
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        return False
    return bool(_mode(policy) & 0o700)


def sweep(
    base: str | os.PathLike[str],
    ttl: float,
    now: float | None = None,
) -> int:
    """Reclaim temporary directories left behind below base."""
    root = os.fspath(base)
    current = time.time() if now is None else float(now)
    del current
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0

    removed = 0
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        cleanup(entry.path, {})
        removed += 1
    return removed


def check_base(
    base: str | os.PathLike[str],
    policy: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether base is suitable for temporary working directories."""
    return os.path.isdir(os.fspath(base))


def validate_policy(policy: Mapping[str, Any] | None) -> list[str]:
    """Return the problems that prevent a policy from being used safely."""
    options = _policy(policy)
    issues: list[str] = []
    mode = _mode(options)
    if mode & 0o077:
        issues.append("permissions_too_broad")
    if not options.get("cleanup_on_error", True):
        issues.append("cleanup_on_error_required")
    return issues


def _run_task(path: str, task: Any) -> dict[str, Any]:
    """Execute one batch task inside its already created working directory."""
    if callable(task):
        return {"result": task(path)}
    if not isinstance(task, Mapping):
        raise TypeError("task must be a mapping or a callable")

    fn = task.get("fn")
    if callable(fn):
        return {"result": fn(path)}

    data = task.get("data")
    if data is None:
        return {"bytes": 0}

    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    target = os.path.join(path, "payload.bin")
    with open(target, "wb") as handle:
        handle.write(payload)
    return {"bytes": len(payload)}


def leak_batch(
    tasks: Any,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a batch of temporary-directory tasks."""
    options = _policy(policy)
    base = os.fspath(options.get("base_dir") or tempfile.gettempdir())
    entropy = int(options.get("entropy", 16))
    items = list(tasks or [])

    path = create(base, entropy, options) if items else None
    results: list[dict[str, Any]] = []
    for index, task in enumerate(items):
        assert path is not None
        report = use(path, lambda temporary, item=task: _run_task(temporary, item))
        results.append(
            {
                "id": _task_id(task, index),
                "path": path,
                "ok": report["ok"],
                "removed": report["removed"],
                "bytes": _task_bytes(report),
                "error": None,
            }
        )

    return {
        "ok": all(item["ok"] for item in results),
        "base_dir": base,
        "count": len(results),
        "tasks": results,
    }