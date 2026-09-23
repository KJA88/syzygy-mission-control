"""Read a saved snapshot with the mandatory heartbeat age guard."""
import argparse
import json
from .config import load_config
from .model import utcnow
from .storage import read_snapshot


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config/services.yaml')
    parser.add_argument('--snapshot', default='state/snapshot.json')
    args = parser.parse_args()
    cfg = load_config(args.config)
    print(json.dumps(read_snapshot(args.snapshot, utcnow(), cfg.get('guardian', {}).get('heartbeat_unknown_s', 120)), indent=2))


if __name__ == '__main__':
    main()
