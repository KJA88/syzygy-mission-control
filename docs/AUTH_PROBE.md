# Guardian Auth Probe (V0.2)

V0.2 adds a dedicated, read-only Guardian identity probe for public MCP routes protected by Cloudflare Access.

## Safety contract

- credentials are never stored in the repository
- Guardian reads them from `/etc/syzygy/guardian-auth.env`
- the file is optional; with no identity configured auth remains `UNKNOWN / AUTH_PROBE_OFF`
- the probe performs only MCP `initialize`, `notifications/initialized`, and `tools/list`
- it never performs `tools/call`
- returned snapshots contain status/metadata only, never credential values
- auth remains non-required until `auth_required: true` is deliberately set for a service

## Environment file

Create on the Pi only after a dedicated Cloudflare Access service token exists:

```bash
sudo install -d -m 0755 /etc/syzygy
sudoedit /etc/syzygy/guardian-auth.env
```

File format:

```text
SYZYGY_AUTH_PROBE_MODE=cloudflare_access
SYZYGY_CF_ACCESS_CLIENT_ID=<dedicated Guardian service-token client id>
SYZYGY_CF_ACCESS_CLIENT_SECRET=<dedicated Guardian service-token secret>
```

Then lock it down:

```bash
sudo chown root:root /etc/syzygy/guardian-auth.env
sudo chmod 600 /etc/syzygy/guardian-auth.env
```

Do not paste the secret into chat, shell history, Git, screenshots, or Mission Control.

## Per-service enablement

A service must explicitly set:

```yaml
auth_probe: guardian_identity
```

`auth_probe: off` prevents any authenticated network probe even if credentials exist.

Keep `auth_required: false` during rollout. Once an authenticated route has been live-verified and is part of the product contract, requiredness can be reviewed separately.

## Deploy

From the Pi checkout:

```bash
cd ~/syzygy-mission-control
git pull --ff-only
.venv/bin/python -m unittest discover -s tests -v
bash scripts/install.sh pi
```

The installer writes the Guardian unit with:

```text
EnvironmentFile=-/etc/syzygy/guardian-auth.env
```

so absence of the file is safe and leaves the probe off.

## Status meaning

- `GREEN`: the dedicated identity completed the authenticated MCP handshake/catalog probe
- `RED / OAUTH_EXPIRED`: the dedicated identity received HTTP 401/403
- `UNKNOWN / AUTH_PROBE_OFF`: probe disabled, unsupported mode, or identity missing
- other RED/UNKNOWN classes retain their existing network/protocol meaning

The current snapshot field is `client_invoke` for schema-v1 compatibility, but V0.2 records `probe_scope: authenticated_mcp_handshake`; a GREEN auth probe does **not** claim that an application tool was invoked.
