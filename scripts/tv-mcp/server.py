#!/usr/bin/env python3
"""SYZYGY TV MCP - Samsung control from Pi (LAN only: Wake-on-LAN + samsungtvws).

UN50U8000FFXZA (calibrated with K JA 2026-09-21/22):
  Physical: HDMI1=soundbar  HDMI2=Pi dashboard  HDMI3=cable (K JA 2026-10-19)
  Inputs: CEC HDMI2 anchor; cable = SOURCE, DOWN, ENTER (visually verified).
  Power on: Wake-on-LAN magic packet. Power off: KEY_POWER over LAN.
  SmartThings cloud path removed 2026-10-18 (API 401 / paywalled).
  KEY_HDMI1/KEY_HDMI2 unreliable; KEY_TV opens Home (do not use).

  TV 192.168.1.221  MAC B8A0B834F2C3
  LAN token: /home/KA_PI/.samsung-tv-token
  State:     /home/KA_PI/tv-mcp/last_input.json
"""
from __future__ import annotations

import functools
import json
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from samsungtvws import SamsungTVWS

TV_HOST = "192.168.1.221"
TV_MAC = "B8A0B834F2C3"
TOKEN = Path("/home/KA_PI/.samsung-tv-token")
STATE_PATH = Path("/home/KA_PI/tv-mcp/last_input.json")
DASH_URL = "http://192.168.1.17:8080/"

# KEY_HDMI cycle order (LAN input switching).
CYCLE = ("livetv", "soundbar", "dashboard", "cable")
INDEX = {name: i for i, name in enumerate(CYCLE)}

# One key-pressing operation at a time so the stateful KEY_HDMI cycle
# cannot interleave (voice + Mission Control at once). Non-blocking:
# a second request returns TV_BUSY instead of queueing extra presses.
KEY_LOCK = threading.Lock()


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not KEY_LOCK.acquire(blocking=False):
            return {"ok": False, "error": "TV_BUSY", "tv": TV_HOST}
        try:
            return fn(*args, **kwargs)
        finally:
            KEY_LOCK.release()
    return wrapper

security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
        "tv.syzygylab.net",
        "tv.syzygylab.net:*",
    ],
    allowed_origins=[
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
        "https://tv.syzygylab.net",
    ],
)

mcp = FastMCP(
    "tv-mcp",
    host="127.0.0.1",
    port=8065,
    streamable_http_path="/mcp",
    transport_security=security,
)


WOL_TARGETS = ("192.168.1.255", "255.255.255.255")


def _wol(repeats: int = 3) -> None:
    """Send Wake-on-LAN magic packets to TV_MAC (UDP port 9, LAN broadcast)."""
    mac = bytes.fromhex(TV_MAC)
    pkt = b"\xff" * 6 + mac * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for i in range(repeats):
            for addr in WOL_TARGETS:
                try:
                    s.sendto(pkt, (addr, 9))
                except OSError:
                    pass
            if i + 1 < repeats:
                time.sleep(0.1)


def _power_state():
    try:
        with urllib.request.urlopen(f"http://{TV_HOST}:8001/api/v2/", timeout=3) as r:
            return json.load(r).get("device", {}).get("PowerState")
    except Exception:
        return None


