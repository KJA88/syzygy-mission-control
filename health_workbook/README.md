# SYZYGY Health Workbook Service — phase 1

Read-only staging service for one shared workbook. Phase 1 serves GET requests on `127.0.0.1` and does not write, back up, lock, create a Measurements sheet, publish a Cloudflare route, or talk to Mission Control, Fitbit, the Macro App, or Polar.

The process opens `HEALTH_WORKBOOK_PATH` with a read and checks that the file is a regular file. Point that variable at a staged copy. Leave the original workbook where it is.

## Read token

`Authorization: Bearer <HEALTH_WORKBOOK_READ_TOKEN>` on every request. The token is an environment variable. Do not put it in Git, URLs, or logs.

There is no write token in this phase. POST, PUT, PATCH, and DELETE return 405.

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
- `GET /v1/rows?sheet=&date=&from=&to=&session_id=&limit=`

`session_id` applies to Training only. `date`, `from`, and `to` are `YYYY-MM-DD`. `limit` defaults to 100, maximum 500, and keeps the last matching rows in sheet order. Measurements is not served.

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
