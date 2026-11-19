"""
Daily network-health alert, posted as a GitHub Issue instead of an email.

Reads the result the dashboard already uses (site/data/latest.json) and
builds the same two tables the old Auto_Check_Sensors_Operation email had:

  1. Σταθμοί εκτός λειτουργίας  — OFFLINE, or under 25% of the day's data
  2. Σταθμοί με πιθανή βλάβη     — dashboard FAULTY, or the old checks:
                                   R² (A vs B) < 0.8, or > 60% of 10-min
                                   pairs disagreeing (|A-B| > 2 µg/m³ and
                                   |A-B|/mean > 15%, after 0.42x+0.26)

How it avoids noise:
  - ONE issue (label "network-alert") is kept open while anything is wrong.
    Its text is rewritten every day with the full current report — editing
    doesn't send notifications.
  - A new comment (which DOES notify you: GitHub app push and/or an email
    from notifications@github.com, nothing in your Sent folder) is posted
    only when the set of problem stations changes: something new broke, or
    something recovered.
  - When everything is fine again the issue is closed with a short comment.

Usage:
    python alert.py              # posts/updates the issue (needs GITHUB_TOKEN + GITHUB_REPOSITORY)
    python alert.py --dry-run    # only prints the report
"""
from __future__ import annotations

import os
import re
import sys
import json
import argparse
import logging
from pathlib import Path

import numpy as np
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("alert")

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
AL = {
    "issue_label": "network-alert",
    "mention": "@gikosmo",
    "include_warning": False,
    "legacy_faulty_pct": 60,
    "legacy_r2": 0.8,
    "legacy_cal_slope": 0.42,
    "legacy_cal_intercept": 0.26,
    "low_data_pct": 25,
    **CONFIG.get("alerts", {}),
}
STATE_RE = re.compile(r"<!-- alert-state: (.*?) -->", re.S)
API = "https://api.github.com"

DISCLAIMER = (
    "Εργαστήριο Φυσικής της Ατμόσφαιρας · Τμήμα Φυσικής · Πανεπιστήμιο Πατρών. "
    "Προκαταρκτικές, ενδεικτικές τιμές — τα οριστικά αποτελέσματα προκύπτουν μετά από "
    "στατιστική επεξεργασία."
)


# ---------------------------------------------------------------- checks
def legacy_faulty_pct(sensor: dict) -> float | None:
    """% of 10-min PM2.5 pairs where A and B disagree (old script's test)."""
    ts = sensor.get("timeseries") or {}
    a = np.array(ts.get("pm2_5_a") or [], dtype=float)
    b = np.array(ts.get("pm2_5_b") or [], dtype=float)
    if len(a) == 0 or len(a) != len(b):
        return None
    ok = ~(np.isnan(a) | np.isnan(b))
    a = a[ok] * AL["legacy_cal_slope"] + AL["legacy_cal_intercept"]
    b = b[ok] * AL["legacy_cal_slope"] + AL["legacy_cal_intercept"]
    if len(a) == 0:
        return None
    dif = np.abs(a - b)
    avg = (a + b) / 2
    with np.errstate(divide="ignore", invalid="ignore"):
        bad = (dif / avg > 0.15) & (dif > 2)
    return round(float(np.mean(bad) * 100), 1)


def classify_for_alert(s: dict) -> tuple[str | None, str]:
    """Return (category, short reason). category: offline / faulty / warning / None."""
    status = s.get("status")
    comp = s.get("completeness_pct") or 0
    if status == "OFFLINE":
        if s.get("last_seen") is None:
            return "offline", "Δεν βρέθηκε στο PurpleAir (λάθος ID, ιδιωτικός ή διαγραμμένος)"
        return "offline", "Δεν στέλνει δεδομένα (εκτός σύνδεσης)"
    if comp < AL["low_data_pct"]:
        return "offline", f"Μόνο {comp:.0f}% των δεδομένων της ημέρας"

    stats = s.get("stats") or {}
    r2 = stats.get("r2")
    fpct = s.get("_faulty_pct")
    reasons = []
    if status == "FAULTY":
        reasons += [r for r in s.get("reasons", [])]
    if r2 is not None and r2 < AL["legacy_r2"] and status != "FAULTY":
        reasons.append(f"R²={r2:.2f} < {AL['legacy_r2']}")
    if fpct is not None and fpct > AL["legacy_faulty_pct"]:
        reasons.append(f"{fpct:.0f}% των 10λεπτων τιμών A/B διαφέρουν")
    if reasons:
        return "faulty", "; ".join(reasons)
    if status == "WARNING" and AL["include_warning"]:
        return "warning", "; ".join(s.get("reasons", []))
    return None, ""


