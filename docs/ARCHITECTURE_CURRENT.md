# Current SYZYGY architecture

Status date: 2026-10-05. This page is the ownership map. It replaces the
2026-09-28 map. Work that is only on the laptop, or only in an uncommitted
tree, is labeled that way.

The health, Polar, registry, and portal work since 2026-10-01 is on branch
`syzygy-health-workbook`. It is not merged to `main`. On 2026-10-05 the Pi
checkout was `94bd9d5` with a dirty tree. The files that match that running
tree are committed on this branch after that. Pushing the branch does not
update the Pi checkout.

```text
Phone / Browser / AI
        |
        +-- Mission Control UI  -->  Guardian, Mission Engine, Health view
        |         |
        |         +-- Health Workbook Service :5052 --> staged workbook + sqlite audit
        |         |         ^
        |         |         +-- Fitbit / Macro / Withings timers
        |         |         +-- Polar capture (operator start only)
        |         |
        |         +-- RoArm control service, Home Assistant, Vision Hub
        |
        +-- Cloudflare MCP portal https://mcp.syzygylab.net/mcp
                  |
                  +-- SYZYGY Registry https://registry.syzygylab.net/mcp
                            |
                            +-- loopback :8076, then workbook :5052 for health.*
```

## Who owns what

| Piece | Owns | Does not own |
|---|---|---|
| Mission Control | Cockpit, HTTP API, presentation, health dashboard, and the entry point for approved actions | Direct Shelly calls, raw Home Assistant services, RoArm firmware or UDP, RTSP, camera credentials, or the workbook file |
| Guardian | Health snapshots, heartbeat freshness, and read-only probes, including the Cloudflare Access MCP probe | Side-effecting TV, home, or robot commands |
| State Engine | Read-only assertions over Guardian evidence and the operational record | Motion, device writes, or mission starts |
| Mission Engine | One active physical mission, checks, and stop/cleanup through adapters | A second concurrent physical mission, or unattended starts from perception |
| Vision Hub / DHRAS | Camera acquisition, perception events, and vision health | Home devices or the arm |
| Home Assistant | Home entities and their device behavior | Mission policy. A friendly name change does not change the allowlist |
| RoArm control service | Named arm skills and command execution | The transport implementation, which remains in the RoArm repository |
| Health Workbook Service | The staged workbook, its backups, and its sqlite audit | Cloudflare, Mission Control, or device credentials |
| SYZYGY Registry | A profile-scoped MCP catalog and a proxy for approved health calls | The workbook file, shells, RoArm motion, and camera control |

STOP in Mission Control or the Mission Engine is an operational stop. It is not RoArm torque-off. Polar Stop ends a heart-rate capture. It is not RoArm torque-off.

## Paths that are live

Observed on the Pi at 2026-10-05T07:14:00Z unless a line says otherwise.

| Surface | Where | Process |
|---|---|---|
| LAN Mission Control | `http://192.168.1.18:9070/` | `syzygy-mission-control.service`, `ui/server.py` |
| Remote Mission Control | `https://mission.syzygylab.net` to `http://127.0.0.1:9070` | `cloudflared.service` on the mission-control tunnel |
| Health Workbook Service | `127.0.0.1:5052` only | `syzygy-health-workbook.service` |
| Withings callback | `127.0.0.1:8791`, public `https://withings.syzygylab.net/callback` | `python -m health_workbook.withings_callback` |
| SYZYGY Registry | `127.0.0.1:8076/mcp`, public `https://registry.syzygylab.net/mcp` | `syzygy-registry.service` |
| MCP portal | `https://mcp.syzygylab.net/mcp` | Cloudflare Access MCP portal, not a Pi process |
| Home Assistant MCP | `127.0.0.1:8095` | Pi only. Not on the portal |
| Home Assistant | port 8123 on the Pi | Not on the Mission Control tunnel |
| Jetson | `192.168.1.17` | Jetson agent and Vision Hub. Not the Mission Control origin |

