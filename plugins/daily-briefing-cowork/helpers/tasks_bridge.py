"""tasks.json Drive bridge — the cross-machine handoff for Phase 2.

The Phase-1 briefing reads the cowork task list straight off James's local
OneDrive (`daily_briefing.TASKS_JSON_PATH`). The Phase-2 briefing runs in
Anthropic's cloud and can't see that disk, so we bridge the file through
Drive:

  WRITE side (this module's CLI / `upload_tasks_bridge`): runs on the laptop
    — wired into run-tasks-live.ps1 as a non-fatal last step — and uploads a
    copy of the local tasks.json to a single, stable Drive file
    (`tasks-bridge.json`) in James's private briefings folder. Idempotent:
    updates the existing file in place rather than spawning duplicates.

  READ side (`read_tasks_bridge`): runs in the cloud — called by
    pull_inputs.py when BRIEFING_IO_LAYER != local — and returns the same
    shape pull_tasks_json() would: active tasks (status != done), capped at
    TASKS_TOP_N, ranking preserved.

Both sides go through the google-api client using `daily_briefing`'s creds,
so no Sheets/Drive MCP is involved (and none exists for this anyway).
"""

from __future__ import annotations
import argparse
import io
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from googleapiclient.discovery import build  # noqa: E402
from googleapiclient.http import MediaIoBaseUpload  # noqa: E402

from daily_briefing import (  # noqa: E402
    google_creds, BRIEFINGS_DRIVE_FOLDER_ID, TASKS_JSON_PATH, TASKS_TOP_N,
)

# Single stable filename so write + read agree without a hardcoded file ID.
TASKS_BRIDGE_FILENAME = "tasks-bridge.json"
TASKS_BRIDGE_MIMETYPE = "application/json"


def _drive(creds):
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _find_bridge_file(svc) -> dict | None:
    """Return the newest bridge file owned by the user, or None.

    Matching by name (not a hardcoded ID) keeps setup zero-config and
    survives the file being recreated. 'me' in owners avoids picking up a
    same-named file someone else shared in.
    """
    resp = svc.files().list(
        q=(f"name = '{TASKS_BRIDGE_FILENAME}' and 'me' in owners "
           f"and trashed = false"),
        orderBy="modifiedTime desc",
        pageSize=10,
        fields="files(id,name,modifiedTime,parents)",
    ).execute()
    files = resp.get("files", [])
    return files[0] if files else None


def upload_tasks_bridge(creds, tasks_json_path: Path | None = None) -> str:
    """Upload the local tasks.json to the Drive bridge file. Creates the
    file under the briefings folder on first run, updates it in place after.
    Returns the file ID. Raises if the local file is missing/unreadable —
    the caller (run-tasks-live.ps1) treats that as non-fatal."""
    path = Path(tasks_json_path) if tasks_json_path else TASKS_JSON_PATH
    # Read + validate before touching Drive so we never publish garbage.
    data = path.read_text(encoding="utf-8")
    parsed = json.loads(data)
    n_tasks = len(parsed.get("tasks", []))

    svc = _drive(creds)
    existing = _find_bridge_file(svc)
    media = MediaIoBaseUpload(io.BytesIO(data.encode("utf-8")),
                              mimetype=TASKS_BRIDGE_MIMETYPE, resumable=False)
    if existing:
        f = svc.files().update(
            fileId=existing["id"], media_body=media,
            fields="id,modifiedTime").execute()
        print(f"[tasks-bridge] updated {TASKS_BRIDGE_FILENAME} "
              f"({n_tasks} tasks) id={f['id']}", file=sys.stderr)
    else:
        metadata = {
            "name": TASKS_BRIDGE_FILENAME,
            "mimeType": TASKS_BRIDGE_MIMETYPE,
            "parents": [BRIEFINGS_DRIVE_FOLDER_ID],
        }
        f = svc.files().create(
            body=metadata, media_body=media, fields="id").execute()
        print(f"[tasks-bridge] created {TASKS_BRIDGE_FILENAME} "
              f"({n_tasks} tasks) id={f['id']}", file=sys.stderr)
    return f["id"]


def read_tasks_bridge(creds) -> list[dict]:
    """Cloud read side. Returns active tasks (status != done), capped at
    TASKS_TOP_N, ranking preserved — identical shape to pull_tasks_json().
    Returns [] if the bridge file is missing or unparseable, so a bridge
    hiccup degrades gracefully (briefing still ships, just without task
    cross-refs) exactly like the local reader does."""
    try:
        svc = _drive(creds)
        found = _find_bridge_file(svc)
        if not found:
            print(f"[tasks-bridge] no {TASKS_BRIDGE_FILENAME} in Drive "
                  f"— returning [] (run the laptop write side first)",
                  file=sys.stderr)
            return []
        raw = svc.files().get_media(fileId=found["id"]).execute()
        data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except Exception as e:
        print(f"[tasks-bridge] read failed ({type(e).__name__}: {e}); "
              f"returning []", file=sys.stderr)
        return []
    tasks = [t for t in data.get("tasks", []) if t.get("status") != "done"]
    return tasks[:TASKS_TOP_N]


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload local tasks.json to "
                                                 "the Drive bridge.")
    parser.add_argument("--tasks-json", default=None,
                        help="Override the local tasks.json path "
                             "(defaults to daily_briefing.TASKS_JSON_PATH).")
    args = parser.parse_args()
    creds = google_creds()
    upload_tasks_bridge(creds, args.tasks_json)


if __name__ == "__main__":
    main()
