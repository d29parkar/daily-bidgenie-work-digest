from __future__ import annotations

import json
from pathlib import Path

from digest.harvest_claude import parse_claude_session, to_local_iso
from digest.harvest_codex import parse_codex_session

CWD = "c:\\projects\\widget"


def _claude_lines() -> list[dict]:
    base = {
        "isSidechain": False,
        "cwd": CWD,
        "gitBranch": "feat/x",
        "sessionId": "abc",
        "version": "2.1.207",
    }
    return [
        {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-07-12T13:00:00.000Z"},
        {
            "type": "attachment",
            "attachment": {"type": "hook_success", "stdout": "SYSTEM NOISE " * 50},
            "timestamp": "2026-07-12T13:00:01.000Z",
            **base,
        },
        {
            "type": "user",
            "promptId": "p1",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "<ide_selection>line 4 selected</ide_selection>"},
                    {"type": "text", "text": "fix the widget crash on save"},
                ],
            },
            "timestamp": "2026-07-12T13:00:02.000Z",
            **base,
        },
        {"type": "ai-title", "aiTitle": "Fix widget crash", "sessionId": "abc"},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-5",
                "content": [
                    {"type": "thinking", "thinking": "PRIVATE THOUGHTS"},
                    {"type": "text", "text": "Looking at the save path now."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {"file_path": "src/widget.py", "old_string": "a", "new_string": "b"},
                    },
                ],
            },
            "timestamp": "2026-07-12T13:00:10.000Z",
            **base,
        },
        {
            "type": "user",
            "promptId": "p1",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
            },
            "timestamp": "2026-07-12T13:00:11.000Z",
            **base,
        },
        {
            "type": "user",
            "isMeta": True,
            "promptId": "p1",
            "message": {"role": "user", "content": [{"type": "text", "text": "SKILL BODY INJECTION"}]},
            "timestamp": "2026-07-12T13:00:12.000Z",
            **base,
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-5",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "Bash",
                        "input": {"command": "pytest tests/ -q"},
                    }
                ],
            },
            "timestamp": "2026-07-12T13:00:20.000Z",
            **base,
        },
        {
            "type": "user",
            "promptId": "p1",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t2",
                        "is_error": True,
                        "content": "1 failed: test_save",
                    }
                ],
            },
            "timestamp": "2026-07-12T13:00:21.000Z",
            **base,
        },
        {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-07-12T13:01:00.000Z"},
        {
            "type": "user",
            "promptId": "p2",
            "message": {"role": "user", "content": [{"type": "text", "text": "now add a test for it"}]},
            "timestamp": "2026-07-12T13:02:00.000Z",
            **base,
        },
    ]


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")


def test_parse_claude_session(tmp_path: Path):
    path = tmp_path / "11111111-2222-3333-4444-555555555555.jsonl"
    _write_jsonl(path, _claude_lines())
    session, turns = parse_claude_session(path)

    assert session.session_id == "claude:11111111-2222-3333-4444-555555555555"
    assert session.title == "Fix widget crash"
    assert session.cwd == CWD
    assert session.turn_count == 2
    assert session.parse_errors == 0

    first, second = turns
    assert first.turn_id.endswith(":p1")
    assert first.user_text == "fix the widget crash on save"
    assert "ide_selection_stripped" in first.flags
    assert "SKILL BODY INJECTION" not in first.user_text
    assert "PRIVATE THOUGHTS" not in first.assistant_text
    assert first.assistant_text == "Looking at the save path now."
    assert first.files == ["src/widget.py"]
    tools = {t["name"]: t for t in first.tools}
    assert tools["Edit"]["ok"] is True
    assert tools["Bash"]["ok"] is False
    assert "test_save" in tools["Bash"]["error_tail"]

    assert second.user_text == "now add a test for it"
    assert "compacted_before" in second.flags