`syzygy-data.service` is also active. It is gunicorn from `/home/KA_PI/syzygy-data` on `127.0.0.1:8765`. It is not the Health Workbook Service and it is not the source of truth for Mission Control health.

## Mission Control

- **Architecture.** Cockpit and HTTP API on the Pi. The health dashboard reads the Health Workbook Service. Polar start and stop are operator actions in that UI.
- **Service / path / port.** `syzygy-mission-control.service`. Code `/home/KA_PI/syzygy-mission-control`. Bind `0.0.0.0:9070`. Remote URL `https://mission.syzygylab.net`. On 2026-10-05 the listener PID matched the unit main PID.
- **Source of truth.** This repository for the UI. Guardian evidence for system health. The workbook service for body and training rows.
- **Live.** The unit is active. The unit description still says "V0.1 LAN read-only UI". That string is not the capability boundary.
- **Automatic.** The service stays up under systemd. Health rows refresh from the workbook service when the dashboard asks.
- **Manual.** Operator starts. Polar capture. Approved device actions.
- **Safety.** No direct Shelly, Home Assistant service, RoArm UDP, or RTSP. STOP is not torque-off. Port 9070 is the only Mission Control origin behind that hostname.
- **Tests.** Health view and UI tests exist in the tree (`tests/test_health_view.py` and the UI files). They were not re-run for this record.
- **Commits.** `94bd9d5` (2026-10-03) "Show Mission Control Health from the workbook service." The Polar UI files in the later commits match the Pi copies of `ui/app.js`, `ui/index.html`, `ui/server.py`, `ui/styles.css`, and `ui/sw.js`.
- **Limitations.** `health/view.py` on the laptop has further dashboard changes that are not on the Pi. Those files stay uncommitted. The Pi will keep serving its dirty tree until that checkout is updated.
- **Next.** Keep health reads on the workbook service. Do not publish 9071, Home Assistant, RoArm, vision, MQTT, or MCP ports on the Mission Control hostname.

## Health Workbook Service

- **Architecture.** Loopback HTTP service. Writes go through one lock, one backup, and one sqlite audit. Sheets the service will write: Measurements, Daily, Meals, Training, Lifts, Notes. README, Weight Trend, and Deficit Bank stay read-only.
- **Service / path / port.** `syzygy-health-workbook.service`, `python -m health_workbook`, `127.0.0.1:5052`. Workbook `/home/KA_PI/syzygy-fitness-data/staging/KJA_Fitness_Master_Oct12.xlsx`. Token file `/home/KA_PI/syzygy-runtime/health-workbook.env` mode 600. Backups live beside the workbook. The audit database is the sqlite file next to that tree.
- **Source of truth.** The staged workbook plus the sqlite audit. Mission Control and the registry are clients.
- **Live.** Unit active since 2026-10-04 00:16 PDT. There is no Cloudflare route to port 5052.
- **Automatic.** The process stays up. Feeders append on their timers.
- **Manual.** Staging a workbook, migration, and any correction that needs a reason.
- **Safety.** No delete and no replace. Protected sheets are rejected. A correction needs `reason` and `row_id`. Identity fields make a repeat append idempotent. Provenance is `source`, `updated_by`, and `recorded_at`.
- **Tests.** Workbook and write tests are in `tests/test_health_workbook.py` and `tests/test_health_workbook_writes.py`. They were not re-run for this record.
- **Commits.** `295c715` read-only service (2026-10-01). `67eba90` Measurements writes (2026-10-02). `401c873` controlled writes for the shared sheets (2026-10-02).
- **Limitations.** `GET /v1/audit` is in the service module. The process that has been running since 2026-10-04 00:16 PDT was started from a file that did not contain the route, and `GET http://127.0.0.1:5052/v1/audit` returned 404. The sqlite file is the live audit until that process is restarted. A portal validation note remains at row `notes:57` ("Portal validation test note. Safe to ignore."). A later void call did not add a second audit row.
- **Next.** Restart the workbook service only when that is approved. Leave `notes:57` unless a normal correction is requested.

