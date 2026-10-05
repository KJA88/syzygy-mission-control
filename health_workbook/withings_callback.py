"""Loopback Withings OAuth callback.

Cloudflare may publish only /callback. This process binds 127.0.0.1 and does
not proxy the health workbook or the Withings measurement API.
"""

from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8791
PUBLIC_CALLBACK = "https://withings.syzygylab.net/callback"
TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
ENV_FILE = Path("/home/KA_PI/syzygy-runtime/withings.env")
STATE_FILE = Path("/home/KA_PI/syzygy-runtime/withings-oauth.state")

READY = "Withings authorization callback is ready."
SUCCESS = "Withings authorization succeeded. You can close this tab."
FAILURE = "Withings authorization failed. You can close this tab."


def load_lines(path: Path) -> dict[str, str]:
    if not path.is_file() or path.is_symlink():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def write_env(path: Path, client_id: str, client_secret: str, access: str, refresh: str) -> None:
    if path.is_symlink():
        raise OSError("token_store")
    text = (
        "WITHINGS_CLIENT_ID=" + client_id + "\n"
        "WITHINGS_CLIENT_SECRET=" + client_secret + "\n"
        "WITHINGS_ACCESS_TOKEN=" + access + "\n"
        "WITHINGS_REFRESH_TOKEN=" + refresh + "\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def exchange_code(client_id: str, client_secret: str, code: str, transport=None) -> dict:
    fields = {
        "action": "requesttoken",
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": PUBLIC_CALLBACK,
    }
    send = transport or _post
    return send(TOKEN_URL, fields)


def _post(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read().decode())
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = {}
        finally:
            error.close()
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def decide(query: dict, state_path: Path, env_path: Path, transport=None) -> tuple[int, str]:
    code = query.get("code") or ""
    state = query.get("state") or ""
    error = query.get("error") or ""
    if not code and not state and not error:
        return 200, READY
    expected = ""
    if state_path.is_file() and not state_path.is_symlink():
        expected = state_path.read_text(encoding="utf-8").strip()
    if error or not code or not state or not expected or not secrets.compare_digest(state, expected):
        return 400, FAILURE
    creds = load_lines(env_path)
    client_id = creds.get("WITHINGS_CLIENT_ID", "")
    client_secret = creds.get("WITHINGS_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return 400, FAILURE
    payload = exchange_code(client_id, client_secret, code, transport)
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    access = body.get("access_token")
    refresh = body.get("refresh_token")
    if payload.get("status") != 0 or not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
        return 400, FAILURE
    try:
        write_env(env_path, client_id, client_secret, access, refresh)
    except OSError:
        return 400, FAILURE
    return 200, SUCCESS


class CallbackHandler(BaseHTTPRequestHandler):
    env_path = ENV_FILE
    state_path = STATE_FILE
    transport = None

    def do_HEAD(self) -> None:
        status, page = self._result()
        body = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self) -> None:
        status, page = self._result()
        body = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _result(self) -> tuple[int, str]:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            return 404, FAILURE
        query = {key: values[0] for key, values in urllib.parse.parse_qs(parsed.query).items() if values}
        return decide(query, self.state_path, self.env_path, self.transport)

    def log_message(self, fmt: str, *args) -> None:
        return


def serve(host: str = HOST, port: int = PORT) -> None:
    server = ThreadingHTTPServer((host, port), CallbackHandler)
    server.serve_forever()


if __name__ == "__main__":
    serve()
