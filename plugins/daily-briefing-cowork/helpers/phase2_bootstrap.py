"""Phase 2 environment bootstrap for the scheduled remote-agent briefing.

Phase 1 (laptop) already has Google OAuth creds on disk and ambient git
auth. Phase 2 runs in Anthropic's scheduled-remote-agent cloud, which has
neither — only the env-var secrets James configures in the routine's env
config UI. This script materializes what the *existing* Phase-1 helpers
(pull_inputs / persist_state / deliver, which all call
`daily_briefing.google_creds()` and a plain `git push`) expect to find,
so those helpers run UNCHANGED in the cloud.

Why this exists instead of MCP helpers: the connected Gmail MCP can only
create drafts (no send), and there is no Google Sheets MCP at all — so the
state/dedup/carryover core (Sheets) and the email send cannot run over
MCP. The remote env therefore needs real Google API creds regardless, and
once it has them the Phase-1 helpers already do everything. This mirrors
the proven GitHub-Actions fallback pattern (.github/workflows/
daily-briefing.yml): base64 token.json into a secret, decode at startup.

What it does (each step is independent + idempotent; a step is skipped if
its env var is absent, so this is a safe no-op under Phase 1):
  1. GOOGLE_TOKEN_B64        -> ~/.config/2ai-briefing/token.json
  2. GOOGLE_CLIENT_SECRET_B64 -> ~/.config/2ai-briefing/client_secret.json
       (optional — a token.json minted by the desktop flow already embeds
        client_id/secret/refresh_token, which is all `google_creds()` needs
        to refresh. client_secret.json is only required for a fresh consent
        flow, which never happens in the cloud.)
  3. GITHUB_PAT_BRIEFING     -> a git credential helper that feeds the PAT
       to pushes to github.com, reading the token from the env at push
       time (the PAT value is NEVER written into ~/.gitconfig). Also sets a
       commit identity if none is configured.

Secrets are NEVER printed. Only presence/absence + byte counts are logged.

Usage (Phase 2 skill Step 0):
    python plugins/daily-briefing-cowork/helpers/phase2_bootstrap.py --verify
"""

from __future__ import annotations
import argparse
import base64
import binascii
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

# Import the canonical paths from the frozen engine so we decode to exactly
# where google_creds() reads from — single source of truth, no drift.
from daily_briefing import TOKEN_PATH, CLIENT_SECRET_PATH  # noqa: E402

GITHUB_HTTPS_PREFIX = "https://github.com/"
DEFAULT_GIT_NAME = "2AI Briefing Bot"
DEFAULT_GIT_EMAIL = "briefing-bot@aiaccessinitiative.org"


def _log(msg: str) -> None:
    print(f"[phase2-bootstrap] {msg}", file=sys.stderr)


def _decode_b64_to_file(env_var: str, dest: Path, *, label: str) -> bool:
    """Decode a base64 env var into `dest`. Returns True if written.

    Validates the decoded bytes are JSON before writing so a mangled secret
    fails fast here with a clear message rather than deep inside google_creds.
    Never logs the secret value — only the destination + byte count.
    """
    raw = os.environ.get(env_var)
    if not raw:
        _log(f"{env_var} not set — skipping {label}")
        return False
    # Tolerate accidental whitespace / newlines from copy-paste into the UI.
    compact = "".join(raw.split())
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as e:
        _log(f"FATAL: {env_var} is not valid base64 ({e}). "
             f"Re-encode with: [Convert]::ToBase64String("
             f"[IO.File]::ReadAllBytes('{label}'))")
        raise SystemExit(2)
    try:
        json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        _log(f"FATAL: {env_var} decoded to non-JSON ({e}). "
             f"Check you base64'd the raw file bytes, not a string.")
        raise SystemExit(2)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(decoded)
    _log(f"wrote {label} -> {dest} ({len(decoded)} bytes)")
    return True


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(REPO_ROOT),
                          capture_output=True, text=True)


