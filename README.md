# Daily briefing automation

Single-script daily briefing. Sweeps Google Calendar, Drive activity, 1:1
running notes, and inbox; runs deep-research over a news-topics sheet;
runs weekly white-space / trends / source-proposer passes; runs a biweekly
evidence digest (RCTs/preprints) and a monthly peer-publisher landscape;
ships HTML briefing to inbox + Drive + Slack at 7:30 AM.

State + acknowledgment loop: items roll forward day to day until you
acknowledge them. Per-item 👍/👎 votes train tomorrow's picks.

See `SETUP.md` for the one-time GCP OAuth + Slack + state-sheets + Apps
Script webhook setup. `output/2026-05-23-briefing.html` is a sample
briefing produced during the 2AI hackathon — reference for the expected
output shape.
