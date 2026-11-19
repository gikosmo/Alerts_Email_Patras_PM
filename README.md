# Alerts_Email_Patras_PM — daily alerts for the Patras PM network

Replaces the old `Auto_Check_Sensors_Operation_Patras_Network.py` Gmail script.
Separate from the dashboard repo (`patras-pm-network-v2`), but reads the
**same Google Sheet** and runs the same QC checks.

Every day at 04:00 UTC (`.github/workflows/alerts.yml`):

1. Downloads the station sheet → active stations = `Uninstalled` is `-`.
2. Downloads the last 24 h of 10-min data from PurpleAir for those stations
   and classifies each as OK / WARNING / FAULTY / OFFLINE.
3. `scripts/alert.py` keeps **one** GitHub Issue (label `network-alert`) open
   while anything is wrong, with two tables:
   - stations not working (offline, or < 25% of the day's data)
   - stations working but possibly faulty (FAULTY, or the old checks:
     R² A-vs-B < 0.8, or > 60% of 10-min pairs disagreeing)

   The issue text is refreshed silently every day. A comment — which GitHub
   emails you about — is added only when the list of problem stations
   changes. When all is OK again the issue is closed.

Nothing is committed back and no website is published; the station sheet
is only used inside the run.

## Setup
1. Settings → Secrets and variables → Actions → New repository secret:
   `PURPLEAIR_API_KEY`.
2. Actions tab → "Daily network alert" → Run workflow (to test).

Thresholds: `config.json` → `alerts` and `thresholds`.
Test locally: `python scripts/alert.py --dry-run` after a run.
