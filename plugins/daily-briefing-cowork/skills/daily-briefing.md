---
name: daily-briefing
description: Run the autonomous daily briefing — pull inputs, synthesize all sections in this conversation's context (no anthropic.Anthropic() calls), render the dashboard, send email + Slack + Drive Doc, push the dashboard to GitHub Pages. Use when invoked by Windows Task Scheduler at 7:30 AM, or manually via `claude -p "/daily-briefing"`.
---

# Daily Briefing — Autonomous Run

You are James Bedford's chief of staff at 2AI. Your job: produce James's daily briefing end-to-end without making any `anthropic.Anthropic()` API calls. **All synthesis runs in your own reasoning** — that's the entire point of this cowork version.

## High-level flow

```
pull_inputs.py → /tmp/briefing-inputs.json
  ↓ (you read, you think)
synthesize each section → /tmp/section-*.html (raw)
  ↓
persist_state.py  (annotate widgets + dedup + state-sheet write + carryover HTML)
  ↓
render_artifacts.py briefing.html dashboard.html
  ↓
deliver.py  (Drive upload + Gmail + Slack + Pages push)
```

Note: persist_state.py runs BEFORE render_artifacts.py because it
mutates the section HTMLs in place (injecting 👍/👎 + action buttons,
stripping duped blocks) and emits the carryover block — both feed into
render_html.

## Step 1 — Pull all inputs

Run `python plugins/daily-briefing-cowork/helpers/pull_inputs.py --out /tmp/briefing-inputs.json`.

This script reuses every `pull_*` function from `daily_briefing.py` (calendar, Drive, 1:1 docs, inbox signals, news topics sheet, state sheet, recent feedback, recent votes, tasks.json, journal.json, weather, stocks). Output is a single JSON file.

Read the JSON. **Do not summarise it — you'll use the raw fields below.**

## Step 2 — Synthesize each section (THIS IS YOUR REASONING WORK)

For each section below, *think* through the output. Don't call any other Claude — you ARE the Claude. The system prompts below are the ones the Python version used; treat them as your own instructions.

### 2a. Prioritization

(System prompt copied verbatim from `synthesize_prioritization` in daily_briefing.py — see source.)

Cross-reference calendar events against 1:1 action items, drive activity, inbox threads, and cowork tasks. Produce four sub-sections:

- `<h2>Top priorities today</h2>` — 3–5 items, each one line + italic "why today"
- `<h2>Gold-standard overreach — if you went all-in</h2>` — 1–3 ambitious versions
- `<h2>Likely to slip — flag now</h2>` — bullet list with evidence + de-risk action
- `<h2>Decisions needed from James</h2>` — bullets surfacing blocking decisions
- `<h2>Calendar prep cues</h2>` — one line per meeting

If feedback digest (from votes) is provided in inputs, treat it as binding.

### 2b. Critic pass

Review your own prioritization draft. Apply the rubric: specificity, action-density, calibration, voice, feedback-alignment, length cap ~700 words. **Silently revise** — output is the revised HTML, no editor's note.

### 2c. TL;DR strip

One Axios-style sentence, 15–30 words. Tight prose: who, what, when. Output plain text (renderer adds the badge).

### 2d. Inbox triage with reply drafts

Two buckets: "Reply / decide" (recent, actionable) and "Likely to slip through" (3–14d unanswered). Skip newsletters / auto-mail entirely. For complex decision-requiring replies, embed a `<div style="...">` draft reply inline that pulls in context from 1:1 notes + calendar. Voice: matter-of-fact, evidence-first, warm-but-direct.

(Full prompt: see `synthesize_inbox_triage` in daily_briefing.py.)

### 2e. Funder watchlist

**Only run if `today.toordinal() % 2 == 0`.** Otherwise output empty string for funder.

For each funder in inputs.funder_watchlist (5 funders): use the `web_search` tool to find moves in the last 7 days, output a paragraph + "So what for 2AI" line per funder.

### 2f. News deep-dives

News picker: from the news_topics_text in inputs, pick 6 topics worth deep research today. Then for each: use `web_search` for last-7-days developments, return a 4–7 sentence briefing in 2AI house voice.

### 2g. 2AI implementation ideas

Use the program_corpus from inputs (what 2AI works on) + today's news context + web_search for fresh AI releases. Output 1–3 concrete ideas (artifact + audience + next step + effort estimate).

### 2h. Daily source proposer

Scan today's news + funder citations + recent state for outlets appearing 2+ times that aren't in the current rotation. Surface 0–3 with ✅ accept / ❌ skip anchors.

### 2i. Weekly / monthly cadence-gated sections

Based on `today.weekday()`:
- Monday (0): synthesize_whitespace
- Tuesday + Thursday (1, 3): synthesize_evidence_digest
- Wednesday (2): synthesize_trends
- Friday (4): propose_new_sources
- First weekday of month: synthesize_publisher_landscape

For each, use the same reasoning patterns as the Python version's system prompts.

