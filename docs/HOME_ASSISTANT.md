# Home Assistant

Home Assistant is the home-device integration layer. Mission Control and the
Home Assistant MCP talk to it through one adapter. They do not call Shelly
APIs or arbitrary Home Assistant services.

## Runtime

- Container files live under `/home/KA_PI/syzygy-runtime/home-assistant/`.
- The process environment file is `/home/KA_PI/syzygy-runtime/home-assistant.env`. Do not print or commit it. The token is not stored in this repository.
- Home Assistant listens on port 8123 on the Pi. Do not publish 8123 through Cloudflare or a router forward.
- `syzygy-home-assistant-mcp.service` binds `127.0.0.1:8095` only.

Entity discovery is dynamic. The adapter lists entities Home Assistant reports. Friendly names are display text. Policy is the entity id. A renamed Shelly does not require a repository edit. A new `switch.*` id does.

## Write policy

`config/services.yaml` sets:

- read domains: sensor, binary_sensor, switch, light
- write domains: switch and light, intersected with `{switch, light}`
- `write_allow`: an exact entity-id list

An entity is writable only when its domain is writable and its id is on that list. Switch and light capabilities are `turn_on` and `turn_off` only when writable. Brightness is only for an allowed light that supports it.

The only writable switches in the current allowlist are:

- `switch.1_plug_shelly`
- `switch.shellyplugusg4_acebe6f74038`
- `switch.shellyplugusg4_acebe6f75888`
- `switch.shellyplugusg4_58e6c537a850`

Locks, alarms, covers, climate, water heaters, humidifiers, scripts, automations, scenes, vacuums, cameras, and media players stay rejected even if someone adds them to the allowlist.

## MCP tools

Protocol `2024-11-05`, server `syzygy-home-assistant`. Tools:

- `list_entities`
- `get_entity`
- `get_sensor`
- `turn_on`
- `turn_off`
- `set_brightness`

`call_service` returns `DOMAIN_REJECTED`. Guardian's routine probe does not call these tools. A Home Assistant failure must not drop cameras, the arm, or system health. Home Assistant is optional in the Guardian rollup.