## Workbook schema v2

- **Architecture.** Two migrations. `python -m health_workbook.migrate` adds the Measurements sheet. `python -m health_workbook.migrate schema-v2` adds Daily `day_status` (`complete` or `open`), formula-owned deficit columns, Deficit Bank formulas, and the Meals footer. `/v1/status` reports `schema_version`. The live modules are `health_workbook/formulas.py`, `pkg.py`, and `sheetxml.py`.
- **Service / path / port.** One-shot commands against the staged workbook. Not a timer. The running service on port 5052 already imports these modules. Its files were last written at 2026-10-04 00:15 PDT, one minute before that process started.
- **Source of truth.** The staged workbook after `schema-v2`, plus the code on the Pi tree.
- **Live.** Schema v2 is what the Pi workbook service is running. It was uncommitted. Measurements writes are commit `67eba90`.
- **Automatic.** Nothing. Both commands are idempotent.
- **Manual.** An operator runs the migration.
- **Safety.** Same backup, lock, and sqlite audit as a write. Formula-owned Daily columns reject direct writes (`formula_field`). A status-only patch does not change `updated_by`, so feeder ownership of `weight_lb` stays intact.
- **Tests.** `tests/test_health_workbook_schema_v2.py`.
- **Commits.** The schema v2 modules had no commit before this reconciliation. `67eba90` is the earlier Measurements sheet only.
- **Limitations.** One-time backfill inputs remain only on the Pi: `scripts/day_status_backfill.json`, `scripts/meals_supersede.json`, `scripts/import_meals_and_statuses.py`, `scripts/e2e_copy_test.py`, and `scripts/lo_verify.py`. They are not the service.
- **Next.** Do not recopy the staged workbook.

## Fitbit feeder

- **Architecture.** Oneshot sync from the Fitbit MCP at `http://127.0.0.1:8010/mcp` into the workbook service. Source `FITBIT`, writer `fitbit-sync`. Burn role `do_not_sum`. It does not write readiness or a sleep score, and it does not replace another writer's weight.
- **Service / path / port.** `syzygy-fitbit-sync.service` and `syzygy-fitbit-sync.timer`. Loopback MCP only.
- **Source of truth.** Fitbit via that MCP, then the staged workbook.
- **Live.** Timer active (every 30 minutes). At 2026-10-05 the last oneshot had `Result=exit-code` and exit status 1.
- **Automatic.** The timer starts the oneshot.
- **Manual.** Recovery after a failed run.
- **Safety.** Same workbook write rules. Treadmill import stays separate from this feeder.
- **Tests.** `tests/test_health_feeders.py`. Not re-run for this record.
- **Commits.** `eafe624` (2026-10-02).
- **Limitations.** From 2026-10-05 09:37 PDT through 12:07 PDT every oneshot exited 1 because `get_fitbit_weight_history` failed, while the other tools still patched Daily. The Fitbit MCP logged HTTP 200 for those calls and did not log the tool error. From 12:37 PDT the same day the oneshot succeeded with `failed_tools: []`. The feeder now exits 0 when one tool fails and the workbook writes succeed, and exits 1 when a write fails or every tool fails. The Pi still runs the previous exit rule until its checkout is updated.
- **Next.** Leave the Fitbit MCP server on the portal unchanged.

## Macro feeder

- **Architecture.** Oneshot sync of Daily intake only: `kcal_in`, `protein_g`, `carbs_g`, `fat_g`. Source `MACRO_APP`, writer `macro-sync`.
- **Service / path / port.** `syzygy-macro-sync.service` and `syzygy-macro-sync.timer` (every 15 minutes).
- **Source of truth.** The Macro App export, then the staged workbook.
- **Live.** Timer active. Last oneshot `Result=success` at the 2026-10-05 check.
- **Automatic.** The timer.
- **Manual.** Export availability when the import table is absent.
- **Safety.** Intake fields only. Same workbook write rules.
- **Tests.** Feeder tests. Not re-run for this record.
- **Commits.** `eafe624`. `292b07f` (2026-10-03) covers a missing import table.
- **Limitations.** None recorded from the last run.
- **Next.** Leave the timer as it is.

