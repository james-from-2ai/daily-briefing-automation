# daily-briefing-cowork plugin (Phase 1 scaffold)

Cowork version of the daily briefing. Synthesis runs in a Claude Code
agent's own reasoning context (billed under Claude.ai subscription)
instead of via `anthropic.Anthropic()` API calls in `daily_briefing.py`.

## Status

🚧 **Scaffold only.** Plugin structure, skill prompts, and helper-script
stubs are in place. Real plumbing is not. See TODOs in each file.

## Architecture

```
Windows Task Scheduler (or manual invoke)
  ↓
  claude -p "/daily-briefing"
  ↓
skills/daily-briefing.md (agent reads instructions)
  ↓
  python helpers/pull_inputs.py        → JSON of all inputs
  agent synthesizes each section
  python helpers/render_artifacts.py   → email.html + dashboard.html
  python helpers/persist_state.py      → state-sheet updates  [TODO]
  python helpers/deliver.py            → Drive + Gmail + Slack + git push
  ↓
artifacts live, agent exits

(later, manually)
  claude /daily-briefing-followup
  ↓
skills/daily-briefing-followup.md
  ↓
  python helpers/load_today_context.py  [TODO]
  agent walks through items interactively
  per-action helpers (send_reply.py, promote_task.py, etc.)  [TODO]
```

## File layout

```
plugins/daily-briefing-cowork/
├── manifest.json              ← plugin metadata, skill registry
├── README.md                  ← you are here
├── skills/
│   ├── daily-briefing.md            ← autonomous run prompt
│   └── daily-briefing-followup.md   ← interactive Q&A prompt
└── helpers/
    ├── pull_inputs.py         ← scaffolded, real imports wired up
    ├── render_artifacts.py    ← scaffolded, real render-call wired up
    └── deliver.py             ← scaffolded, includes git push step
    ⏳ persist_state.py        ← not yet written
    ⏳ load_today_context.py   ← not yet written (for followup)
    ⏳ send_reply.py           ← not yet written (for followup)
    ⏳ promote_task.py         ← not yet written (for followup)
    ⏳ add_comment.py          ← not yet written (for followup)
```

## How to test (when the scaffold is fleshed out)

Local manual test:
```bash
cd /path/to/daily-briefing-automation
claude -p "/daily-briefing"
```

Inspect: did the email arrive, did the dashboard URL work, did the
state-sheet rows look right? Compare against the same day's Python
output for parity.

Interactive followup test (after a successful autonomous run):
```bash
claude /daily-briefing-followup
```

Walk through what the agent surfaces. Verify that:
- It correctly identifies what you've already actioned via the dashboard
- "Send this drafted reply" actually sends via Gmail
- "Promote to tasks" appends to the task_proposals Sheet tab

## Activating locally

Once tested:

1. **Install the skill** in Claude Code:
   ```bash
   # Symlink so the skill is git-tracked but invokable
   New-Item -ItemType SymbolicLink \
     -Path "$HOME\.claude\skills\daily-briefing-cowork" \
     -Target "C:\Users\G09jb\Documents\ClaudeCode_onC\daily-briefing-automation\plugins\daily-briefing-cowork"
   ```
   (Or copy the directory if symlinks aren't workable.)

2. **Set up Windows Task Scheduler:**
   - Action: `claude.exe -p "/daily-briefing"`
   - Working directory: the repo root
   - Trigger: Daily at 07:30 (local time)
   - Run whether user is logged on or not: **off** (Claude Code needs your session)

3. **Turn off the GitHub Actions cron** (workflow stays for `workflow_dispatch`
   manual fallback) once the local cowork has run cleanly for ~5 days:
   ```yaml
   on:
     # schedule:           # ← comment these out
     #   - cron: "30 11 * * *"
     #   - cron: "30 12 * * *"
     workflow_dispatch: {}
   ```

## Why this isn't a one-session build

Each section's prompt in `daily_briefing.py` is the product of iteration —
careful wording, examples, edge-case handling. Porting them faithfully to
a single skill prompt takes time + comparison runs to ensure no quality
regression. Phase 1 = scaffold + minimum viable run. Production-quality
iteration follows.
