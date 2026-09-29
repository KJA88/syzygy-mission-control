# SYZYGY Mission Control

This repository is the Mission Control cockpit for SYZYGY, plus the Guardian
observer, State Engine view, and Mission Engine that live with it.

SYZYGY is the umbrella system. Mission Control is the human and API entry
point. It does not replace the subsystem that owns a device.

## Current architecture

```text
Phone / browser / operator
            |
            v
     Mission Control  (Pi, local :9070 and Access-protected remote UI)
       /          \
 Guardian        Mission Engine
 (observer)      (approved missions only)
       |              |
       +--------------+
              |
     RoArm control    Home Assistant    Vision Hub / DHRAS
     (arm commands)   (home devices)    (cameras and perception)
```

Ownership:

- Guardian aggregates health. It does not command the arm, home devices, or cameras.
- The State Engine publishes a read-only view of evidence and operational records.
- The Mission Engine coordinates one approved physical mission at a time through existing adapters.
- Vision Hub owns camera and perception acquisition.
- Home Assistant owns home integrations and device behavior. Mission Control reaches it through a narrow adapter.
- The RoArm control service owns arm command execution. Firmware, UDP, and serial transport stay in the RoArm repository.
- The browser renders Mission Control. It does not hold Home Assistant, Cloudflare, or device secrets.

Local UI: `http://192.168.1.18:9070/`

Remote UI: `https://mission.syzygylab.net` through Cloudflare Access and the dedicated Mission Control tunnel to `http://127.0.0.1:9070` on the Pi. Port 9070 is not published directly.

The Android shell is a standalone PWA named SYZYGY. See [remote access](docs/REMOTE_ACCESS.md).

## Documentation

- [Current architecture](docs/ARCHITECTURE_CURRENT.md)
- [Roadmap status](docs/ROADMAP_STATUS.md)
- [Mission Control](docs/MISSION_CONTROL.md)
- [Deployment](docs/DEPLOY.md)
- [Home Assistant](docs/HOME_ASSISTANT.md)
- [Mission Engine](docs/MISSION_ENGINE.md)
- [Remote access and PWA](docs/REMOTE_ACCESS.md)
- [Polar / HRV probe](docs/HRV.md)
- [State Engine](docs/STATE_ENGINE.md)

`docs/ADR-SYZ-MC-001.md` and `docs/VERIFY_LIVE.md` record the 2026-09-23 V0.1 baseline. They are not the current scope.

## Tests

```bash
python -m unittest discover -s tests
```

UI access checks, when Node is available:

```bash
node --test tests/test_ui_access.cjs
```

## Evidence

Repository-proven facts may be seeded into configuration. Live checks stay labeled as observed only when a run recorded them. Do not treat a branch, a requested setting, or an old snapshot as the current system.