def _configure_git_auth() -> bool:
    """Set up git so `deliver.py`'s plain `git push` authenticates with the
    fine-scoped PAT — WITHOUT persisting the token into ~/.gitconfig.

    The credential helper is a tiny shell function that echoes the username +
    the PAT read from $GITHUB_PAT_BRIEFING at invocation time. git only calls
    it for github.com over HTTPS. Because the helper references the env var by
    name (not value), the gitconfig file never contains the secret.
    """
    if not os.environ.get("GITHUB_PAT_BRIEFING"):
        _log("GITHUB_PAT_BRIEFING not set — skipping git auth setup "
             "(Phase 1 / laptop uses ambient git credentials)")
        return False

    if os.name == "nt":
        # Windows has no /bin/sh for the helper-function trick. Phase 2 runs
        # on Linux, so this branch is defensive only. Fall back to embedding
        # the token in the remote URL for this process's pushes.
        pat = os.environ["GITHUB_PAT_BRIEFING"]
        url = (f"https://x-access-token:{pat}@github.com/"
               "james-from-2ai/daily-briefing-automation.git")
        _git("remote", "set-url", "origin", url)
        _log("configured origin push URL with PAT (Windows fallback)")
    else:
        # POSIX: env-reading credential helper. Quoting matters — the value
        # stored in gitconfig is the literal shell snippet, expanded by sh
        # each time git asks for credentials.
        helper = ('!f() { test "$1" = get && '
                  'echo "username=x-access-token" && '
                  'echo "password=$GITHUB_PAT_BRIEFING"; }; f')
        _git("config", "--global", "credential.helper", helper)
        # Make sure pushes to our repo use HTTPS (so the helper applies) even
        # if the checkout used a tokenized or ssh URL.
        _git("config", "--global",
             f"url.{GITHUB_HTTPS_PREFIX}.insteadOf",
             "git@github.com:")
        _log("configured git credential helper (reads PAT from env at push)")

    # Commit identity — the cloud checkout usually has none, which makes
    # `git commit` fail. Only set if absent so we never clobber a real one.
    if not _git("config", "user.email").stdout.strip():
        name = os.environ.get("BRIEFING_GIT_USER_NAME", DEFAULT_GIT_NAME)
        email = os.environ.get("BRIEFING_GIT_USER_EMAIL", DEFAULT_GIT_EMAIL)
        _git("config", "--global", "user.name", name)
        _git("config", "--global", "user.email", email)
        _log(f"set commit identity: {name} <{email}>")
    return True


def _verify_google() -> None:
    """Cheap end-to-end check that the decoded token actually authenticates:
    refresh creds + one trivial Drive call. Surfaces a dead/expired token
    NOW (clear message) rather than mid-briefing."""
    try:
        from daily_briefing import google_creds
        from googleapiclient.discovery import build
        creds = google_creds()
        svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        about = svc.about().get(fields="user(emailAddress)").execute()
        who = about.get("user", {}).get("emailAddress", "?")
        _log(f"google creds OK — authenticated as {who}")
    except Exception as e:
        _log(f"FATAL: google creds verification failed: {type(e).__name__}: {e}")
        raise SystemExit(3)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true",
                        help="After decoding, refresh creds + do a trivial "
                             "Drive call to confirm the token works.")
    parser.add_argument("--token-out", default=None,
                        help="Override token.json destination (testing only).")
    parser.add_argument("--require", action="store_true",
                        help="Exit non-zero if GOOGLE_TOKEN_B64 was absent "
                             "(use in Phase 2 where creds are mandatory).")
    args = parser.parse_args()

    token_dest = Path(args.token_out).expanduser() if args.token_out else TOKEN_PATH
    wrote_token = _decode_b64_to_file("GOOGLE_TOKEN_B64", token_dest,
                                      label="token.json")
    _decode_b64_to_file("GOOGLE_CLIENT_SECRET_B64", CLIENT_SECRET_PATH,
                        label="client_secret.json")
    _configure_git_auth()

    if args.require and not wrote_token:
        _log("FATAL: --require set but GOOGLE_TOKEN_B64 was absent. "
             "The remote env must surface the Google token as a secret.")
        raise SystemExit(2)

    if args.verify and wrote_token and not args.token_out:
        _verify_google()

    _log("bootstrap complete")


if __name__ == "__main__":
    main()
