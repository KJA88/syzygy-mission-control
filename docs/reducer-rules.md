# Guardian Reducer Rules

Precedence: never average conflicting evidence.

1. Hard-stale Guardian heartbeat -> overall `unknown`.
2. Probe run crash -> overall `unknown`; never GREEN.
3. Per object:
   - missing/stale required evidence -> `unknown`
   - required layer fail -> `red`
   - unresolved conflict -> `yellow` + `CONFLICTING_PROBES`
   - optional fail / stale catalog / soft degradation -> `yellow`
   - otherwise -> `green`
4. Overall uses `required: true` objects only:
   - any red -> red
   - else any unknown -> unknown
   - else any yellow -> yellow
   - else green

## Auth off

Auth/invoke layers in `off` mode are `unknown`. If optional, they do not block overall GREEN. If marked required while off, the service and overall state become `unknown`.

## Conflict example

`local_mcp=pass` + `public_mcp=fail` keeps both chips visible. Service result depends on requiredness of the public layer, but it is never averaged to GREEN.

## Pseudocode

```text
fn reduce_system(cfg, snaps, guardian_meta, now):
  if heartbeat_hard_stale(guardian_meta, now):
    return unknown, reason=GUARDIAN_HEARTBEAT_STALE
  if guardian_meta.last_run == crash:
    return unknown, reason=PROBE_CRASH

  required = [s for s in snaps if s.required]
  if any(s.status == red for s in required): return red
  if any(s.status == unknown for s in required): return unknown
  if any(s.status == yellow for s in required): return yellow
  return green

fn reduce_service(svc_cfg, facts, now):
  chips = evaluate_all_layers(svc_cfg, facts, now)
  if any required layer fail: return red
  if any required layer unknown: return unknown
  if any optional layer fail or catalog_stale or conflict(chips): return yellow
  return green
```

Snapshot write: temp + fsync + rename only.
