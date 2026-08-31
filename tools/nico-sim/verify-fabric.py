#!/usr/bin/env python3
"""
nico-sim — Fabric health verification (legacy wrapper)

Prefer:  nsim <site> fabric verify [--no-ping] [--switch-only]

Usage:
  ./verify-fabric.py <site>
  ./verify-fabric.py <site> --no-ping
  ./verify-fabric.py <site> --switch-only
  ./verify-fabric.py <site> --dns-host <hostname>
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nsim.collectors import site          as site_col
from nsim.collectors import verify_fabric as verify_col


def main():
    p = argparse.ArgumentParser(description='Verify DC simulation fabric health')
    p.add_argument('site', help='Site folder or yaml file')
    p.add_argument('--no-ping',     action='store_true', help='Skip loopback pings')
    p.add_argument('--switch-only', action='store_true',
                   help='VM-facing peers not counted as failures')
    p.add_argument('--dns-host', default='archive.ubuntu.com',
                   help='Hostname for DNS resolution check')
    args = p.parse_args()

    try:
        site_data = site_col.collect(args.site)
    except Exception as e:
        print(f'Error loading site: {e}', file=sys.stderr)
        sys.exit(1)

    healthy = verify_col.run_checks(
        site_data,
        no_ping=args.no_ping,
        switch_only=args.switch_only,
        dns_host=args.dns_host,
    )
    sys.exit(0 if healthy else 1)


if __name__ == '__main__':
    main()
