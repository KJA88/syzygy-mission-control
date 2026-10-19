# Pi TV MCP deployment source

This is the deployment source for the separate `/home/KA_PI/tv-mcp/server.py`
service, preserved from the working Pi on 2026-10-19. Mission Control calls
its existing loopback MCP endpoint on port 8065. Updating this repository does
not replace or restart the separate service automatically.

## Verified input selection

- HDMI 2: Pi screen, selected with CEC active-source `2.0.0.0` on `/dev/cec1`.
- HDMI 3: cable box. First select HDMI 2, wait two seconds, then send
  `KEY_SOURCE`, `KEY_DOWN`, `KEY_ENTER` over one Samsung remote connection.
  This route was visually confirmed twice, including through the running MCP.
- HDMI 1: soundbar using ARC/eARC, not a picture-selection button.
- Live TV switching is uncalibrated and hidden. The resync selector may still
  record Live TV after manual remote use. Uncalibrated routes return an error.

`KEY_HDMI` did not visibly advance inputs. Never infer a key-cycle position
from `last_input.json`: it records a requested state, not TV feedback.
The TV_BUSY lock and existing tool names remain in place. Success means the
commands were sent; it is not independent confirmation of the displayed input.

## Runtime and rollout

The existing Pi environment has `samsungtvws` 3.0.6, `mcp`, and `cec-ctl`.
Keep the existing virtual environment and systemd unit. Tokens remain outside
Git at `/home/KA_PI/.samsung-tv-token`; no token or input-state file is included.

Back up the live server before copying this file into `/home/KA_PI/tv-mcp/`.
Check syntax with that service's virtual environment. K JA runs all sudo,
including `sudo systemctl restart tv-mcp`; do not change sudoers.
Rollback by restoring the saved server and having K JA restart the service.
No robot control or sound-output settings are changed by this fix.

## Validation

From the repository root:

```sh
python -m unittest discover -s tests -p 'test_tv*.py'
```

The source-menu unit checks mock CEC and remote keys and do not operate a TV.
Live checks require an observer: HDMI 3 -> HDMI 2 -> HDMI 3. Recheck menu layout
after changing connected devices or TV firmware.
