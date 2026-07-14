"""Stage S2: segment sessions into work units with intent (DESIGN_V2.md 3.2).

One LLM call per session-day. The model judges intent, segmentation, and
status; everything mechanical (turn ids, files touched) is taken from harvest
facts, so the model cannot invent files it never saw. On extraction failure
the session degrades to a deterministic stub unit instead of being dropped.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .llm_v2 import BaseLLM, LLMError, LLMUnavailable
from .store_v2 import SessionRecord, TurnRecord, WorkUnit

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

VALID_STATUS = {"done", "in_progress", "blocked", "abandoned"}
VALID_KINDS = {
    "debugging",
    "feature",
    "discovery",
    "review",
    "docs",
    "ops",
    "refactor",
    "other",
}
VALID_CLAIM_TYPES = {"commit", "tests_pass", "deploy", "fix_applied", "other"}

# Render budgets. User text is never trimmed below the assistant floor:
# it is the scarcest, highest-value signal (DESIGN_V2.md 1.2).
INPUT_BUDGET_CHARS = 60000
ASSISTANT_CHARS_DEFAULT = 1500
ASSISTANT_CHARS_SQUEEZED = 400
MAX_OUTPUT_TOKENS = 2500


def extract_session_units(
    llm: BaseLLM,
    session: SessionRecord,
    turns: list[TurnRecord],
    work_date: str,
) -> tuple[list[WorkUnit], str]:
    """Returns (units, method) where method is 'llm' or 'stub'."""
    if not turns:
        return [], "llm"
    try:
        payload = _call_llm(llm, session, turns, work_date)
        units = _units_from_payload(payload, session, turns, work_date)
        if not units:
            raise LLMError("Extraction returned zero work units.")
        return units, "llm"
    except LLMUnavailable:
        raise
    except LLMError as exc:
        logger.warning("Extraction failed for %s: %s", session.session_id, exc)
        return [stub_unit(session, turns, work_date)], "stub"


def _call_llm(
    llm: BaseLLM,
    session: SessionRecord,
    turns: list[TurnRecord],
    work_date: str,
) -> dict[str, Any]:
    system = (PROMPTS_DIR / "v2_extract_system.md").read_text(encoding="utf-8")
    user = _render_user_prompt(session, turns, work_date)
    try:
        payload = llm.complete_json(
            system=system, user=user, max_output_tokens=MAX_OUTPUT_TOKENS, stage="extract"
        )
        _validate_payload(payload, turns)
        return payload
    except LLMError as first_error:
        # One retry, telling the model what was wrong with its output.
        retry_user = (
            f"{user}\n\nYour previous response was rejected: {first_error}\n"
            "Return corrected JSON only."
        )
        payload = llm.complete_json(
            system=system,
            user=retry_user,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            stage="extract",
        )
        _validate_payload(payload, turns)
        return payload


def _render_user_prompt(
    session: SessionRecord, turns: list[TurnRecord], work_date: str
) -> str:
    template = (PROMPTS_DIR / "v2_extract_user.md").read_text(encoding="utf-8")
    rendered = template.format(
        agent=session.agent,
        title=session.title or "(untitled)",
        cwd=session.cwd or "(unknown)",
        branch=session.git_branch or "(unknown)",
        work_date=work_date,
        turns=_render_turns(turns, ASSISTANT_CHARS_DEFAULT),
    )
    if len(rendered) > INPUT_BUDGET_CHARS:
        rendered = template.format(
            agent=session.agent,
            title=session.title or "(untitled)",
            cwd=session.cwd or "(unknown)",
            branch=session.git_branch or "(unknown)",
            work_date=work_date,
            turns=_render_turns(turns, ASSISTANT_CHARS_SQUEEZED),
        )
    return rendered[:INPUT_BUDGET_CHARS]


def _render_turns(turns: list[TurnRecord], assistant_cap: int) -> str:
    blocks: list[str] = []
    for turn in turns:
        lines = [f"### turn {turn.seq} ({turn.started_at or 'no time'})"]
        if turn.flags:
            lines.append(f"flags: {', '.join(turn.flags)}")
        if turn.user_text:
            lines.append(f"USER: {turn.user_text}")
        if turn.assistant_text:
            text = turn.assistant_text
            if len(text) > assistant_cap:
                text = text[:assistant_cap] + " [...trimmed]"
            lines.append(f"ASSISTANT: {text}")
        if turn.tools:
            tool_bits = []
            for tool in turn.tools[:20]:
                bit = tool.get("name", "tool")
                detail = str(tool.get("detail") or "")
                if detail:
                    bit += f"({detail[:80]})"
                if tool.get("ok") is False:
                    bit += " FAILED"
                    tail = str(tool.get("error_tail") or "")[:120]
                    if tail:
                        bit += f": {tail}"
                tool_bits.append(bit)
            more = f" +{len(turn.tools) - 20} more" if len(turn.tools) > 20 else ""
            lines.append(f"TOOLS: {'; '.join(tool_bits)}{more}")
        if turn.files:
            lines.append(f"FILES TOUCHED: {', '.join(turn.files[:15])}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _validate_payload(payload: dict[str, Any], turns: list[TurnRecord]) -> None:
    units = payload.get("work_units")
    if not isinstance(units, list):
        raise LLMError("Missing or non-list 'work_units'.")
    valid_seqs = {turn.seq for turn in turns}
    for index, unit in enumerate(units):
        if not isinstance(unit, dict):
            raise LLMError(f"work_units[{index}] is not an object.")
        seqs = unit.get("turns")
        if not isinstance(seqs, list) or not seqs:
            raise LLMError(f"work_units[{index}].turns must be a non-empty list.")
        bad = [s for s in seqs if s not in valid_seqs]
        if bad:
            raise LLMError(
                f"work_units[{index}].turns references unknown seqs {bad}; "
                f"valid seqs are {sorted(valid_seqs)}."
            )
        if not str(unit.get("intent") or "").strip():
            raise LLMError(f"work_units[{index}].intent is empty.")


def _units_from_payload(
    payload: dict[str, Any],
    session: SessionRecord,
    turns: list[TurnRecord],
    work_date: str,
) -> list[WorkUnit]:
    by_seq = {turn.seq: turn for turn in turns}
    results: list[WorkUnit] = []
    for index, unit in enumerate(payload.get("work_units", []), start=1):
        seqs = sorted(int(s) for s in unit.get("turns", []))
        unit_turns = [by_seq[s] for s in seqs if s in by_seq]
        files = sorted({f for turn in unit_turns for f in turn.files})
        status = str(unit.get("status_claim") or "").strip()
        kind = str(unit.get("kind") or "").strip()
        claims = [
            {
                "type": c.get("type") if c.get("type") in VALID_CLAIM_TYPES else "other",
                "text": str(c.get("text") or ""),
            }
            for c in unit.get("claims_to_verify", [])
            if isinstance(c, dict) and str(c.get("text") or "").strip()
        ]
        results.append(
            WorkUnit(
                unit_key=f"{session.session_id}:{index}",
                work_date=work_date,
                project_id=None,
                turn_ids=[turn.turn_id for turn in unit_turns],
                intent=str(unit.get("intent") or "").strip(),
                kind=kind if kind in VALID_KINDS else "other",
                outcome_claim=str(unit.get("outcome_claim") or "").strip(),
                status_claim=status if status in VALID_STATUS else "unknown",
                files=files,
                entities=[str(e) for e in unit.get("entities", []) if str(e).strip()][:12],
                claims=claims,
                open_questions=[
                    str(q) for q in unit.get("open_questions", []) if str(q).strip()
                ][:8],
                user_corrections=[
                    str(c) for c in unit.get("user_corrections", []) if str(c).strip()
                ][:8],
                extraction="llm",
            )
        )
    return results


def stub_unit(
    session: SessionRecord, turns: list[TurnRecord], work_date: str
) -> WorkUnit:
    """Deterministic fallback when extraction fails: activity, not judgment."""
    files = sorted({f for turn in turns for f in turn.files})
    first_prompt = next((t.user_text for t in turns if t.user_text), "")
    intent = session.title or first_prompt.splitlines()[0][:120] if first_prompt else ""
    return WorkUnit(
        unit_key=f"{session.session_id}:1",
        work_date=work_date,
        project_id=None,
        turn_ids=[turn.turn_id for turn in turns],
        intent=intent or "(unextracted session)",
        kind="other",
        outcome_claim="",
        status_claim="unknown",
        files=files,
        entities=[],
        claims=[],
        open_questions=[],
        user_corrections=[],
        extraction="stub",
    )
