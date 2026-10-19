#!/usr/bin/env python3
"""Apply the Meals batch and Daily.day_status backfill through the workbook HTTP API.

Usage (token comes from the environment, never argv, never printed):
  WB_MAINTAIN_TOKEN=... python3 scripts/import_meals_and_statuses.py \
      --base http://127.0.0.1:5062 --meals meals_append.jsonl --statuses day_status_backfill.json \
      --out results/ [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request

TIME_12H = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp][Mm])$")
NUMERIC = ("kcal", "protein_g", "carbs_g", "fat_g")


def call(base: str, token: str, method: str, path: str, body: dict | None = None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method, headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read() or b"{}")
        except ValueError:
            return error.code, {}


def clean_meal(row: dict):
    """Return (meal, None) or (None, reason). Only format fixes: H:MM AM/PM -> HH:MM:SS AM/PM."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(row.get("date", ""))):
        return None, "bad date"
    if not isinstance(row.get("food"), str) or not row["food"].strip():
        return None, "food is not text (column-shifted daily-total row?)"
    if not isinstance(row.get("time"), str) or not row["time"].strip():
        return None, "time is not text (column-shifted daily-total row?)"
    for key in NUMERIC:
        value = row.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            return None, f"{key} is not numeric"
    meal = dict(row)
    m = TIME_12H.match(meal["time"].strip())
    if m:
        meal["time"] = f"{int(m.group(1)):02d}:{m.group(2)}:{m.group(3) or '00'} {m.group(4).upper()}"
    meal = {k: v for k, v in meal.items() if v is not None}
    return meal, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--meals")
    ap.add_argument("--statuses")
    ap.add_argument("--out", required=True)
    ap.add_argument("--supersede", help="JSON list of meals that replace an existing row via PATCH instead of being appended (opt-in)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    token = os.environ.get("WB_MAINTAIN_TOKEN", "")
    if not token:
        print("WB_MAINTAIN_TOKEN is required", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary: dict = {}
    if args.meals:
        counts = {"appended": 0, "exists": 0, "conflict": 0, "error": 0, "skipped_invalid": 0}
        if args.supersede:
            counts["superseded"] = 0
        supersede = {}
        if args.supersede:
            for item in json.loads(Path(args.supersede).read_text(encoding="utf-8"))["items"]:
                supersede[(item["new"]["date"], item["new"]["time"], item["new"]["food"])] = item
        detail, skipped = [], []
        for line_no, line in enumerate(Path(args.meals).read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            meal, reason = clean_meal(json.loads(line))
            if meal is None:
                counts["skipped_invalid"] += 1
                skipped.append({"line": line_no, "reason": reason, "row": json.loads(line)})
                continue
            if args.dry_run:
                continue
            item = supersede.get((meal["date"], meal["time"], meal["food"]))
            if item is not None:
                _, listing = call(args.base, token, "GET", f"/v1/rows?sheet=meals&date={meal['date']}")
                rows = listing.get("rows") or []
                if any(r.get("time") == meal["time"] and r.get("food") == meal["food"] for r in rows):
                    counts["exists"] += 1
                    detail.append({"line": line_no, "status": 200, "result": "exists"})
                    continue
                old = next((r for r in rows if r.get("time") == item["replace"]["time"] and str(r.get("food", "")).startswith(item["replace"]["food_prefix"])), None)
                if old is not None:
                    fields = {k: meal[k] for k in ("food", "kcal", "protein_g", "carbs_g", "fat_g", "notes") if k in meal}
                    status, body = call(args.base, token, "PATCH", "/v1/meals/" + old["row_id"], {
                        "fields": fields, "reason": item["reason"], "source": meal["source"],
                        "updated_by": meal["updated_by"], "recorded_at": meal["recorded_at"]})
                    key = "superseded" if status == 200 else "error"
                    counts[key] += 1
                    detail.append({"line": line_no, "status": status, "result": body.get("result") or body.get("error"), "row_id": old["row_id"], "superseded_old_food": old.get("food")})
                    continue
            status, body = call(args.base, token, "POST", "/v1/meals", meal)
            result = body.get("result") or body.get("error") or str(status)
            key = {"appended": "appended", "exists": "exists"}.get(result, "conflict" if result == "conflict" else "error")
            counts[key] += 1
            detail.append({"line": line_no, "status": status, "result": result, "row_id": body.get("row_id")})
        summary["meals"] = counts
        (out / "meals_apply_detail.json").write_text(json.dumps(detail, indent=1))
        (out / "meals_skipped_invalid.json").write_text(json.dumps(skipped, indent=1, ensure_ascii=False))
    if args.statuses:
        plan = json.loads(Path(args.statuses).read_text(encoding="utf-8"))
        counts = {"patched": 0, "same": 0, "missing_row": 0, "error": 0}
        detail = []
        for day, entry in sorted(plan["statuses"].items()):
            status_code, body = call(args.base, token, "GET", f"/v1/rows?sheet=daily&date={day}")
            rows = body.get("rows") or []
            if not rows:
                counts["missing_row"] += 1
                detail.append({"date": day, "result": "missing_row"})
                continue
            row = rows[-1]
            if row.get("day_status") == entry["day_status"]:
                counts["same"] += 1
                detail.append({"date": day, "result": "same"})
                continue
            if args.dry_run:
                continue
            status_code, body = call(args.base, token, "PATCH", "/v1/daily/" + row["row_id"], {
                "fields": {"day_status": entry["day_status"]},
                "reason": "backfill day_status: " + entry["evidence"],
                "source": "MANUAL", "updated_by": "Goggins",
                "recorded_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
            ok = status_code == 200
            counts["patched" if ok else "error"] += 1
            detail.append({"date": day, "result": body.get("result") or body.get("error"), "status": status_code})
        summary["statuses"] = counts
        (out / "status_apply_detail.json").write_text(json.dumps(detail, indent=1))
    (out / "apply_summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
