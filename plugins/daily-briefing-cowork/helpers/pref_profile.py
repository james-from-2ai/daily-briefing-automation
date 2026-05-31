"""Build James's learned preference profile from his 👍/👎 vote history.

The Phase-0 Python pipeline derived preferences with a per-run Opus call
(`digest_preferences`). The cowork upgrade makes it a durable, structured
profile computed deterministically from the FULL vote history (the votes
tab is the persistent store; recomputing each run = the learning), joined
to item text via the state sheet. pull_inputs.py includes the result as
`pref_profile` in the inputs JSON, and the skill feeds it into synthesis as
a binding bias — replacing the heuristic "skim recent votes" step.

No LLM, no new sheet tabs: votes + state are already pulled.

Profile shape:
    {
      "n_votes": int,
      "section_scores": {"news": +4, "funder": -2, ...},   # net up−down
      "more_of":  ["funder rfp", "kenya", ...],   # keywords from upvoted items
      "less_of":  ["crypto", "us politics", ...],  # keywords from downvoted
      "liked_examples":   ["short item snippet", ...],
      "disliked_examples":[...],
      "summary": "More of: …; Less of: …",         # one-line, agent-ready
      "recent_lean": {"news": +2, ...}             # last ~30 votes only
    }
"""

from __future__ import annotations
import re
from collections import Counter, defaultdict

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "for", "to", "of", "in", "on", "at",
    "by", "with", "from", "as", "is", "are", "was", "were", "be", "been", "it",
    "its", "this", "that", "these", "those", "has", "have", "had", "will",
    "would", "can", "could", "should", "may", "might", "about", "into", "over",
    "after", "new", "more", "most", "their", "they", "them", "his", "her",
    "you", "your", "2ai", "what", "how", "why", "who", "via", "per", "out",
    "up", "so", "if", "than", "then", "now", "one", "two", "also", "not",
    "no", "yes", "we", "our", "us", "he", "she", "i",
    "today", "tomorrow", "yesterday", "week", "day", "days", "time", "need",
    "needs", "make", "makes", "get", "gets", "before", "still", "just",
    "like", "next", "last", "since", "while", "when", "where", "which",
}
RECENT_N = 30
TOP_KEYWORDS = 8
MAX_EXAMPLES = 4
MIN_KW_FREQ = 2  # a keyword must recur across ≥2 voted items to count


def _plain(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def _keywords(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z\-']{2,}", text.lower())
    # Strip possessive/contraction tails ("ai's" -> "ai", "world's" -> "world").
    words = [re.sub(r"'s$|'$", "", w) for w in words]
    return [w for w in words if w not in _STOPWORDS and len(w) > 3]


def build_profile(votes: list[dict], state: list[dict]) -> dict:
    """Aggregate votes (item_key, vote, voted_at) against state item text."""
    empty = {"n_votes": 0, "section_scores": {}, "more_of": [], "less_of": [],
             "liked_examples": [], "disliked_examples": [], "summary": "",
             "recent_lean": {}}
    if not votes:
        return empty

    key_to_meta = {
        r.get("key", ""): {"section": r.get("section", "other"),
                           "text": _plain(r.get("text_html", ""))}
        for r in state
    }

    section_net: dict[str, int] = defaultdict(int)
    recent_net: dict[str, int] = defaultdict(int)
    liked_words: Counter = Counter()
    disliked_words: Counter = Counter()
    liked_examples: list[str] = []
    disliked_examples: list[str] = []

    # Most recent first for the recent-lean window + example freshness.
    ordered = sorted(votes, key=lambda v: v.get("voted_at", ""), reverse=True)
    for i, v in enumerate(ordered):
        direction = (v.get("vote") or "").strip().lower()
        sign = 1 if direction in ("up", "+1", "1", "yes") else (
            -1 if direction in ("down", "-1", "no") else 0)
        if sign == 0:
            continue
        meta = key_to_meta.get(v.get("item_key", ""), {})
        section = meta.get("section", "other")
        section_net[section] += sign
        if i < RECENT_N:
            recent_net[section] += sign
        text = meta.get("text", "")
        if not text:
            continue
        kws = _keywords(text)
        if sign > 0:
            liked_words.update(kws)
            if len(liked_examples) < MAX_EXAMPLES:
                liked_examples.append(text[:140])
        else:
            disliked_words.update(kws)
            if len(disliked_examples) < MAX_EXAMPLES:
                disliked_examples.append(text[:140])

    # Drop keywords that are both liked + disliked (ambiguous signal).
    liked_set = {w for w, _ in liked_words.most_common(40)}
    disliked_set = {w for w, _ in disliked_words.most_common(40)}
    ambiguous = liked_set & disliked_set
    # Require a keyword to recur (≥ MIN_KW_FREQ across voted items) so one-off
    # noise tokens don't surface as "preferences".
    more_of = [w for w, c in liked_words.most_common(TOP_KEYWORDS * 3)
               if w not in ambiguous and c >= MIN_KW_FREQ][:TOP_KEYWORDS]
    less_of = [w for w, c in disliked_words.most_common(TOP_KEYWORDS * 3)
               if w not in ambiguous and c >= MIN_KW_FREQ][:TOP_KEYWORDS]

    section_scores = dict(sorted(section_net.items(),
                                 key=lambda kv: kv[1], reverse=True))
    liked_secs = [s for s, n in section_scores.items() if n > 0]
    disliked_secs = [s for s, n in section_scores.items() if n < 0]

    summary_bits = []
    if more_of or liked_secs:
        summary_bits.append(
            "More of: " + ", ".join(liked_secs[:3] + more_of[:5]))
    if less_of or disliked_secs:
        summary_bits.append(
            "Less of: " + ", ".join(disliked_secs[:3] + less_of[:5]))
    summary = "; ".join(summary_bits)

    return {
        "n_votes": len([v for v in votes if (v.get("vote") or "").strip()]),
        "section_scores": section_scores,
        "more_of": more_of,
        "less_of": less_of,
        "liked_examples": liked_examples,
        "disliked_examples": disliked_examples,
        "summary": summary,
        "recent_lean": dict(sorted(recent_net.items(),
                                   key=lambda kv: kv[1], reverse=True)),
    }
