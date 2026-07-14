from __future__ import annotations

from pathlib import Path

import pytest

from conftest import write_config
from digest.attribute import (
    INBOX_PROJECT_ID,
    attribute_units,
    ensure_baseline_projects,
    retire_idle_projects,
    slugify,
)
from digest.config import load_config
from digest.store_v2 import SessionRecord, WorkStore, WorkUnit


@pytest.fixture()
def store(tmp_path: Path) -> WorkStore:
    s = WorkStore(tmp_path / "digest.sqlite")
    yield s
    s.close()


def _session(session_id: str, cwd: str | None, branch: str | None = None) -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        agent=session_id.split(":")[0],
        path="x",
        content_hash="h",
        title=None,
        cwd=cwd,
        git_branch=branch,
        started_at="2026-07-12T09:00:00-04:00",
        ended_at="2026-07-12T10:00:00-04:00",
        turn_count=1,
        parse_errors=0,
    )


def _unit(
    unit_key: str,
    intent: str = "do work",
    files: list[str] | None = None,
    work_date: str = "2026-07-12",
) -> WorkUnit:
    return WorkUnit(
        unit_key=unit_key,
        work_date=work_date,
        project_id=None,
        turn_ids=[],
        intent=intent,
        kind="other",
        outcome_claim="",
        status_claim="unknown",
        files=files or [],
        entities=[],
        claims=[],
        open_questions=[],
    )


def _seed(store: WorkStore, tmp_path: Path):
    config = load_config(
        write_config(tmp_path, repos={"my-api-repo": tmp_path / "repo-fastapi"})
    )
    ensure_baseline_projects(store, config)
    return config


def test_baseline_seeding_is_idempotent(store: WorkStore, tmp_path: Path):
    config = _seed(store, tmp_path)
    ensure_baseline_projects(store, config)
    projects = {p.project_id for p in store.list_projects()}
    assert projects == {INBOX_PROJECT_ID, "my-api-repo"}
    assert store.get_project("my-api-repo").trello_scope is True


def test_cwd_match_wins(store: WorkStore, tmp_path: Path):
    _seed(store, tmp_path)
    unit = _unit("claude:s1:1")
    store.replace_work_units("claude:s1", "2026-07-12", [unit])
    sessions = {"claude:s1": _session("claude:s1", str(tmp_path / "repo-fastapi" / "sub"))}
    counts = attribute_units(store, [unit], sessions)
    assert counts["cwd"] == 1
    assert store.list_units_for_date("2026-07-12")[0].project_id == "my-api-repo"


def test_override_beats_everything(store: WorkStore, tmp_path: Path):
    _seed(store, tmp_path)
    store.create_project("other", "Other")
    unit = _unit("claude:s1:1")
    store.replace_work_units("claude:s1", "2026-07-12", [unit])
    store.set_override("claude:s1:1", "other")
    sessions = {"claude:s1": _session("claude:s1", str(tmp_path / "repo-fastapi"))}
    counts = attribute_units(store, [unit], sessions)
    assert counts["override"] == 1
    assert store.list_units_for_date("2026-07-12")[0].project_id == "other"


def test_unknown_cwd_creates_stable_provisional_project(store: WorkStore, tmp_path: Path):
    _seed(store, tmp_path)
    unit = _unit("claude:s2:1")
    store.replace_work_units("claude:s2", "2026-07-12", [unit])
    sessions = {"claude:s2": _session("claude:s2", "C:\\projects\\LotusPetal")}
    counts = attribute_units(store, [unit], sessions)
    assert counts["provisional"] == 1
    project = store.get_project("lotuspetal")
    assert project is not None and project.status == "provisional"

    # Second run: same cwd now matches the stored matcher deterministically.
    unit2 = _unit("claude:s3:1")
    store.replace_work_units("claude:s3", "2026-07-12", [unit2])
    sessions["claude:s3"] = _session("claude:s3", "C:\\projects\\LotusPetal\\api")
    counts2 = attribute_units(store, [unit2], sessions)
    assert counts2["cwd"] == 1


def test_no_cwd_goes_to_inbox(store: WorkStore, tmp_path: Path):
    _seed(store, tmp_path)
    unit = _unit("codex:s4:1")
    store.replace_work_units("codex:s4", "2026-07-12", [unit])
    counts = attribute_units(store, [unit], {"codex:s4": _session("codex:s4", None)})
    assert counts["inbox"] == 1
    assert store.list_units_for_date("2026-07-12")[0].project_id == INBOX_PROJECT_ID


def test_retire_idle_projects(store: WorkStore, tmp_path: Path):
    _seed(store, tmp_path)
    store.create_project("stale", "Stale")
    unit = _unit("claude:s5:1", work_date="2026-06-01")
    store.replace_work_units("claude:s5", "2026-06-01", [unit])
    store.set_unit_project("claude:s5:1", "stale")
    paused = retire_idle_projects(store, "2026-07-12", retire_after_days=21)
    assert paused == ["stale"]
    # Projects with no units at all are left alone (freshly seeded).
    assert store.get_project("my-api-repo").status == "active"


def test_slugify():
    assert slugify("LotusPetal API") == "lotuspetal-api"
    assert slugify("---") == "project"
