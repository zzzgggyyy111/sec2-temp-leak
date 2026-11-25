from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from tmpleak import life


def _policy(base: Path, **overrides: Any) -> dict[str, Any]:
    policy: dict[str, Any] = {
        "base_dir": str(base),
        "prefix": "preview-",
        "entropy": 16,
        "mode": 0o700,
        "ttl": 300,
        "cleanup_on_error": True,
    }
    policy.update(overrides)
    return policy


def test_create_uses_unpredictable_directory_names(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    names = [Path(life.create(str(tmp_path), 16, policy)).name for _ in range(3)]
    suffixes = [name.removeprefix("preview-") for name in names]

    assert len(set(names)) == 3
    assert all(not suffix.isdigit() for suffix in suffixes)
    assert all(len(suffix) >= 24 for suffix in suffixes)


def test_use_cleans_after_a_normal_finish(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    path = life.create(str(tmp_path), 16, policy)

    def work(temporary: str) -> str:
        Path(temporary, "frame.bin").write_bytes(b"rendered-frame")
        return "done"

    report = life.use(path, work)

    assert report["ok"] is True
    assert report["removed"] is True
    assert not Path(path).exists()


def test_use_cleans_after_an_exception(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    path = life.create(str(tmp_path), 16, policy)

    def fail(temporary: str) -> None:
        Path(temporary, "frame.bin").write_bytes(b"partial")
        raise RuntimeError("renderer crashed")

    try:
        report = life.use(path, fail)
    except RuntimeError:
        report = None

    assert report is not None
    assert report["ok"] is False
    assert not Path(path).exists()


def test_directory_permissions_are_owner_only(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    private = life.create(str(tmp_path), 16, policy)
    wide = tmp_path / "wide"
    wide.mkdir()
    try:
        os.chmod(wide, 0o755)
    except OSError:
        pass

    assert life.check_mode(private, policy) is True
    assert life.check_mode(wide, policy) is False


def test_batch_tasks_use_independent_directories(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    tasks = [
        {"id": "first", "data": b"alpha"},
        {"id": "second", "data": b"beta"},
    ]

    result = life.leak_batch(tasks, policy)

    assert result["ok"] is True
    paths = [task["path"] for task in result["tasks"]]
    assert len(set(paths)) == 2
    assert all(task["removed"] is True for task in result["tasks"])
    assert all(not Path(path).exists() for path in paths)