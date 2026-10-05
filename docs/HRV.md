# Polar H10 and HRV

Current capture behavior is below. The ownership map is [Current architecture](ARCHITECTURE_CURRENT.md). This page used to describe only the unmerged RR probe. That probe is historical and is not how captures run now.

## What is live

Two Polar programs exist:

- `polar-h10-mcp.service` on `127.0.0.1:8030` is the read-only MCP (`get_polar_status`, `get_polar_heart_rate`, `get_polar_heart_rate_window`, `get_polar_rr_intervals`). It does not write the workbook.
- Workbook capture is operator-started from Mission Control through `control/polar_capture.py` and `health_workbook/polar_h10.py`. Wearing the strap does not start it.

Morning HRV is a short timed Measurement. The default is 60 seconds and the maximum is 900. It records heart rate, RMSSD, and accepted RR intervals. It does not write Daily HRV and it does not replace Fitbit overnight HRV.

Workout mode records heart rate until Stop. The safety ceiling is 10800 seconds (three hours) and uses the same finish path. Workout RR and RMSSD are not written as the morning Measurement. A start while a capture is already active returns `CAPTURE_ACTIVE`.

`CONNECT_TIMEOUT_S` stays 60 seconds.

## Wi-Fi and the arm hold

The H10 capture and the arm access point share Wi-Fi. Mission Control does not run `nmcli`. `syzygy-polar-wifi.service` listens on `/run/syzygy-polar-wifi/control.sock` and accepts only `status`, `disable`, `restore`, and `verify`. The connection is `roarm-ap` at `192.168.4.2`.

The capture writes its intent before the radio is turned off. It holds the arm as `polar-h10` with reason `POLAR_CAPTURE`. The hold is released only after `roarm-ap` is restored. If restore fails, the reason is `WIFI_NOT_RESTORED` and the hold stays. Polar Stop is not RoArm torque-off.

## Where the code is

Laptop commit `b2aed6b` (2026-10-03) records resting HRV and workout heart rate through the workbook service. That commit is not pushed and is not the Pi HEAD. The Pi checkout on 2026-10-05 was `94bd9d5` with a dirty tree. The Wi-Fi helper unit is active on the Pi and is uncommitted in git.

## Historical probe

Branch `syzygy-polar-h10-rr-probe` and `tools/polar_rr_probe.py` were an experiment. BlueZ could see the Heart Rate Service. RR conversion is `rr_ms = rr_raw * 1000.0 / 1024.0`. Live connects on that branch timed out before a validated print. Do not use that branch or that script as the capture procedure.
