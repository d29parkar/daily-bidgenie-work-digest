from __future__ import annotations

from pathlib import Path

import pytest

from conftest import write_config
from digest.config import load_config
from digest.llm_v2 import NoneLLM
from digest.render_v2 import load_trello_voice, render_morning, render_night, render_trello
from digest.store_v2 import SessionRecord, TurnRecord, WorkStore, WorkUnit
from test_extract import ScriptedLLM

GENERATED_AT = "2026-07-12T21:30:00-04:00"


@pytest.fixture()
def store(tmp_path: Path) -> WorkStore:
    s = WorkStore(tmp_path / "digest.sqlite")
    yield s
    s.close()


def _seed_day(store: WorkStore) -> None:
    store.create_project("widget", "Widget", trello_scope=True)
    session = SessionRecord(
        session_id="claude:abc", agent="claude", path="C:/s/abc.jsonl",
        content_hash="h", title="Fix widget crash", cwd="c:/projects/widget",
        git_branch="main", started_at="2026-07-12T09:00:00-04:00",
        ended_at="2026-07-12T10:00:00-04:00", turn_count=1, parse_errors=0,
    )
    turn = TurnRecord(
        turn_id="claude:abc:1:p1", session_id="claude:abc", seq=1,
        started_at="2026-07-12T09:00:00-04:00", ended_at=None,
        user_text="fix", assistant_text="done", tools=[], files=["widget.py"],
    )
    store.upsert_session(session, [turn])
    units = [
        WorkUnit(
            unit_key="claude:abc:1", work_date="2026-07-12", project_id="widget",
            turn_ids=["claude:abc:1:p1"], intent="Fix the widget crash",
            kind="debugging", outcome_claim="Guarded None in save()",
            status_claim="done", files=["widget.py"], entities=[], claims=[],
            open_questions=["backport to v1?"],
            verification={"verdict": "corroborated_by_commit",
                          "evidence": ["commit:abc123def456"], "notes": []},
        ),
        WorkUnit(
            unit_key="claude:abc:2", work_date="2026-07-12", project_id="widget",
            turn_ids=["claude:abc:1:p1"], intent="Refactor pricing module",
            kind="refactor", outcome_claim="Claims complete rewrite",
            status_claim="done", files=["pricing.py"], entities=[], claims=[],
            open_questions=[],
            verification={"verdict": "contradicted", "evidence": [],
                          "notes": ["claims done, but named files show no commits or local changes"]},
        ),
    ]
    store.replace_work_units("claude:abc", "2026-07-12", units)
    store.write_state_version(
        "widget", "2026-07-12",
        goal="Ship the widget safely",
        system_state="Crash fixed; pricing refactor unverified.",
        narrative_delta="Today moved the widget from crashing to fixed and committed.",
        open_threads=[{"text": "add regression test", "since": "2026-07-11", "status": "open"}],
        evidence={}, written_by="test",
    )


def _config(tmp_path: Path, extra: str = ""):
    return load_config(write_config(tmp_path, extra=extra))


def test_render_night_separates_verified_from_contradicted(store, tmp_path):
    _seed_day(store)
    text = render_night(store, _config(tmp_path), "2026-07-12", GENERATED_AT)
    assert "# End-of-Day Work Digest - 2026-07-12" in text
    assert "**Goal:** Ship the widget safely" in text
    assert "Landed (verified against commits)" in text
    assert "commit:abc123def456" in text
    assert "Claimed done, repo disagrees" in text
    assert "[claude:abc:1]" in text  # provenance citation
    assert "## Sources" in text
    assert "C:/s/abc.jsonl" in text
    assert "backport to v1?" in text


def test_render_night_quiet_day(store, tmp_path):
    text = render_night(store, _config(tmp_path), "2026-07-12", GENERATED_AT)
    assert "Quiet day" in text


def test_render_morning_orders_threads_by_staleness(store, tmp_path):
    _seed_day(store)
    store.write_state_version(
        "widget", "2026-07-12",
        goal="Ship the widget safely", system_state="Crash fixed.",
        narrative_delta="Fixed the crash.",
        open_threads=[
            {"text": "newer thread", "since": "2026-07-12", "status": "open"},
            {"text": "ancient thread", "since": "2026-07-01", "status": "open"},
        ],
        evidence={}, written_by="test",
    )
    text = render_morning(store, _config(tmp_path), "2026-07-13", GENERATED_AT)
    assert "**Yesterday:** Fixed the crash." in text
    assert text.index("ancient thread") < text.index("newer thread")
    assert "**First action:** ancient thread" in text


def test_render_trello_uses_voice_and_llm(store, tmp_path):
    _seed_day(store)
    llm = ScriptedLLM(["Bigger picture text.\n\nToday's Update\n- I completed the widget crash fix."])
    text = render_trello(store, _config(tmp_path), "2026-07-12", GENERATED_AT, llm)
    assert "Today's Update" in text
    # The system prompt embedded the skill (vendored fallback here).
    # ScriptedLLM records only user prompts; check evidence made it through.
    assert "Fix the widget crash" in llm.prompts[0]
    assert "verification: contradicted" in llm.prompts[0]
    assert "—" not in text


def test_render_trello_fallback_is_marked_raw(store, tmp_path):
    _seed_day(store)
    text = render_trello(store, _config(tmp_path), "2026-07-12", GENERATED_AT, NoneLLM())
    assert "RAW FALLBACK - DO NOT PASTE" in text
    assert "Fix the widget crash" in text


def test_load_trello_voice_prefers_configured_paths(store, tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text("MY REAL VOICE RULES", encoding="utf-8")
    config = _config(
        tmp_path,
        f"pipeline:\n  trello_skill_paths:\n    - {skill}",
    )
    voice, notes = load_trello_voice(config)
    assert "MY REAL VOICE RULES" in voice
    assert any("SKILL.md" in n for n in notes)


def test_load_trello_voice_vendored_fallback(store, tmp_path):
    voice, notes = load_trello_voice(_config(tmp_path))
    assert "Today’s Update" in voice or "Today's Update" in voice
    assert any("vendored" in n for n in notes)


def _incidental_unit() -> WorkUnit:
    return WorkUnit(
        unit_key="claude:abc:3", work_date="2026-07-12", project_id="widget",
        turn_ids=["claude:abc:1:p1"], intent="Fix broken venv after Python upgrade",
        kind="ops", outcome_claim="Reinstalled deps", status_claim="done",
        files=[], entities=[], claims=[], open_questions=[],
        verification={"verdict": "no_files", "evidence": [], "notes": []},
        incidental=True,
    )


def test_incidental_units_footnoted_in_night_excluded_from_trello(store, tmp_path):
    _seed_day(store)
    units = store.list_units_for_date("2026-07-12") + [_incidental_unit()]
    store.replace_work_units("claude:abc", "2026-07-12", units)

    night = render_night(store, _config(tmp_path), "2026-07-12", GENERATED_AT)
    assert "Incidental troubleshooting (excluded from project memory)" in night
    assert "Fix broken venv" in night
    # Not listed as a verified/claimed work bucket entry:
    assert "- Fix broken venv after Python upgrade" not in night

    llm = ScriptedLLM(["Bigger picture.\n\nToday's Update\n- I completed the fix."])
    render_trello(store, _config(tmp_path), "2026-07-12", GENERATED_AT, llm)
    assert "broken venv" not in llm.prompts[0]
