"""The Pi installer must keep the checked-in Mission Control unit."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HA_ENV = "EnvironmentFile=-/home/KA_PI/syzygy-runtime/home-assistant.env"


class InstallUnitTests(unittest.TestCase):
    def test_pi_installer_copies_the_checked_in_mission_control_unit(self):
        script = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
        unit = (ROOT / "systemd" / "syzygy-mission-control.service").read_text(encoding="utf-8")
        copy_line = (
            'sudo cp "$root/systemd/syzygy-mission-control.service" '
            "/etc/systemd/system/syzygy-mission-control.service"
        )
        self.assertIn(copy_line, script)
        self.assertNotIn("sudo tee /etc/systemd/system/syzygy-mission-control.service", script)
        self.assertIn(HA_ENV, unit)
        for required in (
            "User=KA_PI",
            "WorkingDirectory=/home/KA_PI/syzygy-mission-control",
            "Environment=MC_HOST=0.0.0.0",
            "Environment=MC_PORT=9070",
            "Environment=MC_HARD_STALE_S=120",
            "ExecStart=/home/KA_PI/syzygy-mission-control/scripts/run-mission-control.sh",
            "Restart=on-failure",
            "RestartSec=5",
            "NoNewPrivileges=true",
            "ProtectSystem=strict",
            "PrivateTmp=true",
        ):
            self.assertIn(required, unit)
        pi_block = script.split('if [[ "$role" == pi ]]; then', 1)[1]
        self.assertLess(pi_block.index(copy_line), pi_block.index("sudo systemctl daemon-reload"))
