# Operating Manual

> **v2 note:** with `pipeline.engine: v2` (the default in
> `config.example.yaml`), the scheduled `send` runs execute the staged
> pipeline from DESIGN_V2.md instead of the legacy summarizer. Everything in
> this manual (scheduling, once-per-day, dry runs, logs) works the same.
> New maintenance verbs: `digest projects list|confirm|set-goal|rollback`,
> `digest assign <unit> <project>`, `digest prune --keep-days 90`.

## One-time setup (the only manual steps, ever)

```powershell
cd <path-to>\daily-work-digest
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item config.example.yaml config.yaml
Copy-Item .env.example .env      # then edit: OPENAI_API_KEY + the 3 SMTP vars
.\scripts\install_windows_task.ps1
.\.venv\Scripts\python.exe -m digest.cli doctor   # everything should say OK/INSTALLED
```

After this, nothing needs to be run manually. If you skip the SMTP variables,
reports are still generated and saved every day; you just won't get the
email (each run prints/logs a loud "EMAIL NOT SENT" note until you add them).

## What runs automatically

| Task | Trigger | Command |
|---|---|---|
| Work Digest Morning | daily 9:00 AM, plus 5 min after logon (or Startup shortcut on the fallback path), catch-up if the laptop was asleep | `run_digest.ps1 -Mode auto` |
| Work Digest Trello | weekdays (Mon-Fri) 12:45 PM | `run_digest.ps1 -Mode trello` |
| Work Digest Night | daily 9:30 PM | `run_digest.ps1 -Mode night` |

The Trello update is a paste-ready card: a "Bigger picture" paragraph first,
then the short "Today's Update" lines, built from the morning's coding-agent
sessions. Change times with `-TrelloTime` on the installer.

`-Mode auto` picks by clock time (before 2 PM = morning brief, after = night
digest), so a logon at 8 PM produces a night digest instead of a mislabeled
"morning" email.

Both call `digest send --mode <mode> --once-per-day`: the first successful
send wins, later triggers the same day are no-ops. Change times with:

```powershell
.\scripts\install_windows_task.ps1 -MorningTime 08:00 -NightTime 22:00
```

Remove everything (tasks + startup shortcut):

```powershell
.\scripts\uninstall_windows_task.ps1
```

## Verifying the next automatic run

```powershell
.\.venv\Scripts\python.exe -m digest.cli doctor      # shows INSTALLED + task status
schtasks /Query /TN "Work Digest Morning" /V /FO LIST   # shows Next Run Time
```

To prove the whole chain right now:

```powershell
schtasks /Run /TN "Work Digest Morning"
# then check the newest log:
Get-ChildItem outputs\logs\morning-digest-*.log | Sort-Object LastWriteTime | Select-Object -Last 1
```

After any run you should see today's files in `outputs\digests\` and a
transcript in `outputs\logs\`. If a run fails, `outputs\logs\LAST_RUN_ERROR.txt`
exists and names the failing log; it is deleted on the next success.

## Running manually (if automation is unavailable)

```powershell
.\.venv\Scripts\python.exe -m digest.cli send --mode morning --once-per-day
.\.venv\Scripts\python.exe -m digest.cli send --mode night --once-per-day
# preview without sending:
.\.venv\Scripts\python.exe -m digest.cli send --mode morning --dry-run
# force a resend (e.g. after fixing SMTP):
.\.venv\Scripts\python.exe -m digest.cli send --mode morning --force
```

## Feeding it more context

- Drop reviewer feedback, meeting notes, ChatGPT exports, or markdown task
  lists into `notes/manual/` (or `notes/reviewer/` for reviewer notes). They are
  picked up on the next run (7-day lookback by default).
- Claude Code / Codex sessions are picked up automatically from their standard
  home directories.

## Reading the digest

- Citations like `[S3]` / `[G1]` point to the sources listed under
  "Source coverage and missing inputs".
- `(observed)` = directly evidenced; `(inferred)` = deduction; anything under
  **Needs verification** is unconfirmed; check it before acting on it.
- The header names the summarizer used. `local_rules (no LLM)` means the
  OpenAI call didn't happen (no key) or failed (the run note says which).

## Routine maintenance

- None required. `data/digest.sqlite` grows slowly (text snippets only); safe
  to delete anytime; it rebuilds on the next run (you lose only the
  "already sent today" memory for the current day).
- Old files under `outputs/` can be deleted freely.
