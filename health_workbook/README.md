# SYZYGY Health Workbook Service

Loopback service for one shared staged workbook. Maintain callers can append and correct Measurements, Daily, Meals, Training, Lifts, and Notes. Weight Trend, Deficit Bank, and README stay read-only. There is no Cloudflare route to port 5052. Mission Control Health reads this service. Polar capture, Fitbit, the Macro App, and Withings append through this API. They do not open the workbook file. Current ownership is in [docs/ARCHITECTURE_CURRENT.md](../docs/ARCHITECTURE_CURRENT.md).

A maintain token is required in addition to the read token. They must differ. The read token cannot write. Every write requires `source`, `updated_by`, and `recorded_at`. A repeated identity with the same values is idempotent. A repeated identity with different values conflicts and must be corrected with `PATCH`, which requires `reason` and keeps the previous row in the audit log. Daily has one row per date. Training keeps a separate row for each `session_id` and `source`.

`python -m health_workbook.migrate` adds the Measurements sheet when it is absent and does nothing when that sheet is already valid. `python -m health_workbook.migrate schema-v2` adds Daily `day_status` and the formula-owned deficit columns. Both use the same backup, lock, and audit path as a write.

The process opens `HEALTH_WORKBOOK_PATH` with a read and checks that the file is a regular file. Point that variable at a staged copy. Leave the original workbook where it is.

## Tokens

`Authorization: Bearer <token>` on every request. `HEALTH_WORKBOOK_READ_TOKEN` can read. `HEALTH_WORKBOOK_MAINTAIN_TOKEN` can read and call the write routes below. Do not put either token in Git, URLs, or logs.

Other write methods return 405. A read token that attempts a write returns 403.

## Endpoints

- `GET /v1/status`
- `GET /v1/readme`
- `GET /v1/sheets/daily`
- `GET /v1/sheets/meals`
- `GET /v1/sheets/training`
- `GET /v1/sheets/lifts`
- `GET /v1/sheets/notes`
- `GET /v1/sheets/weight-trend`
- `GET /v1/sheets/deficit-bank`
- `GET /v1/sheets/measurements`
- `GET /v1/rows?sheet=&date=&from=&to=&session_id=&limit=`
- `GET /v1/audit?limit=&sheet=` (newest first, limit 1..100, default 20). The registry `health.audit` tool calls this route. The workbook process that started on 2026-10-04 00:16 PDT does not serve it until that process is restarted.
- `POST /v1/measurements` and `PATCH /v1/measurements/{row_id}`
- `POST /v1/daily` and `PATCH /v1/daily/{row_id}`
- `POST /v1/meals` and `PATCH /v1/meals/{row_id}`
- `POST /v1/training` and `PATCH /v1/training/{row_id}`
- `POST /v1/lifts` and `PATCH /v1/lifts/{row_id}`
- `POST /v1/notes` and `PATCH /v1/notes/{row_id}`

`session_id` applies to Training only. `date`, `from`, and `to` are `YYYY-MM-DD`. `limit` defaults to 100, maximum 500, and keeps the last matching rows in sheet order.

Required tabs: README, Daily, Meals, Training, Lifts, Notes, Weight Trend, Deficit Bank. A missing tab or a tabular header that does not match the current workbook makes sheet reads return 503. Status still returns 200 with `valid: false`.

## Pi staging commands

These commands are the original staging procedure. The service is already running on the Pi. Do not recopy the workbook or restart the unit to refresh this document.

Run these only after the phase 1 tree is on the Pi. They copy a workbook into the staging directory and start loopback port 5052. They do not modify the source file.

On the Pi:

```bash
mkdir -p /home/KA_PI/syzygy-fitness-data/staging
chmod 700 /home/KA_PI/syzygy-fitness-data /home/KA_PI/syzygy-fitness-data/staging
install -m 600 /dev/null /home/KA_PI/syzygy-runtime/health-workbook.env
```

Create the token on the Pi and store only the env file. Do not paste the token into chat:

```bash
umask 077
TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
printf 'HEALTH_WORKBOOK_READ_TOKEN=%s\n' "$TOKEN" > /home/KA_PI/syzygy-runtime/health-workbook.env
unset TOKEN
```

From the laptop, copy the workbook onto the staging path. This reads the original and writes the copy:

```powershell
scp C:\Users\KJA\Documents\Fitness\KJA_Fitness_Master_Oct12.xlsx raspi:/home/KA_PI/syzygy-fitness-data/staging/KJA_Fitness_Master_Oct12.xlsx
```

On the Pi, after this branch is checked out at `/home/KA_PI/syzygy-mission-control`:

