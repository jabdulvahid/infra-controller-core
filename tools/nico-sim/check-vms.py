#!/usr/bin/env python3
"""
DC Simulation — VM Status Checker (legacy wrapper)

Prefer:  nsim <site> cp verify [--watch]

Usage:
  python3 check-vms.py <site>
  python3 check-vms.py <site> --watch
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nsim.collectors import site       as site_col
from nsim.collectors import verify_vms as verify_vms_col


def main():
    p = argparse.ArgumentParser(description='Check VM cloud-init and network status')
    p.add_argument('site', help='Site folder or yaml file')
    p.add_argument('--watch', '-w', action='store_true',
                   help='Repeat every 15s until all VMs show cloud-init done')
    args = p.parse_args()

    try:
        site_data = site_col.collect(args.site)
    except Exception as e:
        print(f'Error loading site: {e}', file=sys.stderr)
        sys.exit(1)

    done = verify_vms_col.run_checks(site_data, watch=args.watch)
    sys.exit(0 if done else 1)


if __name__ == '__main__':
    main()