## Step 3 — Persist state (annotate + dedup + carryover)

After you've written each section's raw HTML to `/tmp/section-*.html`,
run persist_state.py. It will rewrite those files in place with the
annotated + dedup-cleaned versions, write a carryover block, and persist
to the state sheet.

```bash
python plugins/daily-briefing-cowork/helpers/persist_state.py \
  --inputs-file /tmp/briefing-inputs.json \
  --prioritization-file /tmp/section-priorities.html \
  --inbox-file /tmp/section-inbox.html \
  --news-file /tmp/section-news.html \
  --funder-file /tmp/section-funder.html \
  --whitespace-file /tmp/section-whitespace.html \
  --evidence-file /tmp/section-evidence.html \
  --ideas-file /tmp/section-ideas.html \
  --source-proposals-file /tmp/source-proposals.json \
  --carryover-out /tmp/section-carryover.html \
  --carry-count-out /tmp/carry-count.txt \
  --items-count-out /tmp/items-count.txt
```

If you have proposed sources from your daily-source-proposer or
Friday-weekly-source-proposer reasoning, write them as a JSON list to
`/tmp/source-proposals.json` first (shape: `[{"source_id": "...",
"proposed_at": "2026-05-25", "status": "proposed"}, ...]`). If you
don't, omit `--source-proposals-file` or write `[]` to the file.

Pass `--no-haiku-dedup` if you want to skip the Haiku dedup API call
and do dedup yourself in your own reasoning before writing the section
HTMLs. (~$0.02 savings per run; v0.1 default keeps Haiku.)

## Step 4 — Render artifacts

Now that the section HTMLs are annotated + cleaned and the carryover
block exists, invoke render_artifacts:

```bash
python plugins/daily-briefing-cowork/helpers/render_artifacts.py \
  --tldr "<tldr text>" \
  --prioritization-file /tmp/section-priorities.html \
  --inbox-file /tmp/section-inbox.html \
  --funder-file /tmp/section-funder.html \
  --news-file /tmp/section-news.html \
  --ideas-file /tmp/section-ideas.html \
  --sources-today-file /tmp/section-sources-today.html \
  --whitespace-file /tmp/section-whitespace.html \
  --trends-file /tmp/section-trends.html \
  --evidence-file /tmp/section-evidence.html \
  --publisher-file /tmp/section-publisher.html \
  --sources-file /tmp/section-sources.html \
  --carryover-file /tmp/section-carryover.html \
  --out-email /tmp/briefing-email.html \
  --out-dashboard /tmp/briefing-dashboard.html \
  --dashboard-url-out /tmp/dashboard-url.txt
```

This wraps the existing `render_html` + `make_interactive_dashboard` + verify_urls pipeline. Output: two HTML files + the dashboard URL (UUID-named for GitHub Pages).

## Step 5 — Deliver

```bash
python plugins/daily-briefing-cowork/helpers/deliver.py \
  --email-html /tmp/briefing-email.html \
  --dashboard-html /tmp/briefing-dashboard.html \
  --dashboard-url-file /tmp/dashboard-url.txt \
  --carry-count "$(cat /tmp/carry-count.txt)"
```

Uploads Drive Doc, sends Gmail, posts Slack DM, commits dashboard to `docs/` + pushes to GitHub (which triggers Pages deploy via existing workflow).

## Step 6 — Exit cleanly

Print a final status: section counts, items indexed, dashboard URL, delivery confirmations. Exit. No questions, no waiting — that's the followup skill's job.

---

## Non-negotiables

- **No `anthropic.Anthropic()` calls.** If you find yourself wanting to invoke Claude programmatically, you're doing it wrong — that work is YOUR reasoning.
- **`web_search` is fine.** It's a tool call, not a separate API charge under subscription.
- **Helper Python scripts handle all I/O.** OAuth, HTTP, file I/O, sheet writes, Drive uploads — all go through them. They import functions from `daily_briefing.py` so the logic stays in one place.
- **If anything fails fatally,** post a Slack DM via the existing `alert_slack_failure` helper + raise. Don't try to recover silently — better to crash visibly than ship a half-broken briefing.

## TODOs before this is production-ready

- [ ] Helper scripts `pull_inputs.py`, `render_artifacts.py`, `persist_state.py`, `deliver.py` need to be written (skeletons exist alongside this skill; flesh out the imports + arg parsing)
- [ ] Verify Bash tool can run all the helper scripts in your session's environment
- [ ] Decide whether to keep the Haiku dedup call (separate API) or fold dedup into your own reasoning (zero-cost but uses your context)
- [ ] Test locally with `claude -p "/daily-briefing"` and compare output to today's Python-generated briefing
- [ ] Set up Windows Task Scheduler entry once output quality is validated

## When this skill is invoked manually for testing

If you're running this interactively (not via Task Scheduler), you can talk to James as you work — surface decisions you'd otherwise just make ("the news picker found 8 candidates, picking these 6 — okay?"). For production cron runs the agent runs silently end-to-end.
