# Phase 2: scheduled remote agent setup

> **⚠️ Superseded — see [`PHASE2_DEPLOY.md`](PHASE2_DEPLOY.md) for the
> as-built version.** The MCP-helper approach in §P2.4–P2.6 below was
> dropped during the build: the connected Gmail MCP can't send (drafts
> only) and there's no Google Sheets MCP, so email delivery and the
> state/dedup/carryover core can't run over MCP. Phase 2 instead reuses
> the Phase-1 helpers unchanged with Google creds surfaced as an env
> secret (the same pattern as the GitHub-Actions fallback). The prereqs
> in §Pre-2.1–2.3 are still accurate; the build steps are not. This file
> is kept for the rationale + prereq history.

## What this is

Phase 1 (now working) runs the cowork briefing on James's laptop via
Windows Task Scheduler. It produces the briefing daily, but it depends
on the laptop being on, awake, and logged in at 07:30, and on Claude
Code's local auth still being valid. Realistic reliability: ~85-95%.

Phase 2 moves the daily fire to **Anthropic's scheduled-remote-agent
infrastructure** (via `/schedule` routines). The agent runs in
Anthropic's cloud on a cron, with MCPs you configured at
`claude.ai/customize/connectors` for Google + Slack access. Laptop
irrelevant. No auth re-prompts. No WakeToRun gambling.

## Prerequisites — these are YOUR work, not the build agent's

Phase 2 can't be built/deployed until these three are done:

### Pre-2.1: Connect MCPs at claude.ai/customize/connectors

Required connectors:
- **Google Workspace** — must include Calendar (read), Drive (read +
  write), Gmail (read + send), Sheets (read + write). The default
  Google connector covers all of these.
- **Slack** — must include `chat:write` and `im:write` scopes so the
  agent can DM you the briefing.

Go to https://claude.ai/customize/connectors, click "Add connector"
for each, and complete the OAuth flow. You should see both in the
connected list with a green checkmark.

### Pre-2.2: Decide the tasks.json bridge

The Phase 1 helpers read `tasks.json` directly from your local OneDrive
(`C:\Users\G09jb\OneDrive\...\Task Prio\tasks.json`). The remote agent
can't see your local disk. Pick one:

- **(a) Drive-bridge (recommended).** Modify your existing tasks-cowork
  session to write a copy of tasks.json to a known Drive folder after
  each run. The Phase 2 briefing reads from Drive via the Google
  Workspace MCP. ~5-line change in tasks-cowork's workflow.
- **(b) Move tasks to a Sheet.** Refactor tasks.json into a Google
  Sheet. Bigger lift (~1 hr) but cleaner — Sheets row read/write is
  better-suited to multi-session access than a synced JSON file.
- **(c) Skip tasks context in Phase 2 v1.** Lower priority quality
  (you lose the "your active task X maps to today's meeting Y"
  cross-references in the prioritization section) but lets you ship
  Phase 2 faster.

Tell the build session which one you picked.

### Pre-2.3: GitHub PAT for the agent environment

The skill ends by `git push`-ing the dashboard HTML to `docs/` so the
deploy-pages workflow publishes it. The remote agent environment needs
git push permission, which means:

- Create a fine-scoped PAT (Settings → Developer settings → Personal
  access tokens → Fine-grained). Scope: `Contents: Read and Write` on
  the `james-from-2ai/daily-briefing-automation` repo only.
- Add it to the agent's environment as `GITHUB_TOKEN` (the exact
  mechanism depends on Anthropic's scheduled-agent UX — you set it
  when configuring the routine).

If you already have a PAT that the agent env can see, just confirm
which env-var name it surfaces as.

## What the build agent (Claude) does after prereqs are green

Once those three are done, the remaining work is:

### P2.4: MCP-based helper variants (~1-2 days dev)

Create alongside the Phase 1 helpers:
- `helpers/pull_inputs_mcp.py` — same JSON output shape as
  `pull_inputs.py`, but uses Google Workspace MCP tools instead of
  the local OAuth client.
- `helpers/deliver_mcp.py` — Gmail MCP `send_message`, Slack MCP
  `send_message`, Drive MCP `create_file` (for the Drive Doc), then
  `git push` for Pages.
- `persist_state.py` already works for both phases (uses Sheets via
  the same google-api client; the agent env will have Python +
  google-api-python-client too via requirements.txt).

The skill prompt gets a small addition: detect Phase 1 vs Phase 2 from
an env var (e.g. `BRIEFING_IO_LAYER=local` vs `mcp`) and route helper
invocations accordingly.

### P2.5: One-time test routine

Use `/schedule create` to make a routine that fires ~30 min from now,
running `/daily-briefing` with `--io-layer=mcp`. Watch it produce the
artifacts. Iterate on any tool-name mismatches between what the agent
expected and what your MCP connectors expose.

### P2.6: Create the daily routine

Two cron entries (DST-bracketed, same pattern as the current GH cron):
- `30 11 * * *` (07:30 EDT)
- `30 12 * * *` (07:30 EST)

Same skill, same `--io-layer=mcp`, same connectors. The skill itself
is responsible for idempotency-within-a-day (the state-sheet dedup
already handles this — both fires landing on the same day is a no-op).

### P2.7: Sunset the laptop Task Scheduler

After ~5 days of clean Phase 2 runs:
```powershell
Unregister-ScheduledTask -TaskName 'DailyBriefingCowork' -Confirm:$false
```

Keep the wrapper script in the repo as a manual-fire fallback in case
the cloud routine ever has an outage.

## Rough effort

| Work | Owner | Estimate |
|---|---|---|
| Pre-2.1 — connect MCPs | James | 10 min |
| Pre-2.2 — tasks.json bridge | James + tasks-cowork session | 30 min (option a) to 1 hr (option b) |
| Pre-2.3 — GitHub PAT | James | 10 min |
| P2.4 — MCP helpers | Claude | 1-2 sessions |
| P2.5–6 — schedule + test | Claude | 1 session |
| P2.7 — laptop sunset | James | 2 min |

Total: ~2-3 hours of James's time spread across the prereqs, plus
~2-3 build sessions on the Claude side.

## Doing this in one focused build session

When you're ready:
1. Complete Pre-2.1, Pre-2.2 (option a recommended), Pre-2.3 yourself.
2. Hand back to a fresh Claude session: "Build Phase 2 of the
   cowork daily briefing — prereqs are done." Tell the session which
   tasks.json bridge option you picked.
3. That session writes the MCP helpers, tests via a one-off
   `/schedule` fire, then creates the production cron routine. Stays
   focused on Phase 2 — no laptop work mixed in.

Until you've done the three prereqs, Phase 2 is blocked. The Phase 1
laptop version (now working as of commit 839c082) keeps you covered
daily in the meantime.