## Withings OAuth, Body Smart, and BPM Connect

- **Architecture.** OAuth callback on loopback, tokens in `/home/KA_PI/syzygy-runtime/withings.env` mode 600. The sync timer pulls measurements and appends through the workbook service. Model `16`, the text "body smart", and `wbs13` map to Body Smart. BPM Connect is the blood-pressure model in `health_workbook/withings_feeder.py`.
- **Service / path / port.** Callback `127.0.0.1:8791`, public `https://withings.syzygylab.net/callback` on the mission-control tunnel. Sync: `syzygy-withings-sync.timer` every 30 minutes. State file for the OAuth state is the callback's `withings-oauth.state`.
- **Source of truth.** Withings for the device rows. The staged workbook after the feeder accepts them.
- **Live.** Port 8791 is the callback process. The sync timer is active. Last oneshot `Result=success`.
- **Automatic.** The sync timer. The callback process was running and was not one of the `syzygy-*` units listed on 2026-10-05.
- **Manual.** The first OAuth grant in a browser.
- **Safety.** The callback does not proxy the workbook and does not proxy the Withings measurement API. Tokens stay in the env file. Port 5052 stays unpublished. The callback code is uncommitted on the laptop.
- **Tests.** `tests/test_withings_callback.py` is untracked on the laptop. Not re-run for this record.
- **Commits.** `0a11c0b` feeder (2026-10-03). `f5d9fdb` model 16 as Body Smart (2026-10-03). The callback module has no commit.
- **Limitations.** No checked-in unit owns the callback process. A reboot can drop it until that process is started again.
- **Next.** A unit for the callback, only when a service install is approved.

## Polar H10

Two Polar paths are live. They are not the same program.

The older read MCP is `polar-h10-mcp.service` on `127.0.0.1:8030`, published as `polar-h10-mcp` on the portal, with `cloudflared-polar-h10-mcp.service`. That server remains the four read tools. It does not write the workbook.

Workbook capture is `control/polar_capture.py` and `health_workbook/polar_h10.py`, started from Mission Control. Wearing the strap does not start a capture.

- **Architecture.** Operator start chooses morning HRV or workout. The capture writes through the workbook service. Morning HRV is a short timed Measurement (default 60 seconds, maximum 900). Workout heart rate records until Stop, with a three-hour safety timeout (`10800` seconds) that uses the same finish path. A second start while one capture is active returns `CAPTURE_ACTIVE`.
- **Service / path / port.** Capture runs inside the Mission Control tree. BLE is the H10. There is no separate capture unit. Wi-Fi help is `syzygy-polar-wifi.service` (below).
- **Source of truth.** The strap for the samples. The staged workbook after the service accepts the row. Morning RMSSD is a Measurement and stays beside Fitbit overnight HRV. Workout RR and RMSSD are not written as that Measurement.
- **Live.** The Wi-Fi helper unit is active. Capture code on the Pi matches the copies committed after `b2aed6b`. The Pi checkout itself remains `94bd9d5` until it is updated.
- **Automatic.** Nothing starts a capture. The three-hour workout ceiling and the morning timer end a capture that was already started.
- **Manual.** Start and Stop in Mission Control.
- **Safety.** See the arm-hold section. Duplicate start does not open a second session. `CONNECT_TIMEOUT_S` in `health_workbook/polar_h10.py` stays 60 seconds.
- **Tests.** `tests/test_polar_h10.py`, `tests/test_polar_capture.py`, and `tests/test_polar_wifi.py` are in the working tree. They were not re-run for this record.
- **Commits.** `b2aed6b` (2026-10-03) on the laptop only: "Record Polar H10 resting HRV and workout heart rate through the workbook service." Not pushed. Not the Pi HEAD.
- **Limitations.** The historical RR probe in [HRV](HRV.md) is not this capture path. The portal's Polar MCP is still the read-only server.
- **Next.** Commit and deploy Polar only when that is explicitly requested. Do not start a capture to prove the doc.

