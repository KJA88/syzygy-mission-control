from pathlib import Path
import yaml


def load_config(path):
    with Path(path).open(encoding='utf-8') as stream:
        cfg = yaml.safe_load(stream)
    if cfg.get('schema_version') != 1:
        raise ValueError('Unsupported configuration schema')
    ids = set()
    for group in ('nodes', 'services', 'paths'):
        for item in cfg.get(group, []):
            if item['id'] in ids or 'roarm' in item['id'].lower():
                raise ValueError('Duplicate or out-of-scope component')
            ids.add(item['id'])
            if not isinstance(item.get('required'), bool):
                raise ValueError('required must be boolean')
            if item.get('interval_s', 30) <= 0 or item.get('timeout_s', 5) <= 0:
                raise ValueError('Probe timing must be positive')
    nodes = {x['id'] for x in cfg['nodes']}
    for service in cfg['services']:
        if service['host'] not in nodes:
            raise ValueError('Unknown service host')
    return cfg


def configured_url(url):
    return isinstance(url, str) and url.startswith(('http://', 'https://'))
