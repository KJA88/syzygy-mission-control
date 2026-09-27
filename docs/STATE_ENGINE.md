# SYZYGY State Engine — Phase 2, V0.1

## Purpose

The State Engine extends the existing Guardian snapshot into one coherent,
read-only representation of SYZYGY. It is not a second dashboard and it does
not replace Mission Control. Mission Control remains the human cockpit;
Guardian remains the evidence collector and health reducer.

```text
agents + probes -> Guardian snapshot -> State Engine view -> Mission Control / AI operators
```

## First increment

The first increment adds a backward-compatible top-level `state_engine` object
to every Guardian snapshot. Existing schema-v1 fields remain unchanged.

The State Engine initially derives only facts already supported by Guardian:

- overall system health
- Pi and Jetson health and metrics
- service health and host ownership
- camera health and online state
- explicit unknown placeholders for current mission and active operator
- explicit unknown placeholders for TV power, requested input, and verified input

No new network probes, device commands, robot state, motion, or recovery actions
are introduced.

## Assertion contract

Every attribute contains:

- `value`: asserted value, or `null` when unknown
- `knowledge`: `observed`, `derived`, `configured`, `remembered`, `requested`, `verified`, or `unknown`
- `freshness`: `fresh`, `stale`, `expired`, or `unknown`
- `source`: authoritative producer or adapter
- `observed_at`: UTC evidence timestamp or `null`
- `trace_id`: trace linking the assertion to its evidence
- `confidence`: `0.0..1.0` when meaningful, otherwise `null`
- `reason`: failure/unknown classification or `null`

Rules:

1. Unknown assertions always have `value: null` and `confidence: null`.
2. Requested state is never presented as verified state.
3. Remembered state must never silently become observed or verified state.
4. Stale evidence must not remain green/current through the State Engine.
5. Sources and trace IDs are retained; the State Engine does not erase lower-level evidence.
6. V0.1 is read-only.
7. Static configuration is labeled `configured`, never presented as a live observation.
8. An unavailable metric is `unknown`; `null` is not presented as an observed value.

## Stored-snapshot expiration

Both the Guardian storage reader and Mission Control UI reader enforce the same
boundary when the Guardian heartbeat exceeds its hard-stale limit:

- the system health assertion becomes unknown and stale
- observed, derived, verified, requested, and remembered assertions become stale
- their values and provenance remain intact unless the assertion is system health
- configured assertions remain fresh because they are static configuration, not
  time-sensitive observations
- existing unknown assertions remain unknown

This read-time transform prevents an old snapshot from presenting operational
evidence as current even when no new Guardian cycle has run.

## Compatibility

The State Engine is additive. Existing V0.1/V0.2 consumers may ignore the new
field. The operational card reads `state_engine.operational` only.

## Authoritative operational state

Guardian observations remain evidence. The authoritative operational record is a
separate object, `state_engine.operational`, written only by Guardian through
`state/operational-state.json`. The UI does not write it. It does not change
the system health rollup and it does not command the arm.

States: `IDLE`, `PREPARING`, `MOVING`, `COMPLETE`, `FAULT`, `STOPPED`.

Legal transitions:

```text
IDLE → PREPARING, FAULT, STOPPED
PREPARING → IDLE, MOVING, COMPLETE, FAULT, STOPPED
MOVING → COMPLETE, FAULT, STOPPED
COMPLETE → IDLE, PREPARING, FAULT, STOPPED
FAULT → IDLE, STOPPED
STOPPED → IDLE, FAULT
```

Any other transition is rejected and stored on `last_rejection`. `MOVING`
requires a motion authority. `FAULT` requires a fault class. `motion_permitted`
is true only while `PREPARING` has an authority or while `MOVING`. Phase 2
exposes that flag. It does not enforce it on the RoArm command path.

On process restart, `IDLE`, `COMPLETE`, `FAULT`, and `STOPPED` are recovered as
non-moving state. `PREPARING` and `MOVING` cannot be proven current, so recovery
enters `STOPPED` with reason `RECOVERY_UNPROVEN_MOTION` and
`motion_permitted: false`. A missing record starts at `IDLE`. An invalid record
starts at `IDLE` with reason `RECOVERY_INVALID`.

A stale Guardian heartbeat does not present `PREPARING` or `MOVING` as the
current operational state. Those claims become unknown until a fresh Guardian
cycle. Other fields that lack a value stay unknown.

## Planned adapters

Adapters must be added one at a time with fixtures, freshness rules, and live
verification:

1. TV operational state: power, requested input, verified input, credential expiry
2. Operator/session state
3. DHRAS detection/scene summaries
4. RoArm read-only controller, pose, joints, gripper, and authorization state

RoArm motion enforcement and high-level robot skills remain Phase 3.
Phase 2 does not gate the live command path on `motion_permitted`.
