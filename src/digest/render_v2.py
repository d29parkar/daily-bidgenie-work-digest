"""Stage S6: render the three artifacts from project state + verified units.

Night digest and morning brief are assembled deterministically: the prose in
them was already written (with citations) by the state stage, so assembling
in code keeps them grounded by construction and immune to render-time
hallucination. The Trello card is the one artifact where voice matters more
than assembly, so it is an LLM call whose system prompt is the user's actual
Trello skill + writing-style rules, loaded from the configured skill files
(falling back to vendored copies). If that call fails, the fallback card is
explicitly marked as raw so an off-voice card is never pasted by accident.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from .config import DigestConfig
from .llm_v2 import BaseLLM, LLMError, LLMUnavailable
from .store_v2 import ProjectState, WorkStore, WorkUnit

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
VENDORED_DIR = PROMPTS_DIR / "vendored"
TRELLO_MAX_OUTPUT_TOKENS = 700

VERDICT_BUCKETS = [
    ("corroborated_by_commit", "Landed (verified against commits)"),
    ("applied_by_harness", "Applied by the agent harness (verified diffs)"),
    ("uncommitted", "Implemented, not yet committed"),
    ("no_files", "Discovery / review (no file changes to verify)"),
    ("unverified", "Claimed, not verified"),
    ("contradicted", "Claimed done, repo disagrees"),
    ("unknown", "Unverified activity"),
]


# -- shared assembly helpers ----------------------------------------------------


def _unit_line(unit: WorkUnit) -> str:
    bits = [unit.intent.rstrip(".")]
    if unit.outcome_claim and unit.outcome_claim.lower() != unit.intent.lower():
        bits.append(unit.outcome_claim.rstrip("."))
    evidence = ", ".join(unit.verification.get("evidence", [])[:3])
    line = ". ".join(bits)
    if evidence:
        line += f" ({evidence})"
    line += f" [{unit.unit_key}]"
    if unit.extraction == "stub":
        line += " (unextracted: activity only)"
    return line


def _project_units_section(units: list[WorkUnit]) -> list[str]:
    lines: list[str] = []
    incidental = [u for u in units if u.incidental]
    units = [u for u in units if not u.incidental]
    by_verdict: dict[str, list[WorkUnit]] = {}
    for unit in units:
        verdict = unit.verification.get("verdict", "unknown")
        by_verdict.setdefault(verdict, []).append(unit)
    for verdict, heading in VERDICT_BUCKETS:
        bucket = by_verdict.pop(verdict, [])
        if not bucket:
            continue
        lines.append(f"**{heading}:**")
        lines.extend(f"- {_unit_line(unit)}" for unit in bucket)
        lines.append("")
    for verdict, bucket in by_verdict.items():  # future-proof: unknown verdicts
        lines.append(f"**{verdict}:**")
        lines.extend(f"- {_unit_line(unit)}" for unit in bucket)
        lines.append("")

    questions = [q for unit in units for q in unit.open_questions]
    if questions:
        lines.append("**Open questions:**")
        lines.extend(f"- {q}" for q in dict.fromkeys(questions))
        lines.append("")
    corrections = [c for unit in units for c in unit.user_corrections]
    if corrections:
        lines.append("**Course corrections I gave the agent:**")
        lines.extend(f"- {c}" for c in dict.fromkeys(corrections))
        lines.append("")
    if incidental:
        # Visible but never reported as project work.
        intents = "; ".join(u.intent.rstrip(".") for u in incidental)
        lines.append(
            f"_Incidental troubleshooting (excluded from project memory): {intents}._"
        )
        lines.append("")
    return lines


def _sources_appendix(store: WorkStore, units: list[WorkUnit]) -> list[str]:
    session_ids = sorted({unit.unit_key.rsplit(":", 1)[0] for unit in units})
    if not session_ids:
        return []
    lines = ["## Sources", ""]
    for session_id in session_ids:
        row = store.conn.execute(
            "select path, title, turn_count from sessions_v2 where session_id = ?",
            (session_id,),
        ).fetchone()
        if row:
            title = row["title"] or "(untitled)"
            lines.append(f"- `{session_id}`: {title} ({row['turn_count']} turns) at `{row['path']}`")
        else:
            lines.append(f"- `{session_id}` (session record missing)")
    lines.append("")
    return lines


def _coverage_section(
    store: WorkStore, config: DigestConfig, work_date: str, mode: str
) -> list[str]:
    """Always computed locally, never by an LLM (kept from v1)."""
    lines = ["## Coverage and run health", ""]
    stages = store.get_stage_status(work_date, mode)
    for stage, status in stages.items():
        marker = "ok" if status.startswith("ok") else f"DEGRADED: {status}"
        lines.append(f"- stage {stage}: {marker}")
    provisional = [p for p in store.list_projects() if p.status == "provisional"]
    for project in provisional:
        lines.append(
            f"- New project detected: **{project.name}** (`{project.project_id}`). "
            f"Confirm with `digest projects confirm {project.project_id}` or "
            f"reassign its units with `digest assign`."
        )
    corrupt = store.conn.execute(
        "select path from sessions_v2 where status = 'corrupt'"
    ).fetchall()
    for row in corrupt:
        lines.append(f"- Corrupt session skipped: `{row['path']}`")
    for label, paths in (("claude", config.claude_paths), ("codex", config.codex_paths)):
        missing = [p for p in paths if not p.exists()]
        for path in missing:
            lines.append(f"- Missing {label} input path: {path}")
    lines.append("")
    return lines


def _states_and_units(
    store: WorkStore, work_date: str
) -> list[tuple[str, str, ProjectState | None, list[WorkUnit]]]:
    """[(project_id, name, head_state, today_units)] sorted by activity."""
    units = store.list_units_for_date(work_date)
    by_project: dict[str, list[WorkUnit]] = {}
    for unit in units:
        by_project.setdefault(unit.project_id or "_inbox", []).append(unit)
    result = []
    for project_id, project_units in by_project.items():
        project = store.get_project(project_id)
        name = project.name if project else project_id
        result.append(
            (project_id, name, store.get_head_state(project_id), project_units)
        )
    result.sort(key=lambda item: -len(item[3]))
    return result


# -- night digest ------------------------------------------------------------------


def render_night(
    store: WorkStore, config: DigestConfig, work_date: str, generated_at: str
) -> str:
    lines = [
        f"# End-of-Day Work Digest - {work_date}",
        "",
        f"Generated: {generated_at} | Mode: night | Engine: v2",
        "",
    ]
    sections = _states_and_units(store, work_date)
    if not sections:
        lines += [
            "Quiet day: no coding-agent sessions produced work units today.",
            "",
        ]
    all_units: list[WorkUnit] = []
    for project_id, name, state, units in sections:
        all_units.extend(units)
        lines.append(f"## {name}")
        lines.append("")
        if state:
            if state.goal:
                lines.append(f"**Goal:** {state.goal}")
            if state.as_of_date == work_date and state.narrative_delta:
                lines.append(f"**Today:** {state.narrative_delta}")
            elif state.system_state:
                lines.append(f"**State:** {state.system_state}")
            lines.append("")
        lines.extend(_project_units_section(units))
        if state and state.as_of_date == work_date:
            open_threads = [
                t for t in state.open_threads if t.get("status") == "open"
            ]
            if open_threads:
                lines.append("**Open threads:**")
                lines.extend(
                    f"- {t.get('text')} (since {t.get('since')})" for t in open_threads
                )
                lines.append("")

    inbound = store.list_pr_facts("inbound_feedback")
    if inbound:
        lines.append("## Feedback received on my PRs (inbound, not review work)")
        lines.append("")
        for fact in inbound[:10]:
            title = fact["data"].get("title", "")
            lines.append(f"- {fact['repo']}#{fact['pr_number']}: {title}")
        lines.append("")
    reviews = store.list_pr_facts("review_done")
    if reviews:
        lines.append("## Reviews I did (on others' PRs)")
        lines.append("")
        for fact in reviews[:10]:
            title = fact["data"].get("title", "")
            lines.append(f"- {fact['repo']}#{fact['pr_number']}: {title}")
        lines.append("")

    lines.extend(_sources_appendix(store, all_units))
    lines.extend(_coverage_section(store, config, work_date, "night"))
    return "\n".join(lines)


# -- morning brief -------------------------------------------------------------------


def render_morning(
    store: WorkStore, config: DigestConfig, work_date: str, generated_at: str
) -> str:
    from datetime import date, timedelta

    yesterday = (date.fromisoformat(work_date) - timedelta(days=1)).isoformat()
    lines = [
        f"# Daily Work Brief - {work_date}",
        "",
        f"Generated: {generated_at} | Mode: morning | Engine: v2",
        "",
    ]
    projects = [p for p in store.list_projects() if p.status in {"active", "provisional"}]
    states = [(p, store.get_head_state(p.project_id)) for p in projects]
    states = [(p, s) for p, s in states if s is not None]
    states.sort(key=lambda item: item[1].as_of_date, reverse=True)

    if not states:
        lines += ["No project state recorded yet; run `digest pipeline --mode night` first.", ""]

    for project, state in states:
        lines.append(f"## {project.name}")
        lines.append("")
        if state.goal:
            lines.append(f"**Goal:** {state.goal}")
        if state.system_state:
            lines.append(f"**Where it stands:** {state.system_state}")
        if state.as_of_date == yesterday and state.narrative_delta:
            lines.append(f"**Yesterday:** {state.narrative_delta}")
        elif state.as_of_date < yesterday:
            lines.append(f"_(no work recorded since {state.as_of_date})_")
        open_threads = [t for t in state.open_threads if t.get("status") == "open"]
        if open_threads:
            # Oldest first: staleness is what needs attention in the morning.
            open_threads.sort(key=lambda t: str(t.get("since", "")))
            lines.append("**Open threads (oldest first):**")
            lines.extend(
                f"- {t.get('text')} (since {t.get('since')})" for t in open_threads
            )
            lines.append(f"**First action:** {open_threads[0].get('text')}")
        lines.append("")

    status_facts = store.list_git_facts("status")
    if status_facts:
        lines.append("## Repo status right now")
        lines.append("")
        for fact in status_facts:
            data = fact["data"]
            dirty = len(data.get("dirty_files", []))
            repo = Path(fact["repo_path"]).name
            lines.append(
                f"- {repo}: branch `{data.get('branch') or 'unknown'}`, "
                f"{dirty} uncommitted file(s)"
            )
        lines.append("")

    lines.extend(_coverage_section(store, config, work_date, "morning"))
    return "\n".join(lines)


# -- trello card ------------------------------------------------------------------------


def load_trello_voice(config: DigestConfig) -> tuple[str, list[str]]:
    """Concatenate the user's skill files; fall back to vendored copies.

    Returns (voice_text, notes). Notes record which files were used and their
    content hashes, so a skill edit is visible in run notes.
    """
    notes: list[str] = []
    texts: list[str] = []
    paths = list(config.pipeline.trello_skill_paths)
    if not paths:
        paths = [
            VENDORED_DIR / "dhiraj-writing-style-hard-rules.md",
            VENDORED_DIR / "trello-card-update-skill.md",
        ]
        notes.append("trello voice: using vendored skill copies (no paths configured)")
    for path in paths:
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
            digest = hashlib.sha256(text.encode()).hexdigest()[:10]
            notes.append(f"trello voice: {path.name} ({digest})")
            texts.append(text)
        else:
            notes.append(f"trello voice: MISSING {path}; using vendored fallback")
            fallback = VENDORED_DIR / "trello-card-update-skill.md"
            texts.append(fallback.read_text(encoding="utf-8", errors="replace"))
    return "\n\n---\n\n".join(texts), notes


def render_trello(
    store: WorkStore,
    config: DigestConfig,
    work_date: str,
    generated_at: str,
    llm: BaseLLM,
) -> str:
    scoped = [p for p in store.list_projects() if p.trello_scope]
    if not scoped:
        scoped = [p for p in store.list_projects() if p.project_id != "_inbox"][:1]
    header = [
        f"# Trello Update - {work_date}",
        "",
        f"Generated: {generated_at} | Mode: trello | Engine: v2",
        "",
    ]
    if not scoped:
        return "\n".join(header + ["No Trello-scoped project configured.", ""])
    project = scoped[0]
    state = store.get_head_state(project.project_id)
    units = [
        u
        for u in store.list_units_for_date(work_date)
        if u.project_id == project.project_id and not u.incidental
    ]

    voice, notes = load_trello_voice(config)
    evidence_lines = [f"PROJECT: {project.name}"]
    if state:
        evidence_lines += [
            f"GOAL: {state.goal}",
            f"SYSTEM STATE: {state.system_state}",
            f"LATEST DELTA ({state.as_of_date}): {state.narrative_delta}",
        ]
    evidence_lines.append(f"TODAY'S UNITS ({len(units)}):")
    for unit in units:
        verdict = unit.verification.get("verdict", "unknown")
        evidence_lines.append(
            f"- intent: {unit.intent} | kind: {unit.kind} | claim: "
            f"{unit.outcome_claim or '(none)'} | status: {unit.status_claim} "
            f"| verification: {verdict}"
        )
        if unit.open_questions:
            evidence_lines.append(f"  open questions: {'; '.join(unit.open_questions)}")

    system = (
        "You write Dhiraj's midday Trello card update. The rules and formats "
        "below are his actual writing skill; follow them exactly. Pick the "
        "format variant that matches the day (Discovery / Current Direction / "
        "Implementation / default Today's Update). A unit whose verification "
        "is 'uncommitted' is implemented but not landed; 'unverified' or "
        "'contradicted' work must be stated with that uncertainty, never as "
        "shipped. Output only the paste-ready card: a 'Bigger picture' "
        "paragraph (2-3 sentences), a blank line, then the card body.\n\n"
        + voice
    )
    user = (
        f"Date: {work_date}. Evidence (the only source of truth):\n\n"
        + "\n".join(evidence_lines)
        + "\n\nWrite the card now."
    )
    try:
        card = llm.complete_text(
            system=system,
            user=user,
            max_output_tokens=TRELLO_MAX_OUTPUT_TOKENS,
            stage="trello",
        ).strip()
        card = card.replace("—", ", ").replace(" - -", ",")
    except (LLMError, LLMUnavailable) as exc:
        logger.warning("Trello render failed: %s", exc)
        raw = [
            "RAW FALLBACK - DO NOT PASTE AS-IS (LLM unavailable: voice rules not applied)",
            "",
            f"Project: {project.name}",
        ]
        raw += [f"- {_unit_line(unit)}" for unit in units] or ["- No units today."]
        card = "\n".join(raw)
        notes.append(f"trello: fell back to raw card ({exc})")

    body = header
    if notes:
        body.append("> **Run notes:**")
        body.extend(f"> - {note}" for note in notes)
        body.append("")
    body.append(card)
    body.append("")
    body.extend(_sources_appendix(store, units))
    return "\n".join(body)
