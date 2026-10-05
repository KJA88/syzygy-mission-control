# SYZYGY capability registry

One localhost MCP answers what systems exist, which tools a profile may use, who owns a capability, what the safety rules are, and the declared state. It does not replace Fitbit, Polar, TV, Home Assistant, Vision Hub, or the RoArm control service.

The manifest is `config/syzygy-capabilities.json`. Protocol `2024-11-05`, server `syzygy-registry`, bind `127.0.0.1:8076`, path `/mcp`.

## Profiles

- `discover` may call `read` capabilities. This is the default.
- `operate` may call `read` and `write` capabilities.
- `restricted` capabilities are listed and always refused.

The effective profile comes from a server-held credential on the HTTP `Authorization` header. `SYZYGY_DISCOVER_TOKEN` is discover. `SYZYGY_OPERATE_TOKEN` is operate. Those values must differ, stay in the process environment, and are not returned to the caller. A `profile` field in `tools/call` is ignored. A missing or unknown credential stays discover. Every forwarded `health.write` logs the actor, identity, and effective profile, and does not log workbook tokens.

## Health

`health.read`, `health.write`, and `health.audit` call the Health Workbook Service at `127.0.0.1:5052`. They do not open the workbook file. Writes need `source`, `updated_by`, and `recorded_at`. A correction also needs `reason` and `row_id`. README, Weight Trend, and Deficit Bank are rejected before a write request is sent. There is no delete or replace tool. Identity idempotency, backups, and the audit log stay in the workbook service. The registry's `health.audit` calls `GET /v1/audit`. That route is in `health_workbook/service.py`. The Pi process started on 2026-10-04 00:16 PDT does not serve it until it is restarted. The sqlite file remains the audit log. See [Current architecture](ARCHITECTURE_CURRENT.md).

## Everything else

`roarm.status`, `roarm.skills`, `camera.list`, `pi.status`, `jetson.status`, and the Fitbit, Polar, TV, Home Assistant, and Mission Control status tools return the declared record. They do not probe the host. `roarm.motion`, `camera.snapshot`, and `camera.control` return a restriction and do not run.

A future subsystem is another object in the manifest plus capabilities whose `execution` is `declared` or `restricted`. `proxy` is only implemented for the three health capabilities. The manifest cannot name a shell or filesystem tool.

## What an agent still configures once

The registry process reads the workbook tokens from its environment and sends them only to `127.0.0.1:5052`. Those tokens are not a tool argument and are not returned to the caller. `SYZYGY_DISCOVER_TOKEN` and `SYZYGY_OPERATE_TOKEN` belong in `/home/KA_PI/syzygy-runtime/syzygy-registry.env`, mode 600, separate from the workbook file. Remote agents reach the public portal through Cloudflare Access. They do not receive either registry credential or the workbook maintain token.

## Portal, as observed 2026-10-05

`https://mcp.syzygylab.net/mcp` is the Cloudflare MCP server portal. The registry unit is installed and listening on `127.0.0.1:8076`. Its public origin is `https://registry.syzygylab.net/mcp` on the mission-control tunnel. Port `5052` is not a portal upstream.

There is one registry server record, id `syzygy`. It was Ready. Its bearer is the operate token, so `health.write` is on that record. There is no second `syzygy-operate` server. The profile still comes from that bearer, not from a client `profile` argument. The Guardian service token is on the SYZYGY Registry Access application's Service Auth policy. That addition is what made a new portal session list `syzygy` tools. A `profile` field cannot select the credential.

A new session that day listed 31 tools: portal meta tools, TV, and the registry's 21 tools, including `syzygy_health.read`, `syzygy_health.audit`, and `syzygy_health.write`. One operate write appended Notes `notes:57`. Fitbit, Polar, RoArm, DHRAS, and the git servers remained in the portal catalog and were not all in that session's tool list.

`syzygy_roarm.motion` can appear in the registry tool list. The registry still returns motion restricted and does not run it. The separate `roarm-mcp` server was not changed.

Home Assistant MCP stays on `127.0.0.1:8095` with no public hostname and is not a portal server.

The names below are the approved catalogs. A portal session can show a different subset.

