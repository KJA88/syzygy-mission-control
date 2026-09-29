# Polar H10 and HRV

This work is experimental. It is not merged, and it is not a live Mission
Control feature.

## What is true now

- The Pi Bluetooth adapter and BlueZ can see the Polar H10 and the standard Heart Rate Service.
- `tools/polar_rr_probe.py` on branch `syzygy-polar-h10-rr-probe` parses Heart Rate Measurement notifications, including every RR interval in a packet.
- RR units convert with `rr_ms = rr_raw * 1000.0 / 1024.0`. A raw value of 960 is 937.5 ms.
- That branch constructs `BleakClient` from the discovered device with an explicit 20-second timeout and retries discovery once after a timeout.
- Live connect attempts timed out before a successful heart-rate and RR print was recorded. Do not treat the probe as validated.

There is no HRV calculation, artifact filter, RMSSD, baseline, local database, Mission Control panel, systemd HRV service, or MCP integration on `main` or in production.

The existing `polar-h10-mcp` service is a separate optional health MCP. It is not this probe and it is not an HRV product.

## Intended shape after a successful probe

A later design, not built yet:

```text
Polar H10 -> dedicated local HRV service -> private local database -> Mission Control read/start API
```

Health measurements stay on the runtime host. They do not go into Git. That service does not exist yet.
