# Mission Engine

The Mission Engine runs inside Mission Control. It is not a Guardian service
and it is not a second path around Home Assistant or RoArm.

When nothing is active the engine reports `IDLE`. A run moves
`TRIGGERED` → `CHECKING` → `RUNNING` → `COMPLETE`, or ends `FAULT` or `STOPPED`.

## Rules

- One active physical mission. A second start returns `OWNER_BUSY`.
- The operator starts a mission. A request with `source: perception` is rejected with `TRIGGER_NOT_ENABLED` before any action.
- The mission definition supplies the target. The trigger cannot replace it.
- Subsystem policy still applies. The engine calls the Home Assistant adapter, not a raw service.
- STOP wins. Mission STOP is not RoArm torque-off.
- If the target was energized, cleanup is at most one `turn_off`, then a bounded observation of the required final state.
- A crash mid-run is recorded as interrupted on the next load. It is not replayed and it is not auto-cleaned.
- Mission failure does not turn system health red.

## Reference mission

`config/missions.yaml` defines `shelly_plug_cycle` version 1:

- kind `switch_cycle`, physical, subsystem `home_assistant`
- target `switch.1_plug_shelly`
- final state `"off"` (quoted, because YAML treats bare `off` as a boolean)
- wait 2 seconds, observe timeout 4 seconds

The success path checks health, bridge, target, and start state, turns the plug on, waits, turns it off, and observes `off`. The loader rejects a final state other than `off` and rejects targets that are not switches or lights.

The definition is data. Substituting another approved switch still requires that id on the Home Assistant write allowlist.

## API

- `GET /api/missions`
- `GET /api/missions/status`
- `GET /api/missions/history`
- `POST /api/missions/start` with `mission` and `operator` only
- `POST /api/missions/stop`

Extra fields are `MALFORMED_PARAMETERS`. Production start runs asynchronously so STOP can arrive during the wait.