## Wi-Fi / roarm-ap capture workaround

- **Architecture.** Mission Control cannot change NetworkManager. A root helper accepts four actions on a Unix socket: `status`, `disable`, `restore`, `verify`. The only connection it brings up is `roarm-ap` at `192.168.4.2`. Disable is `nmcli radio wifi off`. Restore is radio on, then `nmcli connection up roarm-ap`.
- **Service / path / port.** `syzygy-polar-wifi.service`. Socket `/run/syzygy-polar-wifi/control.sock`. Code `control/polar_wifi.py`. User `KA_PI` on the socket. The unit is active (main PID 3959230 at the check).
- **Source of truth.** The helper's allowlist, not a free shell.
- **Live.** The unit is installed on the Pi. The unit file and module are uncommitted on the laptop.
- **Automatic.** The helper stays up. It does not toggle Wi-Fi until a capture asks.
- **Manual.** Only as part of an operator-started capture.
- **Safety.** The intent file is written before the radio is turned off, so a crash can restore `roarm-ap` before the arm hold is released. Mission Control keeps `NoNewPrivileges`.
- **Tests.** `tests/test_polar_wifi.py`. Not re-run for this record.
- **Commits.** None. The unit and module are untracked in git.
- **Limitations.** The helper is a workaround for the H10 and the arm AP sharing Wi-Fi. It is not a general network API.
- **Next.** Leave the socket allowlist as those four actions.

## Polar arm hold and recovery

- **Architecture.** Capture takes a generic arm hold on the skill owner. Holder `polar-h10`. Hold reason `POLAR_CAPTURE`. If Wi-Fi does not restore, the release reason is `WIFI_NOT_RESTORED` and the hold stays. The arm is released only after the restore path finishes.
- **Service / path / port.** Hold state is the Mission Control owner file. Wi-Fi is the helper above.
- **Source of truth.** `control/polar_capture.py` and `control/owner.py`.
- **Live.** The code is in the working tree and on the Pi as dirty files. It was not exercised for this record.
- **Automatic.** Recovery follows the intent written before the radio drops.
- **Manual.** An operator starts the capture that takes the hold.
- **Safety.** The hold blocks arm skills for the duration. It is not torque-off and it is not a motion command. Do not issue RoArm motion to test it.
- **Tests.** Capture tests cover the hold and `CAPTURE_ACTIVE`. Not re-run for this record.
- **Commits.** None for the hold helper. Related writer commit is the unpushed `b2aed6b`.
- **Limitations.** A capture left running keeps the hold until Stop, the morning timer, or the three-hour workout ceiling.
- **Next.** No capture until one is requested.

## Cloudflare routes and the MCP portal

