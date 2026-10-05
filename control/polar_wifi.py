"""Root helper for the four Polar capture Wi-Fi actions.

Mission Control keeps NoNewPrivileges and talks to this process over a local
socket. The client sends one fixed action word. Commands are chosen here.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import time

ACTIONS = ("status", "disable", "restore", "verify")
CONNECTION = "roarm-ap"
ADDRESS = "192.168.4.2"
SOCKET_PATH = "/run/syzygy-polar-wifi/control.sock"
ERRORS = ("rejected", "disable_failed", "restore_failed", "verify_failed", "status_failed")

RADIO = ("nmcli", "radio", "wifi")
DEVICE = ("nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status")
ADDR = ("ip", "-br", "addr", "show", "dev", "wlan0")
RADIO_OFF = ("nmcli", "radio", "wifi", "off")
RADIO_ON = ("nmcli", "radio", "wifi", "on")
CONNECTION_UP = ("nmcli", "connection", "up", "roarm-ap")
ALLOWED = {
    RADIO: 15,
    DEVICE: 15,
    ADDR: 10,
    RADIO_OFF: 30,
    RADIO_ON: 30,
    CONNECTION_UP: 40,
}


def run_allowed(args, timeout=None):
    if args not in ALLOWED:
        raise RuntimeError("rejected")
    completed = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=ALLOWED[args] if timeout is None else timeout,
        check=False,
    )
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _name(value):
    text = str(value or "")
    if len(text) > 64 or any(ord(char) < 32 for char in text):
        return ""
    return text


def _address(value):
    text = str(value or "").strip()
    if len(text) > 80 or any(ord(char) < 32 for char in text):
        return ""
    return text


def parse_observation(radio_out, device_out, addr_out):
    lines = [line.strip() for line in str(radio_out).splitlines() if line.strip()]
    radio = lines[-1] if lines else "unknown"
    if radio not in ("enabled", "disabled"):
        radio = "unknown"
    connection = ""
    for line in str(device_out).splitlines():
        parts = line.split(":")
        if len(parts) >= 4 and parts[0] == "wlan0" and parts[1] == "wifi" and parts[2] == "connected":
            connection = parts[3]
    return {
        "radio": radio,
        "connection": _name(connection),
        "address": _address(addr_out),
    }


def _verified(observed):
    return (
        observed.get("radio") == "enabled"
        and observed.get("connection") == CONNECTION
        and ADDRESS in str(observed.get("address") or "")
    )


class PolarWifiActions:
    def __init__(self, run=run_allowed, restore_wait_s=20, sleep=time.sleep):
        self._run = run
        self._restore_wait_s = restore_wait_s
        self._sleep = sleep

    def handle(self, action):
        if action not in ACTIONS:
            return {"ok": False, "error": "rejected"}
        if action == "disable":
            return self._disable()
        if action == "restore":
            return self._restore()
        observed, error = self._observe()
        if error:
            return {"ok": False, "error": error}
        if action == "verify" and not _verified(observed):
            return {"ok": False, "error": "verify_failed", **observed}
        return {"ok": True, **observed}

    def _observe(self):
        code, radio_out, _err = self._run(RADIO)
        device_code, device_out, _device_err = self._run(DEVICE)
        addr_code, addr_out, _addr_err = self._run(ADDR)
        if code != 0 or device_code != 0 or addr_code != 0:
            return None, "status_failed"
        return parse_observation(radio_out, device_out, addr_out), None

    def _disable(self):
        code, _out, _err = self._run(RADIO_OFF)
        if code != 0:
            return {"ok": False, "error": "disable_failed"}
        return {"ok": True}

    def _restore(self):
        code, _out, err = self._run(RADIO_ON)
        if code != 0:
            _log_restore("radio_on", code, err)
            return {"ok": False, "error": "restore_failed"}
        # wlan0 stays unavailable for a moment after the radio returns.
        deadline = time.monotonic() + self._restore_wait_s
        while True:
            up_code, _up_out, up_err = self._run(CONNECTION_UP)
            if up_code != 0:
                _log_restore("connection_up", up_code, up_err)
            else:
                observed, error = self._observe()
                if error is None and _verified(observed):
                    return {"ok": True}
                _log_restore("verify", up_code, error or "not_ready")
            if time.monotonic() >= deadline:
                return {"ok": False, "error": "restore_failed"}
            self._sleep(1)


def _log_restore(step, code, detail):
    text = " ".join(str(detail or "").split())
    lowered = text.lower()
    if "psk" in lowered or "password" in lowered or "secret" in lowered:
        text = "redacted"
    sys.stderr.write("polar-wifi restore %s exit %s %s\n" % (step, code, text[:180]))


def public_result(result):
    if not isinstance(result, dict):
        return {"ok": False, "error": "rejected"}
    ok = result.get("ok") is True
    body = {"ok": ok}
    if not ok:
        error = result.get("error")
        body["error"] = error if error in ERRORS else "rejected"
        return body
    if "radio" in result:
        radio = result.get("radio")
        body["radio"] = radio if radio in ("enabled", "disabled", "unknown") else "unknown"
        body["connection"] = _name(result.get("connection"))
        body["address"] = _address(result.get("address"))
    return body


def accept_line(raw, peer_uid, allowed_uid, actions):
    if peer_uid != allowed_uid:
        return {"ok": False, "error": "rejected"}
    if not isinstance(raw, (bytes, bytearray)):
        return {"ok": False, "error": "rejected"}
    if len(raw) > 32 or raw.count(b"\n") != 1 or not raw.endswith(b"\n"):
        return {"ok": False, "error": "rejected"}
    try:
        action = raw[:-1].decode("ascii")
    except UnicodeError:
        return {"ok": False, "error": "rejected"}
    if action not in ACTIONS:
        return {"ok": False, "error": "rejected"}
    return public_result(actions.handle(action))


def request_action(action, path=SOCKET_PATH, timeout=50):
    if action not in ACTIONS:
        raise RuntimeError("rejected")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(path)
        client.sendall((action + "\n").encode("ascii"))
        data = b""
        while b"\n" not in data and len(data) < 1024:
            chunk = client.recv(256)
            if not chunk:
                break
            data += chunk
    finally:
        client.close()
    if b"\n" not in data:
        raise RuntimeError("status_failed")
    payload = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("status_failed")
    return payload


def _peer_uid(conn):
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


def _reply(conn, result):
    conn.sendall((json.dumps(public_result(result), separators=(",", ":")) + "\n").encode("utf-8"))


def serve(path, allowed_uid, allowed_gid, actions):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    os.chown(directory, 0, allowed_gid)
    os.chmod(directory, 0o750)
    if os.path.exists(path):
        os.unlink(path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    os.chown(path, 0, allowed_gid)
    os.chmod(path, 0o660)
    server.listen(4)
    sys.stderr.write("polar-wifi listening\n")
    while True:
        conn, _addr = server.accept()
        try:
            conn.settimeout(5)
            peer = _peer_uid(conn)
            raw = b""
            while b"\n" not in raw and len(raw) <= 32:
                chunk = conn.recv(32)
                if not chunk:
                    break
                raw += chunk
            result = accept_line(raw, peer, allowed_uid, actions)
            if result.get("error") == "rejected":
                sys.stderr.write("polar-wifi rejected\n")
            else:
                sys.stderr.write("polar-wifi %s\n" % ("ok" if result.get("ok") else result.get("error")))
            _reply(conn, result)
        except Exception:
            sys.stderr.write("polar-wifi rejected\n")
            try:
                _reply(conn, {"ok": False, "error": "rejected"})
            except Exception:
                pass
        finally:
            conn.close()


def main():
    import pwd
    user = os.environ.get("POLAR_WIFI_USER", "KA_PI")
    path = os.environ.get("POLAR_WIFI_SOCKET", SOCKET_PATH)
    record = pwd.getpwnam(user)
    serve(path, record.pw_uid, record.pw_gid, PolarWifiActions())


if __name__ == "__main__":
    main()
