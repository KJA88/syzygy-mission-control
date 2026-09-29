# Remote access and the PWA

Mission Control on the LAN remains `http://192.168.1.18:9070/`.

The phone path is:

```text
https://mission.syzygylab.net
        -> Cloudflare Access
        -> dedicated tunnel on the Pi
        -> http://127.0.0.1:9070
```

The Pi connector is `cloudflared.service`. Its published route is
`mission.syzygylab.net` to `http://127.0.0.1:9070`, with a catch-all 404.
That unit is not `cloudflared-fitbit-mcp`, `cloudflared-pi-git-mcp`,
`cloudflared-polar-h10-mcp`, `cloudflared-roarm-mcp`, or `cloudflared-tv-mcp`.
Do not attach Mission Control to those tunnels.

The dedicated Mission Control connector must remain enabled and active under
systemd. On 2026-09-28, Cloudflare error 1033 was traced to this connector not
remaining running after initial installation because an older disabled
`cloudflared.service` already existed. The current persistent unit was restored
and validated with four QUIC connections. Treat 1033 for this hostname first as
a dedicated connector-health problem, not as evidence that Mission Control on
port 9070 is down. Do not disturb the other SYZYGY tunnel processes while
repairing this connector.

## Boundaries

- Do not forward port 9070 or 9071 on the router.
- Do not publish Home Assistant (8123), the Home Assistant MCP (8095), RoArm, Vision Hub, MQTT, or other MCP ports through this hostname.
- Unauthenticated requests stop at Cloudflare Access.
- The browser does not store the Home Assistant token or a Cloudflare service-token secret.

## PWA

`main` serves a standalone manifest: name `SYZYGY Mission Control`, short name
`SYZYGY`, `start_url` and `scope` `/`, display `standalone`. The manifest link is:

```html
<link rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials" />
```

`crossorigin="use-credentials"` is required so Chrome sends the Access session cookie. Without it, the manifest fetch can receive the Access login page.

`ui/sw.js` caches only the static shell (`syzygy-shell-v1`). Non-GET requests and any `/api/` path return before `respondWith`, so the browser uses the network. The worker does not cache mission, device, arm, perception, auth, or other live state, and it does not queue writes. Offline use should show the existing network failure, not stale control state.

Android installation was live-validated after the credentialed manifest fix: Chrome exposed the installable SYZYGY PWA and the owner confirmed the app appeared on the phone. The installed app remains behind Cloudflare Access and uses the same dedicated Mission Control tunnel.
