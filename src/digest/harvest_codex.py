"""Structured parser for Codex rollout JSONL files.

Schema documented in DESIGN_V2.md section 1.3. User and assistant prose come
from ``event_msg`` payloads (``response_item/message`` duplicates them and
also carries injected developer context, so it is ignored for prose). Tool
activity comes from ``response_item`` function/custom tool calls, and
``patch_apply_end`` events supply harness-recorded file changes, which are
ground truth rather than model claims.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .harvest_claude import (
    ASSISTANT_BLOCK_CAP,
    ASSISTANT_TURN_CAP,
    ERROR_TAIL_CHARS,
    _TurnBuilder,
    to_local_iso,
)
from .store_v2 import SessionRecord, TurnRecord
from .text_utils import file_hash

INJECTED_PREFIXES = (
    "<permissions instructions>",
    "<app-context>",
    "<user_instructions>",
    "<environment_context>",
    "<ENVIRONMENT_CONTEXT>",
    "<turn-aborted>",
)

PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.MULTILINE)


def parse_codex_session(path: Path) -> tuple[SessionRecord, list[TurnRecord]]:
    raw_id = path.stem
    session_id = f"codex:{raw_id}"
    cwd: str | None = None
    model: str | None = None
    timestamps: list[str] = []
    parse_errors = 0
    first_user_text: str | None = None

    turns: list[TurnRecord] = []
    current: _TurnBuilder | None = None

    def flush() -> None:
        nonlocal current
        if current is not None and not current.is_empty():
            turns.append(current.build())
        current = None

    def ensure_turn(line_no: int, timestamp: str | None, turn_key: str | None) -> _TurnBuilder:
        nonlocal current
        if current is None:
            current = _TurnBuilder(session_id, len(turns) + 1, line_no)
            current.turn_key = turn_key or f"line{line_no}"
            current.started_at = timestamp
            current.cwd = cwd
        current.line_end = line_no
        current.ended_at = timestamp or current.ended_at
        return current

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, raw_line in enumerate(handle):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            if not isinstance(event, dict):
                parse_errors += 1
                continue

            event_type = event.get("type")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            timestamp = to_local_iso(event.get("timestamp"))
            if timestamp:
                timestamps.append(timestamp)

            if event_type == "session_meta":
                cwd = str(payload.get("cwd") or "") or cwd
                continue

            if event_type == "turn_context":
                # New agentic turn; a fresh cwd can differ from the session's.
                flush()
                turn_cwd = payload.get("cwd")
                if turn_cwd:
                    cwd = str(turn_cwd)
                continue

            if event_type == "compacted":
                flush()
                current = _TurnBuilder(session_id, len(turns) + 1, line_no)
                current.turn_key = f"compact{line_no}"
                current.started_at = current.ended_at = timestamp
                current.assistant_parts.append(
                    str(payload.get("message") or "(context compacted)")[:ASSISTANT_BLOCK_CAP]
                )
                current.flags.append("compact_summary")
                flush()
                continue

            if event_type == "event_msg":
                kind = payload.get("type")
                if kind == "user_message":
                    text = str(payload.get("message") or "").strip()
                    if not text or text.startswith(INJECTED_PREFIXES):
                        continue
                    flush()
                    current = _TurnBuilder(session_id, len(turns) + 1, line_no)
                    current.turn_key = f"line{line_no}"
                    current.started_at = current.ended_at = timestamp
                    current.cwd = cwd
                    current.user_parts.append(text)
                    if first_user_text is None:
                        first_user_text = text
                elif kind == "agent_message":
                    turn = ensure_turn(line_no, timestamp, None)
                    text = str(payload.get("message") or "")[:ASSISTANT_BLOCK_CAP]
                    if text and turn.assistant_chars < ASSISTANT_TURN_CAP:
                        turn.assistant_parts.append(text)
                        turn.assistant_chars += len(text)
                elif kind == "patch_apply_end":
                    turn = ensure_turn(line_no, timestamp, None)
                    changes = payload.get("changes")
                    files = sorted(changes.keys()) if isinstance(changes, dict) else []
                    turn.files.extend(files)
                    turn.tools.append(
                        {
                            "name": "apply_patch",
                            "detail": ", ".join(Path(f).name for f in files[:5]),
                            "ok": bool(payload.get("success")),
                            "harness_verified": True,
                        }
                    )
                # task_started/task_complete/token_count/reasoning: ignored
                continue

            if event_type == "response_item":
                kind = payload.get("type")
                if kind in {"function_call", "custom_tool_call"}:
                    turn = ensure_turn(line_no, timestamp, None)
                    name = str(payload.get("name") or "tool")
                    detail = _call_detail(name, payload)
                    call_id = str(payload.get("call_id") or "")
                    if call_id:
                        turn.tool_index[call_id] = len(turn.tools)
                    turn.tools.append({"name": name, "detail": detail})
                    if name == "apply_patch":
                        patch_input = str(payload.get("input") or "")
                        turn.files.extend(PATCH_FILE_RE.findall(patch_input))
                elif kind in {"function_call_output", "custom_tool_call_output"}:
                    if current is None:
                        continue
                    call_id = str(payload.get("call_id") or "")
                    index = current.tool_index.get(call_id)
                    if index is None:
                        continue
                    output = str(payload.get("output") or "")
                    ok = output.startswith("Exit code: 0") or "Exit code:" not in output
                    current.tools[index]["ok"] = ok
                    if not ok:
                        current.tools[index]["error_tail"] = output[-ERROR_TAIL_CHARS:]
                # message/reasoning/tool_search: prose comes from event_msg
                continue

    flush()

    title = None
    if first_user_text:
        title = first_user_text.splitlines()[0][:80]

    started = min(timestamps) if timestamps else None
    ended = max(timestamps) if timestamps else None
    session = SessionRecord(
        session_id=session_id,
        agent="codex",
        path=str(path),
        content_hash=file_hash(path),
        title=title,
        cwd=cwd,
        git_branch=None,
        started_at=started,
        ended_at=ended,
        turn_count=len(turns),
        parse_errors=parse_errors,
        status="ok" if turns or parse_errors == 0 else "corrupt",
    )
    return session, turns


def _call_detail(name: str, payload: dict[str, Any]) -> str:
    raw_args = payload.get("arguments") or payload.get("input") or ""
    if name == "apply_patch":
        files = PATCH_FILE_RE.findall(str(raw_args))
        return ", ".join(Path(f).name for f in files[:5])
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            return raw_args[:200]
    elif isinstance(raw_args, dict):
        args = raw_args
    else:
        return ""
    if isinstance(args, dict):
        for key in ("command", "cmd", "path", "file_path", "query"):
            if key in args:
                return str(args[key])[:200]
    return ""