```bash
sudo cp /home/KA_PI/syzygy-mission-control/systemd/syzygy-health-workbook.service /etc/systemd/system/syzygy-health-workbook.service
sudo systemctl daemon-reload
sudo systemctl enable --now syzygy-health-workbook.service
systemctl status syzygy-health-workbook.service --no-pager
ss -ltnp | grep 5052
python3 - <<'PY'
import json, urllib.request
from pathlib import Path
token = ""
for line in Path("/home/KA_PI/syzygy-runtime/health-workbook.env").read_text().splitlines():
    if line.startswith("HEALTH_WORKBOOK_READ_TOKEN="):
        token = line.split("=", 1)[1].strip()
request = urllib.request.Request(
    "http://127.0.0.1:5052/v1/status",
    headers={"Authorization": "Bearer " + token},
)
with urllib.request.urlopen(request, timeout=5) as response:
    body = json.loads(response.read().decode())
print(body["valid"], body["read_only"], body["mode"], body["public_route"], body["measurements_enabled"])
PY
```

The status check prints validity and mode only. `ss` should show `127.0.0.1:5052`. Do not add a Cloudflare hostname for this port.

## Feeders

`python -m health_workbook.fitbit_feeder` reads the loopback Fitbit MCP and writes Daily plus Training through this service. `python -m health_workbook.macro_feeder` reads the Macro App SQLite database and writes Meals plus Daily intake totals. Both use `HEALTH_WORKBOOK_MAINTAIN_TOKEN` from the environment. Each run covers today and yesterday in America/Los_Angeles, and a repeat of the same values does not write again.

Fitbit refreshes its own Daily columns. It leaves meal, macro, vodka, supplement, symptom, and note cells alone. It does not write readiness or sleep score. `weight_lb` is filled when the cell is empty and is left in place when a different value was written by someone other than `fitbit-sync`. `fitbit_resting_burn` is total burn minus active burn when both calories are present and the result is not negative. Training rows use `source=FITBIT`, `updated_by=fitbit-sync`, and `burn_role=do_not_sum`. The session id is the exercise start, type, and end.

Macro meals use `source=MACRO_APP` and `updated_by=macro-sync`. A meal already stored by another writer is not replaced. Daily intake updates are only `kcal_in`, `protein_g`, `carbs_g`, and `fat_g`.

`python -m health_workbook.withings_feeder` reads Withings Body Smart and BPM Connect through the Withings API and writes Measurements, plus Daily `weight_lb` and `body_fat_pct`, through this service. Client id, client secret, and tokens stay in the runtime env file. A refresh replaces the stored tokens there and does not print them. Raw blood pressure stays on Measurements. Daily weight or body fat already stored by another writer is left in place.

On the Pi, `syzygy-fitbit-sync.timer` runs every 30 minutes, `syzygy-macro-sync.timer` runs every 15 minutes, and `syzygy-withings-sync.timer` runs every 30 minutes. They are separate oneshot services. Logs are one JSON object of counts and field names. A Fitbit run that loses one tool and still writes the rest exits 0 and keeps that tool in `failed_tools`. The oneshot exits 1 when a workbook write fails or every Fitbit tool fails.

## Schema v2 (day_status, formula-driven deficit logic, Meals footer)

`python -m health_workbook.migrate schema-v2` (needs `HEALTH_WORKBOOK_PATH`) upgrades a workbook once. It is idempotent, takes the service write lock, writes a backup into `backups/` and an audit row, and records `hw_schema_version=2` as a workbook defined name. `/v1/status` reports `schema_version`.

- **Daily.day_status** is a new last column (`AF`). Values: `complete` or `open`. Anything else is rejected (`invalid_day_status`). Before the migration a write that carries it fails with `schema_outdated`. A status-only PATCH (`fields: {day_status: ...}`) does not change the row's `updated_by`, so feeder ownership of `weight_lb` is unaffected; the audit log still records who changed it.
- **Formula columns.** After the migration `fitbit_deficit, est_maint, est_deficit, under_1800, bank_under_1800_cum, bank_deficit_cum` are real Excel formulas on every Daily row. They are blank unless `day_status` is `complete`; the two bank columns are running sums over complete days with an earlier or equal date (row order does not matter). Writing a value into one of them is rejected (`formula_field`). The service refreshes the cached result of every formula after each write, and Excel/LibreOffice recalculate on open (`fullCalcOnLoad`). The 1800 ceiling is the literal in `under_1800`.
- **Deficit Bank** rows 7-13 and 21-22 are formulas over Daily (complete days only). Weight Trend is unchanged (static).
- **Meals** rows are inserted directly below the last real data row; a totals/summary block below the data is shifted down. Only the first row whose `date` is not `YYYY-MM-DD` starts the footer; footer rows are returned in `footer`, not `rows`. After the migration the `TOTAL` row and each `DAILY TOTALS` line are formulas, and a `DAILY TOTALS` line is added for a new meal date. The service refuses to shift rows if Meals holds formulas it does not manage (`meals_formulas_unsupported`).