def _wait_on(timeout_s: float = 75.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        _wol()
        st = _power_state()
        if st == "on":
            return True
        time.sleep(3)
    return False


def _tv() -> SamsungTVWS:
    if not TOKEN.exists():
        raise FileNotFoundError(f"missing TV token file: {TOKEN}")
    return SamsungTVWS(host=TV_HOST, port=8002, token_file=str(TOKEN))


def _send_key(key: str, settle_s: float = 1.6) -> None:
    _tv().send_key(key)
    time.sleep(settle_s)


def _ensure_on() -> dict | None:
    """Wake TV if needed: Wake-on-LAN magic packets, then wait for the LAN API.

    _wait_on keeps re-sending WoL and polls PowerState for up to 60 s.
    """
    lan = _power_state()
    if lan == "on":
        return {"ok": True, "woke": False, "PowerState": lan}

    if lan == "standby":
        # Wi-Fi TV in network standby ignores WoL but accepts LAN KEY_POWER.
        try:
            _send_key("KEY_POWER", settle_s=0.5)
        except Exception:
            pass
    _wol()
    if not _wait_on(timeout_s=40.0):
        return {
            "ok": False,
            "error": "TV_DID_NOT_WAKE",
            "method": "wol_lan_wait",
            "PowerState": _power_state(),
        }
    # Let the TV finish booting before input keys.
    time.sleep(8.0)
    return {"ok": True, "woke": True, "method": "standby_key_or_wol", "PowerState": _power_state()}


def _load_input() -> str:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        cur = data.get("input")
        if cur in INDEX:
            return cur
    except Exception:
        pass
    return "dashboard"


def _save_input(name: str) -> None:
    STATE_PATH.write_text(
        json.dumps({"input": name, "updated_unix": int(time.time())}, indent=2) + "\n",
        encoding="utf-8",
    )


def _steps(current: str, target: str) -> int:
    return (INDEX[target] - INDEX[current]) % len(CYCLE)


def _goto_keys(target: str) -> dict:
    # Never use remembered input as evidence of the TV's actual input.
    # Only observed menu routes are allowed. KEY_HDMI did not change inputs.
    current = "dashboard"
    routes = {"dashboard": (), "cable": ("KEY_SOURCE", "KEY_DOWN", "KEY_ENTER")}
    if target not in routes:
        return {
            "ok": False, "error": "INPUT_ROUTE_NOT_CALIBRATED",
            "from": "unknown", "to": target,
            "method": "cec_anchor_source_menu", "key_hdmi_presses": 0,
            "cycle": "->".join(CYCLE), "tv": TV_HOST,
        }
    try:
        for args in (
            ["--to", "0", "--active-source", "phys-addr=2.0.0.0"],
        ):
            sent = subprocess.run(
                ["/usr/bin/cec-ctl", "-d1", *args],
                capture_output=True, text=True, timeout=10, check=True,
            )
            # cec-ctl may report a failed transmit without a failing exit code.
            report = (sent.stdout + sent.stderr).lower()
            if any(word in report for word in ("failed", "timed out", "not acknowledged", "nack", "error")):
                raise RuntimeError("CEC transmission failed")
        time.sleep(2.0)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return {
            "ok": False, "error": "CEC_ANCHOR_FAILED",
            "detail": type(exc).__name__, "from": "unknown", "to": target,
            "method": "cec_anchor_source_menu", "key_hdmi_presses": 0,
            "cycle": "->".join(CYCLE), "tv": TV_HOST,
        }
    # CEC transmission is not independent confirmation of the displayed input.
    _save_input("dashboard")
    try:
        if routes[target]:
            # Keep one connection and complete the menu route before timeout.
            with _tv() as remote:
                remote.timeout = 5
                for key in routes[target]:
                    remote.send_key(key)
                    if key == "KEY_SOURCE":
                        time.sleep(0.5)
    except Exception as exc:
        STATE_PATH.unlink(missing_ok=True)
        return {
            "ok": False, "error": "INPUT_KEY_FAILED",
            "detail": type(exc).__name__, "from": current, "to": target,
            "method": "cec_anchor_source_menu", "key_hdmi_presses": 0,
            "cycle": "->".join(CYCLE), "tv": TV_HOST,
        }
    _save_input(target)
    return {
        "ok": True,
        "method": "cec_anchor_source_menu",
        "from": current,
        "to": target,
        "key_hdmi_presses": 0,
        "menu_keys": list(routes[target]),
        "cycle": "->".join(CYCLE),
        "tv": TV_HOST,
    }


def _goto(
    target: str,
    *,
    allow_key_fallback: bool = False,
    woke_from_off: bool = False,
) -> dict:
    """Anchor on HDMI2 with CEC, then use a visually calibrated Source menu route.

    allow_key_fallback / woke_from_off are kept for call compatibility; the
    SmartThings absolute-input path was removed 2026-10-18.
    """
    return _goto_keys(target)


@mcp.tool()
@_locked
def tv_dashboard() -> dict:
    """Wake Samsung TV and switch to HDMI2 (Pi Chromium kiosk = SYZYGY dashboard)."""
    wake = _ensure_on()
    if wake is not None and wake.get("ok") is False:
        wake.update({"input": "HDMI2", "target": "pi_kiosk_dashboard"})
        return wake
    woke = bool(wake and wake.get("woke"))
    result = _goto("dashboard", allow_key_fallback=False, woke_from_off=woke)
    if woke:
        result["woke_from_off"] = True
    result.update(
        {
            "input": "HDMI2",
            "target": "pi_kiosk_dashboard",
            "dashboard_url": DASH_URL,
        }
    )
    return result


@mcp.tool()
@_locked
def tv_cable() -> dict:
    """Wake Samsung TV and switch to HDMI3 (cable box)."""
    wake = _ensure_on()
    if wake is not None and wake.get("ok") is False:
        wake.update({"input": "HDMI3", "target": "cable_box"})
        return wake
    woke = bool(wake and wake.get("woke"))
    result = _goto("cable", allow_key_fallback=False, woke_from_off=woke)
    if woke:
        result["woke_from_off"] = True
    result.update({"input": "HDMI3", "target": "cable_box"})
    return result


@mcp.tool()
@_locked
def tv_set_input_state(input_name: str) -> dict:
    """Tell the Pi what input the TV is actually on (resync after manual remote use).

    input_name: livetv | cable | dashboard | soundbar
    """
    name = (input_name or "").strip().lower()
    if name not in INDEX:
        return {"ok": False, "error": "INVALID_INPUT", "allowed": list(CYCLE)}
    _save_input(name)
    return {"ok": True, "input": name, "note": "state only; no keys sent"}



PAGE_FILE = Path("/home/KA_PI/kiosk/page.txt")
ALLOWED_WALL_PAGES = ("cameras", "status")


def _set_wall_page(page: str) -> dict:
    """Flip the Pi Chromium kiosk page (cameras | status). Does not change TV HDMI."""
    name = (page or "").strip().lower()
    if name not in ALLOWED_WALL_PAGES:
        return {
            "ok": False,
            "error": "INVALID_WALL_PAGE",
            "allowed": list(ALLOWED_WALL_PAGES),
        }
    try:
        PAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PAGE_FILE.write_text(name + chr(10), encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"WRITE_FAILED:{type(e).__name__}", "detail": str(e)}
    return {
        "ok": True,
        "wall_page": name,
        "file": str(PAGE_FILE),
        "note": "kiosk polls page.txt within a couple seconds",
    }


@mcp.tool()
def tv_wall_cameras() -> dict:
    """Show the live camera wall page on the Pi kiosk (Back Yard | Indoor)."""
    return _set_wall_page("cameras")


@mcp.tool()
def tv_wall_status() -> dict:
    """Show the SYSTEM status page on the Pi kiosk (camera online/offline cards)."""
    return _set_wall_page("status")


@mcp.tool()
def tv_wall_page(page: str) -> dict:
    """Set the Pi kiosk wall page.

    page: cameras | status
    """
    return _set_wall_page(page)


@mcp.tool()
@_locked
def tv_off() -> dict:
    """Turn the Samsung TV off over the home network (KEY_POWER via samsungtvws).

    If the TV's LAN API is not answering "on", the TV is already off or in
    standby, so this returns ok with note already_off_or_standby (no key sent).
    """
    if not TOKEN.exists():
        return {"ok": False, "error": f"missing token {TOKEN}"}

    lan = _power_state()
    if lan != "on":
        out = {
            "ok": True,
            "note": "already_off_or_standby",
            "PowerState": lan,
            "tv": TV_HOST,
            "remembered_input": _load_input(),
        }
        if lan == "standby":
            out["hint"] = "TV reports standby; if the screen is still lit, use the remote"
        return out
    try:
        _tv().send_key("KEY_POWER")
        return {
            "ok": True,
            "key": "KEY_POWER",
            "method": "lan_key_power",
            "PowerState_before": lan,
            "tv": TV_HOST,
            "remembered_input": _load_input(),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"KEY_POWER_FAILED:{type(e).__name__}",
            "PowerState": lan,
            "tv": TV_HOST,
        }


@mcp.tool()
@_locked
def tv_on() -> dict:
    """Turn the Samsung TV on without changing input.

    From network standby sends LAN KEY_POWER; Wake-on-LAN is the backup.
    """
    wake = _ensure_on() or {"ok": True, "woke": False}
    wake.setdefault("tv", TV_HOST)
    wake["remembered_input"] = _load_input()
    return wake


@mcp.tool()
@_locked
def tv_input(input_name: str) -> dict:
    """Wake the TV, anchor on HDMI2 with CEC, then use a calibrated Source route.

    input_name: livetv | cable | dashboard | soundbar
    """
    name = (input_name or "").strip().lower()
    if name not in INDEX:
        return {"ok": False, "error": "INVALID_INPUT", "allowed": list(CYCLE)}
    wake = _ensure_on()
    if wake is not None and wake.get("ok") is False:
        wake.update({"target": name, "tv": TV_HOST})
        return wake
    woke = bool(wake and wake.get("woke"))
    result = _goto(name, allow_key_fallback=False, woke_from_off=woke)
    if woke:
        result["woke_from_off"] = True
    return result


@mcp.tool()
def tv_status() -> dict:
    """Read-only TV status: LAN PowerState (port 8001) plus the remembered input.

    Sends no keys and does not wake the TV.
    """
    return {
        "ok": True,
        "PowerState": _power_state(),
        "remembered_input": _load_input(),
        "cycle": list(CYCLE),
        "busy": KEY_LOCK.locked(),
        "tv": TV_HOST,
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