- **Architecture.** One mission-control `cloudflared` publishes Mission Control, the Withings callback, and the registry. The portal at `https://mcp.syzygylab.net/mcp` is Cloudflare's MCP portal (`cloudflare-mcp-portal` 1.0.0). Tool names are `{server_id}_{original_name}`.
- **Service / path / port.** Tunnel unit `cloudflared.service`. Registry public origin `https://registry.syzygylab.net/mcp` to `http://127.0.0.1:8076`. Withings `https://withings.syzygylab.net` to `http://127.0.0.1:8791`. Mission `https://mission.syzygylab.net` to `http://127.0.0.1:9070`.
- **Source of truth.** Cloudflare for hostnames, Access, and portal server records. This repo's `config/services.yaml` for the Guardian catalog. TV's catalog URL is `https://tv.syzygylab.net/mcp`.
- **Live.** On 2026-10-05 the portal server id `syzygy` was Ready. A new portal session after the Guardian service token was added to the SYZYGY Registry Access application returned 31 tools, including `syzygy_health.read` and `syzygy_health.audit`. One `syzygy_health.write` appended Notes row `notes:57`.
- **Automatic.** Tunnel and portal stay up under Cloudflare.
- **Manual.** Portal server records, Access policies, and service-token grants.
- **Safety.** Port 5052 is not on the tunnel. The registry refuses `roarm.motion` and camera control. The separate `roarm-mcp` server on the portal still has its own motion tools and was not changed. Home Assistant MCP has no public hostname.
- **Tests.** Portal checks on 2026-10-05, listed under the registry section. No Cloudflare API token was available from this repo, so dashboard policy text was not re-read after the user added the Guardian token.
- **Commits.** None for the portal or the tunnel route. `config/services.yaml` has uncommitted catalog edits, including the TV auth URL.
- **Limitations.** The 31-tool session matches portal meta tools plus TV plus the 21 registry tools. Fitbit, Polar, RoArm, DHRAS, and the git MCP servers were still in the portal catalog and were not all present in that session's tool list. That session was not used to call them.
- **Next.** Leave existing portal servers as they are. A broader session that enables every catalog server is a separate approval.

## SYZYGY Registry

- **Architecture.** MCP server. Profiles are `discover` (read) and `operate` (read and write). The profile comes from the bearer token compared with `SYZYGY_DISCOVER_TOKEN` and `SYZYGY_OPERATE_TOKEN`. Client `profile` arguments are ignored. Missing or unknown bearer stays discover. The operate token must be set and must differ from the discover token, or operate is never granted. Protocol versions `2024-11-05`, `2025-03-26`, `2025-06-18`, and `2025-11-25` are echoed. `2026-07-28` is answered as `2025-11-25`. Successful JSON-RPC bodies are SSE (`event: message`). `notifications/initialized` is HTTP 202 with an empty JSON body and the session header.
- **Service / path / port.** `syzygy-registry.service`, `127.0.0.1:8076/mcp`, code `python -m registry.mcp_server`. Env `/home/KA_PI/syzygy-runtime/syzygy-registry.env` mode 600. Catalog `config/syzygy-capabilities.json` (21 tools). Public URL `https://registry.syzygylab.net/mcp`.
- **Source of truth.** The capability file for names. The workbook service for health data. The bearer for profile.
- **Live.** Unit active since 2026-10-04 23:06 PDT. Main PID 339917 at the check. Portal server Ready.
- **Automatic.** The unit stays up. Declared status tools do not probe the host.
- **Manual.** Token files and the portal server record.
- **Safety.** Health calls go to `127.0.0.1:5052` with the workbook maintain token. The registry does not open the workbook or the sqlite file. `roarm.motion` returns motion restricted. Camera snapshot and camera control return camera restricted. Tokens are not logged. Every forwarded `health.write` logs actor, identity, profile, capability, and whether it was accepted.
- **Tests.** `tests/test_registry.py` (11 tests) passed in the implementation session before this record. On 2026-10-05, through the portal: `health.read` and `health.audit` were present, `health.write` accepted `notes:57`, the sqlite audit has that append, and the backup file named by the write exists. `GET /v1/audit` on the workbook service returned 404.
- **Commits.** None. `registry/`, `scripts/run-syzygy-registry.sh`, `systemd/syzygy-registry.service`, and `config/syzygy-capabilities.json` are uncommitted.
- **Limitations.** The registry files match the Pi copies aside from CRLF on the laptop. Pi HEAD remains `94bd9d5` until that checkout is updated. The running process is the one started at 2026-10-04 23:06 PDT.
- **Next.** Commit the registry only when asked. Do not print the env file.

## health.read, health.audit, and health.write

