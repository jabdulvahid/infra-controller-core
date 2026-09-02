#!/usr/bin/env python3
"""
nico-dev — make sure the HOST-side image registry is running (idempotent).

  ensure-registry.py            # registry:2 container 'registry' on :5000
  ensure-registry.py --port N

The VM's containerd pulls every nico image from <host>:5000 (192.168.64.1
as seen from the VM). Source builds and the NGC lane both push into it.
dev-up runs this in the `registry` step BEFORE verifying reachability
from the VM — in NGC mode nothing earlier would have created it
(20260902-#5: verify-before-create; masked on Macs whose Colima kept an
old registry container around).

Platform-agnostic: needs only a reachable docker daemon.
"""

import argparse
import subprocess
import sys


def ensure_registry(port):
    r = subprocess.run(['docker', 'inspect', 'registry', '--format',
                        '{{.State.Running}}'], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip() == 'true':
        print(f'  Registry already running on port {port} ✓')
        return
    if subprocess.run(['docker', 'start', 'registry'],
                      capture_output=True).returncode == 0:
        print('  Registry container started ✓')
        return
    print(f'  Creating registry container on port {port}...')
    r3 = subprocess.run(['docker', 'run', '-d', '-p', f'{port}:5000',
                         '--restart=always', '--name', 'registry',
                         'registry:2'], capture_output=True, text=True)
    if r3.returncode != 0:
        print('Error: could not start the registry container — is the '
              'docker daemon running (colima start / systemctl start docker)?',
              file=sys.stderr)
        print(r3.stderr, file=sys.stderr)
        sys.exit(1)
    print(f'  Registry created on port {port} ✓')


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__)
    p.add_argument('--port', type=int, default=5000)
    args = p.parse_args()
    ensure_registry(args.port)


if __name__ == '__main__':
    main()
