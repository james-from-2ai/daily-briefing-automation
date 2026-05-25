"""Annotate per-section HTML with vote/action widgets, run semantic dedup
against recent state, merge today's items into the state sheet, and emit
the carryover HTML block. Runs BEFORE render_artifacts.py because the
annotated/deduped section HTMLs and the carryover block both feed into
render_html.

Inputs:
  --inputs-file: the JSON dump from pull_inputs.py (for state, acks,
      oneonones, today).
  --prioritization-file / --inbox-file / --news-file / --funder-file /
  --whitespace-file / --evidence-file / --ideas-file: the raw,
      just-synthesized section HTML files (the agent wrote these).
      Rewritten in place with annotated + dedup-cleaned HTML.
  --source-proposals-file: optional JSON list of {source_id, proposed_at,
      status} rows to append to the `sources` tab (from the agent's daily
      source proposer + Friday weekly proposer).

Outputs:
  --carryover-out: HTML block for the 'pending from earlier' bar.
  --carry-count-out: integer count (used by deliver.py for the Slack msg).
  --items-count-out: integer count (for the skill's status print).

Side effects:
  - Overwrites state sheet via write_state()
  - Appends to `sources` tab via append_source_rows()
"""

from __future__ import annotations
import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from daily_briefing import (  # noqa: E402
    google_creds, read_state, read_acks, apply_acks_to_state,
    extract_action_items, item_key,
    annotate_topics_h3, annotate_topics_li,
    annotate_prioritization, annotate_inbox,
    dedup_with_haiku, apply_semantic_dedup,
    merge_into_state, get_carryover, render_carryover,
    write_state, append_source_rows,
    ACK_WEBHOOK_URL, THUMBS_TEMPLATE, _vote_url,
)


def _read(path: str | None) -> str:
    if not path:
        return ""
    p = Path(path)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _write(path: str | None, content: str) -> None:
    if path:
        Path(path).write_text(content, encoding="utf-8")


