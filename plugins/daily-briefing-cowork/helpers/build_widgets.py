"""Build the top-of-briefing widget strip for the cowork flow.

The cowork render path supports a --widgets-file but the skill never built
one, so the weather/stocks strip (already pulled into the inputs JSON) was
silently dropped. This helper assembles the full strip and writes it to a
file that render_artifacts.py reads via --widgets-file. It adds two new
daily widgets James asked for:

  - Poem of the day  — a real poem pulled from PoetryDB (free, no key),
    not LLM-generated. Falls back to an embedded poem if the API is down.
  - HSK4 word of the day — rotated deterministically by date from a
    curated list (helpers/data/hsk4_words.json). An optional example
    sentence (--hsk-example) can be supplied by the agent at run time.

Weather + stocks reuse daily_briefing.render_widgets_strip so that part
stays single-sourced with the Phase-0 Python pipeline.

Usage:
    python build_widgets.py --inputs-file /tmp/briefing-inputs.json \\
        --out /tmp/section-widgets.html [--hsk-example "..."]
"""

from __future__ import annotations
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from daily_briefing import render_widgets_strip  # noqa: E402

HSK_DATA = Path(__file__).resolve().parent / "data" / "hsk4_words.json"
# Pull a few and keep the shortest so "poem of the day" stays bite-sized
# (a single random pull can return a very long / archaic piece).
POETRYDB_URL = "https://poetrydb.org/random/4"
POEM_MAX_LINES = 12

# Fallback if PoetryDB is unreachable — a short public-domain poem.
_FALLBACK_POEM = {
    "title": "The Red Wheelbarrow",
    "author": "William Carlos Williams",
    "lines": ["so much depends", "upon", "", "a red wheel", "barrow", "",
              "glazed with rain", "water", "", "beside the white",
              "chickens"],
}


def _fetch_poem() -> dict:
    try:
        r = requests.get(POETRYDB_URL, timeout=6)
        r.raise_for_status()
        data = r.json()
        candidates = [p for p in data if p.get("lines")] if isinstance(data, list) else []
        if candidates:
            # Keep the shortest of the batch so the widget stays compact.
            return min(candidates, key=lambda p: len(p.get("lines", [])))
    except Exception as e:
        print(f"[widgets] poem fetch failed ({e}); using fallback",
              file=sys.stderr)
    return _FALLBACK_POEM


def _render_poem(poem: dict) -> str:
    lines = [ln for ln in poem.get("lines", [])]
    truncated = len(lines) > POEM_MAX_LINES
    shown = lines[:POEM_MAX_LINES]
    body = "<br>".join(
        (ln if ln.strip() else "&nbsp;")
        .replace("&", "&amp;").replace("<", "&lt;") if ln else "&nbsp;"
        for ln in shown
    )
    if truncated:
        body += '<br><span style="color:#9ca3af;">…</span>'
    title = (poem.get("title") or "").replace("<", "&lt;")
    author = (poem.get("author") or "").replace("<", "&lt;")
    return (
        '<div style="background:#fffdf7;border:1px solid #e7e2d3;'
        'border-left:4px solid #b08968;border-radius:8px;padding:12px 16px;'
        'margin:0 0 12px 0;">'
        '<div style="font-size:10px;text-transform:uppercase;letter-spacing:1.2px;'
        'color:#b08968;font-weight:700;margin-bottom:6px;">📜 Poem of the day</div>'
        f'<div style="font-size:14.5px;line-height:1.6;color:#1f2937;'
        f'font-style:italic;">{body}</div>'
        f'<div style="font-size:12px;color:#6b7280;margin-top:8px;">'
        f'— <strong>{title}</strong>, {author}</div>'
        '</div>'
    )


def _pick_hsk_word(today: dt.date) -> dict:
    words = json.loads(HSK_DATA.read_text(encoding="utf-8"))["words"]
    return words[today.toordinal() % len(words)]


def _render_hsk(word: dict, example: str = "") -> str:
    example_html = (
        f'<div style="font-size:13px;color:#374151;margin-top:8px;'
        f'padding-top:8px;border-top:1px dashed #e5e7eb;">{example}</div>'
        if example else ""
    )
    return (
        '<div style="background:#f7faff;border:1px solid #d8e4f0;'
        'border-left:4px solid #c0392b;border-radius:8px;padding:12px 16px;'
        'margin:0 0 12px 0;">'
        '<div style="font-size:10px;text-transform:uppercase;letter-spacing:1.2px;'
        'color:#c0392b;font-weight:700;margin-bottom:6px;">'
        '🀄 HSK4 word of the day</div>'
        f'<div style="font-size:13px;color:#1f2937;">'
        f'<span style="font-size:28px;font-weight:700;vertical-align:middle;">'
        f'{word["hanzi"]}</span>'
        f'<span style="margin-left:12px;color:#0e7490;font-weight:600;">'
        f'{word["pinyin"]}</span>'
        f'<span style="margin-left:10px;color:#4b5563;">{word["gloss"]}</span>'
        f'</div>{example_html}'
        '</div>'
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs-file", default=None,
                        help="briefing-inputs.json (for weather + stocks)")
    parser.add_argument("--out", default=None)
    parser.add_argument("--hsk-example", default="",
                        help="Optional example sentence for today's HSK word "
                             "(the agent can generate one and pass it here).")
    parser.add_argument("--print-hsk", action="store_true",
                        help="Print today's HSK word as JSON and exit. Lets the "
                             "agent craft an example sentence before rendering.")
    args = parser.parse_args()

    if args.print_hsk:
        print(json.dumps(_pick_hsk_word(dt.date.today()), ensure_ascii=False))
        return

    if not args.inputs_file or not args.out:
        parser.error("--inputs-file and --out are required unless --print-hsk")

    inputs = json.loads(Path(args.inputs_file).read_text(encoding="utf-8"))
    today = dt.date.fromisoformat(inputs.get("today") or dt.date.today().isoformat())

    weather = inputs.get("weather") or {}
    stocks = inputs.get("stocks") or {}
    strip = render_widgets_strip(weather, stocks)

    poem_html = _render_poem(_fetch_poem())
    hsk_html = _render_hsk(_pick_hsk_word(today), args.hsk_example)

    widgets = "\n".join(p for p in (strip, poem_html, hsk_html) if p)
    Path(args.out).write_text(widgets, encoding="utf-8")
    print(f"[widgets] wrote {args.out} "
          f"(weather/stocks={'yes' if strip else 'no'}, poem+hsk=yes)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
