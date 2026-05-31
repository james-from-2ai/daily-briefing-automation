# Phase 2 deploy runbook — scheduled remote agent (creds-in-env)

This is the **as-built** Phase 2. It supersedes the MCP-helper plan in
`PHASE2_SETUP.md` (§P2.4–P2.6). Read the "Why not MCP" note below if you're
wondering what changed.

## Why not MCP (the pivot)

The original plan was MCP helper variants. Two hard facts killed that:

1. **The connected Gmail MCP can't send** — it only exposes `create_draft`
   (+ labels / thread-read / search). No send tool. So the briefing email
   can't go out over MCP.
2. **There is no Google Sheets MCP** — only Drive (file-level), Calendar,
   Gmail. But the entire state/dedup/carryover/ack-vote core
   (`read_state`/`write_state`/`read_acks`/`read_votes`/`source_config`)
   does structured per-tab cell read+rewrite on the state Sheet. The Drive
   MCP can't do that, and the Apps Script ack webhook writes to that same
   Sheet — so the core can't move off it.

Either way the remote env needs **real Google API credentials**, and once
it has them the **existing Phase-1 helpers already do everything** (calendar/
drive/gmail/sheets via the same client; email *sends*). So Phase 2 reuses
the Phase-1 helpers unchanged and just **surfaces the credentials as env
secrets** — exactly the proven pattern the GitHub-Actions fallback uses
(`.github/workflows/daily-briefing.yml` base64s `token.json` into a secret).

`daily_briefing.py` and `deliver.py` are untouched. The only code added is
a bootstrap + a tasks.json Drive bridge + a phase flag.

## What was built

| File | Role |
|---|---|
| `helpers/phase2_bootstrap.py` | Decodes `GOOGLE_TOKEN_B64` → `~/.config/2ai-briefing/token.json` (the path `google_creds()` reads); sets a git credential helper that feeds `GITHUB_PAT_BRIEFING` to pushes (token never written to disk); sets a commit identity; `--verify` does a live Google call. Idempotent; no-op under Phase 1. |
| `helpers/tasks_bridge.py` | tasks.json Drive bridge. **Write** side (laptop, wired into `run-tasks-live.ps1` step 5, every 2h) uploads local tasks.json to a stable Drive file `tasks-bridge.json`. **Read** side (`read_tasks_bridge`) returns the same shape as `pull_tasks_json()` for the cloud agent. |
| `helpers/pull_inputs.py` | Env-gated tasks source: local OneDrive when `BRIEFING_IO_LAYER` is unset/`local`, Drive bridge otherwise. Default path byte-identical to before. |
| `commands/daily-briefing.md` | Step 0 (phase detect + bootstrap); Phase 2 uses `--no-haiku-dedup`. |

## Prereqs already verified in this build

- Bootstrap decode round-trips byte-identically; token has a valid
  `refresh_token`.
- Drive bridge: uploaded the live tasks.json (`tasks-bridge.json`, 14 tasks)
  and read back 12 active — already populated, ready for the test fire.
- Full `BRIEFING_IO_LAYER=remote` `pull_inputs.py` run against real Google
  succeeded (tasks 12 from bridge, calendar 27, inbox 20, state 326).

## Step 1 — Configure the routine environment secrets (James)

Set these in the scheduled-agent routine's **environment config UI** (the
same place you put `GITHUB_PAT_BRIEFING`). **Never paste secret values into
chat** — generate base64 locally and paste straight into the UI.

| Env var | Required | Value |
|---|---|---|
| `GOOGLE_TOKEN_B64` | ✅ | base64 of `~/.config/2ai-briefing/token.json` |
| `GOOGLE_CLIENT_SECRET_B64` | optional | base64 of `client_secret.json` (token already embeds client id/secret for refresh, so usually unneeded) |
| `GITHUB_PAT_BRIEFING` | ✅ | the fine-scoped PAT (Contents R/W on this repo) — same value as your laptop env var |
| `SLACK_BOT_TOKEN` | ✅ | same value as your laptop env var |
| `BRIEFING_IO_LAYER` | ✅ | `remote` |

`ANTHROPIC_API_KEY` is **not** needed (Phase 2 skips Haiku dedup).

Generate the base64 locally (copies to clipboard, prints nothing):

```powershell
# token.json -> clipboard, paste into GOOGLE_TOKEN_B64
[Convert]::ToBase64String([IO.File]::ReadAllBytes("$HOME\.config\2ai-briefing\token.json")) | Set-Clipboard
# (optional) client_secret.json -> clipboard, paste into GOOGLE_CLIENT_SECRET_B64
[Convert]::ToBase64String([IO.File]::ReadAllBytes("$HOME\.config\2ai-briefing\client_secret.json")) | Set-Clipboard
```

