from __future__ import annotations

import json

import pytest

from digest.extract import extract_session_units, stub_unit
from digest.llm_v2 import BaseLLM, LLMError, LLMUnavailable, NoneLLM, parse_json_response
from digest.store_v2 import SessionRecord, TurnRecord


class ScriptedLLM(BaseLLM):
    """Returns queued responses; records the prompts it saw."""

    name = "scripted"

    def __init__(self, responses: list[str]):
        super().__init__("scripted")
        self.responses = list(responses)
        self.prompts: list[str] = []

    def complete_text(self, *, system, user, max_output_tokens, stage):
        self.prompts.append(user)
        if not self.responses:
            raise LLMError("Out of scripted responses.")
        return self.responses.pop(0)


def _session() -> SessionRecord:
    return SessionRecord(
        session_id="claude:abc",
        agent="claude",
        path="C:/fake/abc.jsonl",
        content_hash="h",
        title="Fix widget crash",
        cwd="c:/projects/widget",
        git_branch="main",
        started_at="2026-07-12T09:00:00-04:00",
        ended_at="2026-07-12T10:00:00-04:00",
        turn_count=2,
        parse_errors=0,
    )


def _turns() -> list[TurnRecord]:
    return [
        TurnRecord(
            turn_id="claude:abc:1:p1",
            session_id="claude:abc",
            seq=1,
            started_at="2026-07-12T09:00:00-04:00",
            ended_at=None,
            user_text="fix the widget crash on save",
            assistant_text="Fixed by guarding None in save().",
            tools=[{"name": "Edit", "detail": "widget.py", "ok": True}],
            files=["src/widget.py"],
        ),
        TurnRecord(
            turn_id="claude:abc:2:p2",
            session_id="claude:abc",
            seq=2,
            started_at="2026-07-12T09:30:00-04:00",
            ended_at=None,
            user_text="now write docs for the pricing module",
            assistant_text="Drafted docs.",
            tools=[],
            files=["docs/pricing.md"],
        ),
    ]


def _good_payload() -> str:
    return json.dumps(
        {
            "work_units": [
                {
                    "turns": [1],
                    "intent": "Fix the widget crash on save",
                    "kind": "debugging",
                    "outcome_claim": "Guarded None in save()",
                    "status_claim": "done",
                    "entities": ["save"],
                    "claims_to_verify": [{"type": "fix_applied", "text": "crash fixed"}],
                    "open_questions": [],
                    "user_corrections": [],
                    # model hallucinates a file it never touched:
                    "files": ["evil/injected.py"],
                },
                {
                    "turns": [2],
                    "intent": "Write docs for the pricing module",
                    "kind": "docs",
                    "outcome_claim": "Docs drafted",
                    "status_claim": "in_progress",
                    "entities": [],
                    "claims_to_verify": [],
                    "open_questions": ["publish where?"],
                    "user_corrections": [],
                },
            ]
        }
    )


def test_extract_maps_seqs_and_overrides_files():
    llm = ScriptedLLM([_good_payload()])
    units, method = extract_session_units(llm, _session(), _turns(), "2026-07-12")
    assert method == "llm"
    assert [u.unit_key for u in units] == ["claude:abc:1", "claude:abc:2"]
    first, second = units
    assert first.turn_ids == ["claude:abc:1:p1"]
    # files come from harvest facts, never from the model:
    assert first.files == ["src/widget.py"]
    assert first.status_claim == "done"
    assert second.kind == "docs"
    assert second.open_questions == ["publish where?"]


def test_extract_retries_once_with_validator_feedback():
    bad = json.dumps({"work_units": [{"turns": [99], "intent": "x"}]})
    llm = ScriptedLLM([bad, _good_payload()])
    units, method = extract_session_units(llm, _session(), _turns(), "2026-07-12")
    assert method == "llm"
    assert len(units) == 2
    assert "unknown seqs [99]" in llm.prompts[1]


def test_extract_falls_back_to_stub_after_two_failures():
    llm = ScriptedLLM(["not json", "still not json"])
    units, method = extract_session_units(llm, _session(), _turns(), "2026-07-12")
    assert method == "stub"
    assert len(units) == 1
    unit = units[0]
    assert unit.extraction == "stub"
    assert unit.intent == "Fix widget crash"  # falls back to the ai-title
    assert unit.status_claim == "unknown"
    assert set(unit.files) == {"src/widget.py", "docs/pricing.md"}


def test_extract_propagates_unavailable():
    with pytest.raises(LLMUnavailable):
        extract_session_units(NoneLLM(), _session(), _turns(), "2026-07-12")


def test_prompt_contains_user_text_and_failed_tools():
    turns = _turns()
    turns[0].tools[0]["ok"] = False
    turns[0].tools[0]["error_tail"] = "PermissionError: widget.py"
    llm = ScriptedLLM([_good_payload()])
    extract_session_units(llm, _session(), turns, "2026-07-12")
    prompt = llm.prompts[0]
    assert "fix the widget crash on save" in prompt
    assert "FAILED" in prompt
    assert "PermissionError" in prompt
    assert "FILES TOUCHED: src/widget.py" in prompt


def test_stub_unit_uses_first_prompt_without_title():
    session = _session()
    object.__setattr__(session, "title", None)
    unit = stub_unit(session, _turns(), "2026-07-12")
    assert unit.intent == "fix the widget crash on save"
