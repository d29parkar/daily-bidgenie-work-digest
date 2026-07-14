# DESIGN_V2: an agent-native work-memory layer

Status: DRAFT, awaiting review. No implementation has been done.

The goal restated in one sentence: turn raw coding-agent transcripts into an
accurate, evolving, per-project picture of what Dhiraj is building and why,
and render that picture three ways (night digest, morning brief, midday Trello
card) with zero hallucinated accomplishments.

---

## 1. Findings

### 1.1 What the current system actually does

v1 is a retrieval-less RAG pipeline with regex retrieval:

1. `ingest_sessions.py` globs `~/.claude/projects` and `~/.codex/sessions` for
   *any* file with extension `.jsonl/.json/.md/.txt/.log` modified in the last
   48h, flattens JSON to a soup of strings (`text_utils._flatten_json_strings`),
   keeps the file if the soup mentions a configured repo name.
2. `summarize.py` runs five regexes (`REQUEST_RE`, `ACTION_RE`, `COMMAND_RE`,
   `ISSUE_RE`, `REVIEWER_RE`) over the soup and stores up to 12 matching lines
   per category in SQLite.
3. `context.py` concatenates those snippet lists into a tagged evidence pack,
   truncated at 24,000 chars.
4. `llm.py` makes **one** `gpt-4o-mini` chat call per digest with a
   mode-specific prompt, validates that the response has the expected `## `
   headings, and falls back to `LocalRulesSummarizer` on failure.
5. `report.py` staples on a locally computed coverage section; `email_send.py`
   ships it.

The good parts worth keeping: the CLI shape (`ingest` / `generate` / `send` /
`doctor`), the once-per-day + `sent_at` idempotency in `store.py`, the
write-before-send discipline in `generate.py`, the scheduler scripts, the
stdlib-only HTTP client, the fixture provider for offline tests, and the
"coverage section is never LLM-generated" rule. v2 keeps all of that.

### 1.2 What the raw Claude Code session files actually contain

Location: `C:\Users\dhira\.claude\projects\<sanitized-cwd>\<session-uuid>.jsonl`,
one directory per working directory (the directory name is the cwd with
separators mangled: `c--Users-dhira-OneDrive-Desktop-projects-bidgenie-sakesh-fastapi`).
So **Claude sessions map to a project directory by construction** — no text
matching needed. Next to some session files there is a `<session-uuid>/tool-results/`
directory holding oversized tool outputs as loose `.txt`/`.json` files.

I parsed all 88 jsonl files on this machine (~100 MB). Top-level event `type`
counts: `assistant` 9487, `user` 5628, `last-prompt` 1560, `ai-title` 1491,
`attachment` 1101, `file-history-snapshot` 725, `queue-operation` 617,
`mode` 121, `system` 66, `permission-mode` 17.

The event kinds that matter:

- **Real user prompt** — `type: "user"`, no `isMeta`, `message.content` is a
  list of `text` blocks. Carries `promptId`, `uuid`, `parentUuid`, `timestamp`,
  `cwd`, `gitBranch`, `sessionId`, `version`, `entrypoint` (`claude-vscode` /
  `cli`). IDE selections are injected as an extra `text` block wrapped in
  `<ide_selection>...</ide_selection>`:

  ```json
  {"type":"user","promptId":"c957d0ec-...","message":{"role":"user","content":[
    {"type":"text","text":"<ide_selection>...may or may not be related...</ide_selection>"},
    {"type":"text","text":"Look at beast logs -- i am running a container called great_faraday --- see why it seems stuck"}]},
   "cwd":"c:\\...\\bidgenie-sakesh-fastapi","gitBranch":"model-change-validation","timestamp":"2026-07-13T22:07:12.747Z"}
  ```

- **Injected pseudo-user events** — same `type: "user"` but `isMeta: true`
  (skill bodies pushed into context) or a `tool_result` content block (tool
  output coming back; also has structured `toolUseResult`). **These are not the
  user speaking.** Crucially, tool_result events share the `promptId` of the
  user prompt that started the agentic turn, so `promptId` is a native
  turn-grouping key.

- **Assistant events** — `message.content` blocks of type `thinking`, `text`,
  `tool_use` (with tool `name` and full `input`, e.g. `file_path` for
  Edit/Write, `command` for Bash). `message.model` names the model. `usage`
  has token counts.

- **Attachments** — `type: "attachment"`, with `attachment.type` one of:
  `todo_reminder` (545), `skill_listing` (103), `deferred_tools_delta` (95),
  `agent_listing_delta` (87), `hook_additional_context` (74),
  `edited_text_file` (46), `command_permissions` (42), `queued_command` (34),
  `hook_success` (30), `nested_memory`, `date_change`, `compact_file_reference`,
  `file`, `task_reminder`, `read_truncation_notice`, `plan_mode_exit`,
  `auto_mode`. All context plumbing; none of it is work. Hook output lives
  here (`hook_success` / `hook_additional_context`), not in user messages.

- **System events** — `type: "system"` with `subtype`: `stop_hook_summary`,
  `api_error`, `turn_duration`, `compact_boundary`, `away_summary`,
  `local_command`, `scheduled_task_fire`. Compaction is a
  `system/compact_boundary` event (with `compactMetadata.preTokens`,
  `logicalParentUuid`) followed by a `type:"user"` message with
  `isCompactSummary: true` whose text is the model-written summary of the
  truncated history.

