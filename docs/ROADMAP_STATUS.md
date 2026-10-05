# Roadmap status

Phases 1–6 are complete on `main`. Each line is the durable result, not the debug history.

## Phase 1 — Mission Control and Guardian

Complete. Pi and Jetson agents publish read-only health. Guardian on the Pi reduces one snapshot. Mission Control serves that snapshot. Required objects roll up to system health. Optional failures stay visible and do not by themselves turn the system red. The browser does not probe hosts or MCP servers itself.

## Phase 2 — State Engine

Complete. Every Guardian snapshot can carry a `state_engine` view. Values keep knowledge, freshness, source, and trace. Unknown stays unknown. Stale evidence is not presented as current. The engine reads operational state. It does not command devices.

## Phase 3 — RoArm skills and safety layer

Complete. Named skills go through the RoArm control owner in Mission Control. One owner, an uncleared stop, or an unproven recovery blocks motion. `motion_permitted` is a state fact, not an industrial interlock. STOP is not torque-off. Firmware, UDP, and serial transport stay in the RoArm repository. Guardian remains an observer of that record.

## Phase 4 — Perception and the Mission Control UI

Complete. Vision Hub remains the camera and perception owner. Mission Control shows perception, cameras, and events without taking over YOLO, PTZ, or tracking. The UI sections are Overview, Perception, Robots, Devices, Home, Missions, Events, System, and Settings.

## Phase 5 — Home Assistant and the safe MCP

Complete. A shared adapter reads Home Assistant entities and writes only where domain and entity-id policy allow. The MCP on `127.0.0.1:8095` exposes `list_entities`, `get_entity`, `get_sensor`, `turn_on`, `turn_off`, and `set_brightness`. There is no generic `call_service`. See [Home Assistant](HOME_ASSISTANT.md).

## Phase 6 — Mission Engine

Complete. One operator-started physical mission can run at a time. The reference mission is `shelly_plug_cycle`. Perception events do not start it. STOP wins, including a single safe-off when the target was energized. See [Mission Engine](MISSION_ENGINE.md).

## After Phase 6

Secure remote Mission Control and the Android PWA shell are on `main`. The phone reaches only the Mission Control UI through Cloudflare Access and the dedicated tunnel. See [Remote access](REMOTE_ACCESS.md).

Health workbook, Fitbit, Macro, Withings, Mission Control Health, Polar workbook capture, and the SYZYGY Registry are on `syzygy-health-workbook`. They are not merged to `main`. The current map, including what is live on the Pi and what is only on the laptop, is [Current architecture](ARCHITECTURE_CURRENT.md). Polar capture rules are in [HRV](HRV.md). The old RR probe branch is historical and is not the capture path.
