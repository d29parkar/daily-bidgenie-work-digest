"""Stage S1: discover session files and harvest them into the WorkStore.

Discovery is deliberately narrow (DESIGN_V2.md 3.1): only ``<uuid>.jsonl``
directly under a Claude project directory and ``rollout-*.jsonl`` under the
Codex sessions tree. The ``tool-results`` overflow files, memory markdown,
and shell snapshots that v1 misclassified as sessions never match.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .config import DigestConfig
from .harvest_claude import parse_claude_session
from .harvest_codex import parse_codex_session
from .store_v2 import WorkStore
from .text_utils import file_hash

logger = logging.getLogger(__name__)

CLAUDE_SESSION_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$"
)
CODEX_SESSION_RE = re.compile(r"^rollout-.+\.jsonl$")
MAX_SESSION_BYTES = 200 * 1024 * 1024


@dataclass
class HarvestResult:
    scanned: int = 0
    harvested: int = 0
    unchanged: int = 0
    excluded: int = 0
    corrupt: list[str] = field(default_factory=list)


def run_harvest(config: DigestConfig, store: WorkStore) -> HarvestResult:
    result = HarvestResult()
    lookback = max(config.claude_lookback_hours, config.codex_lookback_hours)
    cutoff = datetime.now().astimezone() - timedelta(hours=lookback)

    for agent, path in _discover(config, cutoff):
        result.scanned += 1
        if _is_excluded(path, config.exclude_session_markers):
            result.excluded += 1
            continue
        session_id = f"{agent}:{path.stem}"
        try:
            if store.get_session_hash(session_id) == file_hash(path):
                result.unchanged += 1
                continue
            parser = parse_claude_session if agent == "claude" else parse_codex_session
            session, turns = parser(path)
            if _is_excluded_text(session.cwd, config.exclude_session_markers):
                result.excluded += 1
                continue
            store.upsert_session(session, turns)
            result.harvested += 1
            if session.status == "corrupt":
                result.corrupt.append(str(path))
        except OSError as exc:
            logger.warning("Could not harvest %s: %s", path, exc)
            result.corrupt.append(str(path))
    return result


def _discover(config: DigestConfig, cutoff: datetime) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for base in config.claude_paths:
        if not base.exists():
            continue
        # <base>/<sanitized-cwd>/<uuid>.jsonl -- exactly one level deep, so
        # per-session tool-results/ subdirectories are never scanned.
        for project_dir in base.iterdir():
            if not project_dir.is_dir():
                continue
            for path in project_dir.iterdir():
                if path.is_file() and CLAUDE_SESSION_RE.match(path.name):
                    found.append(("claude", path))
    for base in config.codex_paths:
        if not base.exists():
            continue
        for path in base.rglob("rollout-*.jsonl"):
            if path.is_file() and CODEX_SESSION_RE.match(path.name):
                found.append(("codex", path))

    recent: list[tuple[str, Path]] = []
    for agent, path in found:
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size > MAX_SESSION_BYTES:
            continue
        modified = datetime.fromtimestamp(stat.st_mtime).astimezone()
        if modified >= cutoff:
            recent.append((agent, path))
    return sorted(recent, key=lambda item: str(item[1]))


def _is_excluded(path: Path, markers: list[str]) -> bool:
    return _is_excluded_text(str(path), markers)


def _is_excluded_text(text: str | None, markers: list[str]) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(marker and marker in lowered for marker in markers)
