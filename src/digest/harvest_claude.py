"""Structured parser for Claude Code session JSONL files.

Reduces a raw session (~98% tool output and injected context) to the ~1.5%
that records work: real user prompts, assistant prose, tool names with
salient inputs, and files touched. Event taxonomy and keep/strip policy are
documented in DESIGN_V2.md sections 1.2 and 3.1.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store_v2 import SessionRecord, TurnRecord
from .text_utils import file_hash

ASSISTANT_BLOCK_CAP = 4000
ASSISTANT_TURN_CAP = 12000
ERROR_TAIL_CHARS = 300
COMPACT_SUMMARY_CAP = 4000

IDE_SELECTION_RE = re.compile(r"<ide_selection>.*?</ide_selection>", re.DOTALL)

# Tools whose input names a file that the turn touched (wrote, not just read).
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def to_local_iso(timestamp: str | None) -> str | None:
    """Normalize event timestamps (usually ...Z) to local-offset ISO strings
    so they compare lexicographically with store_v2.now_iso() values."""
    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().isoformat(timespec="seconds")


class _TurnBuilder:
    def __init__(self, session_id: str, seq: int, line: int):
        self.session_id = session_id
        self.seq = seq
        self.line_start = line
        self.line_end = line
        self.turn_key: str | None = None
        self.started_at: str | None = None
        self.ended_at: str | None = None
        self.user_parts: list[str] = []
        self.assistant_parts: list[str] = []
        self.assistant_chars = 0
        self.tools: list[dict[str, Any]] = []
        self.tool_index: dict[str, int] = {}  # tool_use_id -> index in tools
        self.files: list[str] = []
        self.cwd: str | None = None
        self.git_branch: str | None = None
        self.model: str | None = None
        self.flags: list[str] = []

    def build(self) -> TurnRecord:
        key = self.turn_key or f"seq{self.seq}"
        return TurnRecord(
            turn_id=f"{self.session_id}:{key}",
            session_id=self.session_id,
            seq=self.seq,
            started_at=self.started_at,
            ended_at=self.ended_at,
            user_text="\n".join(self.user_parts).strip(),
            assistant_text="\n".join(self.assistant_parts).strip(),
            tools=self.tools,
            files=sorted(set(self.files)),
            cwd=self.cwd,
            git_branch=self.git_branch,
            model=self.model,
            flags=self.flags,
            line_start=self.line_start,
            line_end=self.line_end,
        )

    def is_empty(self) -> bool:
        return not (self.user_parts or self.assistant_parts or self.tools)


def parse_claude_session(path: Path) -> tuple[SessionRecord, list[TurnRecord]]:
    raw_session_id = path.stem
    session_id = f"claude:{raw_session_id}"
    title: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    timestamps: list[str] = []
    parse_errors = 0
    compact_pending = False

    turns: list[TurnRecord] = []
    current: _TurnBuilder | None = None

    def flush() -> None:
        nonlocal current
        if current is not None and not current.is_empty():
            turns.append(current.build())
        current = None

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
            timestamp = to_local_iso(event.get("timestamp"))
            if timestamp:
                timestamps.append(timestamp)
            if event.get("cwd"):
                cwd = str(event["cwd"])
            if event.get("gitBranch"):
                git_branch = str(event["gitBranch"])

            if event_type == "ai-title":
                title = str(event.get("aiTitle") or "") or title
                continue
            if event_type == "system":
                if event.get("subtype") == "compact_boundary":
                    compact_pending = True
                continue
            if event_type != "user" and event_type != "assistant":
                # attachment, queue-operation, file-history-snapshot,
                # last-prompt, mode, permission-mode: context plumbing.
                continue
            if event.get("isSidechain"):
                continue

            message = event.get("message") or {}
            content = message.get("content")

            if event_type == "user":
                if event.get("isMeta"):
                    continue
                if event.get("isCompactSummary"):
                    flush()
                    current = _TurnBuilder(session_id, len(turns) + 1, line_no)
                    current.turn_key = f"compact{line_no}"
                    current.started_at = current.ended_at = timestamp
                    summary_text = _content_text(content)[:COMPACT_SUMMARY_CAP]
                    current.assistant_parts.append(summary_text)
                    current.flags.append("compact_summary")
                    flush()
                    continue

                tool_results = _tool_result_blocks(content)
                if tool_results:
                    if current is not None:
                        current.line_end = line_no
                        current.ended_at = timestamp or current.ended_at
                        for block in tool_results:
                            _apply_tool_result(current, block)
                    continue

                user_text = _content_text(content)
                cleaned = IDE_SELECTION_RE.sub("", user_text).strip()
                if not cleaned:
                    continue
                flush()
                current = _TurnBuilder(session_id, len(turns) + 1, line_no)
                current.turn_key = str(
                    event.get("promptId") or event.get("uuid") or f"line{line_no}"
                )
                current.started_at = current.ended_at = timestamp
                current.cwd = event.get("cwd")
                current.git_branch = event.get("gitBranch")
                current.user_parts.append(cleaned)
                if cleaned != user_text.strip():
                    current.flags.append("ide_selection_stripped")
                if compact_pending:
                    current.flags.append("compacted_before")
                    compact_pending = False
                continue

            # assistant event
            if current is None:
                current = _TurnBuilder(session_id, len(turns) + 1, line_no)
                current.turn_key = f"orphan{line_no}"
                current.started_at = timestamp
                current.flags.append("orphan")
            current.line_end = line_no
            current.ended_at = timestamp or current.ended_at
            current.model = message.get("model") or current.model
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text = str(block.get("text") or "")[:ASSISTANT_BLOCK_CAP]
                    if text and current.assistant_chars < ASSISTANT_TURN_CAP:
                        current.assistant_parts.append(text)
                        current.assistant_chars += len(text)
                elif block_type == "tool_use":
                    entry = _tool_entry(block)
                    tool_use_id = str(block.get("id") or "")
                    if tool_use_id:
                        current.tool_index[tool_use_id] = len(current.tools)
                    current.tools.append(entry)
                    file_path = _file_from_tool(block)
                    if file_path:
                        current.files.append(file_path)
                # thinking blocks are dropped

    flush()

    started = min(timestamps) if timestamps else None
    ended = max(timestamps) if timestamps else None
    session = SessionRecord(
        session_id=session_id,
        agent="claude",
        path=str(path),
        content_hash=file_hash(path),
        title=title,
        cwd=cwd,
        git_branch=git_branch,
        started_at=started,
        ended_at=ended,
        turn_count=len(turns),
        parse_errors=parse_errors,
        status="ok" if turns or parse_errors == 0 else "corrupt",
    )
    return session, turns


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part)
    return ""


def _tool_result_blocks(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    return [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]


def _apply_tool_result(turn: _TurnBuilder, block: dict[str, Any]) -> None:
    tool_use_id = str(block.get("tool_use_id") or "")
    index = turn.tool_index.get(tool_use_id)
    if index is None:
        return
    if block.get("is_error"):
        turn.tools[index]["ok"] = False
        text = _tool_result_text(block.get("content"))
        if text:
            turn.tools[index]["error_tail"] = text[-ERROR_TAIL_CHARS:]
    else:
        turn.tools[index].setdefault("ok", True)


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return _content_text(content)
    return ""


def _tool_entry(block: dict[str, Any]) -> dict[str, Any]:
    name = str(block.get("name") or "tool")
    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
    detail = ""
    if "file_path" in tool_input:
        detail = str(tool_input["file_path"])
    elif "command" in tool_input:
        detail = str(tool_input["command"])[:200]
    elif "skill" in tool_input:
        detail = str(tool_input["skill"])
    elif "pattern" in tool_input:
        detail = str(tool_input["pattern"])[:100]
    return {"name": name, "detail": detail}


def _file_from_tool(block: dict[str, Any]) -> str | None:
    if block.get("name") not in WRITE_TOOLS:
        return None
    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    return str(file_path) if file_path else None