- **Bookkeeping** — `queue-operation` (enqueue/dequeue of queued prompts),
  `file-history-snapshot`, `last-prompt`, `mode`, `permission-mode`, and
  `ai-title`: a free, continuously updated LLM-written session title, e.g.
  `"Fix critical bugs in adv-max-bid before ship"`. v1 ignores this entirely.

- **Sidechains** — every event carries `isSidechain`; in all 88 files on this
  machine it is always `false` (subagent transcripts don't appear inline in
  these sessions). The parser must still handle `true` by tagging, because the
  field exists and other harness versions populate it.

**What a "turn" really is**: one real user prompt (a `promptId`) plus every
assistant/tool_result event that follows under that `promptId` until the next
real user prompt. The `uuid`/`parentUuid` chain gives exact ordering.

**The signal-to-noise number that shapes the whole design**: across all 88
files, real user prompt text + assistant `text` blocks total **1.46 MB of
99.7 MB — about 1.5%**. Tool results and attachments are ~95% of bytes. The
median session has **2** real user prompts; the max observed is 17.

### 1.3 What the raw Codex session files actually contain

Location: `C:\Users\dhira\.codex\sessions\YYYY\MM\DD\rollout-<ts>-<uuid>.jsonl`
(56 files). Different vocabulary, same information:

- `session_meta` (first line): `session_id`, `cwd`, `originator`
  (`Codex Desktop`), `cli_version`, `source` (`vscode`), and the full
  `base_instructions` system prompt (strip it).
- `turn_context`: per-turn `turn_id`, `cwd` (can differ from session cwd —
  observed turns running in `C:\Users\dhira\Documents\Codex\...` scratch dirs
  inside a session whose meta cwd is the bidgenie repo), model settings.
- `response_item` payloads: `message` (role `user`/`assistant`/`developer`,
  content `input_text`/`output_text`), `function_call` +
  `function_call_output` (shell commands), `custom_tool_call` (`apply_patch`
  with the full patch text), `reasoning` (encrypted, useless), `tool_search_*`.
- `event_msg` payloads: `user_message`, `agent_message`, `agent_reasoning`,
  `task_started` / `task_complete` (explicit turn boundaries with `turn_id`),
  `token_count`, and — the gold one — **`patch_apply_end` with
  `success: true/false` and per-file `unified_diff`**: ground-truth file
  changes recorded by the harness itself, not claimed by the model.
- `compacted` with a `replacement_history`, plus `event_msg/context_compacted`.
- Beware duplication: a user prompt appears both as `event_msg/user_message`
  and as `response_item/message` role=user. Injected developer/permissions/
  app-context blocks also arrive as role=`user`/`developer` `input_text` —
  filter by role and by `<permissions instructions>` / `<app-context>` /
  `<user_instructions>` / `<ENVIRONMENT_CONTEXT>` wrappers.

### 1.4 The Trello voice that v1 never uses

`bidgenie-sakesh-fastapi/.claude/skills/trello-card-update-skill/SKILL.md` +
its parent `dhiraj-writing-style/SKILL.md` define the actual contract for the
midday card: first person, short, "Today's Update" with the four-line shape
(completed / main finding / moving toward / blocker), three alternate formats
(Current Direction, Discovery Update, Implementation Update), backticked real
technical names, a discovery pass reported as a discovery pass, and hard bans
(no em dashes, no "made significant progress", no AI symmetry, no fake polish).
v1's `trello_update.md` prompt paraphrases maybe 30% of this from memory and
inherits none of the alternate formats, the tone calibration, or the
anti-AI-pass. v2 must consume the skill files themselves.

---

## 2. Diagnosis: why v1 output is bad, mapped to code

1. **It reads the wrong bytes.** `text_utils._flatten_json_strings` flattens
   *everything with an interesting key name* — tool outputs, skill bodies,
   hook payloads, `<ide_selection>` wrappers — into one string pile, then
   `trim_middle` keeps 60k chars of it. Since real signal is ~1.5% of bytes,
   the evidence pack is ~98% machine noise, and `summarize.py`'s regexes then
   pick "signal" lines out of noise. The growing hand-tuned blocklist in
   `summarize._is_low_signal_line` (`"you are codex"`, `"skill's standing
   warning"`, ...) is the fossil record of this losing battle.

2. **It ingests things that aren't sessions.** The glob in
   `ingest_sessions._discover_files` accepts `.txt/.json/.md` anywhere under
   `.claude/projects`, so the Sources appendix of the 2026-07-07 morning brief
   lists `36220.json`, `b9fpon961.txt`, `MEMORY.md`, and
   `toolu_01LNX8D9cFp4MpsNHRhHJf68.txt` as "Claude Code sessions". Those are
   overflow tool outputs from `<uuid>/tool-results/` and memory files. Claims
   get cited against garbage, which makes citations meaningless.

3. **No goal, no unit of work.** The regexes have no concept of "what was
   asked". `REQUEST_RE` matches any line containing the word "please" or
   "fix" — including the agent's own output and code comments. So the digest
   reports edits without intent ("edited llm.py"), exactly the complaint.

4. **Project attribution is a substring match against a fixed repo list.**
   `is_related_to_repos` + `config.repos` means: (a) anything outside the two
   bidgenie repos is silently dropped — Virginia work and personal projects
   never appear; (b) a session that merely *mentions* the repo name gets
   attributed to it. Meanwhile the directory structure of `.claude/projects`
   already encodes the true cwd, unused.

5. **No memory.** `generate.py` re-derives everything from a 48h lookback
   window every run. There is no representation of "the goal of the
   model-change-validation effort" or "yesterday we believed X". The digest is
   a snapshot generated by an amnesiac.