def test_parse_claude_tolerates_corrupt_lines(tmp_path: Path):
    path = tmp_path / "11111111-2222-3333-4444-555555555555.jsonl"
    lines = [json.dumps(_claude_lines()[2]), "{not json", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    session, turns = parse_claude_session(path)
    assert session.parse_errors == 1
    assert len(turns) == 1


def _codex_lines() -> list[dict]:
    return [
        {
            "timestamp": "2026-07-12T17:00:00.000Z",
            "type": "session_meta",
            "payload": {"session_id": "s1", "cwd": CWD, "base_instructions": {"text": "You are Codex..."}},
        },
        {
            "timestamp": "2026-07-12T17:00:01.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "<permissions instructions>injected</permissions instructions>"},
        },
        {
            "timestamp": "2026-07-12T17:00:02.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "investigate the parser fallback"},
        },
        {
            "timestamp": "2026-07-12T17:00:03.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "call_id": "c1",
                "arguments": json.dumps({"command": "rg fallback src/"}),
            },
        },
        {
            "timestamp": "2026-07-12T17:00:04.000Z",
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "c1", "output": "Exit code: 0\nfound"},
        },
        {
            "timestamp": "2026-07-12T17:00:05.000Z",
            "type": "event_msg",
            "payload": {
                "type": "patch_apply_end",
                "success": True,
                "changes": {"C:\\projects\\widget\\parser.py": {"type": "update"}},
            },
        },
        {
            "timestamp": "2026-07-12T17:00:06.000Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "Fallback removed; parser now raises."},
        },
    ]


def test_parse_codex_session(tmp_path: Path):
    path = tmp_path / "rollout-2026-07-12T17-00-00-abc.jsonl"
    _write_jsonl(path, _codex_lines())
    session, turns = parse_codex_session(path)

    assert session.agent == "codex"
    assert session.cwd == CWD
    assert session.title == "investigate the parser fallback"
    assert len(turns) == 1

    turn = turns[0]
    assert turn.user_text == "investigate the parser fallback"
    assert "injected" not in turn.user_text
    assert "Fallback removed" in turn.assistant_text
    assert turn.files == ["C:\\projects\\widget\\parser.py"]
    names = [t["name"] for t in turn.tools]
    assert names == ["shell_command", "apply_patch"]
    assert turn.tools[1]["harness_verified"] is True


def test_to_local_iso_normalizes_and_sorts():
    early = to_local_iso("2026-07-12T13:00:00.000Z")
    late = to_local_iso("2026-07-12T14:00:00.000Z")
    assert early < late
    assert to_local_iso(None) is None
    assert to_local_iso("garbage") is None


def test_run_harvest_discovery_and_idempotency(tmp_path: Path):
    from conftest import write_config
    from digest.config import load_config
    from digest.harvest import run_harvest
    from digest.store_v2 import WorkStore

    claude_root = tmp_path / "claude"
    project_dir = claude_root / "c--projects-widget"
    project_dir.mkdir(parents=True)
    _write_jsonl(project_dir / "11111111-2222-3333-4444-555555555555.jsonl", _claude_lines())
    # Noise that v1 misclassified as sessions: must not be discovered.
    overflow = project_dir / "11111111-2222-3333-4444-555555555555" / "tool-results"
    overflow.mkdir(parents=True)
    (overflow / "b9fpon961.txt").write_text("tool output", encoding="utf-8")
    (project_dir / "MEMORY.md").write_text("memory", encoding="utf-8")
    (project_dir / "36220.json").write_text("{}", encoding="utf-8")

    codex_root = tmp_path / "codex"
    day_dir = codex_root / "2026" / "07" / "12"
    day_dir.mkdir(parents=True)
    _write_jsonl(day_dir / "rollout-2026-07-12T17-00-00-abc.jsonl", _codex_lines())

    config = load_config(write_config(tmp_path))
    store = WorkStore(tmp_path / "digest.sqlite")
    try:
        result = run_harvest(config, store)
        assert result.scanned == 2
        assert result.harvested == 2
        assert result.corrupt == []

        again = run_harvest(config, store)
        assert again.unchanged == 2
        assert again.harvested == 0
    finally:
        store.close()


def test_run_harvest_excludes_marked_sessions(tmp_path: Path):
    from conftest import write_config
    from digest.config import load_config
    from digest.harvest import run_harvest
    from digest.store_v2 import WorkStore

    claude_root = tmp_path / "claude"
    project_dir = claude_root / "c--projects-daily-work-digest"
    project_dir.mkdir(parents=True)
    _write_jsonl(project_dir / "11111111-2222-3333-4444-555555555555.jsonl", _claude_lines())

    config = load_config(write_config(tmp_path, codex_paths=[]))
    store = WorkStore(tmp_path / "digest.sqlite")
    try:
        result = run_harvest(config, store)
        assert result.excluded == 1
        assert result.harvested == 0
    finally:
        store.close()
