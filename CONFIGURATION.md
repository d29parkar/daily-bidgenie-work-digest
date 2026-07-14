# Configuration

Two files, both at the project root:

- **`config.yaml`**: machine-specific paths and behavior. Git-ignored. Created
  by copying `config.example.yaml`. If absent, `config.example.yaml` is used.
- **`.env`**: secrets. Git-ignored. Created by copying `.env.example`.
  Values already present in the process environment take precedence.

Validate any change with `digest doctor`.

## Environment variables (`.env`)

| Variable | Required for | Notes |
|---|---|---|
| `OPENAI_API_KEY` | LLM summarization | Without it the local rule-based fallback is used (and noted in the report). |
| `DIGEST_EMAIL_FROM` | real email send | From address. |
| `DIGEST_SMTP_USERNAME` | real email send | Gmail address. |
| `DIGEST_SMTP_PASSWORD` | real email send | Gmail **App Password** (myaccount.google.com/apppasswords), not your account password. |

Nothing is required for: ingestion, generation, dry-run send, `doctor`.

The env var *names* for email are themselves configurable (`sender_env`,
`username_env`, `password_env`), so you can point at differently-named
variables without renaming them.

## config.yaml keys

### Repos and sources

```yaml
parent_dir: '%USERPROFILE%\projects'     # base for relative repo paths; env vars expand
repos:                                   # digest scope: sessions/notes must mention one of these
  my-api-repo: '%USERPROFILE%\projects\my-api-repo'
  my-app-repo: '%USERPROFILE%\projects\my-app-repo'
primary_repo: my-api-repo                # the report centers on this repo;
                                         # secondary repos get minimal space.
                                         # Defaults to the first repo listed.
claude:
  paths: ['%USERPROFILE%\.claude\projects', '%USERPROFILE%\.claude\sessions']
  lookback_hours: 48                     # how far back sessions are considered
codex:
  paths: ['%USERPROFILE%\.codex\sessions']
  lookback_hours: 48
notes:
  path: notes                            # relative to project root
  lookback_hours: 168
```

### Output and state

```yaml
output:
  path: outputs\digests                  # generated reports
data:
  sqlite_path: data\digest.sqlite        # ingestion state + run tracking
summary:
  max_transcript_chars: 60000            # per-file read cap (middle trimmed)
  max_items_per_source: 12               # extracted snippets per category
  max_sources: 20                        # newest N sources in the report + Sources appendix
```

### Summarizer

```yaml
model:
  provider: auto        # auto | openai | local_rules | fixture
  name: gpt-4o-mini     # any OpenAI chat model; change this line for a cheaper/better model
  api_key_env: OPENAI_API_KEY
  max_prompt_chars: 24000     # evidence budget sent to the LLM
  max_output_tokens: 2000
  timeout_seconds: 120
  fixture_path: ""            # only for provider: fixture (tests/demos)
```

Cheaper model: set `name: gpt-4.1-nano` (or similar). Different vendor: add a
new `BaseSummarizer` subclass in `src/digest/llm.py` and a branch in
`resolve_summarizer`: the rest of the pipeline is provider-agnostic.

### Filters

```yaml
filters:
  exclude_session_markers:
    - daily-work-digest   # don't ingest this project's own sessions
```

### Email

```yaml
email:
  enabled: true                        # false = never send, generation still works
  recipient: you@example.com
  subject_prefix: Daily Work Digest
  sender_env: DIGEST_EMAIL_FROM
  smtp:
    host: smtp.gmail.com
    port: 587
    username_env: DIGEST_SMTP_USERNAME
    password_env: DIGEST_SMTP_PASSWORD
    use_tls: true
```

### Optional integrations (later-stage, read-only, off by default)

```yaml
integrations:
  gmail:
    enabled: false      # see docs/GOOGLE_INTEGRATIONS.md before enabling
  calendar:
    enabled: false
```

Enabling these today only adds an "enabled but not yet implemented" note to
the report's coverage section; the actual read-only ingesters are a documented
follow-up.

## v2 pipeline keys

```yaml
pipeline:
  engine: v2                       # v2 = staged pipeline (default in example); v1 = legacy summarizer
  retire_after_days: 21            # pause projects with no work units for N days
  registry_export_path: data\registry.md
  trello_skill_paths:              # your SKILL.md voice files; vendored copies are the fallback
    - '%USERPROFILE%\...\trello-card-update-skill\SKILL.md'

models:
  provider: auto                   # auto | openai | anthropic | fixture | none
  extract: gpt-4o-mini             # per-session work-unit extraction
  state: gpt-4o                    # project memory updates (few calls; strong model)
  render: gpt-4o-mini              # trello card voice rendering
  openai_api_key_env: OPENAI_API_KEY
  anthropic_api_key_env: ANTHROPIC_API_KEY
  timeout_seconds: 120
```

`provider: auto` prefers OpenAI when its key is set, falls back to Anthropic
(with a model-name swap), and otherwise runs degraded: deterministic activity
reports, project memory untouched. `digest doctor` shows the resolved setup.