- **Architecture.** Registry tools proxy the workbook service. `health.read` and `health.audit` are discover. `health.write` is operate. The portal prefixes them `syzygy_`.
- **Service / path / port.** Portal `https://mcp.syzygylab.net/mcp` to registry `:8076` to workbook `:5052`.
- **Source of truth.** Workbook responses for the read. Sqlite audit for the audit of a write. The write response's backup name for the backup file on disk.
- **Live.** Proven 2026-10-05 for one Notes append.
- **Automatic.** Nothing writes by itself through the portal.
- **Manual.** An operate bearer, which is the token stored on the single `syzygy` portal server record.
- **Safety.** Writes still need sheet, values, source, updated_by, and recorded_at. Corrections need row_id and reason. The validation write used sheet Notes only.
- **Tests.** The one portal write above. Audit proof was the sqlite row (action append, sheet Notes, row `notes:57`, backup file present), not the HTTP audit route.
- **Commits.** None for the registry proxy.
- **Limitations.** `health.audit` calls `GET /v1/audit`. That route is in the service module. The running Pi process still returns 404 until it is restarted.
- **Next.** Do not void `notes:57` unless a correction is requested.

## Git, as of this record

- Branch `syzygy-health-workbook`. It is not merged to `main`.
- On 2026-10-05 the Pi HEAD was `94bd9d5` with 33 dirty paths. This reconciliation compared those paths to the laptop tree and committed the ones that match the running code.
- Local `main` is behind `origin/main`. Health commits are not on local `main`.
- Pushing this branch does not change the Pi checkout, the running processes, or the workbook.

Important commits on the branch:

| SHA | Date | What |
|---|---|---|
| `295c715` | 2026-10-01 | Read-only health workbook service |
| `67eba90` | 2026-10-02 | Measurements writes |
| `401c873` | 2026-10-02 | Controlled writes for the shared sheets |
| `eafe624` | 2026-10-02 | Fitbit and Macro feeders |
| `292b07f` | 2026-10-03 | Macro when the import table is absent |
| `0a11c0b` | 2026-10-03 | Withings feeder |
| `f5d9fdb` | 2026-10-03 | Withings model 16 as Body Smart |
| `94bd9d5` | 2026-10-03 | Mission Control Health. Pi HEAD |
| `b2aed6b` | 2026-10-03 | Polar workbook writer. Was laptop-only until this reconciliation was pushed |
| `61d69f4` | 2026-10-05 | Live schema v2 workbook |
| `019fdf0` | 2026-10-05 | `GET /v1/audit` |
| `1a38881` | 2026-10-05 | Polar capture, arm hold, and roarm-ap helper |
| `61f9dfb` | 2026-10-05 | Withings OAuth callback |
| `7667634` | 2026-10-05 | Mission Control Polar controls |
| `53deb78` | 2026-10-05 | TV auth probe URL and optional cameras |
| `8aa4dac` | 2026-10-05 | SYZYGY Registry |
| `185aa26` | 2026-10-05 | Fitbit oneshot exits 0 when one tool fails and writes succeed |

Files that matched the Pi and are now in git: the schema v2 workbook modules, Polar capture and the Wi-Fi helper, the Withings callback, the Mission Control Polar UI, the registry, and `config/services.yaml`. `GET /v1/audit` is in the service module and is not in the process that is still running.

Left only on the Pi, not committed: `scripts/day_status_backfill.json`, `scripts/meals_supersede.json`, `scripts/import_meals_and_statuses.py`, `scripts/e2e_copy_test.py`, and `scripts/lo_verify.py`.

Left only on the laptop, not committed: `health/view.py`, `tests/test_health_view.py`, `docs/CAMERAS.md`, `config/vision-cameras.example.json`, `tests/test_camera_integration.py`, and the `tmp_*` scripts.

## Known limits

- Fitbit's latest sync exited 1 on `get_fitbit_weight_history`.
- The Withings callback has no `syzygy-*` unit.
- `syzygy-data.service` on port 8765 is a different program.
- The portal session that proved `syzygy` did not include every other MCP server's tools.
- Registry `roarm.motion` stays restricted. The separate RoArm MCP was not modified and must not be used for motion from this work.
- No RoArm motion, no service restart, no Cloudflare change, and no workbook edit were made while writing this page.
