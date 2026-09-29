# Current SYZYGY architecture

Status date: 2026-09-28. This page is the ownership map for the system that is
in `main` and checked out on the Pi. Experimental work that is not merged is
called out as not live.

```text
Phone / Browser / AI / Future Missions
            |
            v
       Mission Control
       /      |       \
 Guardian   Mission   UI/API
(observer)  Engine
              |
     +--------+---------+
     |        |         |
   RoArm     HA       Vision Hub
 control   adapter     / DHRAS
 service     |            |
     |    Home Assistant  Cameras/YOLO
   RoArm       |
            devices
```

## Who owns what

| Piece | Owns | Does not own |
|---|---|---|
| Mission Control | Cockpit, HTTP API, presentation, and the entry point for approved actions | Direct Shelly calls, raw Home Assistant services, RoArm firmware or UDP, RTSP, or camera credentials |
| Guardian | Health snapshots, heartbeat freshness, and read-only probes | Side-effecting TV, home, or robot commands |
| State Engine | Read-only assertions over Guardian evidence and the operational record | Motion, device writes, or mission starts |
| Mission Engine | One active physical mission, checks, and stop/cleanup through adapters | A second concurrent physical mission, or unattended starts from perception |
| Vision Hub / DHRAS | Camera acquisition, perception events, and vision health | Home devices or the arm |
| Home Assistant | Home entities and their device behavior | Mission policy. A friendly name change does not change the allowlist |
| RoArm control service | Named arm skills and command execution | The transport implementation, which remains in the RoArm repository |

STOP in Mission Control or the Mission Engine is an operational stop. It is not RoArm torque-off.

## Paths that are live

- LAN Mission Control: `http://192.168.1.18:9070/` on the Pi (`192.168.1.18`).
- Remote Mission Control: `https://mission.syzygylab.net` to Cloudflare Access, then the Pi's dedicated tunnel, then `http://127.0.0.1:9070`.
- Jetson (`192.168.1.17`) runs the Jetson agent and Vision Hub. It is not the Mission Control origin.
- Home Assistant MCP: `127.0.0.1:8095` on the Pi only.
- Home Assistant itself listens on port 8123 on the Pi and is not on the Mission Control tunnel.

## Not live yet

The Polar H10 RR probe is unmerged. It is not a Mission Control panel, database, or MCP tool. See [HRV](HRV.md).
