# SYZYGY Health Workbook Service

Loopback service for one shared staged workbook. Maintain callers can append and correct Measurements, Daily, Meals, Training, Lifts, and Notes. Weight Trend, Deficit Bank, and README stay read-only. There is no Cloudflare route and no Mission Control or Polar feeder. Fitbit and the Macro App push through this API; they do not open the workbook file.

A maintain token is required in addition to the read token. They must differ. The read token cannot write. Every write requires `source`, `updated_by`, and `recorded_at`. A repeated identity with the same values is idempotent. A repeated identity with different values conflicts and must be corrected with `PATCH`, which requires `reason` and keeps the previous row in the audit log. Daily has one row per date. Training keeps a separate row for each `session_id` and `source`.

`python -m health_workbook.migrate` adds the Measurements sheet when it is absent and does nothing when that sheet is already valid. It uses the same backup, lock, and audit path as a write.

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
- `POST /v1/measurements` and `PATCH /v1/measurements/{row_id}`
- `POST /v1/daily` and `PATCH /v1/daily/{row_id}`
- `POST /v1/meals` and `PATCH /v1/meals/{row_id}`
- `POST /v1/training` and `PATCH /v1/training/{row_id}`
- `POST /v1/lifts` and `PATCH /v1/lifts/{row_id}`
- `POST /v1/notes` and `PATCH /v1/notes/{row_id}`

`session_id` applies to Training only. `date`, `from`, and `to` are `YYYY-MM-DD`. `limit` defaults to 100, maximum 500, and keeps the last matching rows in sheet order.

Required tabs: README, Daily, Meals, Training, Lifts, Notes, Weight Trend, Deficit Bank. A missing tab or a tabular header that does not match the current workbook makes sheet reads return 503. Status still returns 200 with `valid: false`.

## Pi staging commands

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

On the Pi, `syzygy-fitbit-sync.timer` runs every 30 minutes and `syzygy-macro-sync.timer` runs every 15 minutes. They are separate oneshot services. Logs are one JSON object of counts and field names.