# ---------------------------------------------------------------- report
def period(summary: dict) -> str:
    """Label for the report: calendar date (v1) or rolling window (v2)."""
    if summary.get("date"):
        return summary["date"]
    end = (summary.get("window_end") or summary.get("generated_at") or "")[:16].replace("T", " ")
    hours = summary.get("window_hours", 24)
    return f"{hours}ω έως {end} UTC" if end else "τελευταίο 24ωρο"


def fmt(v, nd=2):
    return "–" if v is None else f"{v:.{nd}f}"


def build_report(summary: dict) -> tuple[str, dict, dict]:
    """Return (markdown, state, sensors_by_id). state = {id: category}."""
    rows = {"offline": [], "faulty": [], "warning": []}
    state, by_id = {}, {}
    for s in summary["sensors"]:
        s["_faulty_pct"] = legacy_faulty_pct(s)
        cat, why = classify_for_alert(s)
        by_id[str(s["sensor_index"])] = s
        if cat:
            rows[cat].append((s, why))
            state[str(s["sensor_index"])] = cat

    c = summary.get("counts", {})
    lines = [
        f"## Δίκτυο αιωρούμενων σωματιδίων Πάτρας — {period(summary)}",
        "",
        f"**{summary['n_sensors']}** σταθμοί · ✅ {c.get('OK', 0)} OK · "
        f"⚠️ {c.get('WARNING', 0)} warning · ❌ {c.get('FAULTY', 0)} faulty · "
        f"⚫ {c.get('OFFLINE', 0)} offline",
        "",
    ]
    if rows["offline"]:
        lines += ["### ⚫ Σταθμοί εκτός λειτουργίας", "",
                  "| Σταθμός | ID | Δεδομένα ημέρας | Αιτία |", "|---|---|---|---|"]
        for s, why in sorted(rows["offline"], key=lambda x: x[0]["name"]):
            lines.append(f"| {s['name']} | {s['sensor_index']} | {s.get('completeness_pct', 0):.0f}% | {why} |")
        lines.append("")
    if rows["faulty"]:
        lines += ["### ❌ Σε λειτουργία, αλλά με πιθανή βλάβη", "",
                  "| Σταθμός | ID | R² | Slope | Intercept | Faulty 10-min (%) | Μέσος A | Μέσος B | Αιτία |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for s, why in sorted(rows["faulty"], key=lambda x: x[0]["name"]):
            st = s.get("stats") or {}
            m = (s.get("mean_by_fraction") or {}).get("pm2_5") or {}
            lines.append(
                f"| {s['name']} | {s['sensor_index']} | {fmt(st.get('r2'))} | {fmt(st.get('slope'))} | "
                f"{fmt(st.get('intercept'), 1)} | {fmt(s.get('_faulty_pct'), 0)} | "
                f"{fmt(m.get('mean_a'), 1)} | {fmt(m.get('mean_b'), 1)} | {why} |")
        lines.append("")
    if rows["warning"]:
        lines += ["### ⚠️ Προειδοποιήσεις", ""]
        for s, why in sorted(rows["warning"], key=lambda x: x[0]["name"]):
            lines.append(f"- **{s['name']}** ({s['sensor_index']}): {why}")
        lines.append("")
    if not state:
        lines += ["✅ Όλοι οι σταθμοί λειτουργούν κανονικά.", ""]

    pages = AL.get("dashboard_url") or pages_url()
    if pages:
        lines += [f"📊 Dashboard: {pages}", ""]
    lines += [f"<sub>{DISCLAIMER}</sub>", "",
              f"<!-- alert-state: {json.dumps(state, sort_keys=True)} -->"]
    return "\n".join(lines), state, by_id


def change_note(old: dict, new: dict, by_id: dict) -> str:
    label = {"offline": "εκτός λειτουργίας", "faulty": "πιθανή βλάβη", "warning": "προειδοποίηση"}
    name = lambda i: by_id.get(i, {}).get("name", i)
    new_bad = [i for i in new if old.get(i) != new[i]]
    fixed = [i for i in old if i not in new]
    out = []
    if new_bad:
        out.append("**Νέα προβλήματα:** " + ", ".join(f"{name(i)} ({label[new[i]]})" for i in new_bad))
    if fixed:
        out.append("**Αποκαταστάθηκαν:** " + ", ".join(name(i) for i in fixed))
    return "\n\n".join(out)


def pages_url() -> str | None:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo or "/" not in repo:
        return None
    owner, name = repo.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/"


# ---------------------------------------------------------------- GitHub
class GitHub:
    def __init__(self, token: str, repo: str):
        self.repo = repo
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}",
                               "Accept": "application/vnd.github+json"})

    def _req(self, method, path, **kw):
        r = self.s.request(method, f"{API}/repos/{self.repo}{path}", timeout=30, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"GitHub {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else {}

    def ensure_label(self, label):
        r = self.s.post(f"{API}/repos/{self.repo}/labels", timeout=30,
                        json={"name": label, "color": "d93f0b",
                              "description": "Daily sensor network alert"})
        if r.status_code not in (201, 422):  # 422 = already exists
            log.warning("Could not create label: %s %s", r.status_code, r.text[:200])

    def open_issue(self, label):
        items = self._req("GET", "/issues", params={"state": "open", "labels": label, "per_page": 5})
        items = [i for i in items if "pull_request" not in i]
        return items[0] if items else None

    def create_issue(self, title, body, label):
        return self._req("POST", "/issues", json={"title": title, "body": body, "labels": [label]})

    def update_issue(self, number, **fields):
        return self._req("PATCH", f"/issues/{number}", json=fields)

    def comment(self, number, body):
        return self._req("POST", f"/issues/{number}/comments", json={"body": body})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--summary", default=str(ROOT / CONFIG["output"]["site_data_dir"] / "latest.json"))
    args = ap.parse_args()

    summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
    report, state, by_id = build_report(summary)

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:  # also shows the report on the Actions run page
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(report + "\n")

    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if args.dry_run or not (token and repo):
        print(report)
        if not args.dry_run:
            log.warning("GITHUB_TOKEN / GITHUB_REPOSITORY not set — printed only, no issue posted.")
        return

    gh = GitHub(token, repo)
    label, mention = AL["issue_label"], AL["mention"].strip()
    ping = f"{mention} " if mention else ""
    issue = gh.open_issue(label)
    n_bad = len(state)
    title = f"⚠️ Δίκτυο Πάτρας: {n_bad} σταθμοί με πρόβλημα ({period(summary)})"

    if not issue:
        if not state:
            log.info("All stations OK and no open alert — nothing to do.")
            return
        gh.ensure_label(label)
        new = gh.create_issue(title, f"{ping}\n\n{report}", label)
        log.info("Opened alert issue #%s (%d station(s))", new["number"], n_bad)
        return

    m = STATE_RE.search(issue.get("body") or "")
    old_state = json.loads(m.group(1)) if m else {}
    num = issue["number"]

    if not state:
        gh.comment(num, f"{ping}✅ Όλοι οι σταθμοί λειτουργούν κανονικά ({period(summary)}). "
                        f"Κλείνει το alert.\n\n{change_note(old_state, state, by_id)}")
        gh.update_issue(num, body=report, state="closed", title=f"✅ Δίκτυο Πάτρας OK ({period(summary)})")
        log.info("All clear — closed issue #%s", num)
        return

    gh.update_issue(num, title=title, body=report)
    if state != old_state:
        gh.comment(num, f"{ping}Αλλαγή κατάστασης δικτύου ({period(summary)}):\n\n"
                        f"{change_note(old_state, state, by_id)}\n\nΠλήρης αναφορά στην περιγραφή του issue.")
        log.info("Issue #%s updated, change comment posted", num)
    else:
        log.info("Issue #%s updated silently (no change since last run)", num)


if __name__ == "__main__":
    main()
