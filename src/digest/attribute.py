"""Stage S3: attribute work units to projects (DESIGN_V2.md 3.3).

Deterministic resolution, in priority order:
1. explicit human overrides (unit_overrides_v2) -- always win;
2. cwd prefix matchers (longest prefix wins);
3. repo-name / branch / keyword matchers against the unit's text and files;
4. a session cwd with no matching project creates a *provisional* project
   named after the directory, with a cwd matcher, so the same work never
   lands anywhere else tomorrow. It is surfaced in the digest until
   confirmed or reassigned (never silently invented as a different name
   on a later run: the slug is derived from the cwd).
5. no cwd at all -> the built-in `_inbox` project.

No LLM call: the session cwd is present on effectively every unit (it is in
every Claude event and every Codex session_meta), which makes deterministic
attribution both cheaper and more predictable than the triage call sketched
in the original design.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

from .config import DigestConfig
from .store_v2 import SessionRecord, WorkStore, WorkUnit

logger = logging.getLogger(__name__)

INBOX_PROJECT_ID = "_inbox"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "project"


def normalize_path(value: str) -> str:
    return value.replace("\\", "/").rstrip("/").lower()


def ensure_baseline_projects(store: WorkStore, config: DigestConfig) -> None:
    """Idempotent seeding: the inbox plus one project per configured repo."""
    store.create_project(INBOX_PROJECT_ID, "Unattributed work", status="active")
    for repo_name, repo_path in config.repos.items():
        project_id = slugify(repo_name)
        store.create_project(
            project_id,
            repo_name,
            status="active",
            trello_scope=(repo_name == config.primary_repo),
        )
        store.add_matcher(project_id, "cwd_prefix", normalize_path(str(repo_path)))
        store.add_matcher(project_id, "repo_name", repo_name.lower())


def attribute_units(
    store: WorkStore,
    units: Iterable[WorkUnit],
    sessions_by_id: dict[str, SessionRecord],
) -> dict[str, int]:
    """Assign project_id to every unit. Returns counts per resolution kind."""
    matchers = store.list_matchers()
    cwd_matchers = [
        (m["pattern"], m["project_id"]) for m in matchers if m["kind"] == "cwd_prefix"
    ]
    # Longest prefix first so nested project dirs beat their parents.
    cwd_matchers.sort(key=lambda item: len(item[0]), reverse=True)
    text_matchers = [
        (m["pattern"], m["project_id"])
        for m in matchers
        if m["kind"] in {"repo_name", "keyword"}
    ]
    branch_matchers = [
        (m["pattern"], m["project_id"]) for m in matchers if m["kind"] == "branch_glob"
    ]

    counts = {"override": 0, "cwd": 0, "text": 0, "provisional": 0, "inbox": 0}
    for unit in units:
        session_id = unit.unit_key.rsplit(":", 1)[0]
        session = sessions_by_id.get(session_id)
        cwd = normalize_path(session.cwd) if session and session.cwd else ""

        project_id: str | None = store.get_override(unit.unit_key)
        method = "override" if project_id else ""

        if project_id is None and cwd:
            for prefix, candidate in cwd_matchers:
                if cwd.startswith(prefix):
                    project_id, method = candidate, "cwd"
                    break
        if project_id is None:
            haystack = " ".join([unit.intent.lower(), " ".join(unit.files).lower(), cwd])
            for pattern, candidate in text_matchers:
                if pattern in haystack:
                    project_id, method = candidate, "text"
                    break
        if project_id is None and session and session.git_branch:
            branch = session.git_branch.lower()
            for pattern, candidate in branch_matchers:
                if re.fullmatch(pattern.replace("*", ".*"), branch):
                    project_id, method = candidate, "text"
                    break
        if project_id is None:
            if cwd:
                project_id, method = _propose_from_cwd(store, cwd), "provisional"
            else:
                project_id, method = INBOX_PROJECT_ID, "inbox"

        store.set_unit_project(unit.unit_key, project_id)
        counts[method] += 1
    return counts


def _propose_from_cwd(store: WorkStore, cwd: str) -> str:
    name = cwd.rsplit("/", 1)[-1] or cwd
    project_id = slugify(name)
    if store.get_project(project_id) is None:
        store.create_project(project_id, name, status="provisional")
        logger.info("Proposed provisional project %r from cwd %s", project_id, cwd)
    store.add_matcher(project_id, "cwd_prefix", cwd, source="llm_proposal")
    return project_id


def retire_idle_projects(store: WorkStore, today: str, retire_after_days: int) -> list[str]:
    """Deterministic pause of projects with no work units for N days."""
    from datetime import date, timedelta

    paused: list[str] = []
    threshold = (date.fromisoformat(today) - timedelta(days=retire_after_days)).isoformat()
    for project in store.list_projects():
        if project.project_id == INBOX_PROJECT_ID or project.status != "active":
            continue
        last = store.last_activity_date(project.project_id)
        if last is not None and last < threshold:
            store.set_project_status(project.project_id, "paused")
            paused.append(project.project_id)
    return paused