For `GITHUB_PAT_BRIEFING` / `SLACK_BOT_TOKEN`, read your existing laptop
values into the clipboard the same way if you don't have them handy:
`$env:GITHUB_PAT_BRIEFING | Set-Clipboard`.

## Step 2 — The routine prompt (what the remote agent runs)

```
You are running the 2AI daily briefing as a scheduled remote agent (Phase 2).
Run silently end-to-end — this delivers real comms (email + Slack DM + Drive
Doc + dashboard). Do not ask questions.

The routine checks out the daily-briefing-automation repo as your working
directory. If plugins/daily-briefing-cowork/ is NOT present, clone it first:
git clone --depth 1 "https://x-access-token:${GITHUB_PAT_BRIEFING}@github.com/james-from-2ai/daily-briefing-automation.git" repo && cd repo

1. Install deps:  pip install -q -r requirements.txt
2. Bootstrap the env (decodes Google creds from GOOGLE_TOKEN_B64, configures
   git auth from GITHUB_PAT_BRIEFING, verifies a live Google call):
   python plugins/daily-briefing-cowork/helpers/phase2_bootstrap.py --verify --require
   If this exits non-zero, STOP and report — the James-ENV secrets are
   misconfigured. Do not attempt a partial briefing.
3. Read plugins/daily-briefing-cowork/commands/daily-briefing.md and follow it
   end to end as your instructions. BRIEFING_IO_LAYER=remote is already set, so:
   tasks come from the Drive bridge, and you pass --no-haiku-dedup to
   persist_state.py and do the semantic dedup in your own reasoning. Delivery
   (deliver.py) pushes the dashboard to docs/, which deploy-pages.yml publishes.
4. On any fatal error the helpers DM Slack via alert_slack_failure — let that
   happen, then report the failure and the failing step.
```

> **Consensus (weekly evidence digest):** the Thursday evidence digest runs
> two tracks in parallel — Consensus academic search (peer-reviewed papers)
> AND `web_search` (fresh org/lab announcements + preprints not yet indexed)
> — then merges. `web_search` always runs; for Track A to use Consensus, add
> the Consensus tool to the routine's `allowed_tools` when you arm Phase 2
> (the connector is already auto-attached). Without it, Track A substitutes
> academic `web_search` and the digest still works.

## Step 3 — Test fire (one-off, ~30 min out)

After the secrets are set, fire a one-off run to validate end-to-end against
real Drive/Gmail/Slack **before** arming the daily cron. Confirm all four
artifacts land: email in inbox, Slack DM, Drive Doc, dashboard URL live.

## Step 4 — Arm the daily cron (single fixed-UTC)

**Decision (2026-05-30):** one routine at **`0 12 * * *` (12:00 UTC)** =
08:00 EDT (summer) / 07:00 EST (winter). Cron is UTC.

The originally-planned DST bracket (`30 11` + `30 12`, both enabled) was
rejected: the engine has no "already delivered today" guard, so two enabled
crons would send James a *second* full briefing (email + Slack + Doc) every
day — the state-sheet dedup only suppresses repeat *items*, not repeat
*delivery*. A single fixed-UTC cron never double-sends and never fires
before 07:00 local; it drifts ±30 min from 07:30 across DST, which is fine
for a morning briefing. (If exact 07:30 year-round is ever wanted, the
clean way is a delivered-today guard in the engine, not two crons.)

## Step 5 — Monitoring plan (next ~5 days)

- Each morning confirm the four artifacts arrived (email + Slack + Doc +
  live dashboard). Slack DM is the at-a-glance signal.
- A failed run DMs Slack via `alert_slack_failure` with the failing step.
- Phase 1 laptop cron stays armed in parallel as backstop. Both fires on the
  same day are deduped by the state sheet — harmless.
- After ~5 clean Phase-2 days, unregister the laptop task (your call):
  `Unregister-ScheduledTask -TaskName 'DailyBriefingCowork' -Confirm:$false`
  Keep `run-cowork-briefing.ps1` as a manual fallback.

## Known v1 limitations (not blockers)

- **Journal context is empty in Phase 2.** Only tasks.json is bridged, not
  journal.json. The briefing loses the velocity/blockers context lines.
  Easy follow-up: bridge journal.json the same way (add to `tasks_bridge.py`
  + `pull_inputs.py`).
- **Tasks bridge freshness.** The Drive copy refreshes every 2h via the
  laptop's tasks-live cron (08:00–22:00). A 07:30 cloud briefing reads the
  ~22:00-prior-day copy. Fine for daily prioritization; if you want it
  fresher, add an early-morning tasks-live trigger on the laptop.
- **Laptop must run tasks-live to keep the bridge fresh.** If the laptop is
  off for days, the bridge goes stale (briefing still ships, tasks just age).
```