| Server id | Portal URL | Published tools |
|---|---|---|
| `syzygy` | `https://registry.syzygylab.net/mcp` | The 21 tools in `config/syzygy-capabilities.json`, including health read, audit, and write |
| `fitbit` | `https://fitbit.syzygylab.net/mcp` | The 62 pinned read tools below |
| `polar` | `https://polar.syzygylab.net/mcp` | `get_polar_status`, `get_polar_heart_rate`, `get_polar_heart_rate_window`, `get_polar_rr_intervals` |
| `tv` | `https://tv.syzygylab.net/mcp` | `tv_dashboard`, `tv_cable`, `tv_set_input_state`, `tv_wall_cameras`, `tv_wall_status`, `tv_wall_page`, `tv_off` |
| `dhras` | `https://dhras.syzygylab.net/mcp` | The 15 read and image tools below. `delete_detection_image` stays disabled |
| `home` | No public hostname. Local process `http://127.0.0.1:8095/mcp` | `list_entities`, `get_entity`, `get_sensor`, `turn_on`, `turn_off`, `set_brightness`. Not attached |

The portal namespaces a tool as `{server_id}_{original_name}`.

Fitbit published names: `get_fitbit_exercises`, `get_fitbit_sleep`, `get_fitbit_resting_heart_rate`, `get_fitbit_hrv`, `get_fitbit_oxygen_saturation`, `get_fitbit_respiratory_rate`, `get_fitbit_sleep_temperature`, `get_fitbit_heart_rate`, `get_fitbit_heart_rate_zones`, `get_fitbit_weight`, `get_fitbit_exercise_history`, `get_fitbit_sleep_history`, `get_fitbit_resting_heart_rate_history`, `get_fitbit_hrv_history`, `get_fitbit_oxygen_saturation_history`, `get_fitbit_respiratory_rate_history`, `get_fitbit_sleep_temperature_history`, `get_fitbit_heart_rate_history`, `get_fitbit_heart_rate_zones_history`, `get_fitbit_weight_history`, `get_fitbit_steps`, `get_fitbit_steps_history`, `get_fitbit_distance`, `get_fitbit_distance_history`, `get_fitbit_active_zone_minutes`, `get_fitbit_active_zone_minutes_history`, `get_fitbit_total_calories`, `get_fitbit_total_calories_history`, `get_fitbit_active_energy_burned`, `get_fitbit_active_energy_burned_history`, `get_fitbit_floors`, `get_fitbit_floors_history`, `get_fitbit_active_minutes`, `get_fitbit_active_minutes_history`, `get_fitbit_time_in_heart_rate_zone`, `get_fitbit_time_in_heart_rate_zone_history`, `get_fitbit_activity_level`, `get_fitbit_activity_level_history`, `get_fitbit_vo2_max`, `get_fitbit_vo2_max_history`, `get_fitbit_height`, `get_fitbit_height_history`, `get_fitbit_blood_glucose`, `get_fitbit_blood_glucose_history`, `get_fitbit_daily_vo2_max`, `get_fitbit_daily_vo2_max_history`, `get_fitbit_run_vo2_max`, `get_fitbit_run_vo2_max_history`, `get_fitbit_altitude`, `get_fitbit_altitude_history`, `get_fitbit_sedentary_period`, `get_fitbit_sedentary_period_history`, `get_fitbit_body_fat`, `get_fitbit_body_fat_history`, `get_fitbit_core_body_temperature`, `get_fitbit_core_body_temperature_history`, `analyze_fitbit_metric_trend`, `compare_fitbit_periods`, `analyze_fitbit_exercise_progress`, `get_fitbit_daily_summary`, `get_fitbit_health_summary`, `get_fitbit_data_quality`.

DHRAS published names: `get_camera_status`, `get_recent_events`, `get_recent_person_events`, `get_recent_vehicle_events`, `get_recent_animal_events`, `get_latest_detection`, `get_latest_detection_image`, `get_detection_count`, `list_detection_images`, `get_detection_image`, `get_detection_summary`, `get_detection_date_range`, `get_detection_storage_usage`, `get_detection_classes`, `get_detection_cameras`.

Not published: Pi or Jetson shell, Git Audit, the RoArm MCP on `127.0.0.1:8040`, RoArm motion, camera snapshot or control, `delete_detection_image`, the workbook file, and port `5052`.

Rollback is to leave the portal unchanged. The TV canary remains on `https://tv.syzygylab.net/mcp`.
