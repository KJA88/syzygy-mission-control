"""Operator-started Polar H10 captures with durable Wi-Fi recovery.

Morning HRV and workout recording are separate. Wearing the strap starts neither.

The arm hold is generic and lives on SkillOwner. Wi-Fi and the collector
stay here. Intent is written before the radio is disabled so a crash or
restart can restore roarm-ap before the arm is released.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from guardian.storage import atomic_json

HOLDER = "polar-h10"
HOLD_REASON = "POLAR_CAPTURE"
RESTORE_REASON = "WIFI_NOT_RESTORED"
DEFAULT_SECONDS = 60
MORNING_MAX_SECONDS = 900
WORKOUT_SAFETY_SECONDS = 3 * 60 * 60
MORNING = "morning_hrv"
WORKOUT = "workout"
MODES = {MORNING: "resting", WORKOUT: "workout"}
READY = (
    "Ready. Morning HRV and workout recording start only from these controls. "
    "Wearing the strap does not start either."
)


def _stamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _mode(value):
    if value is None:
        return MORNING, None
    if not isinstance(value, str) or value not in MODES:
        return None, "MALFORMED_PARAMETERS"
    return value, None


def _seconds(value, default, limit):
    if value is None:
        return default, None
    if isinstance(value, bool) or not isinstance(value, int):
        return None, "MALFORMED_PARAMETERS"
    if value < 1 or value > limit:
        return None, "MALFORMED_PARAMETERS"
    return value, None


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unavailable"
    return str(round(float(value), 2))


def collector_command(root, mode, seconds):
    return [
        "bash",
        str(Path(root) / "scripts" / "run-polar-h10.sh"),
        MODES[mode],
        str(int(seconds)),
    ]


def _public_result(payload):
    if not isinstance(payload, dict):
        return None
    kept = {}
    for key in (
        "found",
        "hr_bpm",
        "hrv_rmssd_ms",
        "rr_count",
        "rr_rejected",
        "seconds",
        "measurements_appended",
        "training_appended",
        "mode",
        "reconnects",
        "errors",
    ):
        if key in payload:
            kept[key] = payload[key]
    return kept or None


def parse_collector_output(text):
    if not text:
        return None
    for line in reversed(str(text).splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        parsed = _public_result(payload)
        if parsed is not None:
            return parsed
    return None


def _run(args, timeout=20):
    completed = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def ethernet_up(host, runner=_run):
    code, addr, _err = runner(["ip", "-br", "addr", "show", "dev", "eth0"])
    route_code, route, _route_err = runner(["ip", "route", "show", "default"])
    if code != 0 or route_code != 0:
        return False
    return str(host) in addr and "dev eth0" in route


class NmcliRadio:
    """Wi-Fi through the root helper. The capture sequence is unchanged."""

    def __init__(self, client=None):
        if client is None:
            from control.polar_wifi import request_action
            client = request_action
        self._client = client

    def available(self):
        try:
            result = self._client("status")
        except Exception:
            return False
        return isinstance(result, dict) and result.get("ok") is True

    def snapshot(self):
        result = self._client("status")
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("wifi status failed")
        return {
            "radio": result.get("radio") or "unknown",
            "connection": result.get("connection") or "",
            "address": result.get("address") or "",
        }

    def disable(self):
        result = self._client("disable")
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("wifi off failed")

    def restore(self, connection):
        if connection != "roarm-ap":
            return
        self._client("restore")


class ProcessHandle:
    def __init__(self, proc):
        self.proc = proc
        self._output = ""
        self._lock = threading.Lock()

    @property
    def pid(self):
        return self.proc.pid

    def wait(self, timeout):
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        self._drain()
        return code

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        self._drain()

    def terminate(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.kill()
        self._drain()

    def output(self):
        with self._lock:
            return self._output

    def _drain(self):
        stream = self.proc.stdout
        if stream is None:
            return
        with self._lock:
            try:
                extra = stream.read()
            except Exception:
                return
            if extra:
                self._output += extra


def live_runner(root):
    def start(mode, seconds):
        proc = subprocess.Popen(
            collector_command(root, mode, seconds),
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return ProcessHandle(proc)

    return start


def _cmdline(pid):
    path = Path("/proc") / str(pid) / "cmdline"
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace")


def _kill_collector_pid(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return
    command = _cmdline(pid)
    if "run-polar-h10" not in command and "health_workbook.polar_h10" not in command:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return


def _wifi_ready(snap, connection, address):
    if not isinstance(snap, dict):
        return False
    return (
        snap.get("radio") == "enabled"
        and snap.get("connection") == connection
        and address in str(snap.get("address") or "")
    )


def _result_message(outcome, last, connection, address, mode):
    if outcome == "failed_restore":
        return (
            "Arm unavailable. Wi-Fi did not return to "
            + connection
            + " at "
            + address
            + ". Restore that connection, then use Stop to try again."
        )
    rr = 0
    hr = "unavailable"
    seconds = "unavailable"
    if isinstance(last, dict):
        count = last.get("rr_count")
        if isinstance(count, int) and not isinstance(count, bool):
            rr = count
        hr = _num(last.get("hr_bpm"))
        seconds = _num(last.get("seconds"))
    tail = "Wi-Fi restored to " + connection + " at " + address + ". The arm is available."
    if mode == WORKOUT and outcome == "completed":
        return (
            "Workout recording finished. Heart rate "
            + hr
            + " bpm, duration "
            + seconds
            + " s. Saved to Training as POLAR_H10. "
            + tail
        )
    if mode == WORKOUT and outcome == "cancelled":
        return "Workout recording stopped before a Training row was written. " + tail
    if outcome == "completed" and rr > 0:
        hrv = _num(last.get("hrv_rmssd_ms")) if isinstance(last, dict) else "unavailable"
        return (
            "Morning HRV finished. Heart rate "
            + hr
            + " bpm, "
            + str(rr)
            + " RR samples, HRV "
            + hrv
            + " ms, duration "
            + seconds
            + " s. "
            + tail
        )
    if outcome == "completed":
        return (
            "Morning HRV finished. Heart rate "
            + hr
            + " bpm and no RR samples were observed. HRV was not established. "
            + tail
        )
    if outcome == "cancelled":
        return "Morning HRV stopped. " + tail
    if outcome == "timeout":
        return "Capture timed out. " + tail
    if outcome == "recovery":
        return "Capture recovery restored Wi-Fi. " + tail
    if outcome == "restore_retry":
        return "Wi-Fi restore succeeded. " + tail
    return "Capture failed. " + tail


class PolarCapture:
    def __init__(
        self,
        owner,
        path,
        ethernet,
        radio,
        runner,
        expected_address="192.168.4.2",
        expected_connection="roarm-ap",
        overhead_s=90.0,
        killer=None,
    ):
        self.owner = owner
        self.path = Path(path)
        self.ethernet = ethernet
        self.radio = radio
        self.runner = runner
        self.expected_address = str(expected_address)
        self.expected_connection = str(expected_connection)
        self.overhead_s = float(overhead_s)
        self.killer = killer or _kill_collector_pid
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._phase = "idle"
        self._message = READY
        self._operator = None
        self._mode = None
        self._started_at = None
        self._seconds = None
        self._saved_connection = None
        self._saved_address = None
        self._pid = None
        self._proc = None
        self._thread = None
        self._last = None
        self._finished = False

    def status(self):
        with self._lock:
            phase = self._phase
            message = self._message
            last = self._last
            seconds = self._seconds
            operator = self._operator
            mode = self._mode
            started_at = self._started_at
            connection = self._saved_connection
        hold = self.owner.hold()
        return {
            "available": True,
            "phase": phase,
            "mode": mode,
            "started_at": started_at,
            "message": message,
            "arm_available": hold is None and phase == "idle",
            "hold": hold,
            "seconds": seconds,
            "operator": operator,
            "connection": connection,
            "last": last,
        }

    def start(self, operator, seconds=None, mode=None):
        operator = _text(operator)
        if not operator:
            return {"accepted": False, "reason": "OPERATOR_REQUIRED"}
        if operator.lower() == "perception":
            return {"accepted": False, "reason": "TRIGGER_NOT_ENABLED"}
        mode, error = _mode(mode)
        if error:
            return {"accepted": False, "reason": error}
        if mode == WORKOUT:
            seconds, error = _seconds(seconds, WORKOUT_SAFETY_SECONDS, WORKOUT_SAFETY_SECONDS)
        else:
            seconds, error = _seconds(seconds, DEFAULT_SECONDS, MORNING_MAX_SECONDS)
        if error:
            return {"accepted": False, "reason": error}
        with self._lock:
            busy = self._busy()
        if busy:
            return busy
        probe = getattr(self.radio, "available", None)
        try:
            privileged = True if probe is None else probe() is True
        except Exception:
            privileged = False
        if not privileged:
            return {"accepted": False, "reason": "WIFI_PRIVILEGE_UNAVAILABLE"}
        try:
            ethernet_ok = self.ethernet() is True
        except Exception:
            ethernet_ok = False
        if not ethernet_ok:
            return {"accepted": False, "reason": "ETHERNET_UNAVAILABLE"}
        try:
            snap = self.radio.snapshot()
        except Exception:
            return {"accepted": False, "reason": "WIFI_NOT_READY"}
        if not _wifi_ready(snap, self.expected_connection, self.expected_address):
            return {"accepted": False, "reason": "WIFI_NOT_READY"}
        with self._lock:
            busy = self._busy()
            if busy:
                return busy
            self._phase = "disabling"
            self._operator = operator
            self._mode = mode
            self._started_at = None
            self._seconds = seconds
            self._saved_connection = snap.get("connection") or self.expected_connection
            self._saved_address = snap.get("address") or ""
            self._cancel.clear()
            self._finished = False
            self._proc = None
            self._pid = None
            self._thread = None
            self._message = "Disabling Wi-Fi. The arm is held until roarm-ap returns."
        held, reason = self.owner.acquire_hold(HOLDER, HOLD_REASON)
        if not held:
            with self._lock:
                self._phase = "idle"
                self._mode = None
                self._started_at = None
                self._message = READY
                self._saved_connection = None
            return {"accepted": False, "reason": reason}
        try:
            with self._lock:
                self._write()
        except Exception:
            self.owner.release_hold(HOLDER)
            with self._lock:
                self._phase = "idle"
                self._mode = None
                self._started_at = None
                self._message = READY
            return {"accepted": False, "reason": "STATE_WRITE_FAILED"}
        try:
            self.radio.disable()
        except Exception:
            self._restore_and_release("capture_failed", None, "")
            return {"accepted": False, "reason": "WIFI_DISABLE_FAILED"}
        with self._lock:
            cancelled = self._cancel.is_set()
            if not cancelled:
                self._phase = "capturing"
                self._started_at = _stamp()
                if mode == WORKOUT:
                    self._message = (
                        "Workout recording. It continues until Stop workout. "
                        "Wi-Fi is off. Stop workout, or the 3-hour safety limit, "
                        "writes Training as POLAR_H10, then restores roarm-ap."
                    )
                else:
                    self._message = (
                        "Morning HRV running for "
                        + str(seconds)
                        + " s. Wi-Fi is off. The arm stays held until roarm-ap returns."
                    )
                self._write()
        if cancelled:
            self._restore_and_release("cancelled", None, "")
            return {"accepted": False, "reason": "CANCELLED"}
        try:
            handle = self.runner(mode, seconds)
        except Exception:
            self._restore_and_release("capture_failed", None, "")
            return {"accepted": False, "reason": "CAPTURE_START_FAILED"}
        with self._lock:
            self._proc = handle
            self._pid = getattr(handle, "pid", None)
            try:
                self._write()
            except Exception:
                pass
            cancelled = self._cancel.is_set()
        if cancelled:
            if mode == WORKOUT:
                handle.terminate()
            else:
                handle.kill()
            self._restore_and_release("cancelled", None, handle.output())
            return {"accepted": False, "reason": "CANCELLED"}
        thread = threading.Thread(target=self._supervise, name="polar-capture", daemon=False)
        with self._lock:
            self._thread = thread
        thread.start()
        return {"accepted": True, "reason": "CAPTURE_STARTED", "phase": "capturing"}

    def stop(self, operator=None):
        operator = _text(operator)
        if not operator:
            return {"accepted": False, "reason": "OPERATOR_REQUIRED"}
        with self._lock:
            phase = self._phase
            proc = self._proc
            mode = self._mode
            if phase == "idle":
                return {"accepted": False, "reason": "NOT_ACTIVE"}
            retry = phase == "failed_restore"
            self._cancel.set()
        if retry:
            with self._lock:
                self._finished = False
            self._restore_and_release("restore_retry", None, "")
        elif proc is not None and mode == WORKOUT and hasattr(proc, "terminate"):
            proc.terminate()
        elif proc is not None:
            proc.kill()
        self._wait_settled(45)
        current = self.status()
        return {
            "accepted": True,
            "reason": "STOP_REQUESTED",
            "phase": current["phase"],
            "message": current["message"],
        }

    def shutdown(self):
        if self.status()["phase"] == "idle":
            return
        self.stop(operator="process")

    def recover(self):
        data = self._read()
        phase = data.get("phase") if isinstance(data, dict) else None
        if phase in (None, "", "idle"):
            with self._lock:
                self._phase = "idle"
                self._started_at = None
                self._last = data.get("last") if isinstance(data, dict) else None
                self._message = READY
            return self.status()
        self._saved_connection = data.get("connection") or self.expected_connection
        self._saved_address = data.get("address") or ""
        pid = data.get("pid")
        if pid:
            try:
                self.killer(pid)
            except Exception:
                pass
        self.owner.acquire_hold(HOLDER, HOLD_REASON)
        with self._lock:
            self._finished = False
            self._phase = "restoring"
            self._message = "Restoring " + self._saved_connection + " after capture recovery."
        self._restore_and_release("recovery", None, "")
        return self.status()

    def _busy(self):
        """Caller holds the lock. A second start must not open another session."""
        if self._phase == "failed_restore":
            return {"accepted": False, "reason": RESTORE_REASON}
        if self._phase != "idle":
            return {
                "accepted": False,
                "reason": "CAPTURE_ACTIVE",
                "phase": self._phase,
                "mode": self._mode,
            }
        return None

    def wait_until(self, phases, timeout=2):
        deadline = time.monotonic() + timeout
        current = self.status()
        while time.monotonic() < deadline:
            current = self.status()
            if current["phase"] in phases:
                return current
            time.sleep(0.01)
        return current

    def _supervise(self):
        with self._lock:
            proc = self._proc
            mode = self._mode
            timeout = (self._seconds or DEFAULT_SECONDS) + self.overhead_s
        if proc is None:
            self._restore_and_release("capture_failed", None, "")
            return
        code = proc.wait(timeout)
        if code is None and mode == WORKOUT and hasattr(proc, "terminate"):
            proc.terminate()
            code = proc.wait(5)
            outcome = "completed" if code == 0 else "timeout"
        elif code is None:
            proc.kill()
            code = proc.wait(5)
            outcome = "timeout"
        elif self._cancel.is_set() and mode == WORKOUT and code == 0:
            outcome = "completed"
        elif self._cancel.is_set():
            outcome = "cancelled"
        elif code == 0:
            outcome = "completed"
        else:
            outcome = "capture_failed"
        self._restore_and_release(outcome, code, proc.output())

    def _restore_and_release(self, outcome, code, output):
        del code
        with self._lock:
            if self._finished:
                return
            self._finished = True
            mode = self._mode
            self._phase = "restoring"
            connection = self._saved_connection or self.expected_connection
            self._message = "Restoring " + connection + "."
            try:
                self._write()
            except Exception:
                pass
        verified = False
        try:
            self.radio.restore(connection)
            verified = self._verified(connection)
        except Exception:
            verified = False
        last = parse_collector_output(output)
        with self._lock:
            if last is not None:
                self._last = last
            self._pid = None
            self._proc = None
            if verified:
                self.owner.release_hold(HOLDER)
                self._phase = "idle"
                self._mode = None
                self._started_at = None
                self._message = _result_message(outcome, self._last, connection, self.expected_address, mode)
            else:
                if not self.owner.set_hold_reason(HOLDER, RESTORE_REASON):
                    self.owner.acquire_hold(HOLDER, RESTORE_REASON)
                self._phase = "failed_restore"
                self._message = _result_message("failed_restore", self._last, connection, self.expected_address, mode)
            try:
                self._write()
            except Exception:
                pass

    def _verified(self, connection):
        snap = self.radio.snapshot()
        return _wifi_ready(snap, connection, self.expected_address)

    def _wait_settled(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.status()["phase"] in ("idle", "failed_restore"):
                return
            time.sleep(0.02)

    def _write(self):
        atomic_json(self.path, {
            "schema_version": 1,
            "phase": self._phase,
            "connection": self._saved_connection,
            "address": self._saved_address,
            "expected_address": self.expected_address,
            "pid": self._pid,
            "operator": self._operator,
            "mode": self._mode,
            "started_at": self._started_at,
            "seconds": self._seconds,
            "message": self._message,
            "last": self._last,
        })

    def _read(self):
        try:
            raw = self.path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}