def _annotate_evidence(html: str, today: dt.date) -> tuple[str, list[dict]]:
    """Reproduces the inline evidence-buttonize closure in daily_briefing.main()."""
    items: list[dict] = []
    if not html:
        return html, items

    def buttonize(m):
        block = m.group(0)
        plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", block)).strip()
        if len(plain) < 20:
            return block
        key = item_key("evidence", plain[:200])
        if ACK_WEBHOOK_URL:
            thumbs = THUMBS_TEMPLATE.format(
                up=_vote_url(today, key, "up"),
                down=_vote_url(today, key, "down"),
            )
            new_block = block.replace("</div>", f"  {thumbs}\n</div>", 1)
        else:
            new_block = block
        items.append({
            "section": "evidence", "key": key, "source": "synth",
            "text_html": block,
            "rendered_block": new_block,
        })
        return new_block

    new_html = re.sub(
        r'<div style="border-left:3px solid #5fae5f;[^"]*"[^>]*>.*?</div>',
        buttonize, html, flags=re.S,
    )
    return new_html, items


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs-file", required=True)
    parser.add_argument("--prioritization-file", required=True)
    parser.add_argument("--inbox-file", default=None)
    parser.add_argument("--news-file", default=None)
    parser.add_argument("--funder-file", default=None)
    parser.add_argument("--whitespace-file", default=None)
    parser.add_argument("--evidence-file", default=None)
    parser.add_argument("--ideas-file", default=None)
    parser.add_argument("--source-proposals-file", default=None,
                        help="Optional JSON list of {source_id, proposed_at, status}")
    parser.add_argument("--carryover-out", required=True)
    parser.add_argument("--carry-count-out", required=True)
    parser.add_argument("--items-count-out", default=None)
    parser.add_argument("--no-haiku-dedup", action="store_true",
                        help="Skip the Haiku dedup API call (agent does dedup itself)")
    parser.add_argument("--no-write", action="store_true",
                        help="Don't persist to Sheets (dry-run for local testing)")
    args = parser.parse_args()

    inputs = json.loads(Path(args.inputs_file).read_text(encoding="utf-8"))
    today = dt.date.fromisoformat(inputs["today"])
    oneonones: dict[str, str] = inputs.get("oneonones", {}) or {}

    creds = google_creds()

    # Re-read state to pick up any acks that landed between pull_inputs and now.
    state = read_state(creds)
    acks = read_acks(creds)
    state = apply_acks_to_state(state, acks)
    print(f"  {len(state)} state rows; {len(acks)} acks", file=sys.stderr)

    # ---- action items from 1:1s ----
    action_items: list[dict] = []
    for name, text in oneonones.items():
        action_items.extend(
            extract_action_items(text or "", f"{name} 1:1 most-recent entries")
        )
    print(f"  {len(action_items)} action items from 1:1 notes", file=sys.stderr)

    # ---- read all section HTMLs ----
    prioritization = _read(args.prioritization_file)
    inbox_html = _read(args.inbox_file)
    news = _read(args.news_file)
    funder_html = _read(args.funder_file)
    whitespace = _read(args.whitespace_file)
    evidence = _read(args.evidence_file)
    ideas_html = _read(args.ideas_file)

    # ---- annotate each section ----
    news, news_items = annotate_topics_h3(news, "news", today)
    funder_html, funder_items = annotate_topics_h3(funder_html, "funder", today)

    if whitespace:
        whitespace, ws_items = annotate_topics_li(whitespace, "whitespace", today)
    else:
        ws_items = []

    if ideas_html:
        ideas_html, ideas_items = annotate_topics_li(ideas_html, "ideas", today)
    else:
        ideas_items = []

    evidence, evidence_items = _annotate_evidence(evidence, today)

    prioritization, prio_items = annotate_prioritization(prioritization, today)
    inbox_html, inbox_items = annotate_inbox(inbox_html, today)

    today_items: list[dict] = []
    today_items += action_items
    today_items += news_items
    today_items += funder_items
    today_items += ws_items
    today_items += evidence_items
    today_items += ideas_items
    today_items += prio_items
    today_items += inbox_items

    # ---- semantic dedup against recent state ----
    if state and not args.no_haiku_dedup:
        print("  semantic dedup against recent state…", file=sys.stderr)
        try:
            dedup_map = dedup_with_haiku(today_items, state, today)
        except Exception as e:
            print(f"  dedup failed ({e}); proceeding without dedup", file=sys.stderr)
            dedup_map = {}
        if dedup_map:
            section_htmls = {
                "news": news, "funder": funder_html,
                "whitespace": whitespace, "evidence": evidence,
            }
            today_items, cleaned = apply_semantic_dedup(
                today_items, dedup_map, section_htmls, state,
            )
            news = cleaned["news"]
            funder_html = cleaned["funder"]
            whitespace = cleaned["whitespace"]
            evidence = cleaned["evidence"]
            print(f"  dropped {len(dedup_map)} dupe(s); bumped historical carry_count",
                  file=sys.stderr)
        else:
            print("  no dupes against recent state", file=sys.stderr)
    elif args.no_haiku_dedup:
        print("  skipping Haiku dedup (--no-haiku-dedup)", file=sys.stderr)
    else:
        print("  skipping semantic dedup — no prior state yet", file=sys.stderr)

    # ---- merge into state + carryover ----
    state = merge_into_state(state, today_items, today)
    carryover = get_carryover(state, today)
    carryover_html = render_carryover(carryover, today)
    print(f"  {len(carryover)} carryover items", file=sys.stderr)

    # ---- write everything back ----
    _write(args.prioritization_file, prioritization)
    _write(args.inbox_file, inbox_html)
    _write(args.news_file, news)
    _write(args.funder_file, funder_html)
    _write(args.whitespace_file, whitespace)
    _write(args.evidence_file, evidence)
    _write(args.ideas_file, ideas_html)
    _write(args.carryover_out, carryover_html)
    _write(args.carry_count_out, str(len(carryover)))
    if args.items_count_out:
        _write(args.items_count_out, str(len(today_items)))

    # ---- persist to Sheets ----
    if args.no_write:
        print("  --no-write: skipping Sheets persistence", file=sys.stderr)
    else:
        write_state(creds, state)
        print(f"  wrote {len(state)} state rows", file=sys.stderr)
        if args.source_proposals_file:
            p = Path(args.source_proposals_file)
            if p.exists():
                source_rows = json.loads(p.read_text(encoding="utf-8"))
                if source_rows:
                    append_source_rows(creds, source_rows)
                    print(f"  appended {len(source_rows)} source proposals",
                          file=sys.stderr)


if __name__ == "__main__":
    main()