6. **Claims are treated as facts.** The single LLM call sees regex snippets
   in which the agent says "all tests passing" and reports "Completed final
   sanity checks with all tests passing" (2026-07-06 night digest). No commit,
   no diff, no test log was checked. Git evidence exists (`ingest_git.py`) but
   arrives as just another evidence block for the same single prompt, so
   reconciliation is left to gpt-4o-mini's discretion at temperature default.

7. **PR reviews aren't even modeled.** The only review-related signal is
   `REVIEWER_RE` matching the word "reviewer" in transcripts. There is no PR
   data source, no notion of author vs reviewer, so review direction cannot be
   fixed inside v1's architecture at all — it needs a real GitHub source.

8. **The Trello card ignores the Trello skill.** See 1.4. Also
   `OPERATING_MANUAL.md` says the Trello update is "built from the morning's
   coding-agent sessions", but `generate.py` uses the same 48h window as the
   other modes, so the card routinely re-reports yesterday.

9. **One LLM call, one budget.** 24k chars of evidence for nine output
   sections. Nothing between "one giant call" and "regex fallback".

None of this is fixable by prompt-tuning the single call. The input has to be
rebuilt from the transcript structure, and state has to persist between runs.

---

## 3. Proposed architecture

### 3.0 Shape

Seven stages, but not the seven in your sketch. Two changes, argued:

- **Your Stage 2 (clean/segment) and Stage 4 (intent extraction) merge.**
  Segmenting turns into units of work *is* an intent judgment — you cannot
  decide whether prompt 3 continues prompt 1's task without understanding
  both. Running a "segmenter" LLM and then an "intent" LLM doubles cost to
  produce an artifact (intentless segments) that nothing consumes. One call
  per session-day does both: it reads the cleaned turn sequence and emits
  work units *with* intent, outcome, and status.

