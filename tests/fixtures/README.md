# Deterministic Guardian Fixtures

Implemented fixture cases for reducer/probe tests. No hardware calls.

1. `01_all_healthy` — all required evidence fresh/pass -> green
2. `02_guardian_heartbeat_stale` — hard-stale Guardian -> unknown
3. `03_pi_unavailable` — required Pi unavailable -> red/unknown per probe evidence class
4. `04_jetson_unavailable` — required Jetson unavailable -> red/unknown per probe evidence class
5. `05_service_inactive` — required service inactive -> red
6. `06_port_closed` — required local port closed -> red
7. `07_mcp_malformed` — MCP response malformed -> red
8. `08_catalog_stale` — required service usable but catalog stale -> yellow
9. `09_local_ok_public_broken` — preserve local pass/public fail; required public layer decides red vs optional yellow
10. `10_public_ok_auth_broken` — public service reachable, auth/client invoke broken
11. `11_timeout` — timeout -> fail with `TOOL_TIMEOUT`
12. `12_dependency_outage` — dependency unavailable -> classified degradation/failure
13. `13_conflicting_probes` — unresolved contradictory evidence -> yellow + `CONFLICTING_PROBES`
14. `14_probe_crash` — Guardian probe crash -> overall unknown
15. `15_recovery` — failed object returns to pass and emits `RECOVERED`/status-change event

Each fixture directory contains `input.json` facts and `expected.json` with reduced
status/class, expected system status, and visible layer names. `tests/test_backend.py`
executes all 15 as subtests. Additional tests cover freshness, auth-off behavior,
per-camera health, session/catalog handling, durable writes, and real loopback SSE.
