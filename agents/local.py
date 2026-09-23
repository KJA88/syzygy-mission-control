"""Same read-only agent on Pi and Jetson; roles come only from services.yaml."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from uuid import uuid4
from guardian.config import load_config
from guardian.model import fact, utcnow, stamp
from guardian.probes import mcp, http_json
from guardian.storage import atomic_json


def unit(name, required, trace):
    try:
        run = subprocess.run(['systemctl', 'is-active', '--', name], capture_output=True, text=True, timeout=3)
        state = run.stdout.strip()
        if state not in ('active', 'inactive', 'failed', 'activating', 'deactivating'):
            return fact('unknown', required, utcnow(), trace, 'SVC_STATE_UNKNOWN')
        return fact('green' if state == 'active' else 'red', required, utcnow(), trace,
                    None if state == 'active' else 'SVC_INACTIVE', unit_state=state)
    except (OSError, subprocess.TimeoutExpired):
        return fact('unknown', required, utcnow(), trace, 'PROBE_CRASH')


def process_running(match, trace):
    try:
        found = False
        for path in Path('/proc').glob('[0-9]*/cmdline'):
            try:
                if match.encode() in path.read_bytes().split(b'\0'):
                    found = True
                    break
            except (OSError, PermissionError):
                continue
        return fact('green' if found else 'red', True, utcnow(), trace, None if found else 'SVC_INACTIVE')
    except OSError:
        return fact('unknown', True, utcnow(), trace, 'PROBE_CRASH')


def camera_health(payload, required, trace):
    online = payload.get('online') if isinstance(payload, dict) else None
    status = 'green' if online is True else 'red' if online is False else 'unknown'
    return fact(status, required, utcnow(), trace,
                None if status == 'green' else 'CAMERA_OFFLINE' if status == 'red' else 'CAMERA_EVIDENCE_MISSING', online=online)


def service_observation(service, trace):
    authoritative = service.get('systemd_unit_authoritative', True)
    layers = {'local_service': unit(service['systemd_unit'], authoritative, trace)}
    if not authoritative:
        layers['local_process'] = process_running(service['process_match'], trace)
    target = urlsplit(service['local_url'])
    try:
        with socket.create_connection((target.hostname, target.port or 80), timeout=service['timeout_s']):
            layers['local_port'] = fact('green', True, utcnow(), trace)
    except OSError:
        layers['local_port'] = fact('red', True, utcnow(), trace, 'PORT_CLOSED')
    if 'probe' in service:
        layers['local_mcp'], layers['local_catalog'] = mcp(service, service['local_url'].rstrip('/') + service['mcp_path'], True, trace)
    if 'health_url' in service:
        health, payload = http_json(service['health_url'], service['timeout_s'], True, trace)
        if health['status'] == 'green':
            if isinstance(payload, dict) and (payload.get('healthy') is False or payload.get('status') in ('error', 'failed', 'unhealthy')):
                health.update(status='red', **{'class': 'SVC_ALIVE_UNHEALTHY'})
        layers['local_health'] = health
    cameras = {}
    for camera in service.get('cameras', []):
        health, payload = http_json(camera['health_url'], service['timeout_s'], camera['required'], trace)
        if health['status'] == 'green':
            health = camera_health(payload, camera['required'], trace)
        cameras[camera['id']] = health
        layers['camera/' + camera['id']] = health
    return dict(id=service['id'], layers=layers, cameras=cameras)


def metrics():
    value = {'cpu_load_1m': None, 'ram_available_bytes': None, 'disk_free_bytes': shutil.disk_usage('/').free,
             'temperature_c': None, 'gpu_utilization_percent': None}
    try:
        value['cpu_load_1m'] = os.getloadavg()[0]
        memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        value['ram_available_bytes'] = int(memory['MemAvailable'].split()[0]) * 1024
        temperatures = list(Path('/sys/class/thermal').glob('thermal_zone*/temp'))
        if temperatures:
            value['temperature_c'] = max(int(p.read_text()) / 1000 for p in temperatures)
    except (OSError, ValueError, AttributeError):
        pass
    return value


class Agent:
    def __init__(self, cfg, node):
        self.cfg, self.node, self.cache = cfg, node, {}
        self.snapshot = None

    def tick(self):
        trace, now = str(uuid4()), utcnow()
        selected = [s for s in self.cfg['services'] if s['host'] == self.node['id']]
        due = [s for s in selected if s['id'] not in self.cache or now - self.cache[s['id']][0] >= s['interval_s']]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda s: service_observation(s, trace), due))
        for service, result in zip(due, results):
            self.cache[service['id']] = (now, result)
        paths = []
        for path in self.cfg['paths']:
            if path.get('host') == self.node['id']:
                paths.append(dict(id=path['id'], layers={name: unit(name, True, trace) for name in path.get('units', [])}))
        self.snapshot = dict(schema_version=1, node_id=self.node['id'], observed_at=stamp(utcnow()), trace_id=trace,
                             metrics=metrics(), services=[self.cache[s['id']][1] for s in selected], paths=paths)
        return self.snapshot


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config/services.yaml')
    parser.add_argument('--node', required=True, choices=['pi', 'jetson'])
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--output', default='state/agent.json')
    args = parser.parse_args()
    cfg = load_config(args.config)
    node = next(n for n in cfg['nodes'] if n['id'] == args.node)
    agent = Agent(cfg, node)
    atomic_json(args.output, agent.tick())
    if args.once:
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/health':
                self.send_error(404)
                return
            payload = json.dumps(agent.snapshot).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((node['agent_bind'], node['agent_port']), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        while True:
            time.sleep(node['interval_s'])
            atomic_json(args.output, agent.tick())
    finally:
        server.shutdown()


if __name__ == '__main__':
    main()