- **Your Stage 5 (corroboration) is deterministic code, not an LLM.** Matching
  claimed files against `git diff` file lists, claimed commits against
  `git log`, and claimed "done" against a clean working tree is set
  intersection, not judgment. The LLM never gets to grade its own homework;
  it receives the verdicts as input. The only LLM-ish part (deciding what a
  unit's completion claim *was*) already happened in extraction.

The pipeline, with data flowing through SQLite between stages:

```
S1 harvest      (deterministic)  raw JSONL -> sessions, turns
S2 extract      (LLM, per session-day)  turns -> work_units (intent, outcome, status, claims)
S3 attribute    (deterministic + rare LLM)  work_units x project registry -> project_id
S4 corroborate  (deterministic)  claims x git/PR facts -> verification verdicts
S5 update state (LLM, per active project)  registry + verified units -> new registry version
S6 render       (LLM, per artifact)  registry version + units -> night email / morning brief / trello card
S7 deliver      (deterministic)  existing email_send / file outputs
```

Every stage writes its output to SQLite keyed deterministically, so a rerun
overwrites the same keys instead of appending duplicates, and a crash resumes
at the first incomplete stage.

### 3.1 Stage S1 — Harvest (deterministic)

**Input contract**: the configured Claude/Codex roots; only files matching
`<uuid>.jsonl` directly under a `.claude/projects/<dir>/` (never inside
`<uuid>/tool-results/`) and `rollout-*.jsonl` under `.codex/sessions/`. The
`.txt/.json/.md/.log` extensions are dropped from session discovery entirely.

**What it does**: stream-parse each file line by line and reduce it to
normalized `Session` and `Turn` rows.

Per event, the keep/strip policy:

| Event | Policy |
|---|---|
| user, real prompt | keep text verbatim; strip `<ide_selection>` blocks into a `context_hint` field |
| user, `isMeta` / skill body | drop; record skill name if identifiable |
| user, `tool_result` | drop content; keep error-tail (last 300 chars) only when the paired tool_use failed |
| user, `isCompactSummary` | keep, flagged `kind=compact_summary` (model-written: low trust) |
| assistant `text` | keep, capped at 4,000 chars per block (tail-trimmed) |
| assistant `thinking` | drop |
| assistant `tool_use` | keep tool name + salient input: `file_path` (Edit/Write/Read), `command` first 200 chars (Bash), `skill` name; full input dropped |
| attachment (all types) | drop; count per type for diagnostics |
| system `compact_boundary`, `api_error`, `turn_duration` | keep as turn metadata |
| ai-title | keep last value as `session.title` |
| Codex `patch_apply_end` | keep `success` + changed file list + diff stat (ground truth!) |
| Codex `event_msg/user_message` vs `response_item/message` | dedupe; role=developer and `<permissions instructions>`/`<app-context>`-wrapped blocks dropped |

**Turn assembly**: Claude — group by `promptId` (fallback: split at each real
user prompt, ordered by `parentUuid` chain). Codex — group by `turn_id` from
`turn_context` / `task_started`.

**Output contract** (SQLite, see §4): one `sessions` row per file, one `turns`
row per turn with:

```json
{
  "turn_id": "claude:0adac3a2:c957d0ec",
  "session_id": "claude:0adac3a2-c458-446a-9e62-827f9a457bc4",
  "seq": 3,
  "started_at": "2026-07-13T22:07:12Z", "ended_at": "...",
  "user_text": "Look at beast logs -- i am running a container called great_faraday...",
  "assistant_text": "...concatenated text blocks, capped...",
  "tools_used": [{"name": "Bash", "detail": "docker logs great_faraday --tail 200", "ok": true}],
  "files_touched": ["modules/.../rfp_analyzer_graph_nodes.py"],
  "cwd": "c:\\...\\bidgenie-sakesh-fastapi",
  "git_branch": "model-change-validation",
  "model": "claude-sonnet-5",
  "flags": ["compacted_before"],
  "source_lines": [4, 41]
}
```

`source_lines` is the provenance anchor: every downstream claim cites
`turn_id`s, and a `turn_id` resolves to (file path, line span).

**Idempotency**: `sessions.content_hash`; unchanged file → skip. Changed file
(sessions append) → delete+reinsert that session's turns inside a
transaction. Turn ids are deterministic, so re-harvesting cannot duplicate.

**Failure**: a JSON-broken line is skipped and counted in
`sessions.parse_errors`; a wholly unreadable file gets `status='corrupt'` and
appears in the coverage section. No exception escapes the stage.

**Cost/latency**: zero LLM. Parsing 100 MB of JSONL in Python ≈ seconds.

### 3.2 Stage S2 — Extract work units (LLM, the workhorse)

**Unit of work decision**: the unit is a **task thread**: a maximal run of
consecutive turns in one session serving one intent. Justification from the
data: the median session has 2 real prompts (usually one task plus a
follow-up), so *session ≈ task* is the 80% case and the session boundary is a
strong prior; but 10-17-prompt sessions exist and provably mix tasks (e.g.
"Harden daily work digest for production", 17 prompts). Cross-session merging
(the same task resumed in a new session) is deliberately *not* done here —
it's handled at the project-day rollup in S5, where both units are visible
side by side. Splitting is local and cheap; merging needs project context.

**Input contract**: for each session with ≥1 turn overlapping the digest
window: session header (title from `ai-title`, cwd, branch, model) + the turn
sequence rendered compactly (user_text verbatim — it is the scarcest, highest
value text — assistant_text trimmed to ~1,500 chars/turn, tools/files as
lists). Budget ≈ 15k tokens/call; sessions bigger than that get
assistant_text squeezed first, never user_text.

**Output contract** (strict JSON, schema-validated):

```json
{
  "work_units": [{
    "unit_key": "claude:0adac3a2:1",
    "turn_ids": ["claude:0adac3a2:c957d0ec", "..."],
    "intent": "Diagnose why the great_faraday container hangs during RFP upload on Beast",
    "kind": "debugging",            // debugging|feature|discovery|review|docs|ops|refactor|other
    "outcome_claim": "Root cause identified: S3 client retries on missing LocalStack env var; fix not applied",
    "status_claim": "in_progress",  // done|in_progress|abandoned|blocked
    "files_touched": ["..."],       // union of harvest facts, not model memory
    "entities": ["great_faraday", "upload_rfp", "LocalStack"],
    "claims_to_verify": [
      {"type": "commit", "text": "committed the sentinel-callback fix"},
      {"type": "tests_pass", "text": "pytest suite green after change"}
    ],
    "open_questions": ["Should BudgetIndicatorEngine join the default tier list?"],
    "user_corrections": ["User rejected the broad rewrite, asked for smallest fix"]
  }]
}
```

**Deterministic vs LLM**: LLM. Segmentation-by-intent and status judgment are
language tasks; everything mechanical (files, tools, timestamps) is computed
in S1 and merely *confirmed* by the model, then overwritten with S1 facts
regardless of what the model outputs (the model cannot invent
`files_touched`).

**Model**: the configured cheap tier (today `gpt-4o-mini`; config key stays
`model.name`). Reason: the call is per-session, runs up to ~15×/day, and the
task is extraction over supplied text, not synthesis. A `model.extract_name`
override allows upgrading independently of the render model.

**Prompt responsibility**: segment turns into units; write intent in terms of
the *user's ask*, not the agent's activity; separate what-was-claimed from
what-is-known; label discovery as discovery. It is explicitly *not*
responsible for prose quality, project naming, or truth-grading.

**Cost/latency**: ~5-15 calls/day × (~12k in / 1k out) ≈ $0.02-0.04/day at
4o-mini prices; ~30-60 s wall clock run serially.

**Fail-safe**: JSON parse + schema check; one retry with the validator errors
appended; on second failure the session gets a single **stub unit** built
deterministically (intent = `ai-title`, status `unknown`, files from S1) and
is flagged `extraction_failed` in the digest's coverage section. State updates
(S5) treat stub units as activity evidence but never rewrite goals from them.

### 3.3 Stage S3 — Project attribution (deterministic first, LLM as exception)

**The registry** (see §4 for schema) holds one row per project: canonical
name, status (`active|paused|retired|provisional`), and *matchers*: cwd path
prefixes, repo names, branch patterns, keywords. Registry ≠ config repos: a
project can span multiple repos (`bidgenie-sakesh-fastapi` +
`bidgenie-sakesh-app` → one "BidGenie" project) and can exist with no repo at
all.

**Resolution order** per work unit:
1. cwd prefix match against `project_matchers` (covers ~100% of Claude
   sessions since cwd is in every event, and Codex via `turn_context.cwd`);
2. explicit `unit_overrides` (human corrections, §6.4) — these always win;
3. keyword/branch matchers;
4. otherwise: the unit goes to the **triage LLM micro-call** (only unmatched
   units, batched into one call/day): given the registry summaries and the
   unit's intent/cwd/files, it either assigns an existing project or proposes
   a new one `{name, matchers, evidence}`.

**New projects are never silently invented**: a proposal creates a
`provisional` registry row; the night digest gets a "New project detected:
LotusPetal — confirm or reassign (`digest projects confirm lotuspetal`)"
line. Provisional projects render normally but are marked, and until
confirmed their goal field stays "unconfirmed".

**Output contract**: `work_units.project_id` set on every unit; new
`projects` rows with `status='provisional'`.

**Fail-safe**: if the triage call fails, unmatched units land in the built-in
`_inbox` project, which renders as its own digest section ("Unattributed
work") rather than polluting a real project.

**Cost**: usually zero LLM calls; at most one small one per day.

### 3.4 Stage S4 — Corroboration (deterministic)

**Ground-truth collectors** (all code, no LLM):

- **Git**: for every repo path attached to any project touched today, collect
  `git log --since <window> --name-only --format=...` (commits with authored
  files), current branch, status, diff stat. This extends `ingest_git.py` to
  record *per-commit file lists* into a `git_facts` table instead of a blob.
- **Codex patches**: `patch_apply_end` events from S1 are already ground
  truth (harness-recorded diffs), stored on the turn.
- **PRs (new, optional)**: if `gh` CLI is installed and authed, per repo:
  `gh pr list --author @me --json ...` and
  `gh search prs --reviewed-by @me --json ...` (plus review events on my PRs).
  Stored in `pr_facts`. If `gh` is absent, the whole source is marked
  `unavailable` in coverage and nothing PR-related is claimed. **The review
  direction rule is enforced here in code, not in a prompt**:
  - `pr.author == me` and reviews/comments by others → `inbound_feedback`.
  - `pr.author != me` and a review/comment authored by me → `review_done`.
  Inbound feedback renders under its own heading ("Feedback received on my
  PRs"), never as review work.

**Verdict computation** per work unit:

| Check | Verdict input |
|---|---|
| unit claims `done` + files_touched ⊆ files in some commit within window | `corroborated_by_commit` |
| files_touched ∩ current `git status` dirty files ≠ ∅ | `uncommitted` |
| Codex `patch_apply_end.success` covering files | `applied_by_harness` |
| claim `tests_pass` but no test command in tools_used and no CI info | `unverified_claim` |
| unit claims `done`, no commit, no dirty file overlap | `contradicted` (files unchanged) |

**Output contract**: `work_units.verification` JSON:
`{"verdict": "corroborated|partial|unverified|contradicted", "evidence": ["commit:abc123", "git_status:dirty:budget_utils.py"]}`.

**Fail-safe**: git command failure → verdict `unknown` + coverage note
(current 20s-timeout `_run_git` pattern kept). Verdicts degrade to
`unverified`, never invent corroboration.

**Cost**: zero LLM. A few git/gh subprocesses.

### 3.5 Stage S5 — Project state update (LLM, per active-today project)

This is the stage that turns the tool from a summarizer into a system.

**Input contract**: the project's current registry state (see schema: `goal`,
`system_state`, `open_threads`, `recent_history` — the last ~7 daily deltas),
plus today's verified work units for that project (intents, outcomes,
verdicts, open questions, user corrections), plus git/PR facts. Typical size:
2-5k tokens.

**Output contract** (strict JSON):

```json
{
  "goal": "Ship the model-change validation harness so prompt regressions are caught before model swaps",
  "goal_changed": false,
  "system_state": "Harness skeleton merged; sweep.py timeout fixed; tier-list membership still undecided",
  "narrative_delta": "Today moved the harness from design to a working sweep: ...",
  "open_threads": [
    {"text": "Decide BudgetIndicatorEngine tier membership", "since": "2026-07-06", "status": "open"},
    {"text": "sweep.py timeout", "since": "2026-07-05", "status": "resolved_today"}
  ],
  "evidence": {"narrative_delta": ["unit:claude:0adac3a2:1", "commit:abc123"]}
}
```

**Write semantics**: appended as a new row in `project_state_versions` keyed
`(project_id, as_of_date)` — a rerun of the same date **replaces** that
version rather than stacking, so reruns cannot compound state drift. The
previous version is never mutated; `projects.head_version` moves forward.
Yesterday's state is therefore always reconstructable, and a bad update is
revertible (`digest projects rollback <name> <date>`).

**Model**: the *strong* configured model (`model.state_name`, default
something like `gpt-4o` / `gpt-4.1`, or Claude if we add an Anthropic
provider — open question §6.2). Reason: this is the memory backbone; a wrong
goal update poisons every later day. It is also tiny (2-4 calls/day), so the
stronger model costs cents.

**Prompt responsibility**: conservative merging. Goals change only on
explicit evidence of a pivot (a user prompt saying so, or sustained work on
something else); `contradicted`/`unverified` units may update open threads
("agent believes X is fixed, unverified") but never mark a goal milestone
done. Every sentence in `narrative_delta` must cite unit/commit ids
(validated in code: unknown citation → retry once → drop the sentence).

**Retirement**: deterministic, not LLM: no work units for
`retire_after_days` (default 21) → status `paused`; a paused project touched
again reactivates. `digest projects retire <name>` forces it. Retired
projects keep their history and stop rendering.

**Fail-safe**: on LLM failure the registry is **left untouched** for that
project and the digest renders from the previous version with a run note
("state not updated for BidGenie: API error"). Stale-but-true beats
fresh-but-fabricated.

### 3.6 Stage S6 — Render (LLM per artifact, deterministic frame)

All three artifacts are rendered from the same substrate — per-project state
versions + today's verified units — with the coverage/sources section always
computed in code (kept from v1).

**(a) Night digest email** (per-project sections, ordered by activity):
For each active-today project: goal line (from registry), narrative delta,
completed (only `corroborated`/`applied_by_harness`), in progress,
contradicted-or-unverified claims ("agent said done, repo disagrees"), open
threads, inbound PR feedback, next actions. One LLM call total (input:
rollups, not raw units — ~4-6k tokens), or zero calls: since S5 already
produced cited narrative text, the night email can be assembled ~fully
deterministically from S5 outputs. **Recommendation: deterministic assembly
with a small LLM polish pass** that may only rephrase, not add content, and is
diff-checked for new numbers/filenames (a cheap grounding check: any token
that looks like a filename/function/number in the output must appear in the
input).

**(b) Morning brief**: generated at 9 AM from the head registry versions +
yesterday's units + *current* git status (things may have changed overnight).
Focus: "where you left off, first action per project, open threads sorted by
staleness". One cheap LLM call, same grounding check.

**(c) Midday Trello card**: input = the Trello-scoped project's (config:
`trello.project`) state version + today-so-far units (the run re-harvests the
morning's sessions first — this fixes the v1 bug of re-reporting yesterday).
The prompt **embeds the actual skill files**: `config.trello.skill_paths`
points at `trello-card-update-skill/SKILL.md` and
`dhiraj-writing-style/SKILL.md` in the bidgenie repo; the renderer reads them
at run time (hash-logged, so a skill edit is visible in run notes) and
instructs the model to pick the right format variant (Discovery / Current
Direction / Implementation) based on the day's unit kinds. Fallback if the
files are missing: a vendored copy under `src/digest/prompts/vendored/`, plus
a coverage warning.

**Fail-safe for all renders**: LLM failure → the deterministic assembly (a)
without polish, a plain "state + units" listing for (b), and for (c) a
clearly marked "RAW - do not paste" fallback card, since an off-voice card is
worse than no card.

### 3.7 Stage S7 — Deliver

Unchanged from v1: write files first, then SMTP; `sent_at` idempotency;
`--dry-run`. The Trello card remains paste-ready text in the email/file (no
Trello API write in this phase; see §6.5).

### 3.8 Cross-cutting: cost & token budget

Worst observed day ≈ 15 sessions. Budget: 15 extract calls × ~13k tokens +
1 triage + 3 state updates × ~4k + 2 render calls × ~5k ≈ **250k in / 25k out
tokens/day ≈ $0.05-0.10/day** on mini-tier models with the strong model only
on S5. Latency: the night run does S1-S6 in ~2-4 minutes serially; morning
and trello runs reuse S5 state and only harvest/extract the delta since the
last run (incremental by `sessions.content_hash` + window), typically <1 min.

### 3.9 Cross-cutting: degradation ladder

| Condition | Behavior |
|---|---|
| No API key | S1, S3 (matchers), S4 run; S2/S5/S6-LLM skipped. Output = deterministic **activity report**: per project, session titles, verbatim first user prompt per session, files touched, commits. Clearly labeled "no LLM: activity only". Registry untouched. |
| API fails mid-run | Stage checkpointing: completed stages persist; next run resumes. Digest renders from whatever is complete, run note names the failed stage. |
| Corrupt session file | Skipped with `status='corrupt'`, listed in coverage. |
| Day with no work | Short "quiet day" email (registry states + nothing-new note); registry untouched; morning brief still renders from state. |
| Registry empty (first run) | Bootstrap mode: S3 proposes projects from all sessions in `seed_lookback_days` (default 14); digest is prefixed with a "review these projects" block. |

---

## 4. Data model (SQLite, additive to the existing DB)

Existing `sources` / `digest_runs` tables are kept for compatibility during
migration (v1 notes ingestion still uses `sources`). New tables:

```sql
create table sessions (
  session_id    text primary key,          -- 'claude:<uuid>' | 'codex:<uuid>'
  agent         text not null,             -- claude | codex
  path          text not null,
  content_hash  text not null,
  title         text,                      -- last ai-title, if any
  cwd           text,
  git_branch    text,
  started_at    text, ended_at text,
  turn_count    integer not null default 0,
  parse_errors  integer not null default 0,
  status        text not null default 'ok',   -- ok | corrupt | excluded
  harvested_at  text not null
);

create table turns (
  turn_id       text primary key,          -- '<session_id>:<promptId|turn_id>'
  session_id    text not null references sessions(session_id),
  seq           integer not null,
  started_at    text, ended_at text,
  user_text     text,
  assistant_text text,
  tools_json    text not null default '[]',
  files_json    text not null default '[]',
  cwd           text, git_branch text, model text,
  flags_json    text not null default '[]',
  line_start    integer, line_end integer   -- provenance anchor into the jsonl
);
create index idx_turns_session on turns(session_id, seq);
create index idx_turns_time on turns(started_at);

create table projects (
  project_id    text primary key,          -- slug
  name          text not null,
  status        text not null default 'active',  -- active|provisional|paused|retired
  created_at    text not null,
  head_version  integer not null default 0,
  trello_scope  integer not null default 0  -- include in trello card?
);

create table project_matchers (
  project_id    text not null references projects(project_id),
  kind          text not null,             -- cwd_prefix | repo_name | branch_glob | keyword
  pattern       text not null,
  source        text not null default 'seed',  -- seed | llm_proposal | human
  created_at    text not null,
  primary key (project_id, kind, pattern)
);

create table project_state_versions (
  project_id    text not null references projects(project_id),
  version       integer not null,
  as_of_date    text not null,             -- one version per project per day; rerun replaces
  goal          text,
  system_state  text,
  narrative_delta text,
  open_threads_json text not null default '[]',
  evidence_json text not null default '{}',
  written_by    text not null,             -- model label | 'human' | 'bootstrap'
  created_at    text not null,
  primary key (project_id, version),
  unique (project_id, as_of_date)
);

create table work_units (
  unit_key      text primary key,          -- '<session_id>:<n>' (deterministic)
  work_date     text not null,             -- digest day it belongs to
  project_id    text references projects(project_id),
  turn_ids_json text not null,
  intent        text, kind text,
  outcome_claim text, status_claim text,
  files_json    text not null default '[]',
  entities_json text not null default '[]',
  claims_json   text not null default '[]',
  open_questions_json text not null default '[]',
  verification_json text not null default '{}',
  extraction    text not null default 'llm',   -- llm | stub
  created_at    text not null
);
create index idx_units_date on work_units(work_date, project_id);

create table git_facts (
  fact_id       text primary key,          -- sha256(repo|kind|ref)
  repo_path     text not null, kind text not null, -- commit | status | branch
  ref           text,                      -- commit sha etc.
  observed_at   text not null,
  data_json     text not null
);

create table pr_facts (
  fact_id       text primary key,
  repo          text not null,
  pr_number     integer not null,
  author        text, my_role text,        -- author | reviewer
  kind          text not null,             -- review_done | inbound_feedback | pr_state
  observed_at   text not null,
  data_json     text not null
);

create table unit_overrides (              -- human corrections, survive reruns
  unit_key      text primary key,
  project_id    text,
  note          text,
  created_at    text not null
);

create table pipeline_runs (
  run_id        text primary key,
  run_date      text not null, mode text not null,
  stage_status_json text not null,         -- {"harvest":"ok","extract":"ok",...}
  started_at    text, finished_at text
);
```

Retention: `turns.user_text/assistant_text` duplicate transcript content;
a `digest prune --keep-days 90` deletes old turn text (keeping ids and files
for provenance) since the jsonl files remain the ultimate source.

Registry mirror: `data/registry.md` is exported after every S5 run — a
human-readable snapshot of all projects (goal, state, threads). Read-only
mirror; edits go through the CLI (§6.4). This gives you a reviewable "what
does the system believe" file without making markdown the source of truth.

---

## 5. Prompt design per LLM stage

Common rules (shared system preamble, replacing today's `system.md`):
written-in-Dhiraj's-voice rules move to the render prompts only; extraction
and state prompts are pure JSON emitters, temperature 0, `response_format`
json_object where supported.

### 5.1 Extraction (S2)

- System: "You segment a coding-agent session into units of work. The USER
  messages are the authority on intent; ASSISTANT messages are claims by an
  agent, not facts. A unit is a maximal run of consecutive turns serving one
  intent. Statuses mean: done = the agent claims completion AND the user did
  not contradict it; blocked = waiting on something named; abandoned = the
  user redirected away without completion."
- User: session header + numbered turns + the JSON schema + two worked
  examples (one single-unit session, one 3-unit session with a mid-session
  pivot — taken from real anonymized transcripts in `tests/fixtures/`).
- Hard rules in-prompt: never merge turns across a user pivot; copy
  `open_questions` only from explicit question text; `user_corrections`
  capture the user overriding the agent (these matter for goal updates);
  every unit lists `turn_ids` (validated in code).

### 5.2 Attribution triage (S3, rare)

- Input: registry one-liners (`id, name, goal-fragment, matchers`) + the
  orphan units (intent, cwd, files, branch).
- Output: `{assignments: [{unit_key, project_id}], proposals: [{name, matchers, evidence_unit_keys}]}`.
- Rule: prefer assignment over proposal; propose only when no project's goal
  plausibly covers the unit; never propose from a single sub-5-minute unit.

### 5.3 State update (S5)

- Input: previous state (goal, system_state, open_threads with ages,
  last 7 narrative_deltas), today's units with verification verdicts, git/PR
  facts.
- Responsibilities, stated in the prompt: (1) merge conservatively — the goal
  survives unless evidence shows a pivot; (2) resolve open threads only with
  corroborated evidence; (3) unverified "done" claims become open threads
  ("claimed fixed, not verified against repo"), not accomplishments;
  (4) every `narrative_delta` sentence carries `[unit:...]`/`[commit:...]`
  citations; (5) output nothing that isn't derivable from input (code-side
  citation check enforces).

### 5.4 Renders (S6)

- Night email polish: "You may rephrase and reorder; you may not introduce
  any file name, function, number, or claim not present in the input" +
  the no-em-dash / no-filler voice rules inherited from `dhiraj-writing-style`
  (the relevant "hard constraints" + "anti-AI" sections are embedded
  verbatim, loaded from the skill file).
- Morning brief: same, plus "output is ordered by 'first action per
  project'".
- Trello: system = concatenation of `dhiraj-writing-style` hard-rules
  sections + full `trello-card-update-skill` SKILL.md, then: "Pick the
  matching format variant (Default / Discovery / Current Direction /
  Implementation) from the day's unit kinds. Output only the paste-ready card
  plus the Bigger picture paragraph." Evidence = the Trello-scoped project's
  state + today's units. The final quality checklist in the skill is included
  and the model is told to apply it before answering.

---

## 6. Open questions for you

1. **Project seed list.** I can see BidGenie (2 repos), this digest project,
   portfolio (2 dirs), aarya-assignment in `.claude/projects`. You mentioned
   LotusPetal and "Virginia work" — neither name appears in any session cwd on
   this machine. Are those the same repos under different names (is BidGenie
   = LotusPetal?), remote work, or work from another machine? I need the
   seed registry entries: name, paths/repos, current goal, one line of state.
2. **Model provider.** Keep OpenAI-only (config today), or add an Anthropic
   provider class so S5 (state) can run on a Claude model? The design is
   provider-agnostic; this is a config/key decision. My recommendation:
   add the Anthropic branch, keep mini-tier OpenAI for S2 extraction.
3. **`gh` CLI**: is it installed and authed on this machine, and do the
   BidGenie repos live on GitHub with you as `d29parkar`? Without it, PR
   review counting stays out of scope and the digest simply never mentions
   reviews (which is at least no longer *wrong*).
4. **Human correction surface**: I propose CLI verbs
   (`digest projects list|confirm|rename|retire|set-goal`,
   `digest assign <unit_key> <project>`, `digest projects rollback`) plus the
   read-only `data/registry.md` mirror. Do you also want the correction loop
   inside the *email* (e.g. reply-parse)? I recommend not in this phase.
5. **Trello delivery**: card stays paste-ready text (current behavior) vs.
   writing to the Trello API directly. Paste-ready recommended for now; API
   write is a small later add.
6. **Turn-text retention**: is storing cleaned turn text in SQLite (with
   `digest prune`) acceptable, or do you want extraction-only storage with
   pointers back into the jsonl (cheaper, but reprocessing needs the original
   files to still exist)?
7. **Morning brief timing**: generate the morning brief at night together
   with the digest (same data, ready at 9 AM instantly) or at 9 AM with fresh
   git state? I lean 9 AM fresh-git (it catches overnight CI/merge changes)
   but it's a one-line scheduling choice.

---

## 7. Rejected alternatives

- **Embedding/vector store over transcripts (RAG).** Retrieval is not the
  problem; the corpus per day is small and fully enumerable. Structure-aware
  parsing + per-session extraction reads *everything relevant* for less than
  the cost of maintaining an index. Rejected as accidental complexity.
- **Per-turn LLM calls.** 100+ calls/day for turns that are mostly "one Bash
  command and its output". Sessions are the natural batching unit; per-turn
  adds cost and loses cross-turn intent.
- **One mega-call over the raw JSONL** ("just give a big model the files").
  Even at 1M-token context, one day can exceed 100 MB raw; 98% is tool noise;
  and a single call gives no persistent state, no idempotent intermediate
  artifacts, no provenance ids. It's v1 with a bigger budget.
- **Clustering/segmenting turns with embeddings or heuristics before the
  LLM.** Segmentation without intent understanding produced the wrong units
  in exactly the cases that matter (pivots mid-session); and the LLM has to
  read the turns anyway to extract intent, so pre-segmentation saves nothing.
- **Letting the LLM see git state and self-grade claims (v1's approach).**
  Verification must be adversarial to the claim source. Set-intersection code
  can't be sweet-talked by an optimistic transcript.
- **Rewriting on an agent framework / LangGraph / etc.** The pipeline is a
  linear DAG with SQLite checkpoints; a framework adds dependencies to a
  stdlib-only project without adding capability.
- **Driving the pipeline with Claude Code itself on a schedule** (agent reads
  transcripts, updates memory). Attractive symmetry, but scheduled-headless
  reliability, cost control, and determinism are all worse than a plain
  Python pipeline making 3 kinds of narrow LLM calls.
- **Markdown files as the registry source of truth.** Human-editable, but
  concurrent writes (human edit + nightly run) corrupt silently and diffs
  don't enforce schema. SQLite owns truth; markdown is an exported mirror;
  edits go through CLI verbs that write matcher rules (so corrections are
  durable and auditable).
- **Fixing v1's regex extractor incrementally.** The blocklist in
  `summarize._is_low_signal_line` is already 40 lines of whack-a-mole. The
  input representation (flattened string soup) discards the structure that
  carries all the signal; no amount of filtering recovers it.

---

## Appendix A: v1 → v2 module map

| v1 | v2 fate |
|---|---|
| `cli.py` | extended: `harvest`, `pipeline`, `projects *`, `assign`, `prune` subcommands; `ingest/generate/send/doctor` kept |
| `ingest_sessions.py` + `text_utils` flattening | replaced by `harvest_claude.py` / `harvest_codex.py` (structured parsers) |
| `summarize.py` | deleted (regex extractor); git-state summary moves into S4 |
| `context.py` | replaced by per-stage input builders |
| `llm.py` | generalized: provider classes + per-stage model config + JSON mode |
| `report.py`, `email_*` | kept, fed by S6 |
| `ingest_git.py` | extended into `facts_git.py` (+ optional `facts_gh.py`) |
| `store.py` | extended with §4 tables |
| `prompts/*.md` | replaced by the §5 set; trello prompt sources the skill files |
| `ingest_notes.py` | kept as-is (notes become additional evidence attached to units by keyword, later) |

## Appendix B: real schema examples collected during this design

Claude turn grouping key observed in the wild: `promptId` is present on the
user prompt *and* on the tool_result user-events of the same agentic turn
(session `0adac3a2`, lines 4-14). Compaction pair observed in session
`085aca0d`: `system/compact_boundary` with `compactMetadata.preTokens: 178208`
followed by `user` with `isCompactSummary: true`. Codex ground-truth diff
observed in `rollout-2026-06-30T17-07-50`: `event_msg/patch_apply_end` with
`success: true` and per-file `unified_diff`. Signal ratio measured across all
88 Claude files: user+assistant text = 1.46 MB of 99.7 MB (1.5%); median real
user prompts per session = 2, max = 17.
