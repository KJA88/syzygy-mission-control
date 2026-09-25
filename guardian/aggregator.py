"""Merge local observations and public probes into the authoritative snapshot."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import time
from uuid import uuid4
from .auth import auth_mcp
from .config import load_config, configured_url
from .model import age, fact, fresh, reduce_object, reduce_system, stamp, utcnow
from .probes import http_json, mcp
from .roarm import probe_roarm, unknown_roarm
from .storage import atomic_json, append_events, changes
from .state_engine import build_state


def missing(required, now, trace, cls='EVIDENCE_MISSING'):
    item = fact('unknown', required, now, trace, cls)
    item.update(observed_at=None, observation_age_s=None)
    return item


def agent_evidence(node, use_candidates, trace):
    url = node.get('agent')
    if not configured_url(url) and use_candidates:
        url = node.get('agent_candidate_url')
    if not configured_url(url):
        return None
    health, payload = http_json(url, 5, True, trace)
    if health.get('class') == 'PROBE_CRASH':
        raise RuntimeError('Agent observation probe crashed')
    if health['status'] != 'green' or not isinstance(payload, dict):
        return None
    if payload.get('schema_version') != 1 or payload.get('node_id') != node['id']:
        return None
    if not isinstance(payload.get('services'), list) or not isinstance(payload.get('paths'), list):
        return None
    return payload


def layer(observations, name, required, now, trace, max_age, host_stale=False):
    raw = observations.get(name)
    if not isinstance(raw, dict) or raw.get('status') not in ('green', 'yellow', 'red', 'unknown'):
        return missing(required, now, trace)
    item = fresh(raw, now, max_age)
    item['required'] = required
    if host_stale:
        item.update(status='unknown', **{'class': 'HOST_AGENT_DOWN'})
    return item


def build_snapshot(cfg, evidence, public, now, trace, crashed=False,
                   auth_evidence=None, roarm=None):
    policy = cfg.get('guardian', {})
    auth_evidence = auth_evidence or {}
    nodes, services, paths, auth = [], [], [], []
    by_node = {}
    for node in cfg['nodes']:
        observation = evidence.get(node['id'])
        elapsed = age(observation.get('observed_at'), now) if observation else None
        state = 'unknown' if elapsed is None or elapsed > policy.get('agent_unknown_s', 90) else 'yellow' if elapsed > policy.get('agent_fresh_s', 60) else 'green'
        item = fact(state, node['required'], now, trace,
                    'HOST_AGENT_DOWN' if state == 'unknown' else 'AGENT_LATE' if state == 'yellow' else None,
                    id=node['id'], metrics=observation.get('metrics', {}) if observation else {})
        item.update(observed_at=observation.get('observed_at') if observation else None, observation_age_s=elapsed)
        nodes.append(item)
        by_node[node['id']] = item
    for service in cfg['services']:
        node = by_node[service['host']]
        agent = evidence.get(service['host']) or {}
        observed = next((s for s in agent.get('services', []) if isinstance(s, dict) and s.get('id') == service['id']), {})
        raw = observed.get('layers', {})
        if not isinstance(raw, dict):
            raw = {}
        maximum = policy.get('observation_unknown_s', 90)
        def local(name, required=True):
            return layer(raw, name, required, now, trace, maximum, node['status'] == 'unknown')
        host = deepcopy(node)
        host['required'] = True
        layers = {'host': host, 'local_service': local('local_service', service.get('systemd_unit_authoritative', True)),
                  'local_port': local('local_port')}
        if not service.get('systemd_unit_authoritative', True):
            layers['local_process'] = local('local_process')
        if 'health_url' in service:
            layers['local_health'] = local('local_health')
        cameras = {}
        for camera in service.get('cameras', []):
            cameras[camera['id']] = local('camera/' + camera['id'], camera['required'])
            layers['camera/' + camera['id']] = cameras[camera['id']]
        if 'probe' in service:
            layers['local_mcp'] = local('local_mcp')
            layers['local_catalog'] = local('local_catalog', False)
            public_layers = public.get(service['id'], {})
            layers['public_mcp'] = layer(public_layers, 'public_mcp', service.get('public_required', False), now, trace, maximum)
            layers['public_catalog'] = layer(public_layers, 'public_catalog', False, now, trace, maximum)
            layers['public_tls'] = layer(public_layers, 'public_tls', service.get('public_required', False), now, trace, maximum)
        auth_required = bool(service.get('auth_required', False))
        auth_raw = auth_evidence.get(service['id'])
        if isinstance(auth_raw, dict):
            auth_item = fresh(auth_raw, now, maximum)
            auth_item['required'] = auth_required
        else:
            auth_item = missing(auth_required, now, trace, 'AUTH_PROBE_OFF')
        layers['client_invoke'] = auth_item
        layers['portal_catalog'] = missing(False, now, trace, 'VERIFY_LIVE')
        services.append(reduce_object(service['id'], service['required'], layers, now, trace, host=service['host'], cameras=cameras))
        auth.append(dict(auth_item, id=service['id'], required=service['required'] and auth_required))
    direct = {s['id']: deepcopy(s) for s in services}
    for service, item in zip(cfg['services'], services):
        for dependency in service.get('dependencies', []):
            dep = direct.get(dependency)
            status = dep['status'] if dep else 'unknown'
            item['layers']['dependency/' + dependency] = fact(status, True, now, trace,
                None if status == 'green' else 'DEPENDENCY_OUTAGE')
        reduced = reduce_object(item['id'], item['required'], item['layers'], now, trace)
        item.update(status=reduced['status'], **{'class': reduced['class']})
    for path in cfg.get('paths', []):
        agent = evidence.get(path.get('host')) or {}
        observed = next((p for p in agent.get('paths', []) if isinstance(p, dict) and p.get('id') == path['id']), {})
        raw = observed.get('layers', {})
        if not isinstance(raw, dict):
            raw = {}
        chips = {name: layer(raw, name, True, now, trace, policy.get('observation_unknown_s', 90),
                            by_node[path['host']]['status'] == 'unknown') for name in path.get('units', [])}
        if not chips:
            chips = {'portal_catalog': missing(True, now, trace, 'VERIFY_LIVE')}
        paths.append(reduce_object(path['id'], path['required'], chips, now, trace))
    guardian = fact('unknown' if crashed else 'green', True, now, trace, 'PROBE_CRASH' if crashed else None,
                    heartbeat_at=stamp(now), last_run_result='crash' if crashed else 'ok')
    status, reason = reduce_system(nodes + services + paths + auth, guardian, now, policy.get('heartbeat_unknown_s', 120))
    snapshot = dict(schema_version=1, generated_at=stamp(now), trace_id=trace, guardian=guardian,
                    system=dict(status=status, reason=reason), nodes=nodes, services=services, paths=paths, auth=auth, activity=[])
    roarm_cfg = cfg.get('roarm')
    if isinstance(roarm_cfg, dict):
        snapshot['roarm'] = roarm or unknown_roarm(
            str(roarm_cfg.get('base_url', '')).rstrip('/'),
            'No RoArm observation',
        )
    snapshot['state_engine'] = build_state(snapshot)
    return snapshot


class Guardian:
    def __init__(self, cfg, use_candidates=False):
        self.cfg, self.use_candidates, self.public_cache = cfg, use_candidates, {}

    def tick(self):
        trace, now = str(uuid4()), utcnow()
        selected = [s for s in self.cfg['services'] if 'probe' in s and configured_url(s.get('public_url'))]
        due = [s for s in selected if s['id'] not in self.public_cache or now - self.public_cache[s['id']][0] >= s['interval_s']]
        roarm_cfg = self.cfg.get('roarm')
        roarm_job = None
        with ThreadPoolExecutor(max_workers=12) as pool:
            agent_jobs = {n['id']: pool.submit(agent_evidence, n, self.use_candidates, trace) for n in self.cfg['nodes']}
            public_jobs = {s['id']: pool.submit(mcp, s, s['public_url'].rstrip('/') + s['public_mcp_path'], s.get('public_required', False), trace) for s in due}
            if isinstance(roarm_cfg, dict):
                roarm_job = pool.submit(
                    probe_roarm,
                    roarm_cfg.get('base_url', ''),
                    roarm_cfg.get('timeout_s', 1.5),
                )
            auth_jobs = {}
            for s in selected:
                auth_url = s.get('auth_url')
                if not configured_url(auth_url):
                    auth_url = s['public_url'].rstrip('/') + s['public_mcp_path']
                auth_jobs[s['id']] = pool.submit(auth_mcp, s, auth_url, trace)
            evidence = {name: job.result() for name, job in agent_jobs.items()}
            auth_evidence = {name: job.result() for name, job in auth_jobs.items()}
            for name, job in public_jobs.items():
                health, catalog = job.result()
                tls = deepcopy(health)
                if health['class'] in ('TLS_FAIL', 'DNS_FAIL'):
                    tls.update(status='red')
                elif health['status'] == 'green' or health['class'] in ('MCP_MALFORMED', 'PUBLIC_ENDPOINT_FAIL'):
                    tls.update(status='green', **{'class': None})
                else:
                    tls.update(status='unknown')
                self.public_cache[name] = (now, {'public_mcp': health, 'public_catalog': catalog, 'public_tls': tls})
            if roarm_job is not None:
                try:
                    roarm = roarm_job.result()
                except Exception as exc:
                    roarm = unknown_roarm(
                        str(roarm_cfg.get('base_url', '')).rstrip('/'),
                        exc,
                    )
            else:
                roarm = None
        public = {name: value[1] for name, value in self.public_cache.items()}
        crashed = any(layer.get('class') == 'PROBE_CRASH' for value in public.values() for layer in value.values())
        crashed |= any(item.get('class') == 'PROBE_CRASH' for item in auth_evidence.values())
        for agent in evidence.values():
            if agent:
                crashed |= any(layer.get('class') == 'PROBE_CRASH' for obj in agent['services'] for layer in obj.get('layers', {}).values())
        return build_snapshot(
            self.cfg, evidence, public, utcnow(), trace, crashed,
            auth_evidence, roarm,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config/services.yaml')
    parser.add_argument('--state-dir', default='state')
    parser.add_argument('--use-candidate-agents', action='store_true', help='Opt in to deployment endpoints still awaiting live verification')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    cfg = load_config(args.config)
    guardian = Guardian(cfg, args.use_candidate_agents)
    directory = Path(args.state_dir)
    try:
        previous = json.loads((directory / 'snapshot.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        previous = {}
    while True:
        try:
            snapshot = guardian.tick()
        except Exception:
            snapshot = build_snapshot(cfg, {}, {}, utcnow(), str(uuid4()), crashed=True)
        events = changes(previous, snapshot)
        snapshot['activity'] = events[-100:]
        append_events(directory / 'events.jsonl', events)
        atomic_json(directory / 'snapshot.json', snapshot)
        previous = snapshot
        if args.once:
            return
        time.sleep(cfg.get('guardian', {}).get('interval_s', 30))


if __name__ == '__main__':
    main()
