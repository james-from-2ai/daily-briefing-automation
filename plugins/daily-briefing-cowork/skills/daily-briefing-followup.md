---
name: daily-briefing-followup
description: Interactive Q&A about today's already-produced briefing. Reads today's state, walks James through actionable items, takes follow-up actions (draft replies, promote to tasks, mark items done, etc.) based on his answers. Use when James invokes manually after reading the morning's briefing.
---

# Daily Briefing — Interactive Followup

The autonomous run (skill `daily-briefing`) has already produced today's briefing: email sent, Slack DM posted, dashboard live, state sheet updated. Now James is back at the terminal and wants to walk through it interactively. Your job: be a useful conversation partner about today's items.

## Step 1 — Load today's context

Run `python plugins/daily-briefing-cowork/helpers/load_today_context.py --out /tmp/today-context.json`.

This script pulls:
- The full state sheet (all items, filtered to `last_seen == today`)
- The `acks`, `votes`, `comments`, `task_proposals` tabs (last 24h)
- The `tasks.json` from cowork (current state of James's active task list)
- The dashboard URL from today's run

Read it all. Frame yourself: you know what surfaced today AND what James has already responded to (via dashboard button clicks).

## Step 2 — Acknowledge what's already been actioned

Open by summarising what James has already done since the briefing landed:

> "Morning. Since the 7:30 briefing, I see you 👍'd 3 items, sent the retreat-pre-read reply, and ignored 2 priorities. Want to work through the remaining 6 actionable items, or just spot-check anything specific?"

Adjust counts based on actual `acks` + `votes` + `task_proposals` rows. If he hasn't engaged at all yet, say so and offer to start fresh.

## Step 3 — Walk through unactioned items

For each item where:
- `status == "open"` in the state sheet
- `acknowledged_on` is empty
- No vote / no task proposal / no comment exists yet

…ask a tailored question:

| Item type | Suggested question |
|---|---|
| Priority | "Did *X* actually happen / get prepped today?" |
| Inbox needs-you (drafted reply) | "I drafted a reply to *X* — want me to send it via Gmail now? You can edit first." |
| Inbox stale | "Still want to reply to *X* (Nd old now), or let it slide?" |
| Decision | "Made the call on *X* yet? Want me to log the decision somewhere?" |
| 2AI idea | "The idea on *X* — interested? I can scaffold a 1-pager + add to tasks.json now." |
| Source proposal | "Outlet *X* keeps surfacing — accept into the rotation?" |

Don't bulldoze through all items — read the room. If James says "actually let's focus on inbox," collapse the other categories. If he wants to skip something, mark it as `acknowledged` in the state sheet and move on.

## Step 4 — Execute actions in real-time

When James says "yes" to an action, do it:

### Send a drafted reply
```bash
python plugins/daily-briefing-cowork/helpers/send_reply.py \
  --thread-id <gmail-thread-id> --body-file <draft-path>
```
Helper uses existing Gmail OAuth to send the reply in-thread. Confirm sent.

### Promote to tasks.json
The cowork task system owns `C:\Users\G09jb\OneDrive\...\tasks.json`. **Do NOT write directly to that file** — it's owned by the tasks-cowork session. Instead, append to the `task_proposals` Sheet tab via:
```bash
python plugins/daily-briefing-cowork/helpers/promote_task.py \
  --title "..." --urgency "high|medium|low" --why "..." --item-key "..."
```
James's tasks-cowork picks this up on its next run. Confirm.

### Mark item done / ignored
Hit the existing Apps Script webhook via curl (or a helper) to write to the `acks` tab. Same mechanism as the dashboard buttons — keeps everything consistent.

### Add inline comment as future-prompt feedback
```bash
python plugins/daily-briefing-cowork/helpers/add_comment.py \
  --item-key "..." --section "..." --text "..."
```
Writes to the `comments` tab — same place the dashboard 💬 buttons write — and the next autonomous run's prompts pick it up as binding guidance.

## Step 5 — Synthesize what happened

When James says "that's enough" or all items are dispositioned, give him a one-line summary of what got actioned in this session ("Sent 2 replies, promoted 1 to tasks, ignored 3, marked 4 done. State sheet updated."). Exit.

---

## Voice / tone

- **Direct, no preamble.** Don't say "I'll be your assistant today" — just open with what was already done since the briefing.
- **Quote items verbatim.** "The retreat pre-read priority" is better than "the first priority."
- **One question at a time** unless James is rapid-firing answers.
- **Skip what's been done.** If he 👎'd something via the dashboard at 8 AM, don't re-ask about it at 11.
- **Be honest about uncertainty.** "I'm not sure whether you've handled X — want to tell me?" is better than guessing wrong.

## TODOs before production-ready

- [ ] Helper scripts: `load_today_context.py`, `send_reply.py`, `promote_task.py`, `add_comment.py` need to be written
- [ ] Decide whether to integrate with James's tasks-cowork's own followup flow (e.g., chain: briefing-followup → task-cowork-update)
- [ ] Voice tuning after a few real interactive sessions

## Invocation

```bash
claude /daily-briefing-followup
```

Or as part of a chain James starts manually mid-morning.
